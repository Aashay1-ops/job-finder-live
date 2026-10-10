"""freehire.me source: US internships + co-ops (every field) and full-time roles in tech categories.

freehire aggregates ~800k US postings from Workday, Greenhouse, SuccessFactors, iCIMS and more.
Its search API caps paging at offset+limit <= 10,000, so:
  - full sweep (twice a week, its own workflow; ~3 hours because the API throttles sustained use):
    every slice is read completely, splitting a big slice by `source` (each posting has exactly
    one source, so the split never double-counts or skips). Also drops postings that closed.
  - incremental (every refresh, 6-hourly): read each slice newest-first (sort=created_at) only until reaching
    postings already seen, so a regular run is a few dozen requests instead of ~3,000.
State lives in data/freehire_store.json.gz; a full sweep writes data/freehire_full.json.gz, which
the next incremental run adopts.

Usage:  python3 src/freehire.py --full     (full sweep -> freehire_full.json.gz)
        fetch_jobs.py calls update(): incremental, or a full sweep when run locally with no store
Standard library only.
"""
import concurrent.futures as cf
import gzip
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "freehire_store.json.gz"
FULL = ROOT / "data" / "freehire_full.json.gz"
API = "https://freehire.me/api/v1"
UA = {"User-Agent": "Mozilla/5.0 (job-finder personal script)"}
CAP = 10_000          # the API refuses offset + limit beyond this
PAGE = 100            # the API's max page size
OVERLAP = timedelta(hours=3)  # incremental runs re-read this much before the last run, to be safe

# Full-time roles are kept only in these categories; internships are kept in every category.
TECH_CATEGORIES = [
    "software_engineering", "backend", "frontend", "fullstack", "mobile", "devops", "sre",
    "ml_ai", "ai_engineering", "data_science", "data_engineering", "data_analytics",
    "hardware", "embedded", "engineering_design", "industrial_engineering", "qa", "security",
    "network_engineering", "solutions_engineering",
]
SLICES = [{"employment_type": "internship"}] + [{"category": c} for c in TECH_CATEGORIES]


MIN_GAP = 0.25  # seconds between requests across all threads (~240/min, under the API's 600 limit)
_lock = threading.Lock()
_next = [0.0]


def _pace():
    with _lock:
        wait = _next[0] - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _next[0] = max(_next[0], time.monotonic()) + MIN_GAP


def get(path, params, tries=6):
    url = f"{API}{path}?" + urllib.parse.urlencode({"countries": "us", **params}, doseq=True)
    for i in range(tries):
        _pace()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                if int(r.headers.get("x-ratelimit-remaining", 100)) < 10:
                    time.sleep(5)
                return json.load(r)
        except urllib.error.HTTPError as e:
            if i == tries - 1:
                raise
            # 429: the API asked us to slow down; honour Retry-After, else wait a minute
            time.sleep(int(e.headers.get("Retry-After") or 60) if e.code == 429 else 3 * (i + 1))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def slim(j):
    """Keep only what the pipeline uses (the raw rows are ~3 KB each)."""
    e = j.get("enrichment") or {}
    r = j.get("reality") or {}
    url = j.get("url") or ""
    if "utm_" in url:  # freehire tags outbound links; drop its tracking params
        p = urllib.parse.urlsplit(url)
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.startswith("utm_")]
        url = urllib.parse.urlunsplit(p._replace(query=urllib.parse.urlencode(q)))
    return {
        "slug": j.get("public_slug"), "title": j.get("title") or "", "company": j.get("company") or "",
        "location": j.get("location") or "", "cities": j.get("cities") or [], "url": url,
        "work_mode": j.get("work_mode") or "", "posted_at": (j.get("posted_at") or "")[:10],
        "created_at": (j.get("created_at") or "")[:19], "skills": (j.get("skills") or [])[:25],
        "description": (j.get("description") or "")[:1200], "source": j.get("source") or "",
        "employment_type": e.get("employment_type") or "", "seniority": e.get("seniority") or "",
        "category": e.get("category") or "", "visa_sponsorship": e.get("visa_sponsorship"),
        "years_min": e.get("experience_years_min"), "summary": (e.get("summary") or "")[:300],
        "repost_count": r.get("repost_count"), "mass_posting_count": r.get("mass_posting_count"),
        "fake_freshness": r.get("fake_freshness"),
    }


def total(params):
    return get("/jobs/search", {**params, "limit": 1})["meta"]["total"]


def read_slice(params, stop_before=None):
    """All rows of one slice (<= CAP), newest first. With stop_before, stop at older rows."""
    rows, offset = [], 0
    while offset + PAGE <= CAP:
        d = get("/jobs/search", {**params, "limit": PAGE, "offset": offset, "sort": "created_at"})
        page = d.get("data") or []
        rows += page
        if len(page) < PAGE:
            break
        if stop_before and page[-1].get("created_at", "")[:19] < stop_before:
            break
        offset += PAGE
    return rows


def split_by_source(params):
    """Break a slice bigger than the cap into per-source slices (each posting has one source)."""
    facets = get("/jobs/facets", params)["data"]["facets"].get("source", {})
    return [{**params, "source": s} for s, n in sorted(facets.items(), key=lambda x: -x[1]) if n]


def full_sweep():
    plan, incomplete = [], []
    for s in SLICES:
        n = total(s)
        if n <= CAP:
            plan.append(s)
            continue
        for sub in split_by_source(s):
            if total(sub) > CAP:
                incomplete.append(sub)  # read what the cap allows and report it
            plan.append(sub)
    rows = {}
    with cf.ThreadPoolExecutor(3) as ex:
        for batch in ex.map(read_slice, plan):
            for j in batch:
                rows[j["public_slug"]] = slim(j)
    return rows, incomplete, len(plan)


def incremental(store, since):
    added = 0
    with cf.ThreadPoolExecutor(3) as ex:
        for batch in ex.map(lambda s: read_slice(s, stop_before=since), SLICES):
            for j in batch:
                if j["public_slug"] not in store:
                    added += 1
                store[j["public_slug"]] = slim(j)
    return added


def _load(path):
    return json.loads(gzip.decompress(path.read_bytes())) if path.exists() else {}


def _save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(state, separators=(",", ":")).encode(), 6))


def sweep():
    """Full sweep into data/freehire_full.json.gz (run by the twice-weekly workflow)."""
    t, now = time.time(), datetime.now(timezone.utc)
    rows, incomplete, n_slices = full_sweep()
    _save(FULL, {"last_full": now.isoformat(), "last_run": now.isoformat(), "jobs": rows})
    note = f" · {len(incomplete)} slice(s) over the 10k cap, read partially" if incomplete else ""
    print(f"  freehire full sweep: {len(rows):,} jobs from {n_slices} slices{note} ({time.time() - t:.0f}s)")


def update():
    """Refresh the store and return its rows. Never starts a full sweep when FREEHIRE_MODE=incremental."""
    state, full = _load(STORE), _load(FULL)
    # a newer full sweep replaces the store (it also drops postings that have since closed)
    if full and full.get("last_full", "") > state.get("last_full", ""):
        state = full
        print(f"  freehire: adopted full sweep from {full['last_full'][:16]} ({len(full['jobs']):,} jobs)")
    if not state:
        if os.environ.get("FREEHIRE_MODE") == "incremental":
            # no sweep yet: start from the last 2 days of new postings until the full sweep lands
            state = {"last_full": "", "last_run": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), "jobs": {}}
        else:
            sweep()
            state = _load(FULL)
    t, now = time.time(), datetime.now(timezone.utc)
    since = (datetime.fromisoformat(state["last_run"]) - OVERLAP).strftime("%Y-%m-%dT%H:%M:%S")
    added = incremental(state["jobs"], since)
    state["last_run"] = now.isoformat()
    _save(STORE, state)
    print(f"  freehire incremental: +{added:,} new · {len(state['jobs']):,} total ({time.time() - t:.0f}s)")
    return list(state["jobs"].values())


if __name__ == "__main__":
    sweep() if "--full" in sys.argv else update()
