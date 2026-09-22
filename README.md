# JOBPILOT

**AI-powered Dice job discovery and controlled application assistant.**

A terminal-only application. No website, no database, no frameworks - just Python,
JSON files, Playwright, and an optional LLM for two narrow jobs.

```
===============================================
                 JOBPILOT
===============================================

1. Setup Candidate
2. Upload / Parse Resume
3. Search Dice Jobs
4. Analyze Jobs
5. Apply to Job
6. Application History
7. Settings
8. Exit

Select:
```

---

## What it does

1. Stores your candidate profile in `data/candidate.json` (never invents data).
2. Parses your real resume (PDF / DOCX) and shows you what it extracted before saving.
3. Searches **Dice only**, through an authorised interface, and saves the real results.
4. Scores each job with a transparent deterministic formula (AI may only nudge it).
5. Drives **supported Dice Easy Apply** forms with Playwright: known fields come from your
   profile, semantic questions may be answered by the AI, sensitive questions always come to you.
6. Shows a full application review and **only submits when you type `YES`**.
7. Records every application in `data/applications.json` and every action in `data/logs.json`.

## What it deliberately does NOT do

* No CAPTCHA solving or bypass, no MFA/OTP bypass, no bot-detection evasion, no fingerprint
  spoofing, no proxy rotation, no rate-limit bypass. A security challenge always stops the run
  with **"Human intervention required."**
* No LinkedIn / Indeed / Glassdoor / Monster / Greenhouse / USAJOBS / Lever / ZipRecruiter.
  Only Dice is implemented (`DiceProvider`); other platforms are not pretended.
* No fake jobs, no fake candidates, no fake answers, no fake submission results.
  Demo mode exists only behind `--demo` and is clearly labelled.
* No submission without explicit typed confirmation, ever.

---

## 1. Requirements

* Python **3.9+** (3.10+ recommended)
* Windows 10/11 PowerShell, macOS, or Linux
* Internet access
* One of the two Dice integrations (see step 6):
  * an official Dice partner/enterprise **API key**, or
  * browser search of the public Dice site (**Playwright**, read-only)
* Optional: an OpenAI-compatible **LLM API key** for semantic matching and question answering.
  Everything works without it, except AI-assisted answers - those questions then come to you.

## 2. Installation

```powershell
# from the JOBPILOT folder
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\activate
```

macOS / Linux:

```bash
source .venv/bin/activate
```

Then, on any platform:

```bash
pip install -r requirements.txt
playwright install chromium
```

## 3. Configure `.env`

```powershell
copy .env.example .env       # Windows
```

```bash
cp .env.example .env         # macOS / Linux
```

Open `.env` and set what you have:

| Variable | Purpose |
| --- | --- |
| `DICE_API_KEY` | Official Dice partner API key. Preferred search path. |
| `DICE_API_BASE`, `DICE_SEARCH_PATH`, `DICE_API_AUTH`, `DICE_CLIENT_ID` | Endpoint details from your Dice agreement. |
| `DICE_ALLOW_BROWSER_SEARCH=true` | Read the public Dice site with a browser session instead of the API. |
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` | AI matching + question understanding (optional). |
| `AI_MIN_CONFIDENCE` | Below this, an AI answer is rejected and you are asked (default 0.75). |
| `BROWSER_HEADLESS=false` | Keep this false so you can watch and sign in. |
| `POLITE_DELAY_SECONDS` | Delay between page requests. Keep it at 1.5 or higher. |

Secrets are read from `.env` only. They are never printed, never logged, and never written to JSON.
API keys can be entered without echoing via **Settings → Set the Dice API key**.

## 4. Resume setup

Put your resume in `uploads/` (for example `uploads/resume.pdf`), then run the program and choose
**2. Upload / Parse Resume**. Supported: `.pdf` and `.docx` (also `.txt` / `.md`).
Scanned image-only PDFs cannot be read (no OCR) - export a text-based PDF or use the DOCX.

Extracted values are displayed with a confidence label (`high` / `medium` / `low`), and nothing is
saved until you answer **"Do you want to save this information as your candidate profile?"**.
Existing profile values are never silently overwritten by empty extractions.

## 5. Running the application

```bash
python jobpilot.py
```

Useful flags:

| Command | Meaning |
| --- | --- |
| `python jobpilot.py` | normal mode (real data only) |
| `python jobpilot.py --check` | environment / configuration check, then exit |
| `python jobpilot.py --state` | summary of the local JSON stores, then exit |
| `python jobpilot.py --demo` | opt-in demo mode (labelled demo records, local demo form, cannot submit) |
| `python jobpilot.py --version` | print the version |

On first run the program creates `data/`, `uploads/`, `data/errors/` and the four JSON files, and
prints an environment report showing exactly what is missing and how to fix it.
`--check` exits with status 1 when something still needs attention (a missing `.env`, no Dice
integration, or a browser that will not start) and 0 when the environment is complete.

### Typical first session

1. `1` Setup Candidate (or `2` after dropping your resume into `uploads/`).
2. `3` Search Dice Jobs - enter title, location, work mode, experience, posted-within.
3. `4` Analyze Jobs - reviews the deterministic score, matches and gaps.
4. `5` Apply to Job - runs the controlled Easy Apply workflow.
5. `6` Application History - see every attempt and its state.

## 6. Which Dice integration do you need?

Job search needs **one** of these; if neither is configured, the program says exactly what is
missing and stores nothing:

* **Official Dice API** - `DICE_API_KEY` (plus `DICE_API_BASE` / `DICE_SEARCH_PATH` from your
  Dice partner agreement). If your agreement uses different field names, adapt
  `DiceProvider._api_record_to_job` and `_parse_api_payload` - they are plain Python.
* **Public Dice site in a browser** - `DICE_ALLOW_BROWSER_SEARCH=true` with Playwright installed.
  This is a read-only search of pages you could visit yourself, at a low request rate. It does not
  log in unless you log in yourself, and it never touches anything behind a security control.

If your Dice access exposes a search capability this MVP does not implement, the program stops with
an explanatory message instead of substituting invented data.

## 7. How scoring works

Deterministic, explainable, and the same for everyone:

| Component | Weight |
| --- | --- |
| Role similarity | 30 |
| Skills overlap | 25 |
| Experience | 20 |
| Location / work mode | 10 |
| Education | 5 |
| Employment fit | 5 |
| Other requirements (sponsorship, clearance) | 5 |

```
80-100 -> APPLY
60-79  -> REVIEW
0-59   -> SKIP
```

The AI may propose a bounded adjustment of at most **-10 to +10** points, with a confidence and a
reason. The adjustment is clamped, recorded next to the deterministic breakdown, and the AI can
never set the score on its own. Without an LLM key the score is purely deterministic.

## 8. How the application workflow behaves

1. Opens the stored Dice job URL (session reused from `data/browser_state.json` if present).
2. If Dice asks for a sign-in, **you** sign in - inside the browser window. No password is ever
   collected, printed or stored.
3. Detects CAPTCHA / MFA / bot checks and stops: `Human intervention required.`
4. Classifies the job: `DICE_EASY_APPLY`, `EXTERNAL_APPLICATION`, `UNKNOWN`.
   External applications print `External application — not supported by this MVP.` and are never
   claimed as submitted.
5. Inspects the form (labels, `aria-label`, `aria-labelledby`, `fieldset/legend`, `name`, `id`),
   normalises each control, and maps known fields straight from your profile:
   First/Last name, Email, Phone, Location, Links, Resume file, Years of experience, Education.
6. Anything left becomes a question. **Sensitive/legal questions never go to the AI**
   (work authorization, sponsorship, criminal history, disability, veteran status, demographics,
   security clearance, legal certifications, salary expectations, availability). If your profile
   stores an explicit value it is used and shown; otherwise you are asked.
7. Non-sensitive semantic questions may be answered by the AI using only your stored facts.
   The answer is validated against the available options and the confidence threshold; otherwise
   the question comes to you. Missing information is never invented.
8. Fills the form with explicit waits, uploads the resume, then re-reads the page and checks every
   `required` field.
9. Unsupported widgets (custom comboboxes, `contenteditable` rich text, multi-file inputs, and
   input types outside the supported set) stop automation immediately:
   `Unsupported application field detected.` → state `REVIEW_REQUIRED`, flag `HUMAN_REVIEW_REQUIRED`.
10. Prints the full **APPLICATION REVIEW** (job, fields, AI questions with confidence, sensitive
    answers, unresolved items) and asks you to type **`YES`**.
    Anything else cancels and records `CANCELLED`.
11. Clicks submit only after `YES`, then looks for real confirmation text on the page.
    Confirmed → `SUBMITTED`. Not confirmed → `REVIEW_REQUIRED` with flag `SUBMISSION_UNVERIFIED`
    (the click may or may not have gone through - it is never reported as success).

States used: `DISCOVERED`, `SCORED`, `SELECTED`, `IN_PROGRESS`, `REVIEW_REQUIRED`,
`READY_TO_SUBMIT`, `SUBMITTED`, `FAILED`, `CANCELLED`.

## 9. Files it creates

```
JOBPILOT/
  jobpilot.py
  requirements.txt
  .env.example
  README.md
  data/
    candidate.json        your profile (real data you entered)
    jobs.json             real Dice jobs that were retrieved
    applications.json     every application attempt + state + answers
    logs.json             timestamp, action, job_id, application_id, status, error
    errors/               screenshots captured on automation failures
    browser_state.json    saved Dice session cookies (created after you sign in)
  uploads/                your resume files
```

## 9b. Self-test

The repository ships with a dependency-free test suite (no network, no browser needed):

```bash
python tests/test_jobpilot.py
```

It checks the scoring weights and thresholds, resume parsing for DOCX/PDF, JSON storage and corrupt
file recovery, AI output validation (option matching, unknown answers, low confidence), deterministic
field mapping, sensitive-question detection, the "yes means YES only" confirmation rule, and the
"no fake data" rules. Run it after you change anything.

## 10. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Dice search is not configured.` | Set `DICE_API_KEY`, or `DICE_ALLOW_BROWSER_SEARCH=true` with Playwright installed. |
| `Dice rejected the credentials (HTTP 401/403)` | Key wrong/expired or not entitled to that endpoint. Check `DICE_API_BASE` / `DICE_SEARCH_PATH`. |
| `Dice API response was not recognised` | Your agreement returns different field names - adapt `_parse_api_payload` / `_api_record_to_job`, or switch to browser search. |
| `Human intervention required.` | Dice showed a CAPTCHA/MFA/bot check. Complete it yourself in a normal browser; this program will not bypass it. |
| `Browser launch failed` | `playwright install chromium`. On Linux you may need `playwright install-deps chromium`. |
| `Resume could not be read` | Use a text-based PDF or DOCX; scanned images need OCR, which is not included. |
| `No supported form fields were found` | The Easy Apply form did not load, or it uses widgets this MVP does not support. The job is marked `REVIEW_REQUIRED`. |
| `Unsupported application field detected.` | Apply to that one manually - the program stops on purpose rather than guessing. |
| `no supported apply control was found` | You may not be signed in, or the job is applied for on the company's site. |
| AI never answers | Check `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`; check `LLM_ENABLED`. Low-confidence answers are rejected by design. |
| `jobs.json is not valid JSON` | The corrupt file is backed up automatically (`jobs.json.corrupt-*`); a fresh store is started. |
| Nothing happens with piped input | Interactive prompts need a terminal. Run it in a real terminal window. |
| Debug detail | Set `JOBPILOT_DEBUG=1` before running. |

## 11. Limitations (please read)

* **Dice only.** No other job platform is implemented.
* **Not "100% compatible with Dice".** Only Dice **Easy Apply** flows built from standard
  controls are supported. Anything else becomes `REVIEW_REQUIRED` for a human.
* **Not fully autonomous, by design.** Submission requires your `YES`; sensitive questions require
  your answer.
* **Job search depends on your Dice access.** If Dice does not expose search to you through an
  authorised interface, search cannot run - it will not be faked.
* Dice can change its page structure at any time; the selectors live in one place
  (`INSPECT_JS`, `FIND_APPLY_JS`, `FIND_SUBMIT_JS`, `DiceProvider._extract_cards`) so they can be
  updated quickly.
* Job-match scoring is a heuristic, not a promise of suitability.
* AI output is advisory and validated; it can be wrong or unavailable, in which case you are asked.
* The program never solves CAPTCHAs, never bypasses MFA or bot detection, never rotates proxies,
  and never tries to evade rate limits. If a security challenge appears, it stops.
* Verify important applications on dice.com yourself. The program only claims success when the
  site confirms it.

---

### Legal / responsible use

Use only in line with Dice's terms of service, your Dice partner agreement, and applicable law.
You are responsible for what is submitted under your name.
#   d i c e  
 