#!/usr/bin/env python3
"""
JOBPILOT self-test. Run it after any change:

    python tests/test_jobpilot.py        (or: python -m pytest tests -q)

It covers scoring, resume parsing, JSON storage, AI schema validation, deterministic
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
class TestStorageAndSafety(unittest.TestCase):
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

    def test_ai_adjustment_is_clamped(self):
        base = jp.score_job(job_fixture(), candidate_fixture())
        raised = jp.apply_ai_adjustment(dict(base, breakdown=dict(base["breakdown"])), 999, 0.9, "x")
        self.assertEqual(raised["ai_adjustment"], 10)
        self.assertLessEqual(raised["score"], 100)
        lowered = jp.apply_ai_adjustment(dict(base, breakdown=dict(base["breakdown"])), -999, 0.9, "x")
        self.assertEqual(lowered["ai_adjustment"], -10)
        self.assertGreaterEqual(lowered["score"], 0)

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


class TestNoFakeData(unittest.TestCase):
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
        self.assertEqual(jp.classify_apply_label("Apply Now"), jp.AT_EXTERNAL)
        self.assertEqual(jp.classify_apply_label("Something else", "nothing here"), jp.AT_UNKNOWN)

    def test_unsupported_kinds_are_rejected(self):
        for kind in ("date", "number", "url", "password", "range", "color", "search"):
            self.assertIn(kind, jp.UNSUPPORTED_KINDS)
        self.assertEqual(jp.normalize_kind("tel"), "phone")
        self.assertEqual(jp.normalize_kind("select-one"), "select")


class TestFieldMappingAndQuestions(unittest.TestCase):
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

    def test_sensitive_question_is_not_answered_by_ai(self):
        """The AI must never be asked a sensitive question."""
        class FakeAI:
            available = True
            last_error = ""
            called = []

            def answer_question(self, question, options, candidate, job):
                FakeAI.called.append(question)
                return {"answer": "Yes", "confidence": 0.99, "reason": "guess"}

        planned = [
            jp.PlannedField(field={}, question="Are you authorized to work in the US?",
                            kind="radio", options=["Yes", "No"], sensitive="work_authorization"),
            jp.PlannedField(field={}, question="Why do you want this job?",
                            kind="textarea", options=[]),
        ]
        answers: list = []
        original = jp.ask_human_for_field
        jp.ask_human_for_field = lambda item: (item.__setattr__("value", "Yes"),
                                               item.__setattr__("resolved", True),
                                               answers.append(item.question))
        try:
            jp.resolve_planned_fields(planned, candidate_fixture(), job_fixture(), FakeAI(), None)
        finally:
            jp.ask_human_for_field = original
        self.assertEqual(FakeAI.called, ["Why do you want this job?"])
        self.assertIn("Are you authorized to work in the US?", answers)

    def test_unmapped_ai_option_is_rejected(self):
        client = jp.AIClient(jp.Config(llm_api_key="test-key", llm_enabled=True))
        self.assertTrue(client.available)
        client._post = lambda payload: {"answer": "Maybe later", "confidence": 0.99,
                                        "reason": "unsure"}
        result = client.answer_question("Are you willing to relocate?", ["Yes", "No"],
                                        candidate_fixture(), job_fixture())
        self.assertIsNone(result)
        self.assertTrue(client.last_error)

        # an off-list answer that matches exactly one option is normalised
        client._post = lambda payload: {"answer": "yes", "confidence": 0.99, "reason": "profile"}
        result = client.answer_question("Are you willing to relocate?", ["Yes", "No"],
                                       candidate_fixture(), job_fixture())
        self.assertEqual(result["answer"], "Yes")

    def test_low_confidence_and_unknown_answers_are_rejected(self):
        client = jp.AIClient(jp.Config(llm_api_key="test-key", llm_enabled=True,
                                       ai_min_confidence=0.75))
        client._post = lambda payload: {"answer": "Yes", "confidence": 0.10, "reason": "guessing"}
        self.assertIsNone(client.answer_question("Relocate?", ["Yes", "No"],
                                                candidate_fixture(), job_fixture()))
        client._post = lambda payload: {"answer": "UNKNOWN", "confidence": 0.99, "reason": "missing"}
        self.assertIsNone(client.answer_question("Relocate?", ["Yes", "No"],
                                                candidate_fixture(), job_fixture()))
        client._post = lambda payload: "not json"
        self.assertIsNone(client.answer_question("Relocate?", ["Yes", "No"],
                                                candidate_fixture(), job_fixture()))

    def test_ai_disabled_without_key(self):
        client = jp.AIClient(jp.Config())
        self.assertFalse(client.available)
        self.assertIsNone(client.answer_question("Q", [], candidate_fixture(), job_fixture()))
        self.assertIsNone(client.semantic_match(job_fixture(), candidate_fixture(),
                                                jp.score_job(job_fixture(), candidate_fixture())))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
