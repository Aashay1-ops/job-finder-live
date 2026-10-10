"""Stages 2-4: pull live US job postings, normalize, tag type / work mode / state, dedupe.

Sources
  - Public ATS job boards (Greenhouse, Lever, Ashby) for every company in config/companies.json
  - SimplifyJobs GitHub lists: active US internships + co-ops, and new-grad full-time roles
  - freehire.me aggregator: every US internship / co-op, plus full-time roles in tech categories
    (src/freehire.py; full sweep daily, newest-only top-up on other runs)

Usage:  python3 src/fetch_jobs.py
Output: data/raw/<source>.json, data/processed/jobs.json
Standard library only.
"""
import concurrent.futures as cf
import sys
import html
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "processed" / "jobs.json"
UA = {"User-Agent": "Mozilla/5.0 (job-finder personal script)"}
SIMPLIFY = {
    "intern": "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "newgrad": "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
}


def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
        return json.load(r)


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "\n• ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def iso(ts):
    return datetime.fromtimestamp(ts / (1000 if ts > 1e11 else 1), timezone.utc).strftime("%Y-%m-%d") if ts else ""


# ---------- fetchers: each returns a list of normalized dicts ----------
# Common fields: id, title, company, location (display string), url, posted, description, source,
# plus optional hints: emp (employment type), mode (Remote/Hybrid/On-site as stated by the source), country.

def fetch_greenhouse(slug, name):
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in d.get("jobs", []):
        emp = mode = ""
        for m in j.get("metadata") or []:
            n, v = (m.get("name") or "").lower(), m.get("value")
            v = ", ".join(v) if isinstance(v, list) else str(v or "")
            if "employment type" in n or n in ("job type", "worker type"):
                emp = v
            elif re.search(r"workplace|remote|location type|work type|work arrangement|work model", n):
                mode = v
        out.append({
            "id": f"gh-{slug}-{j['id']}", "title": j.get("title", ""), "company": name,
            "location": (j.get("location") or {}).get("name", ""), "url": j.get("absolute_url", ""),
            "posted": (j.get("first_published") or j.get("updated_at") or "")[:10],
            "description": strip_html(j.get("content", "")), "emp": emp, "mode": mode, "source": "Greenhouse",
        })
    return out


def fetch_lever(slug, name):
    out = []
    for j in get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        lists = "\n".join(f"{l.get('text','')}\n{strip_html(l.get('content',''))}" for l in j.get("lists", []))
        out.append({
            "id": f"lv-{slug}-{j['id']}", "title": j.get("text", ""), "company": name,
            "location": "; ".join(x for x in locs if x), "url": j.get("hostedUrl", ""), "posted": iso(j.get("createdAt")),
            "description": (j.get("descriptionPlain", "") + "\n\n" + lists).strip(),
            "emp": cats.get("commitment") or "", "mode": j.get("workplaceType") or "", "country": j.get("country") or "",
            "source": "Lever",
        })
    return out


def fetch_ashby(slug, name):
    out = []
    for j in get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [x.get("location", "") for x in j.get("secondaryLocations") or []]
        mode = j.get("workplaceType") or ("Remote" if j.get("isRemote") else "")
        out.append({
            "id": f"ab-{slug}-{j.get('id')}", "title": j.get("title", ""), "company": name,
            "location": "; ".join(x for x in locs if x), "url": j.get("jobUrl", ""), "posted": (j.get("publishedAt") or "")[:10],
            "description": j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
            "emp": j.get("employmentType") or "", "mode": mode,
            "country": ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry", ""), "source": "Ashby",
        })
    return out


PAST = []  # (company, title, location, posted date) of closed / older postings, for repost detection


def fetch_simplify(kind):
    out = []
    for j in get_json(SIMPLIFY[kind]):
        if not j.get("active") or not j.get("is_visible", True):
            PAST.append((j.get("company_name", ""), j.get("title", ""), "; ".join(j.get("locations") or []),
                         iso(j.get("date_posted"))))
            continue
        out.append({
            "id": f"sj-{j['id']}", "title": j.get("title", ""), "company": j.get("company_name", ""),
            "location": "; ".join(j.get("locations") or []), "url": j.get("url", ""), "posted": iso(j.get("date_posted")),
            "description": "", "emp": "Intern" if kind == "intern" else "New grad", "mode": "",
            "terms": j.get("terms") or [], "category": j.get("category") or "",
            "sponsorship": j.get("sponsorship") or "", "source": "Simplify " + ("internships" if kind == "intern" else "new grad"),
        })
    return out


FH_EMP = {"internship": "Intern", "fellowship": "Intern", "part_time": "Part-time", "contract": "Contract"}


def from_freehire(rows):
    out = []
    for r in rows:
        # age = the earlier of the employer's date and freehire's first sighting, so a refreshed
        # "posted today" on an old listing doesn't look fresh
        dates = [d for d in (r["posted_at"], r["created_at"][:10]) if d]
        out.append({
            "id": "fh-" + r["slug"], "title": r["title"], "company": r["company"],
            "location": r["location"] or "; ".join(r["cities"]), "url": r["url"], "posted": min(dates) if dates else "",
            "description": (r["summary"] + "\n\n" + r["description"]).strip(),
            "emp": FH_EMP.get(r["employment_type"], ""), "mode": r["work_mode"], "country": "US",
            "source": "freehire (" + (r["source"] or "?") + ")", "fh": {
                "skills": r["skills"], "seniority": r["seniority"], "category": r["category"],
                "visa": r["visa_sponsorship"], "years": r["years_min"], "reposts": max((r["repost_count"] or 1) - 1, 0),
                "mass": r["mass_posting_count"] or 0, "fake": bool(r["fake_freshness"]),
            },
        })
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}

# ---------- locations → US states ----------

STATE_NAMES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC", "Washington DC": "DC", "Washington, DC": "DC",
    "Washington D.C.": "DC", "Washington, D.C.": "DC",
}
ABBRS = set(STATE_NAMES.values())
CITIES = {  # cities that often appear without a state
    "CA": "San Francisco|SF|SF Bay Area|Bay Area|Silicon Valley|Los Angeles|LA|Palo Alto|Mountain View|Sunnyvale|San Jose|"
          "Santa Clara|Menlo Park|Redwood City|San Mateo|Foster City|Fremont|Oakland|Berkeley|Hawthorne|Irvine|Costa Mesa|"
          "San Diego|Long Beach|El Segundo|Torrance|Santa Monica|Culver City|Burlingame|South San Francisco|Emeryville|"
          "San Carlos|Cupertino|Milpitas|Santa Ana|Pasadena|Mojave|Vandenberg|Sacramento|Livermore|Pleasanton|Carlsbad|"
          "Newark, CA|San Bruno|Brisbane|Hayward|Alameda|Marina del Rey|Playa Vista|Venice",
    "NY": "NYC|New York City|Manhattan|Brooklyn", "WA": "Seattle|Redmond|Bellevue|Kirkland|Everett|Tukwila",
    "MA": "Boston|Somerville|Waltham|Burlington, MA|Woburn|Lexington, MA|Bedford, MA|Watertown|Andover",
    "TX": "Austin|Houston|Dallas|Starbase|McGregor|Bastrop|Plano|San Antonio|Fort Worth|Irving|Frisco|Richardson",
    "IL": "Chicago", "CO": "Denver|Boulder|Colorado Springs|Broomfield|Louisville, CO|Centennial|Englewood",
    "GA": "Atlanta", "PA": "Pittsburgh|Philadelphia", "MI": "Detroit|Ann Arbor|Dearborn",
    "AZ": "Phoenix|Tempe|Scottsdale|Chandler|Mesa, AZ", "UT": "Salt Lake City|Lehi|Draper", "OR": "Portland|Hillsboro|Beaverton",
    "FL": "Miami|Orlando|Tampa|Cape Canaveral|Kennedy Space Center|Merritt Island|Jacksonville",
    "AL": "Huntsville", "OH": "Columbus|Cleveland|Cincinnati", "NC": "Raleigh|Durham|Charlotte|Research Triangle",
    "MD": "Baltimore|Bethesda|Columbia, MD", "MN": "Minneapolis", "TN": "Nashville", "NV": "Las Vegas|Reno",
    "VA": "Reston|Herndon|McLean|Chantilly|Arlington, VA|Alexandria", "DC": r"D\.C\.", "MS": "Stennis",
    "NJ": "Jersey City|Princeton", "MO": "St. Louis|Kansas City, MO", "WI": "Madison|Milwaukee",
}
CITY_RE = [(st, re.compile(r"(?<![\w])(?:" + pat + r")(?![\w])", re.I if st != "CA" else 0)) for st, pat in CITIES.items()]
STATE_NAME_RE = re.compile(r"(?<![\w])(" + "|".join(sorted(map(re.escape, STATE_NAMES), key=len, reverse=True)) + r")(?![\w])", re.I)
# a state code only counts after a comma, "US-", or "(" ("Austin, TX", "US-CA-Sunnyvale"), or as the whole segment.
# Bare "LA" is Los Angeles (Simplify uses it), so it is handled by CITIES instead.
ABBR_RE = re.compile(r"(?:,\s*|\bUS-|\(\s*)(" + "|".join(sorted(ABBRS)) + r")(?=$|[\s,\-)/.])")
US_RE = re.compile(r"United States|\bUSA\b|\bU\.S\.A?\b|\bUS\b|North America|\bAmericas?\b|\bAMER\b", re.I)
FOREIGN = re.compile(
    r"Canada|Toronto|Vancouver|Montr[eé]al|Ontario|Quebec|British Columbia|Alberta|Calgary|Ottawa|London|United Kingdom|"
    r"\bUK\b|England|Scotland|India|Bangalore|Bengaluru|Hyderabad|Pune|Mumbai|Delhi|Germany|Munich|Berlin|Paris|France|"
    r"Japan|Tokyo|Singapore|Australia|Sydney|Melbourne|Israel|Tel Aviv|Ireland|Dublin|Poland|Warsaw|Krak[oó]w|Zurich|"
    r"Switzerland|Netherlands|Amsterdam|Korea|Seoul|Taiwan|China|Shanghai|Beijing|Shenzhen|Hong Kong|Mexico|Brazil|"
    r"S[aã]o Paulo|Spain|Madrid|Barcelona|Sweden|Stockholm|Denmark|Copenhagen|Norway|Oslo|Austria|Vienna|Italy|Milan|"
    r"Rome|Portugal|Lisbon|Belgium|Brussels|Czech|Prague|Romania|Bucharest|Argentina|Buenos Aires|Colombia|Bogot[aá]|"
    r"Chile|Philippines|Manila|Vietnam|Indonesia|Malaysia|Thailand|UAE|Dubai|Abu Dhabi|Saudi|Qatar|Egypt|Nigeria|Kenya|"
    r"South Africa|New Zealand|Finland|Estonia|Ukraine|Serbia|Greece|Turkey|Istanbul|EMEA|APAC|LATAM|Europe", re.I)
REMOTE_RE = re.compile(r"remote|work from home|wfh|anywhere|distributed", re.I)


def parse_location(loc, country=""):
    """→ (sorted state codes, remote_us flag, is_us flag)."""
    states, remote_us, us = set(), False, country.upper() in ("US", "USA", "UNITED STATES")
    for seg in re.split(r"\s*(?:;|\||•|·|/| or |\n)\s*", loc or ""):
        if not seg.strip():
            continue
        found = set()
        for m in STATE_NAME_RE.finditer(seg):
            name = m.group(1)
            # "Washington" alone is the state unless it's "Washington, DC"
            key = next((k for k in STATE_NAMES if k.lower() == name.lower()), None)
            if key:
                found.add(STATE_NAMES[key])
        found |= set(ABBR_RE.findall(seg))
        if seg.strip() in ABBRS and seg.strip() != "LA":
            found.add(seg.strip())
        if "DC" in found and re.search(r"washington\s*,?\s*(d\.?\s?c|district)", seg, re.I):
            found.discard("WA")
        for st, rx in CITY_RE:
            if rx.search(seg):
                found.add(st)
        foreign = bool(FOREIGN.search(seg)) and not found and not US_RE.search(seg)
        if foreign:
            continue
        if found:
            states |= found
            us = True
        if US_RE.search(seg):
            us = True
        if REMOTE_RE.search(seg) and (US_RE.search(seg) or not FOREIGN.search(seg)):
            remote_us = True
    if remote_us and not us and FOREIGN.search(loc or ""):
        remote_us = False
    return sorted(states), remote_us, us or bool(states) or remote_us


# ---------- job type, work mode, level, field, tracks ----------

COOP = re.compile(r"\bco-?op\b|\bcoop\b", re.I)
INTERN = re.compile(r"\bintern(ship)?s?\b|\bsummer (student|analyst|associate)\b|\bstudent (researcher|worker)\b", re.I)
PART = re.compile(r"part[- ]?time|contract|contractor|temporary|\btemp\b|seasonal|per diem|freelance", re.I)


def job_type(j):
    t, emp = j["title"], (j.get("emp") or "")
    if COOP.search(t):
        return "Co-op"
    if emp == "Intern" or re.search(r"intern", emp, re.I) or INTERN.search(t):
        return "Internship"
    if emp == "New grad":
        return "Full-time"
    if PART.search(emp) or re.search(r"\b(contract|part[- ]time|temporary|seasonal)\b", t, re.I):
        return "Part-time / Contract"
    return "Full-time"


HYBRID_D = re.compile(r"\bhybrid\b|\d\s*days? (a|per) week in (the )?office|in[- ]office \d", re.I)
REMOTE_D = re.compile(r"fully remote|100% remote|remote[- ]first|this (role|position) is (fully )?remote|"
                      r"remote position|work remotely from anywhere in the (us|u\.s\.|united states)", re.I)


def work_mode(j, remote_us, states):
    """→ (mode, how we know). Stated by the source beats the location string beats the description."""
    m = (j.get("mode") or "").lower()
    if m:
        if "hybrid" in m:
            return "Hybrid", "stated"
        if "remote" in m:
            return "Remote", "stated"
        if re.search(r"on-?site|in[- ]office|in person", m):
            return "On-site", "stated"
    loc = j.get("location") or ""
    if re.search(r"hybrid", loc + " " + j["title"], re.I):
        return "Hybrid", "location"
    if remote_us and not states:
        return "Remote", "location"
    d = j.get("description") or ""
    if HYBRID_D.search(d):
        return "Hybrid", "inferred"
    if remote_us or REMOTE_D.search(d):
        return "Remote", "inferred" if not remote_us else "location"
    return "On-site", "inferred" if d else "assumed"


SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|head,|vp|vice president|chief|"
                    r"distinguished|fellow|architect|supervisor|president|partner|iii|iv)\b", re.I)
ENTRY = re.compile(r"\b(new grad|graduate|entry|junior|jr\.?|associate|early career|university|campus|apprentice|"
                   r"residency|i|1)\b", re.I)


def level_of(j, jtype):
    if jtype in ("Internship", "Co-op"):
        return "Student"
    if j.get("emp") == "New grad" or ENTRY.search(j["title"]):
        return "Entry / New grad"
    if SENIOR.search(j["title"]):
        return "Senior & above"
    sen = (j.get("fh") or {}).get("seniority")
    if sen in ("junior", "intern"):
        return "Entry / New grad"
    if sen in ("senior", "lead", "staff", "principal", "c_level"):
        return "Senior & above"
    return "Mid / not stated"


FIELDS = [
    ("AI, ML & Data", r"machine learning|\bml\b|\bai\b|artificial intelligence|data scien|data engineer|data analy|analytics|"
                      r"computer vision|perception|\bllm|\bnlp\b|deep learning|research engineer|applied scientist|inference|"
                      r"research scientist"),
    ("Robotics & Embedded", r"robot|embedded|firmware|autonomy|autonomous|controls? engineer|mechatronic|\bgnc\b|avionics|"
                            r"motion planning|localization|\bslam\b|teleop"),
    ("Hardware, Electrical & Mechanical", r"hardware|electrical|electronics|\basic\b|fpga|\brf\b|\bpcb\b|silicon|\bchip|"
                                         r"mechanical|propulsion|structures|thermal|materials|optical|photonic|power "
                                         r"(electronics|systems)|analog|mixed[- ]signal|design verification"),
    ("Test, Quality & Manufacturing", r"\btest|quality|\bqa\b|validation|verification|manufactur|technician|reliability|"
                                      r"production|assembly|inspector|integration engineer|machinist|welder|process engineer"),
    ("Software", r"software|developer|\bswe\b|backend|back-end|frontend|front-end|full[- ]?stack|\bsre\b|devops|"
                 r"site reliability|security engineer|platform engineer|infrastructure|engineer"),
    ("Product & Design", r"product manager|product owner|designer|\bux\b|\bui\b|design"),
    ("Science & Research", r"scientist|research|chemist|biolog|physicist|clinical"),
]
FIELD_RE = [(n, re.compile(p, re.I)) for n, p in FIELDS]
SIMPLIFY_FIELD = {"AI/ML/Data": "AI, ML & Data", "Data Science, AI & Machine Learning": "AI, ML & Data",
                  "Hardware": "Hardware, Electrical & Mechanical", "Hardware Engineering": "Hardware, Electrical & Mechanical",
                  "Software": "Software", "Software Engineering": "Software", "Product": "Product & Design",
                  "Product Management": "Product & Design", "Quant": "Business & Operations"}


FH_FIELD = {
    "ml_ai": "AI, ML & Data", "ai_engineering": "AI, ML & Data", "data_science": "AI, ML & Data",
    "data_engineering": "AI, ML & Data", "data_analytics": "AI, ML & Data", "embedded": "Robotics & Embedded",
    "hardware": "Hardware, Electrical & Mechanical", "engineering_design": "Hardware, Electrical & Mechanical",
    "industrial_engineering": "Test, Quality & Manufacturing", "qa": "Test, Quality & Manufacturing",
    "software_engineering": "Software", "backend": "Software", "frontend": "Software", "fullstack": "Software",
    "mobile": "Software", "devops": "Software", "sre": "Software", "security": "Software",
    "network_engineering": "Software", "solutions_engineering": "Software",
    "product": "Product & Design", "design": "Product & Design",
}


def field_of(j):
    for name, rx in FIELD_RE:
        if rx.search(j["title"]):
            return name
    fh = (j.get("fh") or {}).get("category")
    if fh:
        return FH_FIELD.get(fh, "Business & Operations")
    return SIMPLIFY_FIELD.get(j.get("category"), "Business & Operations")


TRACKS = {
    "AI / ML / CV": r"machine learning|\bml\b|\bai\b|artificial intelligence|computer vision|perception|deep learning|"
                    r"data scientist|research engineer|applied scientist|\bllm|\bnlp\b|inference|\bvision\b|"
                    r"neural|reinforcement|research scientist",
    "Robotics / Embedded": r"robot|autonomy|autonomous|embedded|firmware|controls?\b|mechatronic|electrical engineer|"
                           r"hardware engineer|sensor|motion|automation engineer|electronics|\bpcb\b|fpga|"
                           r"systems engineer|avionics|gnc|localization|slam|actuat|teleop",
    "Test / Technical": r"\btest|validation|verification|quality|\bqa\b|reliability|technician|field (?:service|engineer|"
                        r"application)|applications? engineer|support engineer|integration engineer|"
                        r"manufacturing engineer|deployment engineer|solutions engineer|customer engineer|"
                        r"operations engineer|production engineer|\bcalibration",
}
TRACK_RE = {k: re.compile(v, re.I) for k, v in TRACKS.items()}
NOT_ENG = re.compile(r"recruit|sales|account (executive|manager)|legal|counsel|marketing|finance|accountant|"
                     r"people partner|\bhr\b|policy|communications|writer|editor|chef|barista|"
                     r"executive assistant|facilities|security officer|paralegal|tax\b|payroll", re.I)
JUNK = re.compile(r"conference|talent community|general (application|interest)|interest form|expression of interest|"
                  r"future opportunit|talent pool|join our talent|don.t see (a|the) role", re.I)


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


HISTORY = ROOT / "data" / "history.json"
REPOST_GAP = 7  # posting dates more than this many days apart count as separate postings (a repost)


def job_key(company, title, states):
    return "|".join([norm(company), norm(title), ",".join(states)])


def repost_info(dates):
    """dates: posting dates (YYYY-MM-DD) seen for one job → (times reposted, first posted date)."""
    ds = sorted({d for d in dates if d})
    if not ds:
        return 0, ""
    clusters, prev = 1, datetime.fromisoformat(ds[0])
    for d in ds[1:]:
        cur = datetime.fromisoformat(d)
        if (cur - prev).days > REPOST_GAP:
            clusters += 1
        prev = cur
    return clusters - 1, ds[0]


def main():
    cfg = json.loads((ROOT / "config" / "companies.json").read_text())["companies"]
    RAW.mkdir(parents=True, exist_ok=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    failed = []

    def run(entry):
        ats, slug, name = entry[:3]
        try:
            jobs = FETCHERS[ats](slug, name) if ats in FETCHERS else fetch_simplify(slug)
        except Exception as e:  # one dead board shouldn't kill the run
            failed.append(f"{name}: {e}")
            return []
        (RAW / f"{ats}-{slug}.json").write_text(json.dumps(jobs))
        return jobs

    sources = [["simplify", "intern", "Simplify internships"], ["simplify", "newgrad", "Simplify new grad"]] + cfg
    with cf.ThreadPoolExecutor(24) as ex:
        everything = [j for batch in ex.map(run, sources) for j in batch]
    try:
        import freehire
        everything += from_freehire(freehire.update())
        sources.append(["freehire", "freehire", "freehire.me"])
    except Exception as e:  # the boards + Simplify still make a usable run on their own
        failed.append(f"freehire: {e}")

    # every posting date we can see for each job: today's raw postings (dupes included), Simplify's closed
    # entries, and dates saved by earlier runs of this script
    seen_dates = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    dates = {k: set(v) for k, v in seen_dates.items()}
    for j in everything:
        st, _, us = parse_location(j["location"], j.get("country", ""))
        if us:
            dates.setdefault(job_key(j["company"], j["title"], st), set()).add(j["posted"])
    for company, title, loc, posted in PAST:
        st, _, us = parse_location(loc)
        if us:
            dates.setdefault(job_key(company, title, st), set()).add(posted)

    kept, seen_url, seen_key = [], set(), {}
    # duplicates keep the richest copy: company boards (full description), then freehire (summary +
    # excerpt), then Simplify (no description)
    rank = lambda j: 2 if j["source"].startswith("Simplify") else 1 if j["source"].startswith("freehire") else 0
    everything.sort(key=rank)
    for j in everything:
        if not j["title"] or JUNK.search(j["title"]):
            continue
        states, remote_us, us = parse_location(j["location"], j.get("country", ""))
        if not us:
            continue
        url = (j["url"] or "").split("?")[0].rstrip("/")
        key = (norm(j["company"]), norm(j["title"]), tuple(states))
        if url in seen_url or key in seen_key:
            continue
        seen_url.add(url)
        seen_key[key] = True
        jt = job_type(j)
        mode, mode_src = work_mode(j, remote_us, states)
        tracks = [] if NOT_ENG.search(j["title"]) else [k for k, rx in TRACK_RE.items() if rx.search(j["title"])]
        reposts, first = repost_info(dates.get(job_key(j["company"], j["title"], states), ()))
        reposts = max(reposts, (j.get("fh") or {}).get("reposts", 0))
        j.update(states=states, remote_us=remote_us, type=jt, mode=mode, mode_src=mode_src,
                 level=level_of(j, jt), field=field_of(j), tracks=tracks, reposts=reposts, first_posted=first)
        kept.append(j)

    # remember today's dates for open jobs so the next run can spot reposts
    for j in kept:
        k = job_key(j["company"], j["title"], j["states"])
        seen_dates[k] = sorted(set(seen_dates.get(k, [])) | {j["posted"]})
    HISTORY.write_text(json.dumps(seen_dates))

    from collections import Counter
    OUT.write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "sources": len(sources), "total_pulled": len(everything), "jobs": kept,
    }))
    print(f"pulled {len(everything):,} postings from {len(sources)} sources · {len(kept):,} unique US jobs → "
          f"{OUT.relative_to(ROOT)}")
    print("  type:", dict(Counter(j["type"] for j in kept)))
    print("  mode:", dict(Counter(j["mode"] for j in kept)), "| how known:", dict(Counter(j["mode_src"] for j in kept)))
    print("  reposted:", dict(sorted(Counter(min(j["reposts"], 5) for j in kept).items())), "(5 = 5+)")
    print("  by source:", dict(Counter(j["source"].split(" (")[0].split(" ")[0] for j in kept)))
    print("  states covered:", len({s for j in kept for s in j["states"]}), "| remote-US:", sum(j["remote_us"] for j in kept))
    if failed:
        print("  failed:", "; ".join(failed[:10]))


if __name__ == "__main__":
    main()
