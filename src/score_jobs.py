"""Stage 5: score every job against each resume and pick the best one.

Usage:  python3 src/score_jobs.py [--public]      (run after fetch_jobs.py)
Output: dashboard/jobs.json + dashboard/details-<n>.json   (what the dashboard page loads)
        --public leaves resume text and contact details out of the output (for the public site)

Resumes come from the RESUMES_JSON environment variable when it is set (the refresh GitHub
Action passes them in as a secret), otherwise from the files in resumes/.

How the score works (0-100), per resume:
  75% skill coverage: of the skills/tools the posting asks for (from SKILLS below),
      what weighted share does this resume show? (smoothed by PRIOR, so thin postings can't max out)
  25% track match:    does the resume's focus (AI/ML, Robotics, Test) match the job's track?
Standard library only.
"""
import json
import os
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "resumes"
DASH = ROOT / "dashboard"

# name -> (regex, weight). Weight ~ how much it signals fit for an entry-level engineering role.
SKILLS = {
    "Python": (r"\bpython\b", 3), "C/C++": (r"\bc\+\+|\bc/c\+\+|\bcpp\b|embedded c\b", 3),
    "OpenCV": (r"opencv", 2), "Computer vision": (r"computer vision|\bcv\b", 3),
    "Machine learning": (r"machine learning|\bml\b", 3), "Deep learning": (r"deep learning|neural net", 2),
    "Sensor fusion": (r"sensor fusion|state estimation", 3), "IMU": (r"\bimus?\b|inertial", 2),
    "Depth sensing": (r"depth (camera|sensor|sensing)|rgb-?d|stereo camera", 2),
    "Pose estimation": (r"pose estimation|pose tracking|keypoint", 2),
    "Motion tracking": (r"motion tracking|object tracking|pose tracking", 2),
    "Image processing": (r"image processing|imaging", 2), "Calibration": (r"calibrat", 2),
    "Real-time systems": (r"real-?time", 0.5), "Kinematics": (r"kinematic|biomechanic", 2),
    "Embedded systems": (r"embedded", 3), "Microcontrollers": (r"microcontroller|\bmcu|arduino|stm32|\besp32", 2),
    "Motor control": (r"motor control|motor driver|\bmotors?\b|actuator", 2),
    "Circuits / electronics": (r"circuit|schematic|electronics", 2),
    "Wireless / Bluetooth": (r"bluetooth|\bble\b|wireless", 1), "Sensors": (r"\bsensors?\b", 2),
    "Testing": (r"\btest(ing)?\b|test plans?|test procedures?", 2),
    "Quality / inspection": (r"quality (assurance|control|checks?)|\bqa\b|\bqc\b|inspection", 2),
    "Technical documentation": (r"documentation|technical writing|work instructions", 0.5),
    "Hardware assembly": (r"assembly|assemble", 1), "Troubleshooting": (r"troubleshoot|debug", 2),
    "Customer-facing": (r"customer-facing|client-facing|customer support|technical support", 1), "NLP / documents": (r"\bnlp\b|natural language|summariz|translation", 2),
    "Facial recognition": (r"facial recognition|face recognition", 1), "Robotics": (r"robot", 2),
    "Automation": (r"automation", 0.5), "Data analysis": (r"data analysis|analytics", 1),
    # common asks that are NOT on the resume yet (they show up as gaps)
    "PyTorch": (r"pytorch", 2), "TensorFlow": (r"tensorflow|keras", 1), "ROS": (r"\bros2?\b", 3),
    "Linux": (r"linux|unix", 2), "Git": (r"\bgit\b|github|version control", 1), "Docker": (r"docker|container", 1),
    "Kubernetes": (r"kubernetes|\bk8s\b", 1), "Cloud (AWS/GCP/Azure)": (r"\baws\b|\bgcp\b|azure|cloud", 1),
    "SQL": (r"\bsql\b", 1), "MATLAB/Simulink": (r"matlab|simulink", 2), "CUDA / GPU": (r"cuda|\bgpus?\b", 1),
    "SLAM / localization": (r"\bslam\b|localization|mapping", 2), "LiDAR / radar": (r"lidar|radar", 2),
    "Kalman filtering": (r"kalman", 2), "Controls (PID)": (r"\bpid\b|control (theory|systems)|controls engineer|feedback control", 2),
    "Signal processing": (r"signal processing|\bdsp\b", 2), "FPGA / Verilog": (r"fpga|verilog|vhdl", 2),
    "PCB design": (r"\bpcb|altium|kicad", 2), "Lab equipment": (r"oscilloscope|multimeter|logic analyzer|test equipment", 2),
    "LabVIEW": (r"labview", 1), "CAD (SolidWorks etc.)": (r"\bcad\b|solidworks|creo|catia|fusion 360|nx\b", 1),
    "Lean / Six Sigma": (r"six sigma|\blean\b|root cause|8d\b|fmea", 1), "LLMs": (r"\bllms?\b|large language|transformer", 2),
    "Reinforcement learning": (r"reinforcement learning|\brl\b", 1), "Rust": (r"\brust\b", 1),
    "Java": (r"\bjava\b", 1), "JavaScript/TypeScript": (r"javascript|typescript", 1), "Go": (r"\bgolang\b", 1),
    "Statistics": (r"statistic", 1), "CAN bus": (r"\bcan bus|\bcan\b protocol|canbus", 1),
    "Distributed systems": (r"distributed systems", 1),
}
SKILL_RE = {k: re.compile(rx, re.I) for k, (rx, _) in SKILLS.items()}
W = {k: w for k, (_, w) in SKILLS.items()}

PHD = re.compile(r"\bph\.?\s?d\b|doctorate", re.I)
RESTRICTED = re.compile(r"u\.?s\.? citizen|us citizenship|united states citizen|security clearance|\bitar\b|"
                        r"u\.?s\.? person|export control|secret clearance|ts/sci|permanent resident|green card|"
                        r"lawful permanent|\bear\b regulations|export administration regulations", re.I)
NO_SPONSOR = re.compile(r"(not|unable to|cannot|will not|won't|do not|does not)\s+(be able to\s+)?"
                        r"(provide |offer )?(visa )?sponsor|not eligible for (f-?1|j-?1|cpt|opt)|"
                        r"without (the need for |requiring )?(visa |employer |current or future )?sponsorship|"
                        r"sponsorship[^.]{0,40}(now or in the future|currently or in the future)|"
                        r"not available for any (work |visa )?sponsorship|"
                        r"sponsorship (is|will) not (be )?(available|provided|offered)|\bno (visa |work )?sponsorship", re.I)
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*years?(?:'|’)?\s*(?:of\s+)?"
                   r"(?:[\w/&,\-]+\s+){0,6}?experience", re.I)


PRIOR = 8  # pseudo-weight of "unlisted requirements" every posting has


def docx_text(p):
    """Plain text of a .docx (paragraphs from word/document.xml); works on any OS."""
    with zipfile.ZipFile(p) as z:
        xml = z.read("word/document.xml").decode("utf8")
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
    return "\n".join("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", para)) for para in paras)


def load_resumes():
    """Each candidate resume: label, track it targets (None = general), full text. Plus the master text."""
    secret = os.environ.get("RESUMES_JSON")
    if secret:
        d = json.loads(secret)
        return d["resumes"], d["master"]
    return [
        {"key": "v1", "label": "Original resume (v1)", "track": None,
         "text": docx_text(RES / "originals" / "resume_v1_original.docx")},
        {"key": "neu", "label": "Original resume (NEU)", "track": None,
         "text": docx_text(RES / "originals" / "resume_v2_NEU.docx")},
        {"key": "ai", "label": "AI / ML / CV variant", "track": "AI / ML / CV",
         "text": (RES / "variants" / "resume_ai_ml_cv.md").read_text()},
        {"key": "robotics", "label": "Robotics / Embedded variant", "track": "Robotics / Embedded",
         "text": (RES / "variants" / "resume_robotics_embedded.md").read_text()},
        {"key": "test", "label": "Test / Technical variant", "track": "Test / Technical",
         "text": (RES / "variants" / "resume_test_technical.md").read_text()},
    ], (RES / "master_resume.md").read_text()


def skills_in(text):
    return {k for k, rx in SKILL_RE.items() if rx.search(text)}


def min_years(desc):
    """Smallest 'N years ... experience' figure in the posting (None if never stated)."""
    vals = [int(m.group(1)) for m in YEARS.finditer(desc) if int(m.group(1)) <= 20]
    return min(vals) if vals else None


# Compact codes the dashboard decodes (keep in sync with dashboard/index.html)
TYPES = ["Full-time", "Internship", "Co-op", "Part-time / Contract"]
MODES = ["On-site", "Hybrid", "Remote"]
MODE_SRC = ["stated", "location", "inferred", "assumed"]
LEVELS = ["Student", "Entry / New grad", "Mid / not stated", "Senior & above"]
TRACK_NAMES = ["AI / ML / CV", "Robotics / Embedded", "Test / Technical"]
SHARD = 500  # job descriptions per details file
# column order of each job row in dashboard/jobs.json (the page reads meta.cols)
# (links and descriptions live in details-<row // SHARD>.json, loaded when a job is opened)
COLS = ["t", "c", "l", "s", "r", "p", "ty", "m", "ms", "lv", "f", "tr", "y", "fl", "sc", "b", "k", "g", "rp", "fp"]


def main():
    public = "--public" in sys.argv
    data = json.loads((ROOT / "data" / "processed" / "jobs.json").read_text())
    resumes, master_text = load_resumes()
    for r in resumes:
        # drop the instruction notes ("> VARIANT: ...") so they don't count as skills
        r["text"] = "\n".join(l for l in r["text"].splitlines() if not l.startswith(">"))
        r["skills"] = skills_in(r["text"])
    master_skills = skills_in(master_text)
    skill_names = list(SKILLS)
    sidx = {k: i for i, k in enumerate(skill_names)}

    scored = []
    for j in data["jobs"]:
        desc = j.get("description") or ""
        fh = j.get("fh") or {}
        # freehire's canonical skill tags ("computer-vision") count as asked skills too
        tags = " ".join(t.replace("-", " ") for t in fh.get("skills", []))
        # requirements sit near the top; scanning past 6k chars costs time and rarely adds a skill
        asked = skills_in(j["title"] + "\n" + desc[:6000] + "\n" + tags)
        title_asked = skills_in(j["title"])
        total = sum(W[s] * (2 if s in title_asked else 1) for s in asked)
        # postings with no description (Simplify lists) are scored on the title alone: smaller prior, flagged "estimate"
        prior = PRIOR if desc else 3
        scores = []
        for r in resumes:
            got = sum(W[s] * (2 if s in title_asked else 1) for s in asked & r["skills"])
            # the prior keeps postings that list only 1-2 skills from scoring 100 on thin evidence
            cov = min(1.0, (got / (total + prior)) / 0.7)
            if not j["tracks"]:
                track = 0.0  # job is outside all three tracks: no resume gets a focus bonus
            else:
                track = 1.0 if r["track"] in j["tracks"] else (0.5 if r["track"] is None else 0.0)
            scores.append(round(100 * (0.75 * cov + 0.25 * track)))
        b = max(range(len(resumes)), key=lambda i: (scores[i], resumes[i]["key"] in ("ai", "robotics", "test")))
        spons = j.get("sponsorship", "")
        flags = (1 * (bool(RESTRICTED.search(desc)) or spons == "U.S. Citizenship is Required")
                 | 2 * (bool(NO_SPONSOR.search(desc)) or spons == "Does Not Offer Sponsorship" or fh.get("visa") is False)
                 | 4 * bool(PHD.search(desc)) | 8 * (spons == "Offers Sponsorship" or fh.get("visa") is True)
                 | 16 * (not desc) | 32 * (fh.get("mass", 0) >= 5) | 64 * bool(fh.get("fake")))
        scored.append((j, scores, b, flags, asked, desc))

    scored.sort(key=lambda x: -x[1][x[2]])
    companies = sorted({j["company"] for j, *_ in scored})
    cidx = {c: i for i, c in enumerate(companies)}
    fields = sorted({j["field"] for j, *_ in scored})
    fidx = {f: i for i, f in enumerate(fields)}

    DASH.mkdir(exist_ok=True)
    for old in DASH.glob("details*.json"):
        old.unlink()
    rows, shard, shard_no = [], {}, 0
    for j, scores, b, flags, asked, desc in scored:
        if len(shard) >= SHARD:
            (DASH / f"details-{shard_no}.json").write_text(json.dumps(shard, separators=(",", ":")))
            shard, shard_no = {}, shard_no + 1
        shard[str(len(rows))] = [j["url"], desc[:1800]]  # keyed by row number, the job's id on the page
        fh = j.get("fh") or {}
        row = {
            "t": j["title"].strip(), "c": cidx[j["company"]], "l": j["location"][:80],
            "s": ",".join(j["states"]), "r": int(j["remote_us"]), "p": j["posted"],
            "ty": TYPES.index(j["type"]), "m": MODES.index(j["mode"]), "ms": MODE_SRC.index(j["mode_src"]),
            "lv": LEVELS.index(j["level"]), "f": fidx[j["field"]],
            "tr": sum(1 << i for i, t in enumerate(TRACK_NAMES) if t in j["tracks"]),
            "y": fh.get("years") if fh.get("years") is not None else min_years(desc), "fl": flags, "sc": scores, "b": b,
            "rp": j.get("reposts", 0), "fp": j.get("first_posted") if j.get("first_posted") != j["posted"] else "",
            "k": sorted((sidx[s] for s in asked & resumes[b]["skills"]), key=lambda i: -W[skill_names[i]]),
            "g": sorted((sidx[s] for s in asked - master_skills), key=lambda i: -W[skill_names[i]]),
        }
        rows.append([row[c] for c in COLS])  # one array per job: no repeated key names
    if shard:
        (DASH / f"details-{shard_no}.json").write_text(json.dumps(shard, separators=(",", ":")))

    meta = {
        "fetched_at": data["fetched_at"], "sources": data["sources"], "total_pulled": data["total_pulled"],
        "companies": companies, "fields": fields, "types": TYPES, "modes": MODES, "mode_src": MODE_SRC,
        "levels": LEVELS, "tracks": TRACK_NAMES, "skill_names": skill_names,
        "skills": {k: rx for k, (rx, _) in SKILLS.items()},
        "resumes": [{"key": r["key"], "label": r["label"], "track": r["track"], "text": "" if public else r["text"].strip()}
                    for r in resumes],
        "master": "" if public else "\n".join(l for l in master_text.splitlines() if not l.startswith(">")),
        "public": public, "cols": COLS, "shard": SHARD,
    }
    (DASH / "jobs.json").write_text(json.dumps({"meta": meta, "jobs": rows}, separators=(",", ":")))

    from collections import Counter
    print(f"scored {len(rows):,} jobs · {shard_no + 1} detail files · best resume:",
          dict(Counter(resumes[x[COLS.index("b")]]["key"] for x in rows)))
    sc, b, fl = COLS.index("sc"), COLS.index("b"), COLS.index("fl")
    print("fit ≥80:", sum(x[sc][x[b]] >= 80 for x in rows), "· title-only estimates:", sum(x[fl] & 16 > 0 for x in rows),
          "· citizenship/clearance:", sum(x[fl] & 1 > 0 for x in rows))


if __name__ == "__main__":
    main()
