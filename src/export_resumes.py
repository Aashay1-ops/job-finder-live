"""Pack the resumes into one JSON blob for the RESUMES_JSON GitHub Actions secret.

The public site repo never contains resume files; the refresh workflow reads them from this secret
so it can score jobs, and score_jobs.py --public keeps the text out of the published data.

Usage (after editing anything in resumes/):
    python3 src/export_resumes.py | gh secret set RESUMES_JSON --repo Aashay1-ops/job-finder-site
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score_jobs import load_resumes  # noqa: E402  (reads resumes/ when RESUMES_JSON is unset)

if __name__ == "__main__":
    resumes, master = load_resumes()
    print(json.dumps({"resumes": resumes, "master": master}))
