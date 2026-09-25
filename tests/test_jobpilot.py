#!/usr/bin/env python3
"""
JOBPILOT self-test. Run it after any change:

    python tests/test_jobpilot.py        (or: python -m pytest tests -q)

It covers scoring, resume parsing, JSON storage, the answer bank, deterministic
field mapping, safety guards, and the "no fake data" rules. No network access needed.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jobpilot as jp  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_pdf(path: Path, lines) -> Path:
    """Write a tiny, valid, text-based PDF without extra dependencies."""
    content_parts = ["BT", "/F1 11 Tf", "14 TL", "40 760 Td"]
    for index, line in enumerate(lines):
        safe = (line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)"))
        if index:
            content_parts.append("T*")
        content_parts.append(f"({safe}) Tj")
    content_parts.append("ET")
    stream = "\n".join(content_parts).encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n"
            f"{xref_at}\n%%EOF\n").encode()
    path.write_bytes(bytes(out))
    return path


def make_docx(path: Path, lines) -> Path:
    import docx
    document = docx.Document()
    for line in lines:
        document.add_paragraph(line)
    document.save(str(path))
    return path


RESUME_LINES = [
    "JANE DOE",
    "jane.doe@example.com | (415) 555-0142 | San Francisco, CA",
    "https://linkedin.com/in/janedoe | https://github.com/janedoe",
    "",
    "SKILLS",
    "Python, Django, PostgreSQL, Docker, AWS, React",
    "",
    "EXPERIENCE",
    "Senior Python Developer, Acme Corp",
    "2020 - Present",
    "Built REST APIs with Django and PostgreSQL.",
    "",
    "EDUCATION",
    "Bachelor of Science in Computer Science, State University",
    "",
    "CERTIFICATIONS",
    "AWS Certified Developer Associate",
]


def candidate_fixture() -> dict:
    candidate = dict(jp.CANDIDATE_DEFAULTS)
    candidate.update({
        "first_name": "Jane", "last_name": "Doe", "email": "jane.doe@example.com",
        "phone": "(415) 555-0142", "location": "San Francisco, CA",
        "target_roles": ["Python Developer", "Backend Engineer"],
        "years_experience": 6,
        "skills": ["Python", "Django", "PostgreSQL", "Docker", "AWS", "REST"],
        "education": "Bachelor of Science in Computer Science",
        "certifications": ["AWS Certified Developer Associate"],
        "work_modes": ["Remote", "Hybrid"],
        "employment_type": "FULLTIME",
        "work_authorization": "US citizen",
        "sponsorship_required": "no",
        "linkedin": "https://linkedin.com/in/janedoe",
    })
    return candidate


def job_fixture(**overrides) -> dict:
    job = jp.normalize_job({
        "id": "dice-test-1",
        "title": "Python Backend Developer",
        "company": "Example Corp",
        "location": "San Francisco, CA",
        "description": "We need 5+ years of Python, Django, PostgreSQL, Docker and AWS experience. "
                       "Remote friendly.",
        "skills": ["Python", "Django", "PostgreSQL", "Docker"],
        "employment_type": "FULLTIME",
        "workplace_type": "Remote",
        "posted_date": "2026-09-01",
        "application_type": jp.AT_EASY_APPLY,
        "source_url": "https://www.dice.com/job-detail/test-1",
    })
    job.update(overrides)
    return job


# ---------------------------------------------------------------------------
class TempStoreMixin:
    """Point every JSON store at a throwaway directory for the duration of a test.

    Without this, tests read and write the developer's real data/ folder, so the
    suite fails on leftover demo jobs whenever the app was run first.
    """

    def setUp(self):
        self._real = (jp.DATA_DIR, jp.UPLOADS_DIR, jp.ERRORS_DIR, jp.CANDIDATE_FILE,
                      jp.JOBS_FILE, jp.APPLICATIONS_FILE, jp.LOGS_FILE)
        self._log_cache = jp._LOG_CACHE
        self.tmp = Path(tempfile.mkdtemp())
        jp.DATA_DIR = self.tmp / "data"
        jp.UPLOADS_DIR = self.tmp / "uploads"
        jp.ERRORS_DIR = jp.DATA_DIR / "errors"
        jp.CANDIDATE_FILE = jp.DATA_DIR / "candidate.json"
        jp.JOBS_FILE = jp.DATA_DIR / "jobs.json"
        jp.APPLICATIONS_FILE = jp.DATA_DIR / "applications.json"
        jp.LOGS_FILE = jp.DATA_DIR / "logs.json"
        jp._LOG_CACHE = None
        jp.ensure_dirs()

    def tearDown(self):
        (jp.DATA_DIR, jp.UPLOADS_DIR, jp.ERRORS_DIR, jp.CANDIDATE_FILE,
         jp.JOBS_FILE, jp.APPLICATIONS_FILE, jp.LOGS_FILE) = self._real
        jp._LOG_CACHE = self._log_cache


class TestStorageAndSafety(TempStoreMixin, unittest.TestCase):
    def test_directories_are_created(self):
        self.assertTrue(jp.DATA_DIR.is_dir())
        self.assertTrue(jp.UPLOADS_DIR.is_dir())
        self.assertTrue(jp.ERRORS_DIR.is_dir())

    def test_json_roundtrip_and_corrupt_recovery(self):
        jp.write_json(jp.JOBS_FILE, {"version": 1, "jobs": [{"id": "a"}]})
        self.assertEqual(jp.load_jobs()[0]["id"], "a")

        jp.JOBS_FILE.write_text("{not json at all", encoding="utf-8")
        self.assertEqual(jp.load_jobs(), [])                        # no crash
        backups = list(jp.DATA_DIR.glob("jobs.json.corrupt-*"))
        self.assertTrue(backups, "a corrupt file must be backed up")

    def test_missing_files_are_not_fatal(self):
        self.assertEqual(jp.load_jobs(), [])
        self.assertEqual(jp.load_applications(), [])
        self.assertFalse(jp.candidate_has_profile(jp.load_candidate()))

    def test_job_merge_and_application_states(self):
        added, updated = jp.merge_jobs([job_fixture()])
        self.assertEqual((added, updated), (1, 0))
        job = jp.load_jobs()[0]
        added, updated = jp.merge_jobs([job_fixture(location="Austin, TX")])
        self.assertEqual((added, updated), (0, 1))
        self.assertEqual(len(jp.load_jobs()), 1)

        app = jp.create_application(job, jp.ST_SELECTED)
        self.assertEqual(app["state"], jp.ST_SELECTED)
        jp.set_state(app["id"], jp.ST_SUBMITTED, note="verified")
        stored = jp.get_application(app["id"])
        self.assertEqual(stored["state"], jp.ST_SUBMITTED)
        self.assertTrue(stored["submitted_at"])

    def test_logging_never_stores_keys(self):
        jp.log_event("unit_test", "ok", job_id="j1", error="boom")
        entries = jp._logs()
        self.assertEqual(entries[-1]["action"], "unit_test")
        blob = json.dumps(entries).lower()
        for forbidden in ("sk-", "api_key=", "password", "bearer "):
            self.assertNotIn(forbidden, blob)

    def test_submission_requires_exact_yes(self):
        original = jp.UI.ask
        try:
            for typed, expected in (("YES", True), ("yes", True), ("Y", False),
                                    ("", False), ("yep", False), ("NO", False)):
                jp.UI.ask = staticmethod(lambda *a, **k: typed)      # type: ignore[assignment]
                self.assertEqual(jp.UI.confirm_exact("Type YES to submit"), expected,
                                 f"typed {typed!r}")
        finally:
            jp.UI.ask = original                                 # type: ignore[assignment]


class TestScoring(unittest.TestCase):
    def test_weights_sum_to_100(self):
        self.assertEqual(sum(jp.WEIGHTS.values()), 100)

    def test_strong_match_recommends_apply(self):
        result = jp.score_job(job_fixture(), candidate_fixture())
        self.assertGreaterEqual(result["score"], 80)
        self.assertEqual(result["recommendation"], "APPLY")
        for key in jp.WEIGHTS:
            self.assertIn(key, result["breakdown"])

    def test_boundaries(self):
        self.assertEqual(jp.recommendation_for(80), "APPLY")
        self.assertEqual(jp.recommendation_for(79), "REVIEW")
        self.assertEqual(jp.recommendation_for(60), "REVIEW")
        self.assertEqual(jp.recommendation_for(59), "SKIP")

    def test_unrelated_job_scores_low_and_reports_gaps(self):
        job = job_fixture(title="Truck Driver", location="Chicago, IL",
                          workplace_type="Onsite", skills=["CDL", "Logistics"],
                          description="Requires 10 years of commercial driving experience.",
                          employment_type="CONTRACT")
        result = jp.score_job(job, candidate_fixture())
        self.assertEqual(result["recommendation"], "SKIP")
        self.assertTrue(result["gaps"])

    def test_no_skills_data_does_not_crash(self):
        empty = dict(jp.CANDIDATE_DEFAULTS)
        result = jp.score_job(job_fixture(), empty)
        self.assertIsInstance(result["score"], int)
        self.assertTrue(result["gaps"])

    def test_sponsorship_conflict_is_penalised(self):
        candidate = candidate_fixture()
        candidate["sponsorship_required"] = "yes"
        job = job_fixture(description="Python role. No visa sponsorship available.")
        result = jp.score_job(job, candidate)
        self.assertTrue(any("sponsor" in gap.lower() for gap in result["gaps"]))


class TestResumeParsing(unittest.TestCase):
    def test_docx_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_docx(Path(tmp) / "resume.docx", RESUME_LINES)
            text, error = jp.extract_resume_text(path)
            self.assertIsNone(error)
            self.assertIn("jane.doe@example.com", text)
            parsed = jp.parse_resume(text)
            self.assertEqual(parsed["email"], "jane.doe@example.com")
            self.assertEqual(parsed["first_name"], "Jane")     # ALL CAPS headers are normalised
            self.assertEqual(parsed["last_name"], "Doe")
            self.assertEqual(parsed["work_experience"][0]["start"], "2020")
            self.assertEqual(parsed["work_experience"][0]["end"], "Present")
            self.assertNotIn("github", [skill.lower() for skill in parsed["skills"]])
            self.assertIn("linkedin.com", parsed["linkedin"])
            self.assertIn("github.com", parsed["github"])
            self.assertIn("Python", parsed["skills"])
            self.assertIn("Docker", parsed["skills"])
            self.assertIn("San Francisco", parsed["location"])
            self.assertTrue(parsed["certifications"])
            self.assertIsNotNone(parsed["years_experience"])

    def test_pdf_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "resume.pdf", RESUME_LINES)
            text, error = jp.extract_resume_text(path)
            if error and "No PDF library" in error:
                self.skipTest("no PDF library installed")
            self.assertIsNone(error)
            parsed = jp.parse_resume(text)
            self.assertEqual(parsed["email"], "jane.doe@example.com")
            self.assertIn("Python", parsed["skills"])

    def test_missing_and_broken_files_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            text, error = jp.extract_resume_text(Path(tmp) / "nope.pdf")
            self.assertIsNone(text)
            self.assertIn("not found", error.lower())
            broken = Path(tmp) / "broken.pdf"
            broken.write_bytes(b"this is not a pdf")
            text, error = jp.extract_resume_text(broken)
            self.assertIsNone(text)
            self.assertTrue(error)
            empty_docx = Path(tmp) / "empty.docx"
            empty_docx.write_bytes(b"not a docx")
            text, error = jp.extract_resume_text(empty_docx)
            self.assertIsNone(text)
            self.assertIn("Could not read", error)

    def test_confidence_flags_exist(self):
        parsed = jp.parse_resume("\n".join(RESUME_LINES))
        for key in ("email", "name", "location", "skills", "education"):
            self.assertIn(key, parsed["confidence"])


class TestNoFakeData(TempStoreMixin, unittest.TestCase):
    def test_availability_message_when_nothing_is_configured(self):
        config = jp.Config()
        provider = jp.DiceProvider(config)
        ready, reason = provider.availability()
        self.assertFalse(ready)
        self.assertIn("DICE_API_KEY", reason)
        with self.assertRaises(jp.ProviderError):
            provider.search(jp.SearchQuery(title="python"))

    def test_unconfigured_search_returns_nothing(self):
        """With no Dice integration the search must return an empty list, never sample data."""
        jobs = jp.menu_search_jobs(jp.Config(demo=False))
        self.assertEqual(jobs, [])
        self.assertEqual(jp.load_jobs(), [])

    def test_demo_data_is_labelled(self):
        for job in jp.demo_jobs():
            self.assertEqual(job["source"], "demo")
            self.assertIn("DEMO", job["title"])
            self.assertIn("not a real listing", job["company"])

    def test_api_payload_parsing_is_strict(self):
        provider = jp.DiceProvider(jp.Config())
        with self.assertRaises(jp.ProviderError):
            provider._parse_api_payload({"unexpected": "shape"})
        jobs = provider._parse_api_payload({"jobs": [{
            "title": "Backend Developer", "companyName": "Example",
            "jobDetailUrl": "https://www.dice.com/job-detail/abc123",
            "employmentType": "FULLTIME", "workplaceTypes": "Remote",
            "summary": "Python role with 3+ years experience.",
        }]})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["company"], "Example")
        self.assertTrue(jobs[0]["application_url"].startswith("https://www.dice.com"))
        self.assertEqual(jobs[0]["application_type"], jp.AT_UNKNOWN)   # never assumed

    def test_apply_type_classification(self):
        self.assertEqual(jp.classify_apply_label("Easy Apply"), jp.AT_EASY_APPLY)
        self.assertEqual(jp.classify_apply_label("Apply on Company Site"), jp.AT_EXTERNAL)
        self.assertEqual(jp.classify_apply_label("", "apply on company website"), jp.AT_EXTERNAL)
        self.assertEqual(jp.classify_apply_label("Something else", "nothing here"), jp.AT_UNKNOWN)

    def test_dice_apply_now_button_is_not_misread_as_external(self):
        """Regression: Dice's real Easy Apply button is labelled "Apply Now".

        It used to be classified EXTERNAL_APPLICATION, which made menu 5 refuse
        every Dice Easy Apply job. A bare label is now UNKNOWN, and the wizard
        href is what proves Dice hosts the form.
        """
        self.assertEqual(jp.classify_apply_label("Apply Now"), jp.AT_UNKNOWN)
        signed_out = ("https://www.dice.com/dashboard/login?redirectUrl="
                      "%2Fjob-applications%2F3fd53cce-03b9-47af-825c-4ef74b984763%2Fwizard")
        self.assertEqual(jp.classify_apply_label("Apply Now", "", signed_out), jp.AT_EASY_APPLY)
        signed_in = "https://www.dice.com/job-applications/3fd53cce-03b9/wizard"
        self.assertEqual(jp.classify_apply_label("Apply", "", signed_in), jp.AT_EASY_APPLY)
        offsite = "https://careers.example.com/jobs/1234/apply"
        self.assertEqual(jp.classify_apply_label("Apply Now", "", offsite), jp.AT_EXTERNAL)

    def test_wizard_job_id_extraction(self):
        signed_out = ("https://www.dice.com/dashboard/login?redirectUrl="
                      "%2Fjob-applications%2Fabc-123%2Fwizard")
        self.assertEqual(jp.dice_wizard_job_id(signed_out), "abc-123")
        self.assertEqual(jp.dice_wizard_job_id("https://www.dice.com/job-applications/xyz/wizard"),
                         "xyz")
        self.assertEqual(jp.dice_wizard_job_id("https://careers.example.com/apply"), "")
        self.assertEqual(jp.dice_wizard_job_id(""), "")
        self.assertEqual(jp.dice_wizard_url("abc-123"),
                         "https://www.dice.com/job-applications/abc-123/wizard")

    def test_employment_type_is_canonicalised(self):
        for raw, expected in (("Full-time", "FULLTIME"), ("FULL_TIME", "FULLTIME"),
                              ("Contract W2", "CONTRACT"), ("Part-time", "PARTTIME"),
                              ("Third Party", "THIRD_PARTY")):
            self.assertEqual(jp.normalize_employment_type(raw), expected, raw)
        self.assertEqual(jp.normalize_employment_type(""), "")
        # A full-time contract role keeps both signals.
        self.assertEqual(jp.normalize_employment_type("Full-time, Contract"),
                         "FULLTIME, CONTRACT")
        # And normalize_job applies it, so scoring compares like with like.
        job = jp.normalize_job({"id": "x", "title": "Dev", "employment_type": "Full-time"})
        self.assertEqual(job["employment_type"], "FULLTIME")

    def test_unsupported_kinds_are_rejected(self):
        for kind in ("date", "number", "url", "password", "range", "color", "search"):
            self.assertIn(kind, jp.UNSUPPORTED_KINDS)
        self.assertEqual(jp.normalize_kind("tel"), "phone")
        self.assertEqual(jp.normalize_kind("select-one"), "select")


class TestFieldMappingAndQuestions(TempStoreMixin, unittest.TestCase):
    def test_deterministic_mapping(self):
        candidate = candidate_fixture()
        self.assertEqual(jp.match_known_field(jp.normalize_field_text(
            {"label": "First Name", "name": "firstName"})), "first_name")
        self.assertEqual(jp.match_known_field(jp.normalize_field_text(
            {"label": "Email Address", "name": "email"})), "email")
        self.assertEqual(jp.match_known_field(jp.normalize_field_text(
            {"label": "Phone", "name": "phone"})), "phone")
        self.assertEqual(jp.deterministic_value("email", candidate), "jane.doe@example.com")
        self.assertEqual(jp.deterministic_value("first_name", candidate), "Jane")
        self.assertEqual(jp.deterministic_value("full_name", candidate), "Jane Doe")
        self.assertEqual(jp.deterministic_value("resume", candidate), "")

    def test_sensitive_detection(self):
        cases = {
            "Are you authorized to work in the United States?": "work_authorization",
            "Will you now or in the future require sponsorship?": "sponsorship",
            "Have you ever been convicted of a felony?": "criminal_history",
            "Do you have a disability?": "disability",
            "Are you a protected veteran?": "veteran_status",
            "What is your gender?": "demographic",
            "Do you hold an active security clearance?": "security_clearance",
        }
        for text, expected in cases.items():
            self.assertEqual(jp.detect_sensitive(text), expected, text)
        self.assertIsNone(jp.detect_sensitive("What is your first name?"))
        self.assertIsNone(jp.detect_sensitive("How many years of Python experience do you have?"))

    def test_option_matching_and_abstention(self):
        self.assertEqual(jp.best_option("US citizen", ["Yes", "No"]), None)
        self.assertEqual(jp.best_option("yes", ["Yes", "No"]), "Yes")
        self.assertEqual(jp.best_option("Bachelor of Science in Computer Science",
                                        ["Bachelors", "Masters", "Doctorate"]), "Bachelors")
        self.assertEqual(jp.best_option("", ["Yes", "No"]), None)

    def test_skill_specific_years_question_is_not_filled_from_total_years(self):
        """'Years with Kubernetes?' must never be answered with your TOTAL years."""
        generic = ["How many years of experience do you have?", "Total experience",
                   "Years of relevant experience", "total years of professional experience"]
        specific = ["How many years of experience do you have with Kubernetes?",
                    "Years of experience with Python and Django?",
                    "How many years have you used AWS?", "Years of React experience?"]
        for text in generic:
            self.assertEqual(jp.match_known_field(jp.normalize_field_text({"label": text})),
                             "years_experience", text)
        for text in specific:
            self.assertNotEqual(jp.match_known_field(jp.normalize_field_text({"label": text})),
                                "years_experience", text)
        self.assertEqual(jp.named_technology("years with Kubernetes"), "kubernetes")
        self.assertIsNone(jp.named_technology("how many years of experience do you have"))

    def test_free_text_experience_boxes_are_not_filled_with_a_number(self):
        """'Describe your experience' is not a years question - '6' would be nonsense."""
        for text in ("Describe your professional experience",
                     "Please summarise your work experience",
                     "What experience do you bring to this role?"):
            self.assertNotEqual(jp.match_known_field(jp.normalize_field_text({"label": text})),
                                "years_experience", text)

    def test_an_unmatched_years_question_falls_through_to_the_answer_bank(self):
        candidate = candidate_fixture()
        question = "How many years of experience do you have with Kubernetes?"
        jp.bank_store(candidate, question, "3", "human")
        controls = [{"kind": "text", "label": question, "name": "k8s_years",
                     "state_key": "0", "index": 0}]
        planned, problems = jp.build_planned_fields(controls, candidate, None)
        jp.resolve_planned_fields(planned, candidate, job_fixture(), None)
        self.assertEqual(planned[0].value, "3")       # your answer, not your total years
        self.assertEqual(planned[0].source, "bank")
        self.assertNotEqual(str(planned[0].value), str(candidate.get("years_experience")))

    def test_sensitive_question_is_answered_only_by_you(self):
        """Nothing is banked yet, so a legal question is routed to a human prompt."""
        candidate = candidate_fixture()
        question = "Are you authorized to work in the US?"
        item = jp.PlannedField(field={}, question=question, kind="radio",
                               options=["Yes", "No"], sensitive="work_authorization")
        asked = []

        def fake_human(target, cand=None):
            asked.append(target.question)
            target.value, target.source, target.resolved = "Yes", "human", True

        original = jp.ask_human_for_field
        jp.ask_human_for_field = fake_human
        try:
            jp.resolve_planned_fields([item], candidate, job_fixture(), None)
        finally:
            jp.ask_human_for_field = original
        self.assertEqual(asked, [question])
        self.assertEqual(item.source, "human")      # answered by you, never inferred

    def test_your_own_sensitive_answer_is_reused_across_the_batch(self):
        """You typed it on job 1, so job 17 reuses YOUR answer instead of asking again."""
        candidate = candidate_fixture()
        question = "Will you now or in the future require sponsorship?"
        jp.bank_store(candidate, question, "No", "human")
        item = jp.PlannedField(field={}, question=question, kind="radio",
                               options=["Yes", "No"], sensitive="sponsorship")
        jp.resolve_planned_fields([item], candidate, job_fixture(), None)
        self.assertTrue(item.resolved)
        self.assertEqual(item.value, "No")
        self.assertEqual(item.source, "bank")

    def test_declining_once_is_remembered_as_leave_it_empty(self):
        """A skipped question is not asked 25 more times, and is never guessed either."""
        candidate = candidate_fixture()
        question = "LinkedIn profile URL"
        self.assertTrue(jp.bank_decline(candidate, question))

        item = jp.PlannedField(field={}, question=question, kind="text")
        self.assertTrue(jp._apply_banked_answer(item, candidate))
        self.assertTrue(item.declined)
        self.assertFalse(item.resolved)         # stays empty
        self.assertEqual(item.value, "")

        asked = []
        original = jp.ask_human_for_field
        jp.ask_human_for_field = lambda target, cand=None: asked.append(target.question)
        try:
            fresh = jp.PlannedField(field={}, question=question, kind="text")
            jp.resolve_planned_fields([fresh], candidate, job_fixture(), None)
        finally:
            jp.ask_human_for_field = original
        self.assertEqual(asked, [])             # not re-asked on the next job
        self.assertFalse(fresh.resolved)

    def test_answering_at_the_prompt_banks_the_answer(self):
        candidate = candidate_fixture()
        question = "How many years of experience do you have with Kubernetes?"
        item = jp.PlannedField(field={}, question=question, kind="text")
        prompts = []
        original = jp.UI.ask
        jp.UI.ask = lambda prompt, default="", allow_empty=False: (prompts.append(prompt), "6")[1]
        try:
            jp.ask_human_for_field(item, candidate)
        finally:
            jp.UI.ask = original
        self.assertEqual(len(prompts), 1)
        self.assertTrue(item.resolved)
        self.assertEqual(item.value, "6")
        self.assertEqual(item.source, "human")
        self.assertEqual(jp.bank_lookup(candidate, question), "6")   # reused on the next job

    def test_pressing_enter_at_the_prompt_banks_the_decline(self):
        candidate = candidate_fixture()
        question = "Portfolio website"
        item = jp.PlannedField(field={}, question=question, kind="text")
        original = jp.UI.ask
        jp.UI.ask = lambda prompt, default="", allow_empty=False: ""   # you skip it
        try:
            jp.ask_human_for_field(item, candidate)
        finally:
            jp.UI.ask = original
        self.assertFalse(item.resolved)
        self.assertEqual(item.source, "missing")
        self.assertTrue(jp.bank_entry(candidate, question)["declined"])

    def test_picking_an_option_by_number_banks_the_exact_option_text(self):
        candidate = candidate_fixture()
        question = "Are you willing to relocate?"
        item = jp.PlannedField(field={}, question=question, kind="radio",
                               options=["Yes", "No", "Open to it"])
        original = jp.UI.ask
        jp.UI.ask = lambda prompt, default="", allow_empty=False: "3"   # menu number
        try:
            jp.ask_human_for_field(item, candidate)
        finally:
            jp.UI.ask = original
        self.assertEqual(item.value, "Open to it")     # exact page text, not "3"
        self.assertEqual(jp.bank_lookup(candidate, question), "Open to it")

    def test_build_planned_fields_maps_profile_values(self):
        controls = [
            {"kind": "text", "label": "First Name", "name": "firstName", "state_key": "0", "index": 0},
            {"kind": "email", "label": "Email", "name": "email", "state_key": "1", "index": 1},
            {"kind": "file", "label": "Resume", "name": "resume", "state_key": "2", "index": 2},
            {"kind": "radio", "label": "Yes", "option_label": "Yes", "group_label":
                "Are you authorized to work in the country of this job?", "name": "authorized",
             "value": "Yes", "state_key": "3", "index": 3},
        ]
        jp.attach_radio_options(controls)
        planned, problems = jp.build_planned_fields(controls, candidate_fixture(),
                                                    Path("/tmp/resume.pdf"))
        by_question = {item.question: item for item in planned}
        self.assertTrue(by_question["First Name"].resolved)
        self.assertEqual(by_question["First Name"].source, "profile")
        self.assertEqual(by_question["First Name"].value, "Jane")
        self.assertEqual(by_question["Resume"].source, "profile:resume")
        group = [item for item in planned if item.kind == "radio"][0]
        # stored profile value "US citizen" cannot answer a Yes/No question -> ask the human
        self.assertFalse(group.resolved)
        self.assertEqual(group.hint, "US citizen")     # shown to the human, never guessed
        self.assertEqual(group.source, "profile:sensitive")

    def test_validate_required_detects_empty_fields(self):
        controls = [
            {"kind": "text", "label": "First Name", "required": True, "value": "Jane", "state_key": "0"},
            {"kind": "text", "label": "Last Name", "required": True, "value": "", "state_key": "1"},
            {"kind": "file", "label": "Resume", "required": True, "value": "", "state_key": "2"},
            {"kind": "radio", "label": "Yes", "option_label": "Yes", "name": "auth",
             "group_label": "Work authorization?", "checked": False, "required": True, "state_key": "3"},
        ]
        gaps = jp.validate_required(controls, [])
        self.assertEqual(len(gaps), 3)
        self.assertTrue(any("Last Name" in gap for gap in gaps))
        self.assertTrue(any("file upload" in gap.lower() for gap in gaps))

    def test_challenge_hints_cover_the_required_controls(self):
        for token in ("captcha", "two-factor", "verification code", "are you a robot"):
            self.assertTrue(any(token in hint for hint in jp.CHALLENGE_HINTS))


class TestResumeImportToProfile(unittest.TestCase):
    def test_plain_text_resume_parse(self):
        parsed = jp.parse_resume("\n".join(RESUME_LINES))
        self.assertEqual(parsed["email"], "jane.doe@example.com")
        self.assertEqual(parsed["phone"], "(415) 555-0142")
        self.assertIn("Python", parsed["skills"])
        self.assertIn("Bachelor", parsed["education"])
        self.assertTrue(parsed["work_experience"])
        self.assertIn("Django", parsed["skills"])

    def test_requirements_extraction(self):
        description = ("About the role\nWe build things.\n\nRequirements:\n- 5+ years Python\n"
                       "- Docker\n\nBenefits:\n- Health insurance")
        found = jp.extract_requirements(description)
        self.assertIn("Python", found)
        self.assertNotIn("Health insurance", found)


class TestBatchApply(TempStoreMixin, unittest.TestCase):
    """Batch apply (20-30 jobs at once) with no LLM involved."""

    def test_selection_filters_and_orders(self):
        jobs = [
            {"id": "a", "title": "Low", "application_type": jp.AT_EASY_APPLY,
             "source_url": "https://www.dice.com/job-detail/a", "score": 55},
            {"id": "b", "title": "High", "application_type": jp.AT_EASY_APPLY,
             "source_url": "https://www.dice.com/job-detail/b", "score": 92},
            {"id": "c", "title": "External", "application_type": jp.AT_EXTERNAL,
             "source_url": "https://careers.example.com/1", "score": 99},
            {"id": "d", "title": "Demo", "application_type": jp.AT_EASY_APPLY,
             "source_url": "demo://local/demo-form", "score": 99},
            {"id": "e", "title": "Mid", "application_type": jp.AT_EASY_APPLY,
             "source_url": "https://www.dice.com/job-detail/e", "score": 71},
        ]
        picked = [job["id"] for job in jp.select_batch_jobs(jobs, 25, 60)]
        self.assertEqual(picked, ["b", "e"])          # external + demo excluded, best first
        self.assertEqual([job["id"] for job in jp.select_batch_jobs(jobs, 1, 0)], ["b"])
        self.assertEqual(jp.select_batch_jobs(jobs, 25, 95), [])
        self.assertEqual(jp.select_batch_jobs([], 25, 0), [])

    def test_unscored_jobs_are_not_silently_zero(self):
        jobs = [{"id": "a", "application_type": jp.AT_EASY_APPLY,
                 "source_url": "https://www.dice.com/job-detail/a", "score": None}]
        self.assertEqual(jp.select_batch_jobs(jobs, 25, 1), [])
        self.assertEqual([j["id"] for j in jp.select_batch_jobs(jobs, 25, 0)], ["a"])

    def test_answer_bank_roundtrip(self):
        candidate = dict(jp.CANDIDATE_DEFAULTS)
        jp.save_candidate(candidate)
        question = "How many years of experience do you have with Python?"
        self.assertEqual(jp.bank_lookup(candidate, question), "")
        self.assertTrue(jp.bank_store(candidate, question, "6 years", "human"))
        # Reloaded from disk, and matched case/punctuation-insensitively.
        reloaded = jp.load_candidate()
        self.assertEqual(jp.bank_lookup(reloaded, question), "6 years")
        self.assertEqual(jp.bank_lookup(reloaded, "how many YEARS of experience do you have with python??"),
                         "6 years")
        self.assertFalse(jp.bank_store(candidate, question, "", "human"))

    def test_banked_answer_must_fit_the_options(self):
        candidate = dict(jp.CANDIDATE_DEFAULTS)
        candidate["answer_bank"] = {jp.answer_bank_key("Do you have an MBA?"): {"answer": "No"}}
        item = jp.PlannedField(field={}, question="Do you have an MBA?", kind="radio",
                               options=["Yes", "No"])
        self.assertTrue(jp._apply_banked_answer(item, candidate))
        self.assertEqual(item.value, "No")
        self.assertEqual(item.source, "bank")
        # A saved answer that matches none of this page's options is refused.
        other = jp.PlannedField(field={}, question="Do you have an MBA?", kind="radio",
                                options=["Yes, from a US school", "Yes, from another country"])
        self.assertFalse(jp._apply_banked_answer(other, candidate))
        self.assertFalse(other.resolved)

    def test_pitch_answers_cover_letter_boxes(self):
        candidate = dict(jp.CANDIDATE_DEFAULTS)
        candidate["pitch"] = "I build reliable Python services."
        self.assertEqual(jp.deterministic_value("cover_letter", candidate),
                         "I build reliable Python services.")
        self.assertEqual(jp.deterministic_value("cover_letter", {}), "")
        self.assertEqual(jp.match_known_field("why do you want this job"), "cover_letter")

    def test_batch_config_defaults(self):
        config = jp.Config()
        self.assertEqual(config.batch_size, 25)
        self.assertTrue(config.batch_confirm)            # one typed YES per batch by default
        self.assertGreaterEqual(config.max_applications_per_run, 30)
        self.assertEqual(config.max_wizard_steps, 8)

    def test_wizard_step_detection_never_matches_submit(self):
        self.assertIn("blocked = /submit", jp.FIND_NEXT_JS)
        self.assertIn("/job-applications/", jp.DICE_WIZARD_PATH)
        self.assertTrue(callable(jp.find_next_control))

    def test_the_second_job_in_a_batch_asks_you_nothing(self):
        """The core batch promise: answer a question once, never on jobs 2..N."""
        candidate = dict(jp.CANDIDATE_DEFAULTS)
        candidate.update({"first_name": "Jane", "last_name": "Doe",
                          "email": "jane.doe@example.com", "phone": "+1 555 0100",
                          "location": "Austin, TX", "years_experience": 6})
        jp.save_candidate(candidate)

        def wizard_controls():
            return [
                {"kind": "text", "label": "First Name", "name": "firstName",
                 "state_key": "0", "index": 0},
                {"kind": "text", "label": "How many years of experience do you have with Kubernetes?",
                 "state_key": "1", "index": 1},
                {"kind": "text", "label": "Why are you a good fit for this role?",
                 "state_key": "2", "index": 2},
                {"kind": "text", "label": "Portfolio website", "name": "portfolio",
                 "state_key": "3", "index": 3},
            ]

        replies = iter(["3", "I ship reliable Python services.", "skip"])
        asked = []
        original = jp.UI.ask
        jp.UI.ask = lambda prompt, default="", allow_empty=False: (asked.append(prompt),
                                                                   next(replies))[1]
        try:
            first, problems = jp.build_planned_fields(wizard_controls(), candidate, None)
            jp.resolve_planned_fields(first, candidate, job_fixture(), None)
        finally:
            jp.UI.ask = original
        self.assertEqual(len(asked), 3)          # job 1: three questions, name came from profile
        job1 = {item.question: item for item in first}
        self.assertEqual(job1["First Name"].source, "profile")
        self.assertEqual(job1["How many years of experience do you have with Kubernetes?"].value, "3")
        self.assertEqual(job1["Why are you a good fit for this role?"].value,
                         "I ship reliable Python services.")

        # Job 2 shows the identical wizard: nothing may be asked again.
        def refuse(prompt, default="", allow_empty=False):
            raise AssertionError(f"job 2 asked the human: {prompt}")

        jp.UI.ask = refuse
        try:
            second, problems = jp.build_planned_fields(wizard_controls(), candidate, None)
            jp.resolve_planned_fields(second, candidate, job_fixture(), None)
        finally:
            jp.UI.ask = original
        job2 = {item.question: item for item in second}
        self.assertEqual(job2["First Name"].value, "Jane")                     # from profile
        k8s = job2["How many years of experience do you have with Kubernetes?"]
        self.assertEqual(k8s.value, "3")                                       # from the bank
        self.assertEqual(k8s.source, "bank")
        self.assertNotEqual(k8s.value, "6")                    # NOT your total years of experience
        self.assertEqual(job2["Why are you a good fit for this role?"].value,
                         "I ship reliable Python services.")
        self.assertTrue(job2["Portfolio website"].declined)     # remembered skip, left empty
        self.assertFalse(job2["Portfolio website"].resolved)

    def test_exhausted_input_is_never_saved_as_a_decline(self):
        """A closed stdin looks like 'skip'; banking it would poison every later job."""
        candidate = dict(jp.CANDIDATE_DEFAULTS)
        jp.save_candidate(candidate)
        question = "Do you have an active security clearance?"
        item = jp.PlannedField(field={}, question=question, kind="text")
        saved = jp.UI.eof_seen
        jp.UI.eof_seen = True
        try:
            jp.ask_human_for_field(item, candidate)
        finally:
            jp.UI.eof_seen = saved
        self.assertFalse(item.resolved)
        self.assertEqual(item.value, "")
        self.assertIsNone(jp.bank_entry(candidate, question))   # nothing was recorded
        self.assertIsNone(jp.load_candidate().get("answer_bank", {}).get(
            jp.answer_bank_key(question)))                      # ... and nothing was saved

    def test_an_explicit_skip_is_saved_as_a_decline(self):
        candidate = dict(jp.CANDIDATE_DEFAULTS)
        jp.save_candidate(candidate)
        question = "Do you have an MBA?"
        item = jp.PlannedField(field={}, question=question, kind="text")
        original = jp.UI.ask
        jp.UI.ask = lambda prompt, default="", allow_empty=False: "skip"
        try:
            jp.ask_human_for_field(item, candidate)
        finally:
            jp.UI.ask = original
        self.assertTrue(jp.bank_entry(candidate, question)["declined"])
        self.assertTrue(jp.bank_entry(jp.load_candidate(), question)["declined"])

    def test_score_is_the_half_up_rounding_of_its_parts(self):
        result = jp.score_job(job_fixture(), candidate_fixture())
        exact = round(sum(float(v) for v in result["breakdown"].values()), 1)
        self.assertEqual(result["score"], int(exact + 0.5))    # 92.5 -> 93, not banker's 92
        self.assertLessEqual(abs(exact - result["score"]), 0.5)

    def test_a_rounding_difference_is_disclosed_not_hidden(self):
        self.assertIn("sum=92.5", jp.breakdown_line({"role": 27.5, "skills": 65.0}, 93))
        self.assertNotIn("sum=", jp.breakdown_line({"role": 30.0, "skills": 63.0}, 93))
        self.assertEqual(jp.breakdown_line({}, None), "-")
        self.assertEqual(jp.breakdown_line({"role": "bad"}, None), "role=bad")

    def test_security_challenge_flag_stops_a_batch(self):
        job = jp.normalize_job({"id": "j1", "title": "Dev",
                                "source_url": "https://www.dice.com/job-detail/j1"})
        app = jp.create_application(job)
        jp.challenge_stop(job, app["id"], "test")
        record = jp._latest_application_for("j1")
        self.assertIn("SECURITY_CHALLENGE", record["flags"])
        self.assertEqual(record["state"], jp.ST_REVIEW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
