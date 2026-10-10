"""Stage 6: assemble the public website in site/ from dashboard/ (run after score_jobs.py --public).

dashboard/index.html is written as page content (the claude.ai artifact host adds the document
skeleton). A plain web host needs the full document, so this wraps it, then copies the data files
and writes meta.json, the small file the open page polls to notice a newer update.

Usage:  python3 src/build_site.py
Output: site/   (deployed to GitHub Pages by .github/workflows/hourly.yml)
"""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASH = ROOT / "dashboard"
SITE = ROOT / "site"

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex">
<style>
:root { color-scheme: light dark; padding: env(safe-area-inset-top, 0px) 0 env(safe-area-inset-bottom, 0px) }
body { margin: 0; font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif }
img { max-width: 100% }
[hidden] { display: none !important }
</style>
</head>
<body>
"""


def main():
    meta = json.loads((DASH / "jobs.json").read_text())["meta"]
    if not meta.get("public"):
        raise SystemExit("dashboard/jobs.json still holds resume text; run score_jobs.py --public first")
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir()
    (SITE / "index.html").write_text(HEAD + (DASH / "index.html").read_text() + "\n</body>\n</html>\n")
    files = [DASH / "jobs.json", *sorted(DASH.glob("details-*.json"))]
    for f in files:
        shutil.copy2(f, SITE / f.name)
    n_jobs = len(json.loads((DASH / "jobs.json").read_text())["jobs"])
    (SITE / "meta.json").write_text(json.dumps({"fetched_at": meta["fetched_at"], "jobs": n_jobs}))
    (SITE / ".nojekyll").write_text("")  # serve files as-is, no Jekyll processing
    size = sum(f.stat().st_size for f in SITE.iterdir()) / 1e6
    print(f"site/ ready: {len(files) + 2} files, {size:.0f} MB")


if __name__ == "__main__":
    main()
