# Job Finder · live

An hourly-updated dashboard of live US job openings: every internship and co-op, plus full-time roles in tech fields, sorted by state, job type (full-time / internship / co-op) and work mode (remote / hybrid / on-site), and graded by posting age and reposts.

**Dashboard:** https://aashay1-ops.github.io/job-finder-live/

## How it updates

`.github/workflows/hourly.yml` runs at 7 minutes past every hour:

1. `src/fetch_jobs.py` pulls postings from 234 company job boards (Greenhouse, Lever, Ashby), SimplifyJobs' internship and new-grad lists, and the freehire.me aggregator (`src/freehire.py`: full sweep once a day, newest postings only on the other runs).
2. `src/score_jobs.py --public` scores each job against the owner's resumes. The resumes come from a repository secret and are never published.
3. `src/build_site.py` assembles `site/`, which is deployed to GitHub Pages.

Repost history and the freehire job store carry over between runs in the Actions cache.

The pipeline code is synced from a private repo with `tools/sync_public.sh`; edit it there.
