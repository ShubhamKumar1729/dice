#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JOBPILOT - AI-powered Dice job discovery and controlled application assistant.

Terminal-only MVP. No database, no web server, no frameworks.

  python jobpilot.py            normal mode (real data only, nothing simulated)
  python jobpilot.py --demo     opt-in demo mode (all demo data is clearly labelled)
  python jobpilot.py --check    environment / configuration check, then exit

Design rules enforced by this file:
  * Dice is the only supported platform (DiceProvider is the only real provider).
  * Nothing is ever submitted without an explicit "YES" typed by the user.
  * No CAPTCHA / MFA / bot-detection bypass, no stealth, no proxy rotation.
    A security challenge always stops the automation: "Human intervention required."
  * No fake jobs, no fake candidates, no fake answers, no fake submissions.
  * Every failure produces a readable message instead of a traceback.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Version / identity
# ---------------------------------------------------------------------------

APP_NAME = "JOBPILOT"
APP_TAGLINE = "AI-powered Dice job discovery and controlled application assistant"
VERSION = "1.0.0"
MIN_PYTHON = (3, 9)
SCHEMA_VERSION = 1

DICE_DOMAIN = "dice.com"
DICE_LOGIN_URL = "https://www.dice.com/dashboard/login"
DICE_SEARCH_URL = "https://www.dice.com/jobs"

# ---------------------------------------------------------------------------
# Optional dependencies (imported defensively so that nothing ever crashes)
# ---------------------------------------------------------------------------

try:
    import requests
except Exception:                                             # pragma: no cover
    requests = None                                            # type: ignore

try:
    from dotenv import load_dotenv
except Exception:                                             # pragma: no cover
    load_dotenv = None                                         # type: ignore

try:
    from pydantic import BaseModel, Field, ValidationError
    HAVE_PYDANTIC = True
except Exception:                                             # pragma: no cover
    HAVE_PYDANTIC = False
    BaseModel = object                                         # type: ignore
    Field = None                                               # type: ignore
    ValidationError = Exception                                # type: ignore

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    HAVE_RICH = True
except Exception:                                             # pragma: no cover
    HAVE_RICH = False

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    HAVE_PLAYWRIGHT = True
except Exception:                                             # pragma: no cover
    sync_playwright = None                                     # type: ignore
    PWTimeout = Exception                                      # type: ignore
    HAVE_PLAYWRIGHT = False

# ---------------------------------------------------------------------------
# Paths (always relative to this file -> works on Windows and Linux)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = BASE_DIR / "uploads"
ERRORS_DIR = DATA_DIR / "errors"
CANDIDATE_FILE = DATA_DIR / "candidate.json"
JOBS_FILE = DATA_DIR / "jobs.json"
APPLICATIONS_FILE = DATA_DIR / "applications.json"
LOGS_FILE = DATA_DIR / "logs.json"
BROWSER_STATE_FILE = DATA_DIR / "browser_state.json"
SETUP_MARKER = DATA_DIR / ".setup_complete"
ENV_FILE = BASE_DIR / ".env"
ENV_EXAMPLE = BASE_DIR / ".env.example"

MAX_LOG_ENTRIES = 5000

# ---------------------------------------------------------------------------
# Application states (exactly the states required by the spec)
# ---------------------------------------------------------------------------

ST_DISCOVERED = "DISCOVERED"
ST_SCORED = "SCORED"
ST_SELECTED = "SELECTED"
ST_IN_PROGRESS = "IN_PROGRESS"
ST_REVIEW = "REVIEW_REQUIRED"
ST_READY = "READY_TO_SUBMIT"
ST_SUBMITTED = "SUBMITTED"
ST_FAILED = "FAILED"
ST_CANCELLED = "CANCELLED"

ALL_STATES = [ST_DISCOVERED, ST_SCORED, ST_SELECTED, ST_IN_PROGRESS, ST_REVIEW,
              ST_READY, ST_SUBMITTED, ST_FAILED, ST_CANCELLED]

# Application type categories (spec section 4)
AT_EASY_APPLY = "DICE_EASY_APPLY"
AT_EXTERNAL = "EXTERNAL_APPLICATION"
AT_UNKNOWN = "UNKNOWN"

# ---------------------------------------------------------------------------
# Matching weights (spec section 11)
# ---------------------------------------------------------------------------

WEIGHTS = {
    "role": 30,
    "skills": 25,
    "experience": 20,
    "location": 10,
    "education": 5,
    "employment": 5,
    "other": 5,
}

# ---------------------------------------------------------------------------
# Safety word lists
# ---------------------------------------------------------------------------

# Anything matching these is a platform security control. We stop, we never work around it.
CHALLENGE_HINTS = (
    "recaptcha", "hcaptcha", "g-recaptcha", "captcha", "cf-challenge", "cloudflare",
    "verify you are human", "verify you're human", "are you a robot", "i'm not a robot",
    "two-factor", "two factor", "2-step verification", "two-step verification",
    "verification code", "one-time code", "one time code", "enter the code we sent",
    "security check", "unusual activity", "we've detected", "access denied",
    "please verify your identity", "bot detection", "automated access",
)

# Sensitive / legally consequential questions -> never decided by the AI.
SENSITIVE_PATTERNS = (
    ("work_authorization", r"authoriz(ed|ation)\s+to\s+work|work\s+authoriz|legally\s+authoriz|"
                          r"eligible\s+to\s+work|right\s+to\s+work|work\s+permit|employment\s+eligib"),
    ("sponsorship", r"sponsor(ship|ing)?\b|visa\s+sponsor|require\s+.*visa"),
    ("criminal_history", r"felony|misdemeanor|criminal\s+(record|history|conviction)|convicted|"
                         r"background\s+check|arrest"),
    ("disability", r"disabilit|disability\s+status"),
    ("veteran_status", r"veteran|military\s+service|protected\s+veteran"),
    ("demographic", r"\bgender\b|\brace\b|ethnicit|hispanic|latino|pronoun|date\s+of\s+birth|"
                    r"\bage\b|marital\s+status|sexual\s+orientation"),
    ("security_clearance", r"security\s+clearance|clearance\s+(level|status)|secret\s+clearance|"
                           r"top\s+secret|public\s+trust"),
    ("legal_certification", r"certif(y|ication)\s+that|certify\s+the\s+information|i\s+agree|"
                            r"\battest\b|information is (accurate|true|correct)|"
                            r"terms\s+and\s+conditions|privacy\s+policy|acknowledg"),
    ("salary_expectation", r"expected\s+(salary|compensation|pay)|desired\s+(salary|compensation|rate)|"
                           r"salary\s+expectation|hourly\s+rate"),
    ("availability", r"(start|available)\s+date|when\s+can\s+you\s+start|notice\s+period|"
                     r"willing\s+to\s+relocate|relocation"),
)

UK = "UNKNOWN"

# Skill vocabulary used only to *read* a resume / job text. Never used to invent skills.
SKILL_VOCAB = [
    "python", "java", "javascript", "typescript", "c++", "c#", "c", "go", "golang", "rust",
    "ruby", "php", "scala", "kotlin", "swift", "perl", "bash", "shell", "powershell", "sql",
    "html", "css", "sass", "react", "react native", "angular", "vue", "svelte", "next.js",
    "node.js", "express", "django", "flask", "fastapi", "spring", "spring boot", ".net",
    "asp.net", "laravel", "rails", "graphql", "rest", "rest api", "grpc", "microservices",
    "sql server", "mysql", "postgresql", "postgres", "sqlite", "oracle", "mongodb", "cassandra",
    "dynamodb", "redis", "elasticsearch", "kafka", "rabbitmq", "sns", "sqs", "airflow",
    "spark", "hadoop", "hive", "databricks", "snowflake", "redshift", "bigquery", "etl",
    "elt", "dbt", "pandas", "numpy", "scikit-learn", "tensorflow", "pytorch", "keras",
    "nlp", "llm", "machine learning", "deep learning", "data science", "computer vision",
    "aws", "azure", "gcp", "google cloud", "ec2", "s3", "lambda", "eks", "docker",
    "kubernetes", "terraform", "ansible", "jenkins", "github actions", "gitlab ci", "circleci",
    "ci/cd", "git", "github", "gitlab", "bitbucket", "linux", "unix", "windows server",
    "jira", "confluence", "agile", "scrum", "kanban", "tdd", "unit testing", "pytest",
    "junit", "selenium", "playwright", "cypress", "postman", "swagger", "openapi",
    "pyspark", "tableau", "power bi", "looker", "excel", "vba", "sas", "matlab", "r",
    "hl7", "fhir", "epic", "salesforce", "sap", "servicenow", "workday", "adobe experience",
    "figma", "ui/ux", "product management", "project management", "pmp", "scrum master",
    "okta", "saml", "oauth", "jwt", "cybersecurity", "siem", "splunk", "penetration testing",
]

SEARCH_PARAM_MAP = {
    # Query parameters used when searching the public Dice job search pages.
    # Kept in one place so they are easy to adjust if Dice changes them.
    "posted_date": {"1": "ONE", "3": "THREE", "7": "SEVEN", "30": "THIRTY"},
    "workplace": {"remote": "Remote", "hybrid": "Hybrid", "onsite": "Onsite"},
}

# ===========================================================================
# SECTION 1 - small utilities
# ===========================================================================


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slug(value: str, limit: int = 40) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return clean[:limit] or "item"


def short_hash(value: str, length: int = 12) -> str:
    return hashlib.sha1((value or "").encode("utf-8", "replace")).hexdigest()[:length]


def ensure_dirs() -> None:
    for path in (DATA_DIR, UPLOADS_DIR, ERRORS_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def read_json(path: Path, default: Any) -> Any:
    """Read JSON defensively. A corrupt file is backed up, never fatal."""
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        backup = path.with_name(path.name + f".corrupt-{int(time.time())}")
        try:
            shutil.copy2(str(path), str(backup))
        except OSError:
            backup = None
        UI.warn(f"{path.name} is not valid JSON ({exc}).")
        if backup:
            UI.warn(f"A copy of the broken file was saved as {backup.name}.")
        return default
    except OSError as exc:
        UI.warn(f"Could not read {path.name}: {exc}")
        return default


def write_json(path: Path, data: Any) -> bool:
    """Atomic-ish JSON write that works on Windows and Linux."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        os.replace(str(tmp), str(path))
        return True
    except OSError as exc:
        UI.error(f"Could not save {path.name}: {exc}")
        return False


def clean_text(value: Any, limit: int = 20000) -> str:
    text = re.sub(r"[ \t]+", " ", str(value or ""))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit]


def contains_word(haystack: str, needle: str) -> bool:
    """Case-insensitive term match that does not match inside longer words."""
    if not needle:
        return False
    pattern = r"(?<![a-z0-9+#.])" + re.escape(needle.lower()) + r"(?![a-z0-9+#])"
    return re.search(pattern, (haystack or "").lower()) is not None


def first_line(text: str, limit: int = 300) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:limit]

# ===========================================================================
# SECTION 2 - logging (data/logs.json, never contains secrets)
# ===========================================================================

_LOG_CACHE: Optional[List[Dict[str, Any]]] = None
DEBUG = bool(os.environ.get("JOBPILOT_DEBUG"))


def _logs() -> List[Dict[str, Any]]:
    global _LOG_CACHE
    if _LOG_CACHE is None:
        data = read_json(LOGS_FILE, {"version": SCHEMA_VERSION, "entries": []})
        entries = data.get("entries") if isinstance(data, dict) else None
        _LOG_CACHE = entries if isinstance(entries, list) else []
    return _LOG_CACHE


def log_event(action: str, status: str = "ok", job_id: Optional[str] = None,
              application_id: Optional[str] = None, error: Optional[str] = None,
              **extra: Any) -> None:
    """Append one log entry. Secrets are never passed in here."""
    try:
        entry = {
            "timestamp": now_iso(),
            "action": action,
            "job_id": job_id,
            "application_id": application_id,
            "status": status,
            "error": (str(error)[:600] if error else None),
        }
        for key, value in extra.items():
            if value is None:
                continue
            entry[key] = value if isinstance(value, (int, float, bool)) else str(value)[:400]
        entries = _logs()
        entries.append(entry)
        del entries[:-MAX_LOG_ENTRIES]
        write_json(LOGS_FILE, {"version": SCHEMA_VERSION, "entries": entries})
        if DEBUG and status not in ("ok", "info"):
            print(f"    [debug] log: {action} -> {status} {error or ''}")
    except Exception:
        pass


def flush_logs() -> None:
    try:
        write_json(LOGS_FILE, {"version": SCHEMA_VERSION, "entries": _logs()})
    except Exception:
        pass

# ===========================================================================
# SECTION 3 - terminal UI (rich when available, plain text otherwise)
# ===========================================================================


class UI:
    """All terminal output goes through here so it can never crash the app."""

    console = None
    eof_seen = False

    @classmethod
    def init(cls) -> None:
        if not HAVE_RICH:
            return
        try:
            cls.console = Console()
        except Exception:
            cls.console = None

    # ---- output -----------------------------------------------------------
    @classmethod
    def print(cls, message: str = "", style: Optional[str] = None) -> None:
        try:
            if cls.console is not None:
                cls.console.print(message, style=style, highlight=False, markup=False)
            else:
                plain = re.sub(r"\[/?[a-z ]+\]", "", str(message))
                print(plain)
        except Exception:
            try:
                print(re.sub(r"\[/?[a-z ]+\]", "", str(message)))
            except Exception:
                pass

    @classmethod
    def rule(cls, title: str = "") -> None:
        line = f"--- {title} " if title else ""
        cls.print(line + "-" * max(4, 60 - len(line)))

    @classmethod
    def banner(cls, title: str, subtitle: str = "") -> None:
        if cls.console is not None:
            try:
                cls.console.print(Panel.fit(f"{subtitle}" if subtitle else "",
                                            title=title, border_style="cyan"))
                return
            except Exception:
                pass
        cls.print("=" * 62)
        cls.print(title.center(62))
        if subtitle:
            for chunk in re.findall(r".{1,58}(?:\s|$)", subtitle):
                cls.print(chunk.strip().center(62))
        cls.print("=" * 62)

    @classmethod
    def ok(cls, message: str) -> None:
        cls.print(f"[OK] {message}", style="green")

    @classmethod
    def info(cls, message: str) -> None:
        cls.print(f"[i] {message}", style="cyan")

    @classmethod
    def warn(cls, message: str) -> None:
        cls.print(f"[!] {message}", style="yellow")

    @classmethod
    def error(cls, message: str) -> None:
        cls.print(f"[x] {message}", style="bold red")

    @classmethod
    def panel(cls, title: str, lines: List[str]) -> None:
        body = "\n".join(lines)
        if cls.console is not None:
            try:
                cls.console.print(Panel(body, title=title, border_style="blue", expand=False))
                return
            except Exception:
                pass
        cls.print("-" * 62)
        cls.print(title.center(62))
        cls.print("-" * 62)
        cls.print(body)
        cls.print("-" * 62)

    @classmethod
    def table(cls, columns: List[str], rows: List[List[str]], title: str = "") -> None:
        rows = [[("" if cell is None else str(cell)) for cell in row] for row in rows]
        if not rows:
            cls.warn("No rows to display.")
            return
        if cls.console is not None:
            try:
                table = Table(title=title or None, header_style="bold cyan", box=None)
                for column in columns:
                    table.add_column(column, overflow="fold")
                for row in rows:
                    table.add_row(*row)
                cls.console.print(table)
                return
            except Exception:
                pass
        widths = [len(str(c)) for c in columns]
        for row in rows:
            for index, cell in enumerate(row):
                if index < len(widths):
                    widths[index] = max(widths[index], min(len(cell), 44))
        if title:
            cls.print(title)
        cls.print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(columns)))
        cls.print("  ".join("-" * widths[i] for i in range(len(columns))))
        for row in rows:
            cells = []
            for index, cell in enumerate(row):
                value = cell if len(cell) <= 44 else cell[:41] + "..."
                cells.append(value.ljust(widths[index]) if index < len(widths) else value)
            cls.print("  ".join(cells))

    @classmethod
    def clear(cls) -> None:
        if cls.console is not None and cls.console.is_terminal:
            try:
                cls.console.clear()
                return
            except Exception:
                pass

    @classmethod
    @contextmanager
    def status(cls, message: str):
        if cls.console is not None and getattr(cls.console, "is_terminal", False):
            try:
                with cls.console.status(message):
                    yield
                return
            except Exception:
                pass
        cls.info(message)
        yield

    # ---- input ------------------------------------------------------------
    @classmethod
    def ask(cls, prompt: str, default: str = "", allow_empty: bool = False) -> str:
        """Free text. EOF (piped input finished) returns the default, never crashes."""
        label = f"{prompt} [{default}]: " if default else f"{prompt}: "
        if cls.console is not None:
            try:
                from rich.prompt import Prompt
                # Always pass a real default so pressing Enter is never an "invalid response":
                # an empty answer is validated by the caller, not by the prompt library.
                shown = f"{prompt} [{default}]" if default else prompt
                value = Prompt.ask(shown, default=default or "", show_default=False)
                value = (value or "").strip()
            except EOFError:
                cls.eof_seen = True
                return default
            except KeyboardInterrupt:
                raise
            except Exception:
                value = cls._input(label)
        else:
            value = cls._input(label)
        if not value and not allow_empty and not default:
            return ""
        return value

    @classmethod
    def _input(cls, label: str) -> str:
        try:
            return (input(label) or "").strip()
        except EOFError:
            cls.eof_seen = True
            return ""
        except KeyboardInterrupt:
            raise

    @classmethod
    def ask_yes_no(cls, prompt: str, default: Optional[bool] = None) -> Optional[bool]:
        hint = "y/n" if default is None else ("Y/n" if default else "y/N")
        while True:
            raw = cls.ask(f"{prompt} ({hint})", allow_empty=True).strip().lower()
            if not raw:
                if default is None:
                    cls.warn("Please answer y or n.")
                    if cls.eof_seen:
                        return default
                    continue
                return default
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            cls.warn("Please answer y or n.")

    @classmethod
    def ask_int(cls, prompt: str, default: Optional[int] = None,
                minimum: Optional[int] = None, maximum: Optional[int] = None) -> Optional[int]:
        while True:
            raw = cls.ask(prompt, str(default) if default is not None else "", allow_empty=True)
            if not raw and default is not None:
                return default
            if not raw and cls.eof_seen:
                return default
            try:
                value = int(float(raw))
            except ValueError:
                cls.warn("Please enter a whole number.")
                continue
            if minimum is not None and value < minimum:
                cls.warn(f"Value must be at least {minimum}.")
                continue
            if maximum is not None and value > maximum:
                cls.warn(f"Value must be at most {maximum}.")
                continue
            return value

    @classmethod
    def ask_list(cls, prompt: str, current: Optional[List[str]] = None) -> List[str]:
        shown = ", ".join(current or [])
        raw = cls.ask(prompt, shown, allow_empty=True)
        if not raw:
            return list(current or [])
        return [item.strip() for item in re.split(r"\s*[,;|]\s*", raw) if item.strip()]

    @classmethod
    def choose(cls, prompt: str, options: List[str], allow_cancel: bool = True) -> Optional[int]:
        for index, option in enumerate(options, start=1):
            cls.print(f"  {index}. {option}")
        if allow_cancel:
            cls.print("  0. Back")
        while True:
            raw = cls.ask(prompt, "", allow_empty=True)
            if raw == "" and cls.eof_seen:
                return None
            if not raw:
                continue
            try:
                value = int(raw)
            except ValueError:
                cls.warn("Enter the number of your choice.")
                continue
            if allow_cancel and value == 0:
                return None
            if 1 <= value <= len(options):
                return value - 1
            cls.warn(f"Enter a number between 1 and {len(options)}.")

    @classmethod
    def confirm_exact(cls, prompt: str, expected: str = "YES") -> bool:
        raw = cls.ask(prompt, "", allow_empty=True)
        return raw.strip().upper() == expected.upper()

# ===========================================================================
# SECTION 4 - configuration (.env, never hard-coded secrets)
# ===========================================================================


@dataclass
class Config:
    dice_api_key: str = ""
    dice_api_base: str = "https://api.dice.com"
    dice_search_path: str = "/services/partners/jobsearch/v1/simple.json"
    dice_api_auth: str = "header"           # header | query
    dice_client_id: str = ""
    dice_allow_browser_search: bool = False
    dice_max_results: int = 25
    dice_enrich_limit: int = 10
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_enabled: bool = True
    ai_min_confidence: float = 0.75
    headless: bool = False
    request_timeout: int = 30
    nav_timeout: int = 45
    pol_request_delay: float = 1.5
    max_applications_per_run: int = 1
    demo: bool = False

    @property
    def dice_api_ready(self) -> bool:
        return bool(self.dice_api_key)

    @property
    def dice_browser_ready(self) -> bool:
        return self.dice_allow_browser_search and HAVE_PLAYWRIGHT

    @property
    def ai_ready(self) -> bool:
        return bool(self.llm_enabled and self.llm_api_key and requests is not None)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw.strip()))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def load_config(demo: bool = False) -> Config:
    if load_dotenv is not None:
        try:
            load_dotenv(str(ENV_FILE))
        except Exception:
            pass
    config = Config(
        dice_api_key=os.environ.get("DICE_API_KEY", "").strip(),
        dice_api_base=os.environ.get("DICE_API_BASE", "https://api.dice.com").strip().rstrip("/"),
        dice_search_path=os.environ.get("DICE_SEARCH_PATH", "/services/partners/jobsearch/v1/simple.json").strip(),
        dice_api_auth=os.environ.get("DICE_API_AUTH", "header").strip().lower(),
        dice_client_id=os.environ.get("DICE_CLIENT_ID", "").strip(),
        dice_allow_browser_search=_env_bool("DICE_ALLOW_BROWSER_SEARCH", False),
        dice_max_results=_env_int("DICE_MAX_RESULTS", 25),
        dice_enrich_limit=_env_int("DICE_ENRICH_LIMIT", 10),
        llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"),
        llm_model=os.environ.get("LLM_MODEL", "gpt-4o-mini").strip(),
        llm_enabled=_env_bool("LLM_ENABLED", True),
        ai_min_confidence=_env_float("AI_MIN_CONFIDENCE", 0.75),
        headless=_env_bool("BROWSER_HEADLESS", False),
        request_timeout=_env_int("REQUEST_TIMEOUT", 30),
        nav_timeout=_env_int("NAV_TIMEOUT", 45),
        pol_request_delay=_env_float("POLITE_DELAY_SECONDS", 1.5),
        max_applications_per_run=_env_int("MAX_APPLICATIONS_PER_RUN", 1),
        demo=demo,
    )
    if config.dice_search_path and not config.dice_search_path.startswith("/"):
        config.dice_search_path = "/" + config.dice_search_path
    return config


def update_env_file(updates: Dict[str, str]) -> bool:
    """Write/refresh keys in .env, keeping every other line untouched."""
    lines: List[str] = []
    if ENV_FILE.exists():
        try:
            lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            UI.error(f"Could not read .env: {exc}")
            return False
    remaining = dict(updates)
    out: List[str] = []
    for line in lines:
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    try:
        ENV_FILE.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
        return True
    except OSError as exc:
        UI.error(f"Could not write .env: {exc}")
        return False

# ===========================================================================
# SECTION 5 - environment / first-run checks
# ===========================================================================


def _pkg_status() -> List[Tuple[str, bool, str]]:
    result: List[Tuple[str, bool, str]] = []
    for module, name in (("rich", "rich"), ("requests", "requests"), ("dotenv", "python-dotenv"),
                         ("pydantic", "pydantic"), ("pypdf", "pypdf"), ("docx", "python-docx")):
        try:
            __import__(module)
            result.append((name, True, ""))
        except Exception:
            alt = None
            if module == "pypdf":
                try:
                    __import__("PyPDF2")
                    alt = "PyPDF2"
                except Exception:
                    alt = None
            result.append((name, bool(alt), f"pip install {name}" if not alt else ""))
    return result


def _pypdf_module():
    try:
        import pypdf
        return pypdf
    except Exception:
        try:
            import PyPDF2
            return PyPDF2
        except Exception:
            return None


def chromium_installed() -> Tuple[Optional[bool], str]:
    if not HAVE_PLAYWRIGHT:
        return None, "playwright package is not installed"
    try:
        with sync_playwright() as play:
            path = Path(play.chromium.executable_path)
        return path.exists(), str(path)
    except Exception as exc:
        return False, str(exc)[:200]


def environment_report(config: Config, deep: bool = False) -> bool:
    """Print exactly what is installed / missing and how to fix it. Never raises."""
    ok = True
    UI.banner(f"{APP_NAME} environment check", f"version {VERSION}")

    python_ok = sys.version_info >= MIN_PYTHON
    UI.table(["Check", "Result", "Notes"], [
        ["Python version", "OK" if python_ok else "MISSING",
         f"{sys.version.split()[0]} (needs >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})"],
        ["Project folder", "OK", str(BASE_DIR)],
    ])
    if not python_ok:
        ok = False
        UI.error(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required.")

    UI.rule("Python packages")
    pkg_rows = []
    for name, present, hint in _pkg_status():
        if not present:
            ok = False
        pkg_rows.append([name, "OK" if present else "MISSING", hint or "-"])
    UI.table(["Package", "Result", "Fix"], pkg_rows)

    UI.rule("Playwright browser")
    if not HAVE_PLAYWRIGHT:
        ok = False
        UI.warn("Playwright is not installed. Application automation and browser search are disabled.")
        UI.print("    Fix:  pip install playwright   then   playwright install chromium")
    else:
        installed, detail = chromium_installed()
        if installed:
            UI.ok("Chromium browser is installed.")
        else:
            ok = False
            UI.warn("Chromium is not installed (or could not be located).")
            UI.print("    Fix:  playwright install chromium")
        if deep and detail:
            UI.print(f"    ({detail})")

    if deep and HAVE_PLAYWRIGHT:
        UI.rule("Browser launch test")
        UI.info("Launching headless Chromium once to confirm it really works...")
        try:
            with sync_playwright() as play:
                browser = play.chromium.launch(headless=True)
                browser.close()
            UI.ok("Chromium launched successfully.")
        except Exception as exc:
            ok = False
            UI.error(f"Chromium failed to launch: {first_line(str(exc), 200)}")
            UI.print("    Fix:  playwright install chromium   (and make sure no firewall blocks it)")

    UI.rule("Configuration (.env)")
    if not ENV_FILE.exists():
        ok = False
        UI.warn(".env not found.")
        UI.print("    Windows:      copy .env.example .env")
        UI.print("    macOS/Linux:  cp .env.example .env")
        UI.print("    Then open .env and fill in the values you need.")
    else:
        UI.ok(".env found (values are never printed).")

    config_rows = [
        ["DICE_API_KEY", "set" if config.dice_api_ready else "not set",
         "official Dice partner API search" if config.dice_api_ready else "required for Dice API search"],
        ["DICE_ALLOW_BROWSER_SEARCH", "on" if config.dice_allow_browser_search else "off",
         "search the public Dice site in a real browser" if config.dice_allow_browser_search else "-"],
        ["LLM_API_KEY", "set" if config.llm_api_key else "not set",
         "semantic matching + question understanding" if config.llm_api_key else "optional (AI features stay off)"],
        ["LLM_MODEL", config.llm_model, "AI model name"],
        ["BROWSER_HEADLESS", "on" if config.headless else "off", "apply flow is easier to follow with this off"],
    ]
    UI.table(["Config", "Value", "Meaning"], config_rows)
    if not config.dice_api_ready and not config.dice_allow_browser_search:
        ok = False
        UI.warn("No Dice integration is configured, so job search cannot work.")
        UI.print("    Set DICE_API_KEY (official Dice partner API), or set")
        UI.print("    DICE_ALLOW_BROWSER_SEARCH=true to search the public Dice site in a browser.")
    if not config.llm_api_key:
        UI.info("No LLM_API_KEY: AI matching and AI question support are disabled. "
                "Everything else still works; unresolved questions go to you.")

    UI.rule("Folders")
    ensure_dirs()
    folder_rows = []
    for folder in (DATA_DIR, UPLOADS_DIR, ERRORS_DIR):
        folder_rows.append([folder.name + "/", "OK" if folder.exists() else "MISSING", str(folder)])
    UI.table(["Folder", "Result", "Path"], folder_rows)

    UI.rule()
    if ok:
        UI.ok("Environment looks usable.")
    else:
        UI.warn("Some items above need attention before the full workflow works.")
        UI.print("    The program can still start; every feature explains what it needs.")
    return ok

# ===========================================================================
# SECTION 6 - candidate profile (data/candidate.json)
# ===========================================================================

CANDIDATE_DEFAULTS: Dict[str, Any] = {
    "first_name": "", "last_name": "", "email": "", "phone": "", "location": "",
    "target_roles": [], "years_experience": None, "skills": [], "education": "",
    "certifications": [], "work_experience": [], "projects": [],
    "linkedin": "", "github": "", "portfolio": "",
    "work_authorization": "", "sponsorship_required": "", "work_modes": [],
    "employment_type": "FULLTIME",
    "resume_file": "",
    # Optional self-identification data. Only used if the user explicitly provides it.
    "veteran_status": "", "disability_status": "", "gender": "", "race": "",
    "updated_at": "",
}


def load_candidate() -> Dict[str, Any]:
    data = read_json(CANDIDATE_FILE, {})
    if not isinstance(data, dict):
        UI.warn("candidate.json is not an object; starting from an empty profile.")
        data = {}
    candidate = dict(CANDIDATE_DEFAULTS)
    candidate.update({k: v for k, v in data.items() if k in CANDIDATE_DEFAULTS or k.startswith("_")})
    for key in ("target_roles", "skills", "certifications", "work_experience", "projects", "work_modes"):
        if not isinstance(candidate.get(key), list):
            candidate[key] = []
    return candidate


def candidate_has_profile(candidate: Dict[str, Any]) -> bool:
    return bool(candidate.get("first_name") or candidate.get("last_name") or candidate.get("email"))


def save_candidate(candidate: Dict[str, Any]) -> bool:
    candidate["updated_at"] = now_iso()
    saved = write_json(CANDIDATE_FILE, candidate)
    log_event("save_candidate", "ok" if saved else "error", error=None if saved else "write failed")
    return saved


def candidate_facts(candidate: Dict[str, Any], limit: int = 60) -> str:
    """Plain-text fact sheet handed to the AI / shown in reviews. Only real data."""
    lines = []
    name = f"{candidate.get('first_name','')} {candidate.get('last_name','')}".strip()
    pairs = [("Name", name), ("Email", candidate.get("email")), ("Phone", candidate.get("phone")),
             ("Location", candidate.get("location")), ("LinkedIn", candidate.get("linkedin")),
             ("GitHub", candidate.get("github")), ("Portfolio", candidate.get("portfolio")),
             ("Years of experience", candidate.get("years_experience")),
             ("Target roles", ", ".join(candidate.get("target_roles") or [])),
             ("Skills", ", ".join((candidate.get("skills") or [])[:limit])),
             ("Education", candidate.get("education")),
             ("Certifications", ", ".join(candidate.get("certifications") or [])),
             ("Work authorization", candidate.get("work_authorization")),
             ("Sponsorship required", candidate.get("sponsorship_required")),
             ("Preferred work modes", ", ".join(candidate.get("work_modes") or [])),
             ("Veteran status", candidate.get("veteran_status")),
             ("Disability status", candidate.get("disability_status"))]
    for label, value in pairs:
        if value not in (None, "", []):
            lines.append(f"{label}: {value}")
    for item in (candidate.get("work_experience") or [])[:5]:
        if isinstance(item, dict):
            lines.append(f"Experience: {item.get('title','')} at {item.get('company','')} "
                         f"({item.get('start','')} - {item.get('end','')}) {clean_text(item.get('summary',''), 200)}")
    for item in (candidate.get("projects") or [])[:4]:
        if isinstance(item, dict):
            lines.append(f"Project: {item.get('name','')} - {clean_text(item.get('description',''), 200)}")
    return "\n".join(lines) if lines else "(profile is empty)"

# ===========================================================================
# SECTION 7 - resume parsing (PDF / DOCX)
# ===========================================================================

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:(?<!\d)(?:\+?\d{1,3}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d))")
URL_RE = re.compile(r"https?://[^\s<>\"')]+")
CITY_STATE_RE = re.compile(r"\b([A-Z][a-zA-Z.\-]+(?:\s[A-Z][a-zA-Z.\-]+)?),\s*([A-Z]{2}|[A-Z][a-z]+)\b")
YEAR_RANGE_RE = re.compile(r"(19|20)\d{2}\s*(?:-|–|to)\s*((19|20)\d{2}|present|current|now)", re.I)
DEGREE_RE = re.compile(r"\b(bachelor|master|mba|ph\.?d|doctorate|b\.?s\.?|m\.?s\.?|b\.?a\.?|m\.?a\.?|"
                       r"associate|b\.?tech|m\.?tech|bca|mca|diploma)\b", re.I)

SECTION_NAMES = {
    "summary": r"^(professional\s+)?(summary|profile|objective|about)\b",
    "skills": r"^(technical\s+)?(skills|core\s+competencies|technologies|technical\s+proficiencies)\b",
    "experience": r"^(work\s+|professional\s+|employment\s+)?(experience|history|employment)\b",
    "education": r"^(education|academics|academic\s+background|qualifications)\b",
    "projects": r"^(projects|personal\s+projects|selected\s+projects)\b",
    "certifications": r"^(certifications?|licenses?|courses?|training)\b",
    "awards": r"^(awards|honors|achievements)\b",
}


def extract_resume_text(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return (text, error). Never raises."""
    if not path.exists():
        return None, f"File not found: {path}"
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            module = _pypdf_module()
            if module is None:
                return None, "No PDF library installed. Fix: pip install pypdf"
            reader = module.PdfReader(str(path))
            if getattr(reader, "is_encrypted", False):
                try:
                    reader.decrypt("")
                except Exception:
                    return None, "This PDF is password protected; remove the password first."
            pages = []
            for page in reader.pages[:30]:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:
                    pages.append("")
            text = "\n".join(pages)
            if len(text.strip()) < 40:
                return None, ("No readable text found in the PDF. If it is a scanned image, OCR is "
                              "not supported - export a text-based PDF or upload the DOCX version.")
            return text, None
        if suffix in (".docx",):
            try:
                import docx
            except Exception:
                return None, "python-docx is not installed. Fix: pip install python-docx"
            document = docx.Document(str(path))
            parts = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.append(" | ".join(cell.text for cell in row.cells))
            text = "\n".join(parts)
            if len(text.strip()) < 20:
                return None, "The DOCX file appears to be empty."
            return text, None
        if suffix in (".doc",):
            return None, ("Legacy .doc files cannot be read. Save the resume as .docx or .pdf and retry.")
        if suffix in (".txt", ".md"):
            return path.read_text(encoding="utf-8", errors="replace"), None
        return None, f"Unsupported resume format '{suffix}'. Supported: .pdf, .docx (and .txt/.md)."
    except Exception as exc:
        return None, f"Could not read resume: {first_line(str(exc), 200)}"


def _lines(text: str) -> List[str]:
    return [re.sub(r"\s+", " ", line).strip() for line in (text or "").splitlines()]


def _split_sections(text: str) -> Dict[str, List[str]]:
    sections: Dict[str, List[str]] = {"_head": []}
    current = "_head"
    for raw in _lines(text):
        lowered = raw.lower().rstrip(":").strip()
        matched = None
        if 0 < len(lowered) <= 40:
            for name, pattern in SECTION_NAMES.items():
                if re.match(pattern, lowered, re.I):
                    matched = name
                    break
        if matched:
            current = matched
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(raw)
    return sections


def _bullets(lines: List[str], limit: int = 12) -> List[str]:
    out = []
    for line in lines:
        clean = re.sub(r"^[\-\u2022\*\u25cf\u25aa\u2023\u2043o]\s*", "", line).strip()
        if 4 <= len(clean) <= 220:
            out.append(clean)
        if len(out) >= limit:
            break
    return out


def parse_resume(text: str) -> Dict[str, Any]:
    """Extract profile fields. Every value carries a confidence flag so nothing is
    silently treated as true (spec section 8)."""
    sections = _split_sections(text)
    head_text = "\n".join(sections.get("_head", [])[:12])
    flat = re.sub(r"\s+", " ", text)
    out: Dict[str, Any] = {
        "first_name": "", "last_name": "", "email": "", "phone": "", "location": "",
        "skills": [], "education": "", "certifications": [], "work_experience": [],
        "projects": [], "linkedin": "", "github": "", "portfolio": "",
        "years_experience": None, "confidence": {},
    }

    # email / phone
    email = EMAIL_RE.search(flat)
    out["email"] = email.group(0) if email else ""
    out["confidence"]["email"] = "high" if email else "missing"
    phone = PHONE_RE.search(flat)
    out["phone"] = phone.group(0).strip() if phone else ""
    out["confidence"]["phone"] = "medium" if phone else "missing"

    # links
    for url in URL_RE.findall(flat):
        low = url.lower().rstrip(".,;")
        if "linkedin.com" in low and not out["linkedin"]:
            out["linkedin"] = low
        elif "github.com" in low and not out["github"]:
            out["github"] = low
        elif not out["portfolio"] and not any(x in low for x in ("linkedin.com", "github.com")):
            out["portfolio"] = low
    for key in ("linkedin", "github", "portfolio"):
        out["confidence"][key] = "high" if out[key] else "missing"

    # name -> first line that looks like a human name
    name_tokens = []
    for line in (sections.get("_head") or [])[:8]:
        candidate_line = re.sub(r"[^A-Za-z' .\-]", "", line).strip()
        words = candidate_line.split()
        if (2 <= len(words) <= 4 and len(candidate_line) <= 40
                and not EMAIL_RE.search(line) and not any(ch.isdigit() for ch in line)
                and candidate_line.lower() not in ("resume", "curriculum vitae", "cv")):
            name_tokens = words
            break
    if name_tokens:
        full = " ".join(name_tokens)
        if full.isupper():                     # resume headers are often ALL CAPS
            full = " ".join(word.capitalize() for word in name_tokens)
        else:
            full = " ".join(word[:1].upper() + word[1:] for word in name_tokens.split())
        parts = full.split()
        out["first_name"] = parts[0]
        out["last_name"] = " ".join(parts[1:])
        out["confidence"]["name"] = "medium"
        out["confidence"]["first_name"] = "medium"
        out["confidence"]["last_name"] = "medium"
    else:
        out["confidence"]["name"] = "missing"
        out["confidence"]["first_name"] = "missing"
        out["confidence"]["last_name"] = "missing"

    # location -> "City, ST"
    location = ""
    for line in (sections.get("_head") or [])[:12]:
        match = CITY_STATE_RE.search(line)
        if match and not any(ch.isdigit() for ch in match.group(0)):
            location = match.group(0)
            break
    if not location:
        match = CITY_STATE_RE.search(flat[:2000])
        if match:
            location = match.group(0)
    out["location"] = location
    out["confidence"]["location"] = "medium" if location else "missing"

    # skills -> explicit section plus vocabulary found anywhere in the resume
    found: List[str] = []
    skills_lines = sections.get("skills") or []
    for line in skills_lines[:14]:
        for token in re.split(r"[,;|\u2022\u25cf/\u2013]|\s{2,}", line):
            token = token.strip(" .-")
            if 2 <= len(token) <= 30 and not token.lower().endswith(":"):
                found.append(token)
    lowered = URL_RE.sub(" ", flat).lower()      # profile links are not skills
    for skill in SKILL_VOCAB:
        if contains_word(lowered, skill):
            # prefer the spelling used in the vocabulary list, but keep resume casing if present
            found.append(skill)
    seen = set()
    skills = []
    for skill in found:
        key = skill.lower()
        if key not in seen:
            seen.add(key)
            skills.append(skill)
    out["skills"] = skills[:40]
    out["confidence"]["skills"] = "medium" if skills else "missing"

    # education
    edu_lines = _bullets(sections.get("education") or [], 6)
    if not edu_lines:
        edu_lines = [re.sub(r"\s+", " ", line) for line in _lines(text)
                     if DEGREE_RE.search(line) and len(line) < 140][:4]
    out["education"] = " | ".join(edu_lines) if edu_lines else ""
    out["confidence"]["education"] = "medium" if edu_lines else "missing"

    # certifications
    cert_lines = _bullets(sections.get("certifications") or [], 10)
    out["certifications"] = cert_lines
    out["confidence"]["certifications"] = "medium" if cert_lines else "missing"

    # projects
    for line in _bullets(sections.get("projects") or [], 8):
        if ":" in line:
            name, _, rest = line.partition(":")
            out["projects"].append({"name": name.strip()[:80], "description": rest.strip()[:300], "link": ""})
        else:
            out["projects"].append({"name": line[:80], "description": "", "link": ""})
    out["confidence"]["projects"] = "medium" if out["projects"] else "missing"

    # experience entries: "title/company" lines, with date ranges attached to the entry above
    exp_lines = sections.get("experience") or []
    entry: Optional[Dict[str, str]] = None
    for line in exp_lines[:60]:
        if not line:
            continue
        match = YEAR_RANGE_RE.search(line)
        if match:
            if entry is None or entry["start"]:
                if entry:
                    out["work_experience"].append(entry)
                entry = {"title": line[:120], "company": "", "start": "", "end": "", "summary": ""}
                match = YEAR_RANGE_RE.search(line)
            years = re.findall(r"(?:19|20)\d{2}", match.group(0))
            entry["start"] = years[0] if years else ""
            entry["end"] = years[1] if len(years) > 1 else "Present"
            remainder = (line[:match.start()] + " " + line[match.end():]).strip(" ,-|")
            if remainder and not entry["company"]:
                entry["company"] = remainder[:90]
            continue
        if entry is None:
            entry = {"title": line[:120], "company": "", "start": "", "end": "", "summary": ""}
            continue
        looks_like_sentence = (line.rstrip().endswith(".")
                               or re.match(r"^(built|developed|led|created|designed|managed|"
                                           r"implemented|worked|responsible|wrote|helped|delivered|"
                                           r"maintained|improved|owned|used|using)\b", line, re.I))
        if not entry["company"] and "," in entry["title"] and not entry["company"]:
            head, _, tail = entry["title"].partition(",")
            entry["title"], entry["company"] = head.strip()[:120], tail.strip()[:90]
        if not entry["company"] and len(line) <= 60 and not looks_like_sentence:
            entry["company"] = line[:90]
        else:
            entry["summary"] = (entry["summary"] + " " + line)[:400].strip()
    if entry:
        out["work_experience"].append(entry)
    out["work_experience"] = [item for item in out["work_experience"] if item is not None][:8]
    out["confidence"]["work_experience"] = "medium" if out["work_experience"] else "missing"

    years = None
    spans = YEAR_RANGE_RE.findall(text)
    starts = [int(match[0] + match[1]) for match in
              ((m.group(0)[:4], m.group(0)[:4]) for m in YEAR_RANGE_RE.finditer(text))
              if str(match).isdigit()]
    starts = [int(m.group(0)[:4]) for m in YEAR_RANGE_RE.finditer(text)]
    if starts:
        earliest = min(starts)
        if 1970 <= earliest <= datetime.now().year:
            years = max(0, datetime.now().year - earliest)
    out["years_experience"] = years
    out["confidence"]["years_experience"] = "low" if years else "missing"
    return out


def render_parsed_resume(parsed: Dict[str, Any]) -> None:
    confidence = parsed.get("confidence", {})
    rows = []
    for key, label in (("first_name", "First name"), ("last_name", "Last name"), ("email", "Email"),
                       ("phone", "Phone"), ("location", "Location"), ("years_experience", "Years (estimated)"),
                       ("education", "Education"), ("linkedin", "LinkedIn"), ("github", "GitHub"),
                       ("portfolio", "Portfolio")):
        value = parsed.get(key)
        rows.append([label, str(value) if value not in (None, "") else "-",
                     confidence.get(key, "") or "-"])
    UI.table(["Field", "Extracted value", "Confidence"], rows)
    UI.table(["List", "Extracted items"],
             [["Skills", ", ".join(parsed.get("skills") or []) or "-"],
              ["Certifications", ", ".join(parsed.get("certifications") or []) or "-"],
              ["Projects", ", ".join(p.get("name", "") for p in parsed.get("projects") or []) or "-"],
              ["Work experience", ", ".join(e.get("title", "") for e in parsed.get("work_experience") or []) or "-"]])
    UI.warn("Values marked low/medium confidence were guessed from text layout. "
            "Check and edit them before saving.")

# ===========================================================================
# SECTION 8 - Dice job provider (the ONLY platform implemented)
# ===========================================================================


@dataclass
class SearchQuery:
    title: str = ""
    location: str = ""
    work_mode: str = ""            # "" | Remote | Hybrid | Onsite
    employment_type: str = ""      # "" | FULLTIME | CONTRACT | PARTTIME ...
    max_years_experience: Optional[int] = None
    easy_apply_only: bool = False
    posted_within_days: Optional[int] = None
    limit: int = 25


class ProviderError(Exception):
    """Raised with a human-readable explanation (never a stack trace)."""


def normalize_job(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Canonical job record - only fields actually supplied are filled in."""
    job = {
        "id": str(raw.get("id") or ""),
        "title": clean_text(raw.get("title"), 200),
        "company": clean_text(raw.get("company"), 200),
        "location": clean_text(raw.get("location"), 200),
        "description": clean_text(raw.get("description")),
        "requirements": clean_text(raw.get("requirements")),
        "skills": [s for s in (raw.get("skills") or []) if s],
        "salary": clean_text(raw.get("salary"), 120),
        "employment_type": clean_text(raw.get("employment_type"), 60),
        "workplace_type": clean_text(raw.get("workplace_type"), 40),
        "posted_date": clean_text(raw.get("posted_date"), 40),
        "application_type": raw.get("application_type") or AT_UNKNOWN,
        "application_url": clean_text(raw.get("application_url"), 500),
        "source_url": clean_text(raw.get("source_url"), 500),
        "source": raw.get("source") or "dice",
        "retrieved_at": now_iso(),
    }
    if not job["id"]:
        job["id"] = "dice-" + short_hash(job["source_url"] or job["title"] + job["company"])
    return job


class DiceProvider:
    """Dice only. Two authorized paths:

      1. Official Dice partner/enterprise job search API (needs DICE_API_KEY).
      2. The public Dice website, read with a normal browser session
         (needs DICE_ALLOW_BROWSER_SEARCH=true and Playwright).

    Nothing here defeats, avoids or weakens any Dice security control.
    """

    name = "dice"

    def __init__(self, config: Config):
        self.config = config

    # ---- availability -----------------------------------------------------
    def availability(self) -> Tuple[bool, str]:
        if self.config.dice_api_ready:
            return True, "official Dice API"
        if self.config.dice_allow_browser_search and HAVE_PLAYWRIGHT:
            return True, "public Dice website (browser)"
        if self.config.dice_allow_browser_search and not HAVE_PLAYWRIGHT:
            return False, ("DICE_ALLOW_BROWSER_SEARCH is on but Playwright is missing. "
                           "Fix: pip install playwright && playwright install chromium")
        return False, ("No Dice integration configured. Set DICE_API_KEY (official Dice partner API) "
                       "in .env, or set DICE_ALLOW_BROWSER_SEARCH=true to read the public Dice site "
                       "with a browser session.")

    # ---- public API -------------------------------------------------------
    def search(self, query: SearchQuery) -> List[Dict[str, Any]]:
        if self.config.dice_api_ready:
            jobs = self._search_api(query)
            if jobs:
                return jobs
            if not self.config.dice_allow_browser_search:
                raise ProviderError("The Dice API returned no usable job records for this search.")
            UI.warn("Dice API returned nothing; falling back to the public Dice website.")
        if self.config.dice_allow_browser_search:
            if not HAVE_PLAYWRIGHT:
                raise ProviderError("Playwright is required for browser search. "
                                    "Fix: pip install playwright && playwright install chromium")
            return self._search_site(query)
        raise ProviderError("No Dice integration configured. See the message above for what to set in .env.")

    # ---- path 1: official API --------------------------------------------
    def _search_api(self, query: SearchQuery) -> List[Dict[str, Any]]:
        if requests is None:
            raise ProviderError("The 'requests' package is missing. Fix: pip install requests")
        params: Dict[str, Any] = {"text": query.title or "", "page": 1,
                                  "pageSize": max(1, min(query.limit, self.config.dice_max_results))}
        if query.location:
            params["location"] = query.location
        if query.posted_within_days:
            days = str(query.posted_within_days)
            mapped = SEARCH_PARAM_MAP["posted_date"].get(days)
            if mapped:
                params["postedDate"] = mapped
        if query.employment_type:
            params["jobType"] = query.employment_type
        if query.work_mode:
            params["workplaceTypes"] = query.work_mode
        if query.max_years_experience is not None:
            params["yearsOfExperience"] = query.max_years_experience

        headers = {"Accept": "application/json", "User-Agent": f"{APP_NAME}/{VERSION}"}
        if self.config.dice_client_id:
            headers["Dice-Client-Id"] = self.config.dice_client_id
        if self.config.dice_api_auth == "query":
            params["api_key"] = self.config.dice_api_key
        else:
            headers["x-api-key"] = self.config.dice_api_key

        url = self.config.dice_api_base + self.config.dice_search_path
        try:
            response = requests.get(url, params=params, headers=headers,
                                    timeout=self.config.request_timeout)
        except Exception as exc:
            raise ProviderError("Dice connection failed.\n"
                                "Possible causes:\n"
                                "  - network connection problem\n"
                                "  - DNS / proxy / firewall blocking api.dice.com\n"
                                f"Details: {first_line(str(exc), 200)}")

        if response.status_code in (401, 403):
            raise ProviderError("Dice rejected the credentials (HTTP %d).\n"
                                "Possible causes:\n"
                                "  - DICE_API_KEY is wrong, expired or for another environment\n"
                                "  - your Dice agreement does not cover this endpoint\n"
                                "The key itself is never printed." % response.status_code)
        if response.status_code == 404:
            raise ProviderError("Dice API endpoint not found (HTTP 404): %s\n"
                                "Set DICE_SEARCH_PATH in .env to the path from your Dice partner documentation."
                                % self.config.dice_search_path)
        if response.status_code == 429:
            raise ProviderError("Dice rate limit reached (HTTP 429). Wait a while and try again. "
                                "This program does not attempt to bypass rate limits.")
        if response.status_code >= 400:
            raise ProviderError(f"Dice API error (HTTP {response.status_code}): "
                                f"{first_line(response.text, 200)}")

        try:
            payload = response.json()
        except ValueError:
            raise ProviderError("Dice API did not return JSON. "
                                "Check DICE_API_BASE / DICE_SEARCH_PATH in .env.")
        jobs = self._parse_api_payload(payload)
        log_event("dice_api_search", "ok", extra={"count": len(jobs)})
        return jobs

    def _parse_api_payload(self, payload: Any) -> List[Dict[str, Any]]:
        records: List[Any] = []
        if isinstance(payload, dict):
            for key in ("resultItemList", "jobs", "results", "data", "content", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    records = value
                    break
                if isinstance(value, dict):
                    for inner in ("jobs", "results", "content", "items", "resultItemList"):
                        if isinstance(value.get(inner), list):
                            records = value[inner]
                            break
                    if records:
                        break
        elif isinstance(payload, list):
            records = payload
        if not records:
            keys = ", ".join(list(payload.keys())[:12]) if isinstance(payload, dict) else type(payload).__name__
            raise ProviderError("Dice API response was not recognised (no job list found).\n"
                                f"Top-level fields received: {keys}\n"
                                "Adjust the parser or DICE_SEARCH_PATH to match your Dice agreement, "
                                "or use DICE_ALLOW_BROWSER_SEARCH=true.")

        jobs = []
        for record in records:
            if not isinstance(record, dict):
                continue
            job = self._api_record_to_job(record)
            if job:
                jobs.append(job)
        return jobs

    @staticmethod
    def _api_record_to_job(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        def pick(*names: str) -> str:
            for name in names:
                value = record.get(name)
                if isinstance(value, (str, int, float)) and str(value).strip():
                    return str(value).strip()
            return ""

        title = pick("title", "jobTitle", "name")
        if not title:
            return None
        detail = pick("detailsPageUrl", "jobDetailUrl", "url", "applyUrl")
        if detail.startswith("/"):
            detail = "https://www.dice.com" + detail
        skills = record.get("skills") or record.get("skillList") or []
        if isinstance(skills, str):
            skills = [s.strip() for s in re.split(r"[,;|]", skills) if s.strip()]
        employment = pick("employmentType", "jobType", "positionType")
        workplace = pick("workplaceTypes", "workplaceType", "remoteType", "locationType")
        if not workplace and record.get("isRemote") in (True, "true", "True"):
            workplace = "Remote"
        salary = pick("salary", "salaryRange", "payRange", "estimatedSalary")
        description = pick("summary", "description", "jobDescription", "snippet")
        requirements = pick("requirements", "qualifications")
        application_url = detail
        app_type = AT_UNKNOWN
        apply_flag = record.get("easyApply") or record.get("isEasyApply")
        if str(apply_flag).lower() in ("true", "1", "yes"):
            app_type = AT_EASY_APPLY
        elif str(apply_flag).lower() in ("false", "0", "no"):
            app_type = AT_EXTERNAL
        return normalize_job({
            "id": pick("id", "jobId", "guid") or ("dice-" + short_hash(detail or title)),
            "title": title,
            "company": pick("company", "companyName", "employer", "hiringOrganization"),
            "location": pick("location", "jobLocation", "city", "formattedLocation"),
            "description": description,
            "requirements": requirements,
            "skills": skills,
            "salary": salary,
            "employment_type": employment,
            "workplace_type": workplace,
            "posted_date": pick("postedDate", "datePosted", "created", "date"),
            "application_type": app_type,
            "application_url": application_url,
            "source_url": detail or application_url,
            "source": "dice-api",
        })

    # ---- path 2: public Dice website via Playwright ----------------------
    def _search_site(self, query: SearchQuery) -> List[Dict[str, Any]]:
        jobs: List[Dict[str, Any]] = []
        limit = max(1, min(query.limit, self.config.dice_max_results))
        try:
            with sync_playwright() as play:
                browser, context = launch_browser(play, self.config, for_apply=False)
                page = context.new_page()
                url = build_dice_search_url(query)
                log_event("dice_site_search", "info", extra={"url": url})
                try:
                    page.goto(url, timeout=self.config.nav_timeout * 1000, wait_until="domcontentloaded")
                except PWTimeout:
                    raise ProviderError("Dice search page timed out. Check your connection and retry.")
                except Exception as exc:
                    raise ProviderError(f"Could not open Dice search page: {first_line(str(exc), 200)}")

                challenge = detect_challenge(page)
                if challenge:
                    raise ProviderError("Human intervention required.\n"
                                        f"Dice presented a security control ({challenge}).\n"
                                        "This program never bypasses CAPTCHA, MFA or bot detection. "
                                        "Open dice.com in your own browser, complete it there, then retry.")

                cards = self._extract_cards(page)
                if not cards:
                    screenshot(page, "search-no-results")
                    raise ProviderError("No job cards were found on the Dice search page.\n"
                                        "Possible causes:\n"
                                        "  - the search returned zero jobs (try a wider search)\n"
                                        "  - you are not signed in, or Dice is showing a different layout\n"
                                        "  - Dice changed its page structure (this MVP reads a fixed set of selectors)\n"
                                        "No jobs were invented or substituted.")

                for index, card in enumerate(cards[:limit], start=1):
                    job = normalize_job({
                        "id": "dice-" + short_hash(card["url"]),
                        "title": card.get("title", ""),
                        "company": card.get("company", ""),
                        "location": card.get("location", ""),
                        "workplace_type": card.get("workplace_type", ""),
                        "posted_date": card.get("posted_date", ""),
                        "salary": card.get("salary", ""),
                        "employment_type": card.get("employment_type", ""),
                        "application_url": card["url"],
                        "source_url": card["url"],
                        "application_type": AT_UNKNOWN,
                        "source": "dice-site",
                    })
                    jobs.append(job)
                    if index < limit:
                        time.sleep(max(0.0, self.config.pol_request_delay))

                enrich = min(self.config.dice_enrich_limit, len(jobs))
                for index, job in enumerate(jobs[:enrich], start=1):
                    UI.info(f"Reading job details {index}/{enrich}: {job['title'][:60]}")
                    self._enrich_from_site(page, job)
                    time.sleep(max(0.0, self.config.pol_request_delay))
                browser.close()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("Dice browser search failed: " + first_line(str(exc), 300))
        log_event("dice_site_search", "ok", extra={"count": len(jobs)})
        return jobs

    @staticmethod
    def _extract_cards(page) -> List[Dict[str, str]]:
        script = r"""
        () => {
          const text = (el) => (el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '');
          const seen = new Set();
          const out = [];
          const links = Array.from(document.querySelectorAll(
            'a[href*="/job-detail/"], a[href*="/jobs/detail"], a[data-testid*="job-title" i], a[data-testid*="jobTitle" i]'
          ));
          links.forEach((a) => {
            const href = a.href || '';
            if (!href || seen.has(href)) return;
            const title = text(a);
            if (!title || title.length < 3) return;
            seen.add(href);
            let card = a.closest('div[data-testid="job-card"], li, article, div');
            for (let i = 0; i < 6 && card && card.parentElement; i++) {
              if (card.querySelector && (card.querySelector('[data-testid="company-name"]')
                  || card.querySelector('[data-testid="search-result-company-name"]')
                  || /\\$|\\d{2,3},?\\d{3}/.test(text(card)))) break;
              card = card.parentElement;
            }
            const cardText = text(card) || '';
            const pick = (sel) => { const n = card ? card.querySelector(sel) : null; return text(n); };
            let company = pick('[data-testid="company-name"]') || pick('[data-testid="search-result-company-name"]');
            let location = pick('[data-testid="location"]') || pick('[data-testid="search-result-location"]');
            out.push({
              url: href, title: title, company: company, location: location,
              card_text: cardText.slice(0, 400),
            });
          });
          return out;
        }
        """
        try:
            cards = page.evaluate(script) or []
        except Exception:
            return []
        results = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            card_text = card.get("card_text", "") or ""
            workplace = ""
            for word in ("Remote", "Hybrid", "Onsite", "On-site"):
                if re.search(r"\b" + re.escape(word) + r"\b", card_text, re.I):
                    workplace = "Remote" if word.lower().startswith("remote") else (
                        "Hybrid" if word.lower().startswith("hybrid") else "Onsite")
                    break
            salary_match = re.search(r"\$[\d,.]+(?:\s*[-–]\s*\$?[\d,.]+)?(?:\s*(?:per|/)\s*\w+)?", card_text)
            posted = ""
            posted_match = re.search(r"(posted\s+\w+|\d+\s+days?\s+ago|today|just posted)", card_text, re.I)
            if posted_match:
                posted = posted_match.group(0)
            results.append({
                "url": card.get("url", ""),
                "title": card.get("title", ""),
                "company": card.get("company", ""),
                "location": card.get("location", ""),
                "workplace_type": workplace,
                "salary": salary_match.group(0) if salary_match else "",
                "posted_date": posted,
            })
        return [card for card in results if card["url"] and card["title"]]

    def _enrich_from_site(self, page, job: Dict[str, Any]) -> None:
        """Read structured data + the apply button of one Dice job page."""
        try:
            page.goto(job["source_url"], timeout=self.config.nav_timeout * 1000,
                      wait_until="domcontentloaded")
        except Exception as exc:
            log_event("dice_job_detail", "warn", job_id=job.get("id"),
                      error=first_line(str(exc), 200))
            return
        if detect_challenge(page):
            UI.warn("Human intervention required (security control on a job page). Stopping the detail pass.")
            log_event("dice_job_detail", "blocked", job_id=job.get("id"), error="security control")
            raise ProviderError("Human intervention required.\n"
                                "Dice showed a security control while reading job details. "
                                "Complete it yourself in a normal browser, then retry. "
                                "This program does not bypass security controls.")
        try:
            data = page.evaluate(r"""
            () => {
              const out = { json: null, apply: null, text: '' };
              const nodes = Array.from(document.querySelectorAll('script[type="application/ld+json"]'));
              for (const n of nodes) {
                try {
                  const parsed = JSON.parse(n.textContent || '{}');
                  const list = Array.isArray(parsed) ? parsed : [parsed];
                  for (const item of list) {
                    if (item && (item['@type'] === 'JobPosting' || item.jobPosting)) {
                      out.json = item.jobPosting || item;
                      break;
                    }
                  }
                } catch (e) { /* ignore malformed block */ }
                if (out.json) break;
              }
              const buttons = Array.from(document.querySelectorAll('button, a[role="button"], a[data-testid*="apply" i]'));
              for (const b of buttons) {
                const label = (b.innerText || b.textContent || '').replace(/\s+/g, ' ').trim();
                if (/apply/i.test(label) && label.length < 60) { out.apply = label; break; }
              }
              out.text = (document.body ? document.body.innerText : '').slice(0, 6000);
              return out;
            }
            """) or {}
        except Exception as exc:
            log_event("dice_job_detail", "warn", job_id=job.get("id"), error=first_line(str(exc), 200))
            return

        payload = data.get("json") or {}
        if isinstance(payload, dict) and payload:
            job["title"] = clean_text(payload.get("title"), 200) or job["title"]
            org = payload.get("hiringOrganization")
            if isinstance(org, dict) and org.get("name"):
                job["company"] = clean_text(org["name"], 200)
            elif isinstance(org, str) and org:
                job["company"] = clean_text(org, 200)
            if payload.get("description"):
                job["description"] = html_to_text(payload["description"])
            employment = payload.get("employmentType")
            if isinstance(employment, list) and employment:
                job["employment_type"] = clean_text(employment[0], 60)
            elif isinstance(employment, str):
                job["employment_type"] = clean_text(employment, 60)
            if payload.get("datePosted"):
                job["posted_date"] = clean_text(payload["datePosted"], 40)
            location = payload.get("jobLocation")
            if isinstance(location, list) and location:
                location = location[0]
            if isinstance(location, dict):
                address = location.get("address") or {}
                parts = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
                text_loc = ", ".join([p for p in parts if isinstance(p, str) and p])
                if text_loc:
                    job["location"] = text_loc
            elif isinstance(location, str) and location:
                job["location"] = clean_text(location, 200)
            if str(payload.get("jobLocationType", "")).upper() == "TELECOMMUTE":
                job["workplace_type"] = job["workplace_type"] or "Remote"
            salary = payload.get("baseSalary")
            if isinstance(salary, dict):
                value = salary.get("value") or {}
                if isinstance(value, dict):
                    low, high = value.get("minValue"), value.get("maxValue")
                    unit = value.get("unitText") or ""
                    if low or high:
                        job["salary"] = f"{low or ''} - {high or ''} {unit}".strip()
                elif value:
                    job["salary"] = clean_text(value, 120)
            elif isinstance(salary, str) and salary:
                job["salary"] = clean_text(salary, 120)

        if job.get("description"):
            job["requirements"] = extract_requirements(job["description"])

        apply_label = (data.get("apply") or "").strip()
        body = (data.get("text") or "").lower()
        job["application_type"] = classify_apply_label(apply_label, body)
        job["retrieved_at"] = now_iso()


def build_dice_search_url(query: SearchQuery) -> str:
    params = [f"q={quote_plus(query.title or '')}"]
    if query.location:
        params.append(f"location={quote_plus(query.location)}")
    if query.posted_within_days:
        mapped = SEARCH_PARAM_MAP["posted_date"].get(str(query.posted_within_days))
        if mapped:
            params.append(f"filters.postedDate={mapped}")
    if query.work_mode:
        mapped = SEARCH_PARAM_MAP["workplace"].get(query.work_mode.lower())
        if mapped:
            params.append(f"filters.workplaceTypes={mapped}")
    if query.employment_type:
        params.append(f"filters.employmentType={quote_plus(query.employment_type)}")
    if query.easy_apply_only:
        params.append("filters.easyApply=true")
    if query.max_years_experience is not None:
        params.append(f"filters.yearsOfExperience={query.max_years_experience}")
    return DICE_SEARCH_URL + "?" + "&".join(params)


def quote_plus(value: str) -> str:
    try:
        from urllib.parse import quote_plus as _qp
        return _qp(value)
    except Exception:
        return str(value)


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", str(html or ""))
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'"), ("&rsquo;", "'")):
        text = text.replace(entity, char)
    return clean_text(text)


def extract_requirements(description: str) -> str:
    """Pull a requirements/qualifications block out of a real description, if present."""
    if not description:
        return ""
    lines = [line.strip() for line in description.splitlines()]
    heading = re.compile(r"^(requirements?|qualifications?|required skills|must have|must-haves|"
                         r"what you(?:'| wi)ll need|basic qualifications|minimum qualifications)\b[:\s]*$", re.I)
    for index, line in enumerate(lines):
        if heading.match(re.sub(r"[*#\s]+$", "", line).strip()):
            block = []
            for following in lines[index + 1:index + 25]:
                if re.match(r"^(benefits?|about (us|the company)|responsibilities|preferred)\b", following, re.I):
                    break
                if following:
                    block.append(following)
            if block:
                return clean_text("\n".join(block), 4000)
    return ""


def classify_apply_label(label: str, page_text: str = "") -> str:
    """Classify how a Dice job is applied to. Never guesses positively."""
    label_low = (label or "").lower()
    if "easy apply" in label_low:
        return AT_EASY_APPLY
    if label_low:
        if any(token in label_low for token in ("company site", "company website", "external",
                                                "apply now", "apply on", "apply externally")):
            return AT_EXTERNAL
    text = (page_text or "").lower()
    if "easy apply" in text:
        return AT_EASY_APPLY
    if "apply on company site" in text or "apply on company website" in text:
        return AT_EXTERNAL
    return AT_UNKNOWN

# ===========================================================================
# SECTION 9 - job storage
# ===========================================================================


def load_jobs() -> List[Dict[str, Any]]:
    data = read_json(JOBS_FILE, {"version": SCHEMA_VERSION, "jobs": []})
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        UI.warn("jobs.json has an unexpected shape; treating it as empty.")
        return []
    return [job for job in jobs if isinstance(job, dict)]


def save_jobs(jobs: List[Dict[str, Any]]) -> bool:
    return write_json(JOBS_FILE, {"version": SCHEMA_VERSION, "updated_at": now_iso(), "jobs": jobs})


def merge_jobs(new_jobs: List[Dict[str, Any]]) -> Tuple[int, int]:
    jobs = load_jobs()
    index = {job.get("id"): job for job in jobs}
    added = updated = 0
    for job in new_jobs:
        existing = index.get(job["id"])
        if existing:
            for key, value in job.items():
                if value not in ("", None, [], AT_UNKNOWN) or not existing.get(key):
                    existing[key] = value
            existing["score"] = None       # force re-scoring after refresh
            existing["matches"] = []
            existing["gaps"] = []
            updated += 1
        else:
            jobs.append(job)
            index[job["id"]] = job
            added += 1
    save_jobs(jobs)
    return added, updated


def update_job(job: Dict[str, Any]) -> None:
    jobs = load_jobs()
    for index, existing in enumerate(jobs):
        if existing.get("id") == job.get("id"):
            jobs[index] = job
            break
    else:
        jobs.append(job)
    save_jobs(jobs)


def job_label(job: Dict[str, Any]) -> str:
    return f"{job.get('title','(no title)')} @ {job.get('company','(unknown company)')}"

# ===========================================================================
# SECTION 10 - applications storage
# ===========================================================================


def load_applications() -> List[Dict[str, Any]]:
    data = read_json(APPLICATIONS_FILE, {"version": SCHEMA_VERSION, "applications": []})
    apps = data.get("applications") if isinstance(data, dict) else data
    return apps if isinstance(apps, list) else []


def save_applications(apps: List[Dict[str, Any]]) -> bool:
    return write_json(APPLICATIONS_FILE,
                      {"version": SCHEMA_VERSION, "updated_at": now_iso(), "applications": apps})


def create_application(job: Dict[str, Any], state: str = ST_SELECTED) -> Dict[str, Any]:
    apps = load_applications()
    for app in apps:
        if app.get("job_id") == job.get("id") and app.get("state") not in (ST_FAILED, ST_CANCELLED):
            return app
    application = {
        "id": "app-" + short_hash(job.get("id", "") + now_iso(), 10),
        "job_id": job.get("id"),
        "job_title": job.get("title", ""),
        "company": job.get("company", ""),
        "score": job.get("score"),
        "state": state,
        "application_type": job.get("application_type", AT_UNKNOWN),
        "url": job.get("source_url") or job.get("application_url") or "",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "submitted_at": "",
        "flags": [],
        "answers": [],
        "last_error": "",
        "notes": "",
        "demo": bool(job.get("source") == "demo"),
    }
    apps.append(application)
    save_applications(apps)
    log_event("application_created", "ok", job_id=job.get("id"), application_id=application["id"])
    return application


def get_application(app_id: str) -> Optional[Dict[str, Any]]:
    for app in load_applications():
        if app.get("id") == app_id:
            return app
    return None


def set_state(app_id: str, state: str, error: str = "", note: str = "",
              flags: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    apps = load_applications()
    for app in apps:
        if app.get("id") == app_id:
            app["state"] = state
            app["updated_at"] = now_iso()
            if error:
                app["last_error"] = clean_text(error, 800)
            if note:
                app["notes"] = clean_text((app.get("notes", "") + " " + note), 800)
            if flags:
                for flag in flags:
                    if flag not in app.setdefault("flags", []):
                        app["flags"].append(flag)
            if state == ST_SUBMITTED:
                app["submitted_at"] = now_iso()
            save_applications(apps)
            log_event("state_change", state.lower(), job_id=app.get("job_id"),
                      application_id=app_id, error=error or None)
            return app
    UI.warn(f"Application {app_id} was not found.")
    return None


def store_answer(app_id: str, question: str, answer: str, source: str,
                 confidence: Optional[float] = None) -> None:
    apps = load_applications()
    for app in apps:
        if app.get("id") == app_id:
            app.setdefault("answers", []).append({
                "question": clean_text(question, 300),
                "answer": clean_text(answer, 500),
                "source": source,
                "confidence": confidence,
                "at": now_iso(),
            })
            app["updated_at"] = now_iso()
            save_applications(apps)
            return

# ===========================================================================
# SECTION 11 - deterministic match scoring (AI can only adjust, never decide)
# ===========================================================================


def _ratio(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _token_overlap(a: str, b: str) -> float:
    tokens_a = {t for t in re.findall(r"[a-z0-9+#.]+", (a or "").lower()) if len(t) > 1}
    tokens_b = {t for t in re.findall(r"[a-z0-9+#.]+", (b or "").lower()) if len(t) > 1}
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a)


def required_years(text: str) -> Optional[int]:
    match = re.search(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|to|–)?\s*(\d{1,2})?\s*years?", text or "", re.I)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if 0 < value <= 40 else None


def recommendation_for(score: int) -> str:
    """80-100 APPLY, 60-79 REVIEW, 0-59 SKIP (spec section 11)."""
    return "APPLY" if score >= 80 else ("REVIEW" if score >= 60 else "SKIP")


def score_job(job: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Explainable, deterministic score. Weights per the spec; AI may only nudge it."""
    breakdown: Dict[str, float] = {}
    matches: List[str] = []
    gaps: List[str] = []

    title = job.get("title", "")
    description = job.get("description", "") or ""
    requirements = job.get("requirements", "") or ""
    job_text = " ".join([title, description, requirements, job.get("location", ""),
                         job.get("workplace_type", ""), job.get("salary", ""),
                         " ".join(job.get("skills") or [])])

    # 1. Role similarity (30)
    roles = candidate.get("target_roles") or []
    role_ratio = 0.0
    best_role = ""
    for role in roles:
        value = max(_token_overlap(role, title), _ratio(role, title))
        if value > role_ratio:
            role_ratio, best_role = value, role
    breakdown["role"] = round(WEIGHTS["role"] * min(1.0, role_ratio), 1)
    if role_ratio >= 0.55:
        matches.append(f"Title '{title}' is close to target role '{best_role}'")
    elif roles:
        gaps.append(f"Title '{title}' does not clearly match target roles: {', '.join(roles[:4])}")

    # 2. Skills (25)
    candidate_skills = [s for s in (candidate.get("skills") or []) if s]
    required_skills = [s for s in (job.get("skills") or []) if s]
    if required_skills:
        have = [s for s in required_skills if any(_ratio(s, c) > 0.85 for c in candidate_skills)]
        missing = [s for s in required_skills if s not in have]
        ratio = len(have) / max(1, len(required_skills))
        matches.extend(f"Skill match: {s}" for s in have[:8])
        gaps.extend(f"Missing skill listed by Dice: {s}" for s in missing[:8])
    else:
        mentioned = [s for s in candidate_skills if contains_word(job_text, s)]
        ratio = min(1.0, len(mentioned) / 5.0) if candidate_skills else 0.0
        matches.extend(f"Candidate skill found in job text: {s}" for s in mentioned[:8])
        if candidate_skills and len(mentioned) < 5:
            gaps.append(f"Only {len(mentioned)} of your skills appear in the job text")
    breakdown["skills"] = round(WEIGHTS["skills"] * min(1.0, ratio), 1)

    # 3. Experience (20)
    needed = required_years(job_text)
    years = candidate.get("years_experience")
    try:
        years = float(years) if years is not None and str(years).strip() != "" else None
    except (TypeError, ValueError):
        years = None
    if needed is None:
        breakdown["experience"] = round(WEIGHTS["experience"] * 0.7, 1)
        gaps.append("Job text does not state a required number of years (scored neutral)")
    elif years is None:
        breakdown["experience"] = round(WEIGHTS["experience"] * 0.5, 1)
        gaps.append(f"Job asks for {needed}+ years; your profile has no years of experience value")
    elif years >= needed:
        breakdown["experience"] = float(WEIGHTS["experience"])
        matches.append(f"{int(years)} years experience meets the {needed}+ years requirement")
    else:
        shortfall = needed - years
        factor = 0.7 if shortfall <= 1 else (0.4 if shortfall <= 2 else 0.15)
        breakdown["experience"] = round(WEIGHTS["experience"] * factor, 1)
        gaps.append(f"Job asks for {needed}+ years, profile shows {int(years)}")

    # 4. Location / work mode (10)
    job_location = (job.get("location") or "").lower()
    workplace = (job.get("workplace_type") or "").lower()
    preferred_modes = [m.lower() for m in (candidate.get("work_modes") or [])]
    candidate_location = (candidate.get("location") or "").lower()
    loc_score = 0.6
    if workplace.startswith("remote"):
        if not preferred_modes or "remote" in preferred_modes:
            loc_score = 1.0
            matches.append("Job is remote and remote work is acceptable to you")
        else:
            loc_score = 0.3
            gaps.append("Job is remote but remote work is not in your preferred work modes")
    elif candidate_location and job_location:
        city = candidate_location.split(",")[0].strip()
        if city and city in job_location:
            loc_score = 1.0
            matches.append(f"Location matches your location ({job.get('location')})")
        else:
            state_a = candidate_location.split(",")[-1].strip()
            state_b = job_location.split(",")[-1].strip()
            if state_a and state_a == state_b:
                loc_score = 0.7
                matches.append("Job is in the same state/region as you")
            else:
                loc_score = 0.35
                gaps.append(f"Job location '{job.get('location')}' differs from yours")
    breakdown["location"] = round(WEIGHTS["location"] * loc_score, 1)

    # 5. Education (5)
    education = (candidate.get("education") or "").lower()
    degree_required = DEGREE_RE.search(job_text)
    if not degree_required:
        breakdown["education"] = round(WEIGHTS["education"] * 0.9, 1)
    elif education and degree_required.group(0).lower().strip(".") in education:
        breakdown["education"] = float(WEIGHTS["education"])
        matches.append(f"Education requirement mentions '{degree_required.group(0)}' which your profile has")
    elif education:
        breakdown["education"] = round(WEIGHTS["education"] * 0.7, 1)
        gaps.append(f"Job mentions '{degree_required.group(0)}'; check your education entry matches")
    else:
        breakdown["education"] = round(WEIGHTS["education"] * 0.2, 1)
        gaps.append(f"Job mentions '{degree_required.group(0)}' but your profile has no education entry")

    # 6. Employment fit (5)
    job_type = (job.get("employment_type") or "").upper()
    preferred_type = (candidate.get("employment_type") or "").upper()
    if not job_type:
        breakdown["employment"] = round(WEIGHTS["employment"] * 0.8, 1)
    elif preferred_type and (preferred_type in job_type or job_type in preferred_type):
        breakdown["employment"] = float(WEIGHTS["employment"])
        matches.append(f"Employment type '{job.get('employment_type')}' matches your preference")
    else:
        breakdown["employment"] = round(WEIGHTS["employment"] * 0.3, 1)
        gaps.append(f"Dice lists employment type '{job.get('employment_type')}'")

    # 7. Other requirements (5) - sponsorship / authorization / clearance signals
    low_text = job_text.lower()
    other = 0.8
    no_sponsor = re.search(r"(no|not|cannot|unable to|does not)\s+(offer\s+|provide\s+|consider\s+)?"
                           r"(visa\s+)?sponsor", low_text)
    wants_sponsor = str(candidate.get("sponsorship_required", "")).lower().startswith("y")
    authorization = (candidate.get("work_authorization") or "").lower()
    if no_sponsor and wants_sponsor:
        other = 0.0
        gaps.append("Job states no visa sponsorship, but your profile says you need sponsorship")
    elif re.search(r"\b(citizen|green card|clearance required|must be authorized)\b", low_text) and \
            authorization and "citizen" in authorization.lower() and "not" not in authorization.lower():
        other = 0.9
    if "security clearance" in low_text and "clearance" not in " ".join(candidate.get("certifications") or []).lower():
        other = min(other, 0.3)
        gaps.append("Job mentions a security clearance you have not listed")
    breakdown["other"] = round(WEIGHTS["other"] * other, 1)

    total = round(sum(breakdown.values()))
    total = max(0, min(100, total))
    recommendation = recommendation_for(total)
    return {
        "score": total,
        "breakdown": breakdown,
        "matches": matches[:12],
        "gaps": gaps[:12],
        "recommendation": recommendation,
        "ai_adjustment": 0,
        "ai_reason": "",
    }


def apply_ai_adjustment(result: Dict[str, Any], adjustment: int, confidence: float,
                        reason: str) -> Dict[str, Any]:
    """Apply a validated AI nudge. Bounded, clamped, and always recorded."""
    try:
        adjustment = int(adjustment)
    except (TypeError, ValueError):
        return result
    adjustment = max(-10, min(10, adjustment))
    result["ai_adjustment"] = adjustment
    result["ai_reason"] = clean_text(reason, 400)
    result["score"] = max(0, min(100, result["score"] + adjustment))
    result["breakdown"]["ai_adjustment"] = adjustment
    result["recommendation"] = recommendation_for(result["score"])
    return result


if HAVE_PYDANTIC:
    class AIQuestionAnswer(BaseModel):                       # type: ignore[misc]
        answer: str = Field(default="")
        confidence: float = Field(default=0.0)
        reason: str = Field(default="")

    class AIMatchVerdict(BaseModel):                         # type: ignore[misc]
        adjustment: int = Field(default=0)
        confidence: float = Field(default=0.0)
        reason: str = Field(default="")
else:                                                        # pragma: no cover
    AIQuestionAnswer = None                                  # type: ignore
    AIMatchVerdict = None                                    # type: ignore


class AIClient:
    """Thin, defensive wrapper. Returns None on any problem - the caller then
    falls back to asking the human. Never controls the browser."""

    def __init__(self, config: Config):
        self.config = config
        self.last_error = ""

    @property
    def available(self) -> bool:
        return self.config.ai_ready

    def _post(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = self.config.llm_base_url + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.config.llm_api_key}",
                   "Content-Type": "application/json", "User-Agent": f"{APP_NAME}/{VERSION}"}
        for attempt in (0, 1):
            try:
                response = requests.post(url, headers=headers, json=payload,
                                         timeout=self.config.request_timeout * 2)
            except Exception as exc:
                self.last_error = f"AI request failed: {first_line(str(exc), 160)}"
                log_event("ai_request", "error", error=self.last_error)
                return None
            if response.status_code in (400, 422) and attempt == 0 and "response_format" in payload:
                payload = dict(payload)
                payload.pop("response_format", None)
                continue
            if response.status_code in (401, 403):
                self.last_error = "AI provider rejected the API key (HTTP %d)." % response.status_code
            elif response.status_code == 429:
                self.last_error = "AI provider rate limit reached (HTTP 429)."
            elif response.status_code >= 400:
                self.last_error = f"AI provider error HTTP {response.status_code}: {first_line(response.text, 160)}"
            else:
                try:
                    data = response.json()
                    content = data["choices"][0]["message"]["content"]
                except Exception:
                    self.last_error = "AI provider returned an unexpected response shape."
                    log_event("ai_request", "error", error=self.last_error)
                    return None
                if isinstance(content, list):     # some providers return content parts
                    content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
                try:
                    parsed = json.loads(content)
                except Exception:
                    match = re.search(r"\{.*\}", content or "", re.S)
                    if not match:
                        self.last_error = "AI output was not valid JSON."
                        log_event("ai_invalid_json", "error", error=self.last_error)
                        return None
                    try:
                        parsed = json.loads(match.group(0))
                    except Exception:
                        self.last_error = "AI output was not valid JSON."
                        log_event("ai_invalid_json", "error", error=self.last_error)
                        return None
                if not isinstance(parsed, dict):
                    self.last_error = "AI output was not a JSON object."
                    return None
                log_event("ai_request", "ok")
                return parsed
            log_event("ai_request", "error", error=self.last_error)
            return None
        return None

    def semantic_match(self, job: Dict[str, Any], candidate: Dict[str, Any],
                       base: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Ask the AI for a small bounded score adjustment (-10..+10)."""
        if not self.available:
            return None
        facts = candidate_facts(candidate)
        prompt = (
            "You adjust a deterministic job-match score by at most -10 to +10 points.\n"
            "Rules:\n"
            "- Judge only semantic closeness between the candidate facts and this job.\n"
            "- Never invent facts about the candidate.\n"
            "- Output JSON only: {\"adjustment\": int, \"confidence\": 0-1, \"reason\": str}.\n"
            "- adjustment must be between -10 and 10. Use 0 if unsure.\n\n"
            f"CANDIDATE FACTS\n{facts}\n\n"
            f"JOB\nTitle: {job.get('title','')}\nCompany: {job.get('company','')}\n"
            f"Location: {job.get('location','')}\nWorkplace: {job.get('workplace_type','')}\n"
            f"Employment type: {job.get('employment_type','')}\n"
            f"Description: {clean_text(job.get('description',''), 3000)}\n\n"
            f"CURRENT DETERMINISTIC RESULT\nScore {base.get('score')} "
            f"(recommendation {base.get('recommendation')}), matches: "
            f"{'; '.join(base.get('matches', [])[:6]) or 'none'}, gaps: "
            f"{'; '.join(base.get('gaps', [])[:6]) or 'none'}\n"
        )
        parsed = self._post(self._payload(prompt))
        if not parsed:
            return None
        try:
            if HAVE_PYDANTIC:
                verdict = AIMatchVerdict(**parsed)
            else:
                verdict = AIMatchVerdict
                verdict.adjustment = int(parsed.get("adjustment", 0) or 0)
                verdict.confidence = float(parsed.get("confidence", 0) or 0)
                verdict.reason = str(parsed.get("reason", ""))[:400]
        except Exception as exc:
            self.last_error = f"AI output failed validation: {first_line(str(exc), 160)}"
            log_event("ai_invalid_output", "error", error=self.last_error)
            return None
        if not isinstance(verdict.adjustment, int):
            return None
        if verdict.confidence < 0.5:
            self.last_error = "AI was not confident enough to adjust the score."
            return None
        return {"adjustment": max(-10, min(10, verdict.adjustment)),
                "confidence": float(verdict.confidence),
                "reason": clean_text(verdict.reason, 400)}

    def answer_question(self, question: str, options: List[str], candidate: Dict[str, Any],
                        job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Answer one application question using ONLY known candidate facts."""
        if not self.available:
            return None
        facts = candidate_facts(candidate)
        option_block = "\n".join(f"- {option}" for option in options) if options else "(no fixed options)"
        prompt = (
            "You answer ONE job-application question using ONLY the candidate facts below.\n"
            "Rules:\n"
            "- If the facts do not contain the information, answer with answer=\"UNKNOWN\".\n"
            "- Never invent or assume anything about the candidate.\n"
            "- If options are given, the answer must be exactly one of them.\n"
            "- Output JSON only: {\"answer\": str, \"confidence\": 0-1, \"reason\": str}\n\n"
            f"CANDIDATE FACTS\n{facts}\n\n"
            f"JOB CONTEXT\nTitle: {job.get('title','')}\nCompany: {job.get('company','')}\n\n"
            f"QUESTION\n{question}\n\n"
            f"AVAILABLE OPTIONS\n{option_block}\n"
        )
        parsed = self._post(self._payload(prompt))
        if not parsed:
            return None
        try:
            if HAVE_PYDANTIC:
                answer = AIQuestionAnswer(**parsed)
            else:                                            # pragma: no cover
                answer = AIQuestionAnswer
                answer.answer = str(parsed.get("answer", ""))[:500]
                answer.confidence = float(parsed.get("confidence", 0) or 0)
                answer.reason = str(parsed.get("reason", ""))[:400]
        except Exception as exc:
            self.last_error = f"AI output failed validation: {first_line(str(exc), 160)}"
            log_event("ai_invalid_output", "error", error=self.last_error)
            return None
        text = (answer.answer or "").strip()
        if not text or text.upper() in ("UNKNOWN", "N/A", "NONE", "NULL"):
            self.last_error = "AI reported that the candidate facts do not contain this information."
            return None
        if options:
            exact = next((option for option in options if option.strip().lower() == text.lower()), None)
            if exact is None:
                partial = [option for option in options if text.lower() in option.lower()
                           or option.lower() in text.lower()]
                if len(partial) != 1:
                    self.last_error = ("AI answer did not match any available option; "
                                       "human input required.")
                    log_event("ai_unmapped_answer", "warn", error=self.last_error)
                    return None
                text = partial[0]
            else:
                text = exact
        if answer.confidence < self.config.ai_min_confidence:
            self.last_error = (f"AI confidence {answer.confidence:.2f} is below the "
                               f"{self.config.ai_min_confidence:.2f} threshold.")
            return None
        return {"answer": text, "confidence": float(answer.confidence),
                "reason": clean_text(answer.reason, 400)}

    def _payload(self, user_prompt: str) -> Dict[str, Any]:
        return {
            "model": self.config.llm_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "You return strict JSON only. You never invent "
                                              "candidate data and you never take actions."},
                {"role": "user", "content": user_prompt},
            ],
        }

# ===========================================================================
# SECTION 13 - Playwright helpers (browser, screenshots, challenge detection)
# ===========================================================================


def screenshot(page, name: str) -> Optional[str]:
    try:
        ERRORS_DIR.mkdir(parents=True, exist_ok=True)
        path = ERRORS_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug(name)}.png"
        page.screenshot(path=str(path), full_page=True)
        UI.info(f"Screenshot saved: {path}")
        return str(path)
    except Exception:
        return None


def launch_browser(play, config: Config, for_apply: bool = True, headless_override=None):
    """Launch Chromium with the saved session if one exists. No stealth, no evasion."""
    if headless_override is not None:
        headless = headless_override
    else:
        headless = config.headless if for_apply else True
    browser = play.chromium.launch(headless=headless, slow_mo=120 if not headless else 0)
    context_kwargs: Dict[str, Any] = {"viewport": {"width": 1440, "height": 900},
                                      "accept_downloads": False,
                                      "user_agent": None}
    context_kwargs.pop("user_agent", None)
    if BROWSER_STATE_FILE.exists():
        try:
            context = browser.new_context(storage_state=str(BROWSER_STATE_FILE), **context_kwargs)
        except Exception as exc:
            UI.warn(f"Saved browser session could not be used ({first_line(str(exc), 120)}). "
                    "Continuing without it.")
            context = browser.new_context(**context_kwargs)
    else:
        context = browser.new_context(**context_kwargs)
    context.set_default_timeout(config.request_timeout * 1000)
    return browser, context


def save_browser_state(context) -> bool:
    try:
        context.storage_state(path=str(BROWSER_STATE_FILE))
        log_event("browser_state_saved", "ok")
        return True
    except Exception as exc:
        UI.warn(f"Could not save the browser session: {first_line(str(exc), 120)}")
        return False


def detect_challenge(page) -> Optional[str]:
    """Detect CAPTCHA / MFA / bot checks. Detection only - never bypassed."""
    try:
        text = (page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 8000)") or "").lower()
    except Exception:
        text = ""
    for hint in CHALLENGE_HINTS:
        if hint in text:
            return hint
    for frame in page.frames:
        url = (frame.url or "").lower()
        if any(token in url for token in ("captcha", "/challenge", "recaptcha", "hcaptcha", "verify")):
            return f"challenge page ({url[:80]})"
    return None


def challenge_stop(job: Dict[str, Any], app_id: Optional[str], where: str) -> None:
    UI.print("")
    UI.error("Human intervention required.")
    UI.print(f"Dice presented a security control while {where}.")
    UI.print("This program never solves, bypasses or works around CAPTCHA, MFA, OTP or bot checks.")
    UI.print("Nothing was submitted. Complete the challenge yourself in a normal browser, then retry.")
    if app_id:
        set_state(app_id, ST_REVIEW, error=f"Security challenge detected ({where}). "
                                           "Human intervention required.",
                  flags=["HUMAN_REVIEW_REQUIRED"])
    log_event("security_challenge", "blocked", job_id=job.get("id"), application_id=app_id,
              error=where)

# ===========================================================================
# SECTION 14 - application form inspection + deterministic mapping
# ===========================================================================

INSPECT_JS = r"""
() => {
  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    return rect.width > 0 && rect.height > 0;
  };
  const textOf = (el) => (el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '');
  const labelOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const parts = labelledBy.split(/\s+/).map((id) => document.getElementById(id))
        .filter(Boolean).map(textOf).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    let label = null;
    if (el.id) {
      try { label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); } catch (e) { label = null; }
    }
    if (!label) label = el.closest('label');
    if (label && textOf(label)) return textOf(label);
    const fieldset = el.closest('fieldset');
    if (fieldset) { const legend = fieldset.querySelector('legend'); if (legend && textOf(legend)) return textOf(legend); }
    const group = el.closest('[role="radiogroup"], [role="group"]');
    if (group) {
      const heading = group.querySelector('legend, [data-testid*="label" i], label, p, span');
      if (heading && textOf(heading)) return textOf(heading);
    }
    return el.getAttribute('placeholder') || '';
  };
  const cssOf = (el) => {
    if (el.id) { try { return '#' + CSS.escape(el.id); } catch (e) { /* fall through */ } }
    const name = el.getAttribute('name');
    if (name) return el.tagName.toLowerCase() + '[name="' + name.replace(/"/g, '\\"') + '"]';
    return null;
  };
  const optionLabelOf = (el) => {
    let label = null;
    if (el.id) {
      try { label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); } catch (e) { label = null; }
    }
    if (!label) label = el.closest('label');
    if (label && textOf(label)) return textOf(label);
    return el.value || '';
  };
  const groupLabelOf = (el) => {
    const fieldset = el.closest('fieldset');
    if (fieldset) { const legend = fieldset.querySelector('legend'); if (legend && textOf(legend)) return textOf(legend); }
    const group = el.closest('[role="radiogroup"], [role="group"], [data-testid*="question" i], div');
    if (group) {
      const heading = group.querySelector('legend, [data-testid*="label" i], label, p, span, h3, h4');
      if (heading && textOf(heading) && textOf(heading).length < 200) return textOf(heading);
    }
    return '';
  };
  const controls = [];
  const elements = Array.from(document.querySelectorAll('input, textarea, select'));
  elements.forEach((el, index) => {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tag;
    if (['hidden', 'submit', 'button', 'reset', 'image'].indexOf(type) !== -1) return;
    const visible = isVisible(el);
    if (!visible && type !== 'file') return;
    if (el.disabled) return;
    const item = {
      kind: type, tag: tag,
      name: el.getAttribute('name') || '',
      id: el.id || '',
      label: labelOf(el),
      option_label: (type === 'radio' || type === 'checkbox') ? optionLabelOf(el) : '',
      group_label: (type === 'radio' || type === 'checkbox') ? groupLabelOf(el) : '',
      placeholder: el.getAttribute('placeholder') || '',
      required: !!el.required || el.getAttribute('aria-required') === 'true',
      visible: visible,
      value: type === 'file' ? '' : (el.value || ''),
      file_count: (type === 'file' && el.files) ? el.files.length : 0,
      file_name: (type === 'file' && el.files && el.files.length) ? el.files[0].name : '',
      checked: !!el.checked,
      multiple: !!el.multiple,
      css: cssOf(el),
      index: index,
      state_key: '',
      options: [],
      unsupported_reason: '',
    };
    if (tag === 'select') {
      item.options = Array.from(el.options).map((o) => ({
        value: o.value, label: textOf(o), disabled: !!o.disabled
      })).filter((o) => !(o.label === '' && (o.value === '' || o.value === '-1')));
    }
    try { el.setAttribute('data-jobpilot-idx', String(index)); item.state_key = String(index); }
    catch (e) { item.state_key = ''; }
    controls.push(item);
  });
  // custom widgets we cannot drive reliably
  const customs = [];
  document.querySelectorAll('[contenteditable="true"]').forEach((el) => {
    if (isVisible(el)) customs.push({ kind: 'contenteditable', label: labelOf(el), css: cssOf(el),
                                      unsupported_reason: 'rich-text editor (contenteditable)' });
  });
  document.querySelectorAll('div[role="combobox"], span[role="combobox"], [role="listbox"]').forEach((el) => {
    if (isVisible(el)) customs.push({ kind: 'custom-combobox', label: labelOf(el), css: cssOf(el),
                                      unsupported_reason: 'custom dropdown widget' });
  });
  document.querySelectorAll('input[role="combobox"]').forEach((el) => {
    if (isVisible(el)) customs.push({ kind: 'custom-combobox-input', label: labelOf(el), css: cssOf(el),
                                      unsupported_reason: 'custom combobox input' });
  });
  return { controls: controls, customs: customs };
}
"""

SUPPORTED_KINDS = {"text", "email", "tel", "textarea", "select", "select-one", "select-multiple",
                   "radio", "checkbox", "file", "phone", "number_ignored"}


def normalize_kind(kind: str) -> str:
    kind = (kind or "").lower()
    if kind in ("tel", "phone", "phonenumber"):
        return "phone"
    if kind == "select-one":
        return "select"
    if kind in ("textarea",):
        return "textarea"
    if kind in ("text", "email", "radio", "checkbox", "file", "select", "date", "number", "url",
                "search", "password", "time", "month", "week", "datetime-local", "range", "color"):
        return kind
    return kind or "text"


UNSUPPORTED_KINDS = {"date", "number", "url", "time", "month", "week", "datetime-local",
                     "range", "color", "password", "search"}


def normalize_field_text(field: Dict[str, Any]) -> str:
    parts = [field.get("label", ""), field.get("name", ""), field.get("placeholder", ""),
             field.get("id", "")]
    text = " ".join(part for part in parts if part)
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text).strip().lower()
    return re.sub(r"\s+", " ", text)


KNOWN_FIELD_PATTERNS: List[Tuple[str, str]] = [
    ("email", r"\be ?mail\b|email address"),
    ("first_name", r"\bfirst name\b|\bgiven name\b|\bfname\b|\bfirstname\b"),
    ("last_name", r"\blast name\b|\bfamily name\b|\bsurname\b|\blastname\b|\blname\b"),
    ("full_name", r"\bfull name\b|\byour name\b|^name$|\bname\b$|\bcandidate name\b"),
    ("phone", r"\bphone\b|\bmobile\b|\bcell\b|\btelephone\b|contact number"),
    ("location", r"\blocation\b|\bcity\b|current location|\baddress\b|where are you located|city state"),
    ("linkedin", r"linkedin"),
    ("github", r"github"),
    ("portfolio", r"\bportfolio\b|personal website|\bwebsite\b|personal site"),
    ("resume", r"\bresume\b|\bcv\b|curriculum vitae|upload.*(resume|cv|file)|attach"),
    ("years_experience", r"years? of experience|years? experience|experience in years|total experience"),
    ("salary_expectation", r"expected (salary|compensation|pay|rate)|desired (salary|compensation|pay|rate)|salary expectation"),
    ("cover_letter", r"cover letter|message to|anything else|additional information|why do you want"),
    ("education", r"\beducation\b|highest (degree|education)|degree"),
    ("company_name", r"current (company|employer)|present employer"),
    ("job_title", r"current (title|role|position)"),
]


def match_known_field(text: str) -> Optional[str]:
    for key, pattern in KNOWN_FIELD_PATTERNS:
        if re.search(pattern, text, re.I):
            return key
    return None


def detect_sensitive(text: str) -> Optional[str]:
    for key, pattern in SENSITIVE_PATTERNS:
        if re.search(pattern, text, re.I):
            return key
    return None


def deterministic_value(key: str, candidate: Dict[str, Any]) -> str:
    mapping = {
        "first_name": candidate.get("first_name", ""),
        "last_name": candidate.get("last_name", ""),
        "email": candidate.get("email", ""),
        "phone": candidate.get("phone", ""),
        "location": candidate.get("location", ""),
        "linkedin": candidate.get("linkedin", ""),
        "github": candidate.get("github", ""),
        "portfolio": candidate.get("portfolio", ""),
        "years_experience": ("" if candidate.get("years_experience") in (None, "")
                             else str(candidate.get("years_experience"))),
        "education": candidate.get("education", ""),
        "resume": candidate.get("resume_file", ""),
    }
    if key == "full_name":
        return " ".join(part for part in [candidate.get("first_name", ""),
                                          candidate.get("last_name", "")] if part).strip()
    return str(mapping.get(key, "") or "")


def sensitive_value(key: str, candidate: Dict[str, Any]) -> str:
    """Only values the user explicitly stored in their own profile."""
    if key == "work_authorization":
        return candidate.get("work_authorization", "")
    if key == "sponsorship":
        return candidate.get("sponsorship_required", "")
    if key == "veteran_status":
        return candidate.get("veteran_status", "")
    if key == "disability":
        return candidate.get("disability_status", "")
    if key == "demographic":
        parts = [p for p in (candidate.get("gender", ""), candidate.get("race", "")) if p]
        return " / ".join(parts)
    return ""

# ===========================================================================
# SECTION 15 - Dice session handling (login is always done by the human)
# ===========================================================================


LOGIN_CHECK_JS = r"""
() => {
  const text = (document.body ? document.body.innerText : '').toLowerCase();
  const labels = Array.from(document.querySelectorAll('a, button'))
    .map((el) => (el.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase())
    .filter(Boolean);
  const signedOut = labels.some((label) => label === 'sign in' || label === 'log in'
    || label === 'login' || label === 'sign up' || label === 'register');
  const signedIn = labels.some((label) => label.indexOf('sign out') !== -1
    || label.indexOf('log out') !== -1 || label === 'my profile' || label === 'dashboard');
  return { signedOut: signedOut, signedIn: signedIn, url: location.href };
}
"""


def is_signed_in(page) -> Optional[bool]:
    """True / False / None (unknown). Never guesses in favour of signed in."""
    try:
        state = page.evaluate(LOGIN_CHECK_JS) or {}
    except Exception:
        return None
    if state.get("signedIn"):
        return True
    if state.get("signedOut"):
        return False
    return None


def interactive_login(page, config: Config) -> bool:
    """Open Dice sign-in and let the user log in personally. No credentials touched."""
    UI.print("")
    UI.warn("You are not signed in to Dice.")
    UI.print("Sign in yourself in the browser window that opened.")
    UI.print("This program never asks for, prints or stores your Dice password.")
    try:
        page.goto(DICE_LOGIN_URL, wait_until="domcontentloaded",
                  timeout=config.nav_timeout * 1000)
    except Exception as exc:
        UI.warn(f"Could not open the Dice sign-in page: {first_line(str(exc), 160)}")
        UI.print("Open https://www.dice.com/dashboard/login manually in that browser window.")
    UI.print("Waiting for you to complete sign-in (Ctrl+C in this terminal aborts)...")
    waited = 0
    while waited < 300:
        if detect_challenge(page):
            challenge_stop({}, None, "signing in")
            return False
        if is_signed_in(page) is True:
            UI.ok("Dice session detected.")
            return True
        time.sleep(5)
        waited += 5
        if waited % 30 == 0:
            UI.info(f"Still waiting for sign-in... ({waited}s)")
    answer = UI.ask_yes_no("Could not confirm the Dice session automatically. Are you signed in now?",
                           default=None)
    return bool(answer)


# ===========================================================================
# SECTION 16 - the controlled Easy Apply flow
# ===========================================================================


@dataclass
class PlannedField:
    field: Dict[str, Any]
    question: str
    kind: str
    value: str = ""
    source: str = ""            # profile | profile:sensitive | profile:resume | ai | human | missing
    confidence: Optional[float] = None
    reason: str = ""
    sensitive: Optional[str] = None
    options: List[str] = field(default_factory=list)
    hint: str = ""
    resolved: bool = False

    @property
    def display_value(self) -> str:
        return self.value or "(empty)"


def group_key(field: Dict[str, Any]) -> str:
    """Identify the question a radio/checkbox belongs to (legend, then its own label)."""
    group = (field.get("group_label") or "").strip()
    label = (field.get("label") or "").strip()
    name = (field.get("name") or "").strip()
    return group or label or name or f"idx{field.get('index')}"


def map_years_to_options(years: Any, options: List[str]) -> Optional[str]:
    """Map a numeric years value onto options like "0-1", "2-3", "4-6", "7+"."""
    try:
        value = int(float(years))
    except (TypeError, ValueError):
        return None
    for option in options or []:
        numbers = [int(n) for n in re.findall(r"\d+", option or "")]
        if not numbers:
            continue
        low = numbers[0]
        high = numbers[1] if len(numbers) > 1 else None
        if high is not None and low <= value <= high:
            return option
        if high is None and ("+" in option or "plus" in option.lower() or "over" in option.lower()):
            if value >= low:
                return option
        elif high is None and value == low:
            return option
    return None


def best_option(value: str, options: List[str], threshold: float = 0.5) -> Optional[str]:
    """Pick the option that best matches a free-text value. Returns None when unsure,
    so the caller asks the human instead of guessing."""
    text = (value or "").strip().lower()
    if not text:
        return None
    best, best_score = None, 0.0
    for option in options:
        label = (option or "").strip().lower()
        if not label:
            continue
        if text == label:
            return option
        score = max(_ratio(text, label) * 0.95, _token_overlap(text, label))
        if contains_word(text, label) or contains_word(label, text):
            score = max(score, 0.9)
        for word_a in re.findall(r"[a-z0-9]+", text):
            for word_b in re.findall(r"[a-z0-9]+", label):
                if len(word_a) >= 4 and len(word_b) >= 4 and (
                        word_b.startswith(word_a) or word_a.startswith(word_b)):
                    score = max(score, 0.85)
        if score > best_score:
            best, best_score = option, score
    return best if best_score >= threshold else None


def build_planned_fields(controls: List[Dict[str, Any]], candidate: Dict[str, Any],
                         resume_path: Optional[Path]) -> Tuple[List[PlannedField], List[str]]:
    """Deterministic mapping first (spec section 15). Radio/checkbox options are grouped."""
    planned: List[PlannedField] = []
    problems: List[str] = []
    seen_groups: Dict[str, PlannedField] = {}

    for field in controls:
        kind = normalize_kind(field.get("kind", "text"))
        raw_label = " ".join([field.get("label", ""), field.get("group_label", ""),
                              field.get("placeholder", ""), field.get("name", "")]).strip()
        if kind in ("radio", "checkbox"):
            key = group_key(field)
            group = seen_groups.get(key)
            if group is None:
                group = PlannedField(field=field, question=key, kind=kind,
                                     sensitive=detect_sensitive(key))
                seen_groups[key] = group
                planned.append(group)
            option_text = (field.get("option_label") or field.get("label")
                           or field.get("value") or "").strip()
            if option_text and option_text not in group.options:
                group.options.append(option_text)
            continue

        text = normalize_field_text(field)
        known = match_known_field(text)
        sensitive = detect_sensitive(raw_label)
        question = (field.get("label") or field.get("placeholder") or field.get("name")
                    or field.get("id") or f"field #{field.get('index')}").strip()
        item = PlannedField(field=field, question=question, kind=kind, sensitive=sensitive)

        if kind == "file":
            if field.get("multiple"):
                problems.append(f"File upload '{question}' accepts multiple files.")
            if resume_path:
                item.value, item.source, item.resolved = str(resume_path), "profile:resume", True
            else:
                item.source = "missing"
        elif sensitive and sensitive_value(sensitive, candidate):
            stored = sensitive_value(sensitive, candidate)
            if kind == "select" and field.get("options"):
                item.options = [option["label"] or option["value"] for option in field["options"]]
            if kind in ("radio", "checkbox") or item.options:
                matched = best_option(stored, item.options) if item.options else None
                if matched:
                    item.value, item.source, item.resolved = matched, "profile:sensitive", True
                else:
                    item.hint, item.source = stored, "profile:sensitive"
            else:
                item.value, item.source, item.resolved = stored, "profile:sensitive", True
        elif known and deterministic_value(known, candidate):
            item.value = deterministic_value(known, candidate)
            item.source, item.resolved = "profile", True
            if kind == "select" and field.get("options"):
                item.options = [option["label"] or option["value"] for option in field["options"]]
                if best_option(item.value, item.options) is None:
                    mapped = map_years_to_options(item.value, item.options)
                    if mapped:
                        item.value = mapped
        elif kind == "select" and field.get("options"):
            item.options = [option["label"] or option["value"] for option in field["options"]]
            if known == "years_experience":
                mapped = map_years_to_options(candidate.get("years_experience"), item.options)
                if mapped:
                    item.value, item.source, item.resolved = mapped, "profile", True
        planned.append(item)

    # Radio/checkbox groups are built incrementally, so finish them here: a stored
    # sensitive answer is only used when it maps cleanly onto one of the options.
    for group in planned:
        if group.kind not in ("radio", "checkbox") or not group.sensitive:
            continue
        stored = sensitive_value(group.sensitive, candidate)
        if not stored:
            continue
        matched = best_option(stored, group.options)
        if matched:
            group.value, group.source, group.resolved = matched, "profile:sensitive", True
        else:
            group.hint, group.source = stored, "profile:sensitive"
    return planned, problems


def ask_human_for_field(item: PlannedField) -> None:
    UI.print("")
    UI.warn(f"Unresolved question: {item.question}")
    if item.sensitive:
        UI.print(f"    Sensitive/legal question ({item.sensitive.replace('_', ' ')}). "
                 "The AI is never allowed to answer it.")
    if item.hint:
        UI.print(f"    Your profile says: {item.hint}")
    if item.options:
        for index, option in enumerate(item.options, start=1):
            UI.print(f"      {index}. {option}")
        while True:
            raw = UI.ask("Your answer (number or exact text, or 'skip' to leave it empty)",
                         allow_empty=True)
            if raw.lower() in ("skip", ""):
                item.value, item.source, item.resolved = "", "missing", False
                return
            if raw.isdigit() and 1 <= int(raw) <= len(item.options):
                item.value, item.source, item.resolved = item.options[int(raw) - 1], "human", True
                return
            exact = next((option for option in item.options if option.lower() == raw.lower()), None)
            if exact:
                item.value, item.source, item.resolved = exact, "human", True
                return
            UI.warn("Please pick one of the listed options (or type skip).")
    else:
        raw = UI.ask("Your answer (or 'skip' to leave it empty)", allow_empty=True)
        if raw.lower() == "skip" or raw == "":
            item.value, item.source, item.resolved = "", "missing", False
        else:
            item.value, item.source, item.resolved = raw, "human", True


def resolve_planned_fields(planned: List[PlannedField], candidate: Dict[str, Any],
                           job: Dict[str, Any], ai: "AIClient", app_id: Optional[str]) -> None:
    """Fill the gaps: sensitive questions always go to the human, everything else may be
    answered by the AI (only when it is confident and validated), otherwise by the human."""
    for item in planned:
        if item.resolved or item.kind == "file":
            continue
        if item.sensitive:
            UI.info(f"Sensitive question needs your input ({item.sensitive.replace('_', ' ')}).")
            ask_human_for_field(item)
        elif ai.available:
            UI.info(f"Asking the AI about: {item.question[:80]}")
            result = ai.answer_question(item.question, item.options, candidate, job)
            if result:
                item.value = result["answer"]
                item.confidence = result["confidence"]
                item.reason = result["reason"]
                item.source, item.resolved = "ai", True
                UI.ok(f"AI answer: {item.value} (confidence {item.confidence:.2f}) - {item.reason}")
            else:
                UI.warn(f"AI could not answer this: {ai.last_error or 'no usable answer'}")
                UI.print("    Candidate information is missing - please provide the answer.")
                ask_human_for_field(item)
        else:
            UI.info("AI is not configured, so this question goes to you.")
            ask_human_for_field(item)
        if item.resolved and app_id:
            store_answer(app_id, item.question, item.value, item.source, item.confidence)


def _locate_control(page, field: Dict[str, Any]):
    """Robust locator: DOM marker first, then id/name/CSS, then nothing."""
    if field.get("state_key"):
        try:
            locator = page.locator(f'[data-jobpilot-idx="{field["state_key"]}"]')
            if locator.count() == 1:
                return locator
        except Exception:
            pass
    if field.get("css"):
        try:
            locator = page.locator(field["css"])
            if locator.count() == 1:
                return locator
        except Exception:
            pass
    if field.get("name"):
        try:
            locator = page.locator(f'[name="{field["name"]}"]')
            if locator.count() == 1:
                return locator
        except Exception:
            pass
    return None


def _radio_options(field: Dict[str, Any]) -> List[Dict[str, Any]]:
    found = field.get("_options") or []
    if found:
        return found
    single = {"value": field.get("value", ""), "option_label": field.get("option_label", "")}
    return [single] if single["value"] else []


def fill_planned_fields(page, planned: List[PlannedField]) -> List[str]:
    """Fill the page with explicit waits. Returns a list of human-readable problems."""
    problems: List[str] = []
    for item in planned:
        if not item.resolved and item.kind != "file":
            continue
        field = item.field
        locator = _locate_control(page, field)
        if locator is None:
            problems.append(f"Could not locate the field '{item.question}' at fill time.")
            continue
        try:
            if item.kind == "file":
                if not item.value:
                    problems.append(f"No resume file available for '{item.question}'.")
                    continue
                locator.set_input_files(item.value, timeout=20000)
            elif item.kind in ("text", "email", "phone", "textarea"):
                locator.fill("", timeout=15000)
                locator.fill(item.value, timeout=15000)
            elif item.kind == "select":
                filled = False
                try:
                    locator.select_option(label=item.value, timeout=10000)
                    filled = True
                except Exception:
                    filled = False
                if not filled:
                    option_labels = [(option.get("label") or option.get("value") or "")
                                     for option in (field.get("options") or [])]
                    chosen = best_option(item.value, option_labels) \
                        or map_years_to_options(item.value, option_labels)
                    for option in (field.get("options") or []):
                        label = (option.get("label") or "").strip()
                        value = (option.get("value") or "").strip()
                        if chosen and label == chosen and value:
                            locator.select_option(value=value, timeout=10000)
                            filled = True
                            break
                if not filled:
                    problems.append(f"Could not select '{item.value}' for '{item.question}' "
                                    f"(available: {', '.join(item.options[:8])}).")
            elif item.kind == "radio":
                options = _radio_options(field)
                labels = [option.get("option_label") or option.get("value") or "" for option in options]
                chosen = best_option(item.value, labels)
                if chosen is None:
                    problems.append(f"Could not match the answer '{item.value}' to the options of "
                                    f"'{item.question}' ({', '.join(labels[:6])}).")
                else:
                    target = next((option for option in options
                                   if (option.get("option_label") or "") == chosen
                                   or (option.get("value") or "") == chosen), None)
                    name = (target or {}).get("name") or field.get("name") or ""
                    value = (target or {}).get("value") or ""
                    if not name or not value:
                        problems.append(f"Could not determine the radio option to select for "
                                        f"'{item.question}'.")
                    else:
                        radio = page.locator(f'input[name="{name}"][value="{value}"]')
                        radio.first.check(timeout=10000)
            elif item.kind == "checkbox":
                target = item.value.strip().lower()
                if target.startswith(("yes", "true", "1", "y", "agree", "i agree")):
                    locator.check(timeout=10000)
                elif target:
                    problems.append(f"Left checkbox '{item.question}' unticked: the answer "
                                    f"'{item.value}' is not a clear yes.")
                else:
                    problems.append(f"Checkbox '{item.question}' has no answer; left unticked.")
            else:
                problems.append(f"Unsupported control type '{item.kind}' for '{item.question}'.")
        except Exception as exc:
            problems.append(f"Could not fill '{item.question}': {first_line(str(exc), 160)}")
    return problems


def inspect_page(page) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    data = page.evaluate(INSPECT_JS) or {}
    return data.get("controls") or [], data.get("customs") or []


def attach_radio_options(controls: List[Dict[str, Any]]) -> None:
    """Give every radio control access to all siblings of its group."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for field in controls:
        if normalize_kind(field.get("kind")) == "radio":
            groups.setdefault(group_key(field), []).append(field)
    for group_fields in groups.values():
        payload = [{"value": f.get("value", ""),
                    "option_label": f.get("option_label") or f.get("value", ""),
                    "name": f.get("name", "")} for f in group_fields]
        for field in group_fields:
            field["_options"] = payload


def render_application_review(job: Dict[str, Any], planned: List[PlannedField],
                              problems: List[str], unsupported: List[str]) -> None:
    UI.print("")
    UI.print("-----------------------------------------------")
    UI.print("APPLICATION REVIEW")
    UI.print("-----------------------------------------------")
    UI.print("")
    UI.print(f"Job: {job.get('title','')}")
    UI.print(f"Company: {job.get('company','')}")
    UI.print(f"Location: {job.get('location','') or '-'}")
    UI.print(f"Apply type: {job.get('application_type','')}")
    UI.print(f"URL: {job.get('source_url') or job.get('application_url')}")
    UI.print("")

    rows = []
    for item in planned:
        if item.kind == "file" and item.value:
            rows.append([item.question[:44], Path(item.value).name, "resume file"])
        elif item.resolved:
            rows.append([item.question[:44], item.display_value[:56], item.source])
        else:
            rows.append([item.question[:44], "(left empty)", "no answer"])
    UI.table(["Field / Question", "Value", "Source"], rows)

    ai_items = [item for item in planned if item.source == "ai"]
    if ai_items:
        UI.print("")
        UI.print("AI Questions:")
        for item in ai_items:
            UI.print(f"  Question:   {item.question[:100]}")
            UI.print(f"  Answer:     {item.value[:100]}")
            confidence = f"{item.confidence:.2f}" if item.confidence is not None else "n/a"
            UI.print(f"  Confidence: {confidence}  ({item.reason[:120]})")
            UI.print("")

    sensitive_items = [item for item in planned if item.sensitive]
    if sensitive_items:
        UI.print("Sensitive / legal questions (never decided by the AI):")
        UI.table(["Question", "Answer", "Source", "Category"],
                 [[item.question[:40], item.display_value[:44] or "(no answer)",
                   item.source or "-", item.sensitive.replace("_", " ")] for item in sensitive_items])

    unresolved = [item for item in planned if not item.resolved and item.kind != "file"]
    if unresolved:
        UI.warn(f"{len(unresolved)} question(s) will be left EMPTY because no answer was available.")
    if problems:
        UI.error("Problems detected before submission:")
        for problem in problems:
            UI.print(f"    - {problem}")
    if unsupported:
        UI.error("Unsupported application field detected.")
        for item in unsupported:
            UI.print(f"    - {item}")
    UI.print("-----------------------------------------------")


def validate_required(controls: List[Dict[str, Any]], planned: List[PlannedField]) -> List[str]:
    """Check that every required control really holds a value now."""
    gaps: List[str] = []
    for field in controls:
        kind = normalize_kind(field.get("kind"))
        if kind in ("radio", "checkbox"):
            group = group_key(field)
            group_fields = [f for f in controls if group_key(f) == group]
            if any(f.get("checked") for f in group_fields):
                continue
            if any(f.get("required") for f in group_fields) or (group_fields and
                                                               group_fields[0].get("required")):
                gaps.append(f"Required question is unanswered: {group[:80]}")
            continue
        if not field.get("required"):
            continue
        if kind == "file":
            has_file = bool(field.get("file_count")) or bool((field.get("value") or "").strip())
            if not has_file:
                gaps.append("Required file upload is empty: "
                            + str(field.get("label") or field.get("name") or "resume")[:60])
            continue
        if not (field.get("value") or "").strip():
            label = (field.get("label") or field.get("name") or field.get("id") or "?")
            gaps.append(f"Required field is empty: {str(label)[:80]}")
    deduped: List[str] = []
    for gap in gaps:
        if gap not in deduped:
            deduped.append(gap)
    return deduped


FIND_APPLY_JS = r"""
() => {
  const textOf = (el) => (el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '');
  const buttons = Array.from(document.querySelectorAll('button, a[role="button"], a[data-testid*="apply" i]'));
  const visible = buttons.filter((b) => {
    const r = b.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && !b.disabled;
  });
  let chosen = null;
  for (const b of visible) {
    if (/easy\s*apply/i.test(textOf(b))) { chosen = b; break; }
  }
  if (!chosen) {
    for (const b of visible) {
      const label = textOf(b);
      if (/^apply\b|apply now|apply for|apply on/i.test(label) && label.length < 60) { chosen = b; break; }
    }
  }
  const forms = document.querySelectorAll('form');
  let formControls = 0;
  forms.forEach((f) => { formControls += f.querySelectorAll('input, textarea, select').length; });
  formControls = Math.max(formControls, document.querySelectorAll('form input, form textarea, form select, input[type=file]').length);
  return {
    found: !!chosen,
    label: chosen ? textOf(chosen) : '',
    text: (document.body ? document.body.innerText : '').slice(0, 4000),
    has_form: forms.length > 0,
    form_controls: formControls,
  };
}
"""

FIND_SUBMIT_JS = r"""
() => {
  const textOf = (el) => (el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '');
  const buttons = Array.from(document.querySelectorAll('button, input[type=submit], a[role="button"]'));
  const visible = buttons.filter((b) => {
    const r = b.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && !b.disabled && b.getAttribute('aria-disabled') !== 'true';
  });
  const patterns = [/submit application/i, /^submit$/i, /submit and apply/i, /send application/i,
                    /finish and submit/i, /^submit\b/i, /^apply now$/i];
  for (const pattern of patterns) {
    for (const b of visible) {
      const label = textOf(b) || b.getAttribute('value') || '';
      if (pattern.test(label) && label.length < 60) {
        return { found: true, label: label };
      }
    }
  }
  return { found: false, label: '' };
}
"""


def _button_locator(page, label: str):
    queries = (f'button:has-text("{label}")', f'input[type=submit][value="{label}"]',
               f'a:has-text("{label}")', f'[role="button"]:has-text("{label}")')
    for query in queries:
        try:
            locator = page.locator(query)
            if locator.count() >= 1:
                return locator.first
        except Exception:
            continue
    return None


def find_apply_control(page) -> Dict[str, Any]:
    try:
        info = page.evaluate(FIND_APPLY_JS) or {}
    except Exception as exc:
        log_event("find_apply_control", "warn", error=first_line(str(exc), 160))
        info = {}
    label = info.get("label") or ""
    info["locator"] = _button_locator(page, label) if (info.get("found") and label) else None
    if info.get("found") and info["locator"] is None:
        info["found"] = False
    return info


def find_submit_control(page) -> Dict[str, Any]:
    try:
        info = page.evaluate(FIND_SUBMIT_JS) or {}
    except Exception as exc:
        log_event("find_submit_control", "warn", error=first_line(str(exc), 160))
        info = {}
    label = info.get("label") or ""
    info["locator"] = _button_locator(page, label) if (info.get("found") and label) else None
    if info.get("found") and info["locator"] is None:
        info["found"] = False
    return info


CONFIRMATION_RE = re.compile(
    r"(application (has been )?(submitted|received|sent)|thank you for applying|"
    r"thanks for applying|we(?:'ve| have) received your application|"
    r"your application was submitted|successfully applied|application complete)", re.I)


def verify_submission(page) -> Tuple[bool, str]:
    """Only report success on real evidence found on the page. No optimistic assumptions."""
    try:
        page.wait_for_timeout(1500)
        text = page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 8000)") or ""
    except Exception:
        return False, ""
    match = CONFIRMATION_RE.search(text)
    if match:
        snippet = clean_text(text[max(0, match.start() - 80):match.end() + 140], 260)
        return True, snippet
    return False, ""


def run_easy_apply(config: Config, ai: AIClient, candidate: Dict[str, Any],
                   job: Dict[str, Any], app_id: Optional[str] = None) -> str:
    """The whole controlled workflow (spec section 13). Returns the final state."""
    resume_path = Path(candidate["resume_file"]) if candidate.get("resume_file") else None
    if resume_path and not resume_path.exists():
        UI.warn(f"The stored resume file is missing: {resume_path}")
        resume_path = None
    if not resume_path:
        UI.warn("No resume file is set in your profile, so no resume will be uploaded.")

    application = get_application(app_id) if app_id else create_application(job, ST_SELECTED)
    app_id = application["id"]
    set_state(app_id, ST_IN_PROGRESS)

    if not HAVE_PLAYWRIGHT:
        UI.error("Playwright is not installed, so the application workflow cannot run.")
        UI.print("Fix: pip install playwright   then   playwright install chromium")
        set_state(app_id, ST_FAILED, error="Playwright not installed")
        return ST_FAILED

    page = None
    local_demo_page = (job.get("source_url") or "").startswith("file://")
    with sync_playwright() as play:
        try:
            browser, context = launch_browser(play, config, for_apply=True,
                                              headless_override=True if local_demo_page else None)
        except Exception as exc:
            UI.error("Browser launch failed.")
            UI.print(f"Details: {first_line(str(exc), 200)}")
            UI.print("Fix: playwright install chromium   (and make sure nothing blocks it)")
            set_state(app_id, ST_FAILED, error=f"browser launch failed: {first_line(str(exc), 200)}")
            return ST_FAILED

        try:
            page = context.new_page()
            url = job.get("source_url") or job.get("application_url")
            if not url:
                UI.error("This job has no URL stored, so nothing can be opened.")
                set_state(app_id, ST_FAILED, error="job has no URL")
                return ST_FAILED

            UI.info(f"Opening: {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=config.nav_timeout * 1000)
                page.wait_for_timeout(1500)
            except Exception as exc:
                UI.print("")
                UI.error("Could not open the job page.")
                UI.print("Possible causes:")
                UI.print("  - network / DNS problem")
                UI.print("  - the site is unavailable right now")
                UI.print("  - the stored URL is no longer valid")
                UI.print(f"Details: {first_line(str(exc), 200)}")
                UI.print("The application was not submitted.")
                screenshot(page, "navigation-failed")
                set_state(app_id, ST_FAILED, error=f"navigation failed: {first_line(str(exc), 200)}")
                return ST_FAILED

            if detect_challenge(page):
                screenshot(page, "challenge-open")
                challenge_stop(job, app_id, "opening the job page")
                return ST_REVIEW

            if is_signed_in(page) is False:
                if not interactive_login(page, config):
                    UI.warn("Sign-in was not completed. Stopping - nothing was submitted.")
                    set_state(app_id, ST_REVIEW, error="sign-in not completed",
                              flags=["HUMAN_REVIEW_REQUIRED"])
                    return ST_REVIEW
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=config.nav_timeout * 1000)
                    page.wait_for_timeout(1200)
                except Exception as exc:
                    UI.warn(f"Could not return to the job page: {first_line(str(exc), 160)}")
                    set_state(app_id, ST_FAILED, error="could not reopen job page after sign-in")
                    return ST_FAILED
            if not local_demo_page:
                try:
                    save_browser_state(context)
                except Exception:
                    pass

            # --- apply entry point -------------------------------------------
            apply_info = find_apply_control(page)
            page_type = classify_apply_label(apply_info.get("label", ""), apply_info.get("text", ""))
            if page_type == AT_EXTERNAL:
                job["application_type"] = AT_EXTERNAL
                update_job(job)
                UI.print("")
                UI.warn("External application — not supported by this MVP.")
                UI.print("This job is applied for on the company's own site. Nothing was submitted.")
                set_state(app_id, ST_REVIEW,
                          error="external application - not supported by this MVP",
                          flags=["HUMAN_REVIEW_REQUIRED"])
                return ST_REVIEW

            form_present = bool(apply_info.get("has_form")) and apply_info.get("form_controls", 0) >= 1
            if apply_info.get("found") and not form_present:
                UI.info(f"Clicking the apply button (\u201c{apply_info.get('label')}\u201d)...")
                try:
                    apply_info["locator"].click(timeout=20000)
                    page.wait_for_timeout(2500)
                except Exception as exc:
                    UI.error("The apply button could not be clicked: " + first_line(str(exc), 160))
                    UI.print("Nothing was submitted.")
                    screenshot(page, "apply-click-failed")
                    set_state(app_id, ST_FAILED, error="apply click failed")
                    return ST_FAILED
                if "dice.com" not in (page.url or ""):
                    job["application_type"] = AT_EXTERNAL
                    update_job(job)
                    UI.print("")
                    UI.warn("External application — not supported by this MVP.")
                    UI.print(f"The click moved the browser to {page.url[:120]} (outside Dice).")
                    UI.print("Nothing was submitted.")
                    set_state(app_id, ST_REVIEW, error="redirected off Dice",
                              flags=["HUMAN_REVIEW_REQUIRED"])
                    return ST_REVIEW
            elif not apply_info.get("found") and not form_present:
                UI.print("")
                UI.error("No supported apply control was found on this page.")
                UI.print("Possible causes:")
                UI.print("  - this job is applied for on the company's own site")
                UI.print("  - you are not signed in to Dice")
                UI.print("  - Dice changed its page structure")
                UI.print("Nothing was submitted. This job is marked for human review.")
                screenshot(page, "no-apply-control")
                set_state(app_id, ST_REVIEW, error="no supported apply control found",
                          flags=["HUMAN_REVIEW_REQUIRED"])
                return ST_REVIEW

            if detect_challenge(page):
                screenshot(page, "challenge-apply")
                challenge_stop(job, app_id, "opening the application form")
                return ST_REVIEW

            if job.get("application_type") in (AT_UNKNOWN, ""):
                job["application_type"] = AT_EASY_APPLY if page_type == AT_EASY_APPLY else AT_UNKNOWN
                update_job(job)

            # --- inspect ------------------------------------------------------
            try:
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(800)
                controls, customs = inspect_page(page)
            except Exception as exc:
                UI.error("Could not inspect the application form: " + first_line(str(exc), 160))
                screenshot(page, "inspect-failed")
                set_state(app_id, ST_FAILED, error="form inspection failed")
                return ST_FAILED

            if not controls:
                UI.print("")
                UI.error("No supported form fields were found on the application page.")
                UI.print("Possible causes:")
                UI.print("  - the Easy Apply form did not load")
                UI.print("  - this listing is applied for externally")
                UI.print("  - the page uses widgets this MVP does not support")
                UI.print("Nothing was submitted. Marked for human review.")
                screenshot(page, "no-fields")
                set_state(app_id, ST_REVIEW, error="no supported form fields detected",
                          flags=["HUMAN_REVIEW_REQUIRED"])
                return ST_REVIEW

            unsupported: List[str] = []
            for custom in customs:
                unsupported.append(f"{custom.get('unsupported_reason', 'unsupported widget')}: "
                                   f"{custom.get('label') or custom.get('kind') or 'unnamed'}")
            supported_controls = []
            for field in controls:
                kind = normalize_kind(field.get("kind", "text"))
                label = str(field.get("label") or field.get("name") or field.get("id") or "unnamed")[:60]
                if kind in UNSUPPORTED_KINDS:
                    unsupported.append(f"input type '{kind}' is not supported: {label}")
                    continue
                if kind not in ("text", "email", "phone", "textarea", "select", "radio",
                                "checkbox", "file"):
                    unsupported.append(f"unknown control type '{kind}': {label}")
                    continue
                supported_controls.append(field)

            if unsupported:
                UI.print("")
                UI.error("Unsupported application field detected.")
                for item in unsupported:
                    UI.print(f"    - {item}")
                UI.print("Automation stopped. Nothing was submitted. This application needs a human.")
                screenshot(page, "unsupported-fields")
                set_state(app_id, ST_REVIEW,
                          error="unsupported application field detected",
                          flags=["HUMAN_REVIEW_REQUIRED"])
                return ST_REVIEW

            attach_radio_options(supported_controls)
            planned, problems = build_planned_fields(supported_controls, candidate, resume_path)
            mapped = [item for item in planned if item.resolved and item.source != "ai"]
            UI.info(f"{len(supported_controls)} supported control(s); {len(planned)} question(s)/field(s); "
                    f"{len(mapped)} mapped directly from your profile.")

            # --- resolve the unknowns (AI only for non-sensitive semantic questions)
            resolve_planned_fields(planned, candidate, job, ai, app_id)

            # --- fill ---------------------------------------------------------
            UI.info("Filling the form...")
            problems += fill_planned_fields(page, planned)
            try:
                page.wait_for_timeout(1000)
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            # --- validate required fields -------------------------------------
            try:
                after_controls, _ = inspect_page(page)
                attach_radio_options(after_controls)
            except Exception:
                after_controls = supported_controls
            problems += validate_required(after_controls, planned)

            # --- review -------------------------------------------------------
            render_application_review(job, planned, problems, unsupported)

            if problems:
                UI.print("")
                UI.error("Required or incorrectly filled fields remain:")
                for problem in problems:
                    UI.print(f"    - {problem}")
                UI.print("Automation stopped before submission. Nothing was submitted.")
                screenshot(page, "validation-failed")
                set_state(app_id, ST_REVIEW, error="; ".join(problems)[:600],
                          flags=["HUMAN_REVIEW_REQUIRED"])
                return ST_REVIEW

            if config.demo:
                UI.print("")
                UI.warn("DEMO MODE: this is a local demo form. Submission is disabled in demo mode.")
                set_state(app_id, ST_READY, note="demo mode: form prepared, never submitted")
                return ST_READY

            set_state(app_id, ST_READY, note="form filled, waiting for your confirmation")
            UI.print("")
            UI.print("Submit application?")
            UI.print("Type YES to submit. Anything else cancels.")
            if not UI.confirm_exact("Type YES to submit", expected="YES"):
                UI.warn("Cancelled. Nothing was submitted.")
                set_state(app_id, ST_CANCELLED, note="cancelled at the confirmation step")
                return ST_CANCELLED

            # --- the only place that can submit --------------------------------
            if detect_challenge(page):
                screenshot(page, "challenge-before-submit")
                challenge_stop(job, app_id, "preparing to submit")
                return ST_REVIEW

            submit_info = find_submit_control(page)
            if not submit_info.get("found"):
                UI.error("The submit button could not be found, so nothing was submitted.")
                UI.print("The filled form is still open in the browser window if you want to finish "
                         "manually.")
                screenshot(page, "no-submit-button")
                set_state(app_id, ST_REVIEW, error="submit button not found",
                          flags=["HUMAN_REVIEW_REQUIRED"])
                return ST_REVIEW

            UI.info(f"Submitting (\u201c{submit_info.get('label')}\u201d)...")
            try:
                submit_info["locator"].click(timeout=20000)
            except Exception as exc:
                UI.error("Clicking submit failed: " + first_line(str(exc), 200))
                UI.print("The application may NOT have been submitted - check Dice yourself.")
                screenshot(page, "submit-click-failed")
                set_state(app_id, ST_FAILED, error="submit click failed")
                return ST_FAILED

            try:
                page.wait_for_timeout(3000)
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass

            if detect_challenge(page):
                screenshot(page, "challenge-after-submit")
                challenge_stop(job, app_id, "submitting the application")
                return ST_REVIEW

            verified, evidence = verify_submission(page)
            if verified:
                UI.ok("Submission confirmed by the site: " + evidence)
                set_state(app_id, ST_SUBMITTED, note=evidence)
                log_event("application_submitted", "submitted", job_id=job.get("id"),
                          application_id=app_id)
                return ST_SUBMITTED
            UI.warn("The submission was NOT confirmed.")
            UI.print("The click was sent, but no confirmation text appeared on the page.")
            UI.print("Check the application status on the site yourself before applying again.")
            screenshot(page, "submission-unverified")
            set_state(app_id, ST_REVIEW,
                      error="submission could not be verified automatically",
                      note="submit clicked, confirmation not detected",
                      flags=["SUBMISSION_UNVERIFIED"])
            return ST_REVIEW

        except KeyboardInterrupt:
            UI.print("")
            UI.warn("Interrupted by you. The form was left open and nothing was submitted.")
            set_state(app_id, ST_CANCELLED, note="interrupted by the user")
            raise
        except Exception as exc:
            UI.print("")
            UI.error("The application workflow stopped because of an unexpected problem.")
            UI.print(f"Details: {first_line(str(exc), 300)}")
            UI.print("The application was not confirmed as submitted.")
            if DEBUG:
                traceback.print_exc()
            try:
                screenshot(page, "unexpected-error")
            except Exception:
                pass
            set_state(app_id, ST_FAILED, error=first_line(str(exc), 400))
            log_event("apply_flow", "error", job_id=job.get("id"), application_id=app_id,
                      error=first_line(str(exc), 400))
            return ST_FAILED
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


# ===========================================================================
# SECTION 17 - demo mode (explicitly opt-in; nothing is ever submitted)
# ===========================================================================


def demo_jobs() -> List[Dict[str, Any]]:
    """Clearly labelled demo records. Reachable only with --demo."""
    stamp = now_iso()
    return [
        normalize_job({
            "id": "demo-backend-001",
            "title": "Backend Python Developer (DEMO)",
            "company": "DEMO - Northwind Analytics (not a real listing)",
            "location": "Austin, TX",
            "description": "DEMO DATA. Python services with FastAPI, PostgreSQL and Docker. "
                           "3+ years of experience required. Hybrid role in Austin, TX. "
                           "Requirements: Python, FastAPI, PostgreSQL, Docker, REST APIs.",
            "requirements": "3+ years Python, FastAPI, PostgreSQL, Docker, REST APIs",
            "skills": ["Python", "FastAPI", "PostgreSQL", "Docker", "REST"],
            "salary": "USD 110,000 - 130,000 per year",
            "employment_type": "FULLTIME",
            "workplace_type": "Hybrid",
            "posted_date": stamp[:10],
            "application_type": AT_EASY_APPLY,
            "source_url": "demo://local/demo-form",
            "source": "demo",
        }),
        normalize_job({
            "id": "demo-react-002",
            "title": "React Frontend Developer (DEMO)",
            "company": "DEMO - Brightline Labs (not a real listing)",
            "location": "Remote",
            "description": "DEMO DATA. React and TypeScript product work. 5+ years of experience. "
                           "Fully remote. Skills: React, TypeScript, GraphQL, Jest.",
            "requirements": "5+ years React, TypeScript, GraphQL",
            "skills": ["React", "TypeScript", "GraphQL", "Jest"],
            "salary": "USD 120,000 - 150,000 per year",
            "employment_type": "FULLTIME",
            "workplace_type": "Remote",
            "posted_date": stamp[:10],
            "application_type": AT_EASY_APPLY,
            "source_url": "demo://local/demo-form",
            "source": "demo",
        }),
        normalize_job({
            "id": "demo-data-003",
            "title": "Data Engineer (DEMO)",
            "company": "DEMO - Contoso Retail (not a real listing)",
            "location": "Chicago, IL",
            "description": "DEMO DATA. Spark and Airflow pipelines on AWS. 4+ years experience. "
                           "Onsite. Skills: Spark, Airflow, AWS, SQL, Python.",
            "requirements": "4+ years Spark, Airflow, AWS, SQL",
            "skills": ["Spark", "Airflow", "AWS", "SQL", "Python"],
            "salary": "USD 130,000 - 145,000 per year",
            "employment_type": "FULLTIME",
            "workplace_type": "Onsite",
            "posted_date": stamp[:10],
            "application_type": AT_EXTERNAL,
            "source_url": "demo://local/external",
            "source": "demo",
        }),
    ]


DEMO_FORM_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>JOBPILOT demo application form</title>
<style>body{font-family:Segoe UI,Arial,sans-serif;margin:32px;max-width:760px}
label{display:block;margin:12px 0 4px;font-weight:600}small{color:#666}
fieldset{margin:16px 0;border:1px solid #ccc;padding:12px}input[type=text],input[type=email],
input[type=tel],textarea,select{width:100%;padding:6px;box-sizing:border-box}</style></head>
<body>
<h1>DEMO application form (local file, not Dice)</h1>
<p><small>Created only for <code>--demo</code> runs. It is a plain local HTML file and nothing
here is sent anywhere.</small></p>
<form onsubmit="document.getElementById('out').textContent='demo: submission disabled';return false;">
  <label for="firstName">First Name</label><input id="firstName" name="firstName" type="text" required>
  <label for="lastName">Last Name</label><input id="lastName" name="lastName" type="text" required>
  <label for="email">Email</label><input id="email" name="email" type="email" required>
  <label for="phone">Phone</label><input id="phone" name="phone" type="tel">
  <label for="location">Location</label><input id="location" name="location" type="text">
  <label for="resume">Resume</label><input id="resume" name="resume" type="file" required>
  <label for="years">Years of Experience</label>
  <select id="years" name="years" required><option value="">Select</option>
    <option>0-1</option><option>2-3</option><option>4-6</option><option>7+</option></select>
  <fieldset><legend>Are you authorized to work in the country of this job?</legend>
    <label><input type="radio" name="authorized" value="Yes" required> Yes</label>
    <label><input type="radio" name="authorized" value="No"> No</label></fieldset>
  <fieldset><legend>What is your highest level of education?</legend>
    <label><input type="radio" name="education" value="Bachelors"> Bachelors</label>
    <label><input type="radio" name="education" value="Masters"> Masters</label>
    <label><input type="radio" name="education" value="Doctorate"> Doctorate</label></fieldset>
  <label for="why">Why are you a good fit for this role?</label>
  <textarea id="why" name="why" rows="3"></textarea>
  <label><input type="checkbox" name="agree" value="agree" required> I agree the information is accurate</label>
  <p><button type="submit">Submit Application</button>
  <button type="button" id="cancel">Cancel</button></p>
</form>
<p id="out"></p>
</body></html>
"""


def write_demo_form() -> Path:
    path = DATA_DIR / "demo-application-form.html"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEMO_FORM_HTML, encoding="utf-8")
    except OSError as exc:
        UI.error(f"Could not write the demo form: {exc}")
    return path


def demo_note() -> None:
    UI.warn("DEMO MODE is ON (--demo).")
    UI.print("    - jobs shown are clearly labelled DEMO records, never real listings")
    UI.print("    - the apply flow uses a local HTML form and cannot submit anything")
    UI.print("    - normal runs never touch this data")


# ===========================================================================
# SECTION 18 - terminal menus
# ===========================================================================


def menu_setup_candidate(config: Config) -> None:
    """Menu 1: create / update the candidate profile. Never invents values."""
    while True:
        candidate = load_candidate()
        UI.banner("Candidate profile",
                  "Saved to data/candidate.json - empty fields stay empty, nothing is guessed.")
        if candidate_has_profile(candidate):
            UI.table(["Field", "Value"], [
                ["Name", f"{candidate.get('first_name','')} {candidate.get('last_name','')}".strip()],
                ["Email", candidate.get("email", "")],
                ["Phone", candidate.get("phone", "")],
                ["Location", candidate.get("location", "")],
                ["Target roles", ", ".join(candidate.get("target_roles") or [])],
                ["Years of experience", candidate.get("years_experience")],
                ["Skills", ", ".join((candidate.get("skills") or [])[:12])],
                ["Education", clean_text(candidate.get("education", ""), 60)],
                ["Work authorization", candidate.get("work_authorization", "")],
                ["Sponsorship required", candidate.get("sponsorship_required", "")],
                ["Resume file", candidate.get("resume_file", "")],
            ])
        else:
            UI.warn("No candidate profile yet.")
        options = ["Edit basic details (name, email, phone, location)",
                   "Edit target roles, skills, experience, work modes",
                   "Edit education, certifications, work experience, projects",
                   "Edit links (LinkedIn, GitHub, portfolio) and resume file",
                   "Edit work authorization / sponsorship / voluntary self-identification",
                   "Import from a resume file",
                   "Delete the profile"]
        choice = UI.choose("Select", options)
        if choice is None:
            return
        try:
            if choice == 0:
                candidate["first_name"] = UI.ask("First name", candidate.get("first_name", ""))
                candidate["last_name"] = UI.ask("Last name", candidate.get("last_name", ""))
                candidate["email"] = UI.ask("Email", candidate.get("email", ""))
                candidate["phone"] = UI.ask("Phone", candidate.get("phone", ""))
                candidate["location"] = UI.ask("Location (City, ST)", candidate.get("location", ""))
            elif choice == 1:
                candidate["target_roles"] = UI.ask_list("Target roles (comma separated)",
                                                        candidate.get("target_roles"))
                candidate["skills"] = UI.ask_list("Skills (comma separated)", candidate.get("skills"))
                candidate["years_experience"] = UI.ask_int("Years of experience",
                                                           candidate.get("years_experience") or 0,
                                                           0, 60)
                modes = UI.ask_list("Preferred work modes: Remote / Hybrid / Onsite",
                                    candidate.get("work_modes"))
                candidate["work_modes"] = [m for m in modes
                                           if m.lower() in ("remote", "hybrid", "onsite", "on-site")]
                employment = UI.ask("Preferred employment type (FULLTIME/CONTRACT/PARTTIME)",
                                    candidate.get("employment_type", "FULLTIME"))
                candidate["employment_type"] = employment.upper()
            elif choice == 2:
                candidate["education"] = UI.ask("Highest education (one line)",
                                                clean_text(candidate.get("education", ""), 200))
                candidate["certifications"] = UI.ask_list("Certifications (comma separated)",
                                                          candidate.get("certifications"))
                if UI.ask_yes_no("Replace your work experience list?", default=False):
                    entries = []
                    UI.print("Leave the job title empty to finish.")
                    while True:
                        title = UI.ask("Job title", allow_empty=True)
                        if not title:
                            break
                        entries.append({"title": title,
                                        "company": UI.ask("Company", allow_empty=True),
                                        "start": UI.ask("Start (e.g. 2021-03)", allow_empty=True),
                                        "end": UI.ask("End (or 'present')", allow_empty=True),
                                        "summary": UI.ask("Short summary", allow_empty=True)})
                    candidate["work_experience"] = entries
                if UI.ask_yes_no("Replace your projects list?", default=False):
                    projects = []
                    UI.print("Leave the project name empty to finish.")
                    while True:
                        name = UI.ask("Project name", allow_empty=True)
                        if not name:
                            break
                        projects.append({"name": name,
                                         "description": UI.ask("Description", allow_empty=True),
                                         "link": UI.ask("Link", allow_empty=True)})
                    candidate["projects"] = projects
            elif choice == 3:
                candidate["linkedin"] = UI.ask("LinkedIn URL", candidate.get("linkedin", ""))
                candidate["github"] = UI.ask("GitHub URL", candidate.get("github", ""))
                candidate["portfolio"] = UI.ask("Portfolio / website URL", candidate.get("portfolio", ""))
                uploads = sorted(UPLOADS_DIR.glob("*.pdf")) + sorted(UPLOADS_DIR.glob("*.docx"))
                default_resume = candidate.get("resume_file") or (str(uploads[0]) if uploads else "")
                resume = UI.ask("Resume file path (relative or absolute, empty to unset)",
                                default_resume, allow_empty=True)
                candidate["resume_file"] = (str(check_resume_path(resume)) if resume else "")
            elif choice == 4:
                UI.print("These values are only used to answer application questions. "
                         "Leave them empty and you will be asked instead.")
                candidate["work_authorization"] = UI.ask(
                    "Work authorization (e.g. 'US citizen', 'Green card', 'H-1B')",
                    candidate.get("work_authorization", ""))
                candidate["sponsorship_required"] = UI.ask(
                    "Do you require sponsorship? (yes/no)", candidate.get("sponsorship_required", ""))
                UI.print("The fields below are voluntary self-identification data. "
                         "Leave them empty to answer those questions yourself each time.")
                candidate["veteran_status"] = UI.ask("Veteran status (optional)",
                                                     candidate.get("veteran_status", ""))
                candidate["disability_status"] = UI.ask("Disability status (optional)",
                                                        candidate.get("disability_status", ""))
                candidate["gender"] = UI.ask("Gender (optional)", candidate.get("gender", ""))
                candidate["race"] = UI.ask("Race/ethnicity (optional)", candidate.get("race", ""))
            elif choice == 5:
                import_from_resume(config)
                continue
            elif choice == 6:
                if UI.ask_yes_no("Delete data/candidate.json? This cannot be undone.", default=False):
                    try:
                        if CANDIDATE_FILE.exists():
                            CANDIDATE_FILE.unlink()
                        UI.ok("Candidate profile deleted.")
                    except OSError as exc:
                        UI.error(f"Could not delete the profile: {exc}")
                continue
            if save_candidate(candidate):
                UI.ok("Candidate profile saved.")
        except KeyboardInterrupt:
            UI.warn("Cancelled.")
            return


def check_resume_path(value: str) -> Optional[Path]:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        for base in (BASE_DIR, UPLOADS_DIR, Path.cwd()):
            candidate_path = base / value
            if candidate_path.exists():
                path = candidate_path
                break
    if not path.exists():
        UI.warn(f"Resume file not found: {value}")
        return None
    if path.suffix.lower() not in (".pdf", ".docx", ".doc", ".txt", ".md"):
        UI.warn(f"Unusual resume extension '{path.suffix}'. Supported: .pdf and .docx.")
    return path


def import_from_resume(config: Config) -> Optional[Dict[str, Any]]:
    """Menu 2: pick a resume, parse it, show it, ask before saving. Never assumes."""
    UI.banner("Upload / parse resume", "PDF or DOCX. Nothing is saved until you confirm.")
    found = sorted([p for p in UPLOADS_DIR.glob("*")
                    if p.suffix.lower() in (".pdf", ".docx", ".txt", ".md")])
    if found:
        UI.print("Files in uploads/:")
        for index, path in enumerate(found, start=1):
            UI.print(f"  {index}. {path.name}")
        UI.print("  (or type a full path yourself)")
    raw = UI.ask("Resume path or number", found[0].name if found else "", allow_empty=True)
    path: Optional[Path] = None
    if raw.isdigit() and found and 1 <= int(raw) <= len(found):
        path = found[int(raw) - 1]
    elif raw:
        path = check_resume_path(raw)
    if path is None:
        UI.warn("No readable resume selected. Put your resume in uploads/ and try again.")
        return None

    if path.parent != UPLOADS_DIR and UI.ask_yes_no(f"Copy {path.name} into uploads/?", default=True):
        try:
            UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
            target = UPLOADS_DIR / path.name
            shutil.copy2(str(path), str(target))
            path = target
            UI.ok(f"Copied to {path}")
        except OSError as exc:
            UI.warn(f"Could not copy the file ({exc}); using the original path.")

    with UI.status(f"Reading {path.name} ..."):
        text, error = extract_resume_text(path)
    if error:
        UI.print("")
        UI.error("Resume could not be read.")
        UI.print(f"Details: {error}")
        UI.print("Your profile was not changed.")
        log_event("resume_parse", "error", error=error)
        return None
    log_event("resume_parse", "ok", extra={"file": path.name})

    parsed = parse_resume(text or "")
    render_parsed_resume(parsed)
    UI.print(f"Read {len(text or '')} characters of text from {path.name}.")

    while True:
        answer = UI.ask_yes_no("Do you want to save this information as your candidate profile?",
                               default=None)
        if answer is None:
            return None
        if not answer:
            if not UI.ask_yes_no("Edit the extracted values now?", default=True):
                UI.print("Nothing was saved. Your previous profile is unchanged.")
                return None
            parsed["first_name"] = UI.ask("First name", parsed.get("first_name", ""))
            parsed["last_name"] = UI.ask("Last name", parsed.get("last_name", ""))
            parsed["email"] = UI.ask("Email", parsed.get("email", ""))
            parsed["phone"] = UI.ask("Phone", parsed.get("phone", ""))
            parsed["location"] = UI.ask("Location", parsed.get("location", ""))
            parsed["skills"] = UI.ask_list("Skills (comma separated)", parsed.get("skills"))
            parsed["education"] = UI.ask("Education", parsed.get("education", ""))
            parsed["certifications"] = UI.ask_list("Certifications (comma separated)",
                                                   parsed.get("certifications"))
            parsed["linkedin"] = UI.ask("LinkedIn", parsed.get("linkedin", ""))
            parsed["github"] = UI.ask("GitHub", parsed.get("github", ""))
            parsed["portfolio"] = UI.ask("Portfolio", parsed.get("portfolio", ""))
            parsed["years_experience"] = UI.ask_int("Years of experience",
                                                    parsed.get("years_experience") or 0, 0, 60)
            continue
        candidate = load_candidate()
        for key in ("first_name", "last_name", "email", "phone", "location", "skills", "education",
                    "certifications", "work_experience", "projects", "linkedin", "github",
                    "portfolio", "years_experience"):
            value = parsed.get(key)
            if value in (None, "", [], 0):
                continue
            candidate[key] = value
        candidate["resume_file"] = str(path)
        if save_candidate(candidate):
            UI.ok("Candidate profile saved from the resume.")
            UI.print("Refine anything with menu option 1 (Setup Candidate).")
        return parsed


def menu_search_jobs(config: Config) -> List[Dict[str, Any]]:
    """Menu 3: search Dice for real jobs only."""
    UI.banner("Search Dice jobs", "Real Dice results only - nothing is generated locally.")
    if config.demo:
        UI.warn("DEMO MODE: using clearly labelled demo records instead of Dice. "
                "These are NOT real listings.")
        jobs = demo_jobs()
        added, updated = merge_jobs(jobs)
        UI.table(["Title", "Company", "Location", "Mode", "Apply type"],
                 [[job["title"], job["company"], job["location"], job["workplace_type"],
                   job["application_type"]] for job in jobs])
        UI.ok(f"Saved {added} demo record(s) ({updated} updated) to data/jobs.json.")
        return jobs

    provider = DiceProvider(config)
    ready, reason = provider.availability()
    if not ready:
        UI.print("")
        UI.error("Dice search is not configured.")
        for line in reason.splitlines():
            UI.print("    " + line)
        UI.print("")
        UI.print("Set it up with menu option 7 (Settings) or by editing .env, then try again.")
        UI.print("No jobs were retrieved and no placeholder jobs were substituted.")
        return []

    candidate = load_candidate()
    query = SearchQuery()
    query.title = UI.ask("Job title", (candidate.get("target_roles") or [""])[0])
    query.location = UI.ask("Location (empty for anywhere)", candidate.get("location", ""))
    mode_options = ["Any", "Remote", "Hybrid", "Onsite"]
    mode_index = UI.choose("Work mode", mode_options)
    query.work_mode = "" if mode_index in (None, 0) else mode_options[mode_index]
    type_options = ["Any", "FULLTIME", "CONTRACT", "PARTTIME"]
    type_index = UI.choose("Employment type", type_options)
    query.employment_type = "" if type_index in (None, 0) else type_options[type_index]
    query.max_years_experience = UI.ask_int("Experience in years (empty for any)", None, 0, 40)
    UI.print("Posted within (days): 1, 3, 7, 30 or empty for any")
    posted = UI.ask("Posted within", "", allow_empty=True)
    query.posted_within_days = int(posted) if posted.isdigit() and int(posted) > 0 else None
    query.easy_apply_only = bool(UI.ask_yes_no("Easy Apply only?", default=True))
    query.limit = UI.ask_int("Maximum results to save", 25, 1, 100) or 25

    try:
        with UI.status(f"Searching Dice ({reason}) ..."):
            jobs = provider.search(query)
    except ProviderError as exc:
        UI.print("")
        UI.error("Dice search failed.")
        for line in str(exc).splitlines():
            UI.print("    " + line)
        UI.print("No jobs were saved and no sample data was substituted.")
        log_event("dice_search", "error", error=str(exc)[:400])
        return []
    except Exception as exc:
        UI.print("")
        UI.error("Dice search failed with an unexpected error.")
        UI.print(f"Details: {first_line(str(exc), 200)}")
        UI.print("No jobs were saved. Nothing was submitted.")
        log_event("dice_search", "error", error=first_line(str(exc), 300))
        if DEBUG:
            traceback.print_exc()
        return []

    if query.easy_apply_only:
        confirmed = [job for job in jobs if job.get("application_type") == AT_EASY_APPLY]
        if not confirmed:
            UI.warn("None of the results are confirmed Dice Easy Apply jobs.")
            UI.print("Dice only reports Easy Apply when its own page says so; the full result set "
                     "is stored so you can inspect each one.")

    added, updated = merge_jobs(jobs)
    UI.table(["Title", "Company", "Location", "Mode", "Apply type", "Posted"],
             [[job["title"][:44], job["company"][:28], job["location"][:24],
               job["workplace_type"] or "-", job["application_type"], job["posted_date"] or "-"]
              for job in jobs])
    counts: Dict[str, int] = {}
    for job in jobs:
        counts[job["application_type"]] = counts.get(job["application_type"], 0) + 1
    UI.ok(f"Saved {len(jobs)} real Dice job(s) to data/jobs.json ({added} new, {updated} updated).")
    UI.print("    Apply types: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    UI.print("    Use menu 4 to score them, then menu 5 to apply.")
    return jobs


def render_job_detail(job: Dict[str, Any]) -> None:
    UI.table(["Field", "Value"], [
        ["Title", job.get("title", "")], ["Company", job.get("company", "")],
        ["Location", job.get("location", "")], ["Workplace", job.get("workplace_type", "")],
        ["Employment type", job.get("employment_type", "")], ["Salary", job.get("salary", "")],
        ["Posted", job.get("posted_date", "")], ["Apply type", job.get("application_type", "")],
        ["URL", job.get("source_url", "")], ["Score", job.get("score", "-")],
        ["Recommendation", job.get("recommendation", "-")],
        ["AI adjustment", f"{job.get('ai_adjustment', 0):+d}".strip()
                          + (" " + job["ai_reason"] if job.get("ai_reason") else "")],
        ["Skills", ", ".join(job.get("skills") or []) or "-"],
        ["Score breakdown", ", ".join(f"{k}={v}" for k, v in (job.get("breakdown") or {}).items()) or "-"],
    ])
    if job.get("matches"):
        UI.print("MATCHES")
        for match in job["matches"]:
            UI.print(f"  + {match}")
    if job.get("gaps"):
        UI.print("GAPS")
        for gap in job["gaps"]:
            UI.print(f"  - {gap}")
    if job.get("description"):
        UI.print("DESCRIPTION (from Dice)")
        UI.print(clean_text(job["description"], 1500))


def menu_analyze_jobs(config: Config) -> None:
    """Menu 4: deterministic scoring first, optional AI nudge, then save."""
    UI.banner("Analyze jobs", "Deterministic score first - the AI can only nudge it, never decide.")
    candidate = load_candidate()
    if not candidate_has_profile(candidate):
        UI.warn("Create your candidate profile first (menu option 1).")
        return
    jobs = load_jobs()
    if not jobs:
        UI.warn("No jobs in data/jobs.json yet. Run a search first (menu option 3).")
        return

    ai = AIClient(config)
    use_ai = False
    if ai.available:
        use_ai = bool(UI.ask_yes_no("Use AI for semantic matching as well?", default=True))
    else:
        UI.info("AI matching is off (no LLM_API_KEY configured). Deterministic scoring only.")

    ai_calls = 0
    with UI.status("Scoring jobs ..."):
        for job in jobs:
            result = score_job(job, candidate)
            if use_ai and ai_calls < 25:
                verdict = ai.semantic_match(job, candidate, result)
                ai_calls += 1
                if verdict:
                    result = apply_ai_adjustment(result, verdict["adjustment"],
                                                 verdict["confidence"], verdict["reason"])
                    log_event("ai_match", "ok", job_id=job.get("id"),
                              extra={"adjustment": verdict["adjustment"]})
                elif ai.last_error:
                    log_event("ai_match", "warn", job_id=job.get("id"), error=ai.last_error)
            job.update({"score": result["score"], "breakdown": result["breakdown"],
                        "matches": result["matches"], "gaps": result["gaps"],
                        "recommendation": result["recommendation"],
                        "ai_adjustment": result.get("ai_adjustment", 0),
                        "ai_reason": result.get("ai_reason", ""), "scored_at": now_iso()})
    save_jobs(jobs)
    log_event("score_jobs", "ok", extra={"count": len(jobs)})

    ordered = sorted(jobs, key=lambda item: item.get("score") or 0, reverse=True)
    UI.table(["Score", "Rec", "Job", "Company", "Apply type", "AI nudge"],
             [[str(job.get("score", "-")), job.get("recommendation", "-"),
               job.get("title", "")[:38], job.get("company", "")[:22],
               job.get("application_type", AT_UNKNOWN),
               (f"{job.get('ai_adjustment', 0):+d}" if job.get("ai_adjustment") else "-")]
              for job in ordered])
    apply_count = len([job for job in jobs if job.get("recommendation") == "APPLY"])
    UI.ok(f"Scored {len(jobs)} job(s). {apply_count} recommend APPLY "
          f"(80+, Easy Apply jobs are the ones menu 5 will offer).")

    if UI.ask_yes_no("Show the detailed breakdown of one job?", default=False):
        labels = [f"{job.get('score','-')} | {job.get('title','')[:40]} | {job.get('company','')[:24]}"
                  for job in ordered]
        index = UI.choose("Job", labels)
        if index is not None:
            render_job_detail(ordered[index])


def menu_apply(config: Config) -> None:
    """Menu 5: pick a job and run the controlled Easy Apply workflow."""
    UI.banner("Apply to a job", "Dice Easy Apply only. Nothing is submitted without your YES.")
    candidate = load_candidate()
    if not candidate_has_profile(candidate):
        UI.warn("Create your candidate profile first (menu option 1).")
        return
    if not candidate.get("resume_file"):
        UI.warn("No resume file is set in your profile. The flow can still run, but a required "
                "resume upload will stop it before submission.")

    jobs = load_jobs()
    if not jobs:
        UI.warn("No jobs stored. Run a search first (menu option 3).")
        return

    eligible = [job for job in jobs if job.get("application_type") == AT_EASY_APPLY
                and (job.get("score") or 0) >= 60]
    job: Optional[Dict[str, Any]] = None
    if eligible:
        UI.print("Recommended (Easy Apply, score 60+):")
        labels = [f"{item.get('score','-')} | {item.get('title','')[:38]} | "
                  f"{item.get('company','')[:22]}" for item in eligible]
        index = UI.choose("Select a job (0 to pick from all stored jobs)", labels)
        job = eligible[index] if index is not None else None
    if job is None:
        if not eligible:
            UI.warn("No Easy Apply job with score 60+ is stored - pick from all stored jobs instead.")
        ordered = sorted(jobs, key=lambda item: item.get("score") or 0, reverse=True)
        labels = [f"{item.get('score','-')} | {item.get('title','')[:34]} | "
                  f"{item.get('company','')[:20]} | {item.get('application_type','')}"
                  for item in ordered]
        index = UI.choose("Job", labels)
        if index is None:
            return
        job = ordered[index]
    if not job:
        return

    UI.print("")
    UI.print(f"Selected: {job_label(job)}")
    UI.print(f"URL: {job.get('source_url') or job.get('application_url')}")
    UI.print(f"Apply type: {job.get('application_type')}")

    if job.get("application_type") == AT_EXTERNAL:
        UI.print("")
        UI.warn("External application — not supported by this MVP.")
        UI.print("This job is applied for on the company's own website. Nothing was submitted.")
        create_application(job, ST_REVIEW)
        return
    if job.get("application_type") == AT_UNKNOWN:
        UI.info("The apply type of this job is unknown, so it will be detected on the page.")
        if not UI.ask_yes_no("Open the page to find out how this job is applied for?", default=True):
            return

    existing = [app for app in load_applications()
                if app.get("job_id") == job.get("id") and app.get("state") == ST_SUBMITTED]
    if existing:
        UI.warn(f"You already recorded a SUBMITTED application for this job "
                f"({(existing[0].get('submitted_at') or '')[:19]}).")
        if not UI.ask_yes_no("Run it again anyway?", default=False):
            return

    submitted_today = [app for app in load_applications()
                       if app.get("state") == ST_SUBMITTED
                       and (app.get("submitted_at") or "").startswith(
                           datetime.now().strftime("%Y-%m-%d"))]
    if len(submitted_today) >= max(1, config.max_applications_per_run):
        UI.warn(f"You have already submitted {len(submitted_today)} application(s) today "
                f"(MAX_APPLICATIONS_PER_RUN={config.max_applications_per_run}).")
        UI.print("This guard rail exists to stop runaway automation; raise the value in .env "
                 "if you really want to continue.")
        if not UI.ask_yes_no("Continue with another application anyway?", default=False):
            return

    if config.demo:
        demo_note()
        write_demo_form()
        job = dict(job)
        job["source_url"] = (DATA_DIR / "demo-application-form.html").as_uri()
        job["application_url"] = job["source_url"]
        UI.info("Demo run: the local demo form will be used and submission stays disabled.")
    else:
        UI.print("")
        UI.print("A browser window will open. Sign in yourself if the site asks.")
        UI.print("Every question your profile does not answer will be shown to you.")
        UI.print("Nothing is submitted unless you type YES at the review screen.")
        if not UI.ask_yes_no("Start the application workflow?", default=True):
            return

    state = run_easy_apply(config, AIClient(config), candidate, job)
    UI.print("")
    UI.print(f"Final state: {state}")
    if state == ST_SUBMITTED:
        UI.ok("Saved to data/applications.json as SUBMITTED (verified on the page).")
    elif state == ST_CANCELLED:
        UI.info("You cancelled. Nothing was submitted; recorded as CANCELLED.")
    elif state == ST_READY:
        UI.info("The form was prepared but submission was not performed.")
    elif state == ST_REVIEW:
        UI.warn("This application needs a human (REVIEW_REQUIRED).")
    elif state == ST_FAILED:
        UI.error("The workflow failed. Nothing was confirmed as submitted.")


def open_in_browser(url: str) -> None:
    import webbrowser
    if not url:
        UI.warn("No URL stored for this record.")
        return
    try:
        webbrowser.open(url)
        UI.ok("Opened in your default browser.")
    except Exception as exc:
        UI.warn(f"Could not open a browser: {exc}")


def menu_history(config: Config) -> None:
    """Menu 6: application history from data/applications.json."""
    UI.banner("Application history", "data/applications.json")
    apps = load_applications()
    if not apps:
        UI.warn("No applications recorded yet.")
        return
    apps_sorted = sorted(apps, key=lambda item: item.get("updated_at", ""), reverse=True)
    UI.table(["Job", "Company", "Score", "Status", "Updated", "Apply type"],
             [[app.get("job_title", "")[:36], app.get("company", "")[:20],
               str(app.get("score", "-")), app.get("state", ""),
               (app.get("updated_at") or "")[:16], app.get("application_type", "")]
              for app in apps_sorted])
    counts: Dict[str, int] = {}
    for app in apps:
        key = app.get("state", "?")
        counts[key] = counts.get(key, 0) + 1
    UI.print("Totals: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    if not UI.ask_yes_no("Show details for one application?", default=False):
        return
    labels = [f"{app.get('job_title','')[:40]} | {app.get('state','')} | {app.get('id','')}"
              for app in apps_sorted]
    index = UI.choose("Application", labels)
    if index is None:
        return
    app = apps_sorted[index]
    UI.table(["Field", "Value"], [
        ["Application id", app.get("id", "")], ["Job", app.get("job_title", "")],
        ["Company", app.get("company", "")], ["Score", app.get("score")],
        ["State", app.get("state", "")], ["Apply type", app.get("application_type", "")],
        ["URL", app.get("url", "")], ["Created", app.get("created_at", "")],
        ["Updated", app.get("updated_at", "")],
        ["Submitted", app.get("submitted_at") or "-"],
        ["Flags", ", ".join(app.get("flags") or []) or "-"],
        ["Last error", app.get("last_error", "") or "-"],
        ["Notes", app.get("notes", "") or "-"],
    ])
    if app.get("answers"):
        UI.table(["Question", "Answer", "Source", "Confidence"],
                 [[answer.get("question", "")[:42], str(answer.get("answer", ""))[:38],
                   answer.get("source", ""),
                   (f"{answer['confidence']:.2f}" if answer.get("confidence") is not None else "-")]
                  for answer in app["answers"]])
    if app.get("url") and UI.ask_yes_no("Open this job's URL in a normal browser?", default=False):
        open_in_browser(app["url"])


def _tty() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def sign_in_now(config: Config) -> None:
    if not HAVE_PLAYWRIGHT:
        UI.error("Playwright is not installed, so a browser cannot be opened.")
        UI.print("Fix: pip install playwright   then   playwright install chromium")
        return
    UI.info("Opening Dice so you can sign in yourself. Your password is never typed here.")
    with sync_playwright() as play:
        try:
            browser, context = launch_browser(play, config, for_apply=True)
        except Exception as exc:
            UI.error(f"Could not launch the browser: {first_line(str(exc), 200)}")
            return
        try:
            page = context.new_page()
            interactive_login(page, config)
            save_browser_state(context)
        except KeyboardInterrupt:
            UI.warn("Stopped. Any session that was already saved is kept.")
        except Exception as exc:
            UI.error(f"Sign-in flow stopped: {first_line(str(exc), 200)}")
        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass


def show_logs(limit: int = 25) -> None:
    entries = _logs()
    if not entries:
        UI.warn("No log entries yet (data/logs.json).")
        return
    rows = [[(entry.get("timestamp") or "")[:19], entry.get("action", ""), entry.get("status", ""),
             entry.get("job_id") or "-", (entry.get("error") or "")[:56]]
            for entry in entries[-limit:]]
    UI.table(["Time (UTC)", "Action", "Status", "Job", "Error"], rows)
    UI.print(f"Showing the last {len(rows)} of {len(entries)} entries.")


def menu_settings(config: Config) -> None:
    """Menu 7: configuration helpers. Secrets are never displayed."""
    while True:
        config = load_config(demo=config.demo)
        UI.banner("Settings", ".env values (secrets are never shown)")
        masked = lambda value: ("set (******)" if value else "not set")
        UI.table(["Setting", "Value"], [
            ["DICE_API_KEY", masked(config.dice_api_key)],
            ["DICE_API_BASE", config.dice_api_base],
            ["DICE_SEARCH_PATH", config.dice_search_path],
            ["DICE_API_AUTH", config.dice_api_auth],
            ["DICE_CLIENT_ID", config.dice_client_id or "not set"],
            ["DICE_ALLOW_BROWSER_SEARCH", "on" if config.dice_allow_browser_search else "off"],
            ["DICE_MAX_RESULTS", str(config.dice_max_results)],
            ["DICE_ENRICH_LIMIT", str(config.dice_enrich_limit)],
            ["LLM_API_KEY", masked(config.llm_api_key)],
            ["LLM_BASE_URL", config.llm_base_url],
            ["LLM_MODEL", config.llm_model],
            ["LLM_ENABLED", "on" if config.llm_enabled else "off"],
            ["AI_MIN_CONFIDENCE", str(config.ai_min_confidence)],
            ["BROWSER_HEADLESS", "on" if config.headless else "off"],
            ["REQUEST_TIMEOUT", f"{config.request_timeout}s"],
            ["NAV_TIMEOUT", f"{config.nav_timeout}s"],
            ["POLITE_DELAY_SECONDS", str(config.pol_request_delay)],
            ["MAX_APPLICATIONS_PER_RUN", str(config.max_applications_per_run)],
        ])
        options = ["Set the Dice API key (official partner API)",
                   "Set the LLM API key / model / base URL (AI features)",
                   "Turn browser search of the public Dice site on or off",
                   "Set browser headless mode",
                   "Sign in to Dice (opens a browser; you type the password)",
                   "Run the environment check again",
                   "Clear the saved Dice browser session",
                   "View recent log entries"]
        choice = UI.choose("Select", options)
        if choice is None:
            return
        if choice == 0:
            value = (getpass.getpass("Dice API key (hidden input): ").strip() if _tty()
                     else UI.ask("Dice API key (visible: no TTY detected)", allow_empty=True))
            if value and update_env_file({"DICE_API_KEY": value}):
                UI.ok("Dice API key saved to .env. It is never printed or logged.")
        elif choice == 1:
            value = (getpass.getpass("LLM API key (hidden input): ").strip() if _tty()
                     else UI.ask("LLM API key (visible: no TTY detected)", allow_empty=True))
            updates = {}
            if value:
                updates["LLM_API_KEY"] = value
            updates["LLM_BASE_URL"] = UI.ask("LLM base URL", config.llm_base_url)
            updates["LLM_MODEL"] = UI.ask("LLM model", config.llm_model)
            updates["LLM_ENABLED"] = "true" if UI.ask_yes_no("Enable AI features?", default=True) else "false"
            if update_env_file(updates):
                UI.ok("AI settings saved to .env.")
        elif choice == 2:
            enabled = UI.ask_yes_no("Allow searching the public Dice site in a browser session?",
                                    default=config.dice_allow_browser_search)
            if enabled and not HAVE_PLAYWRIGHT:
                UI.warn("Playwright is missing. Fix: pip install playwright && playwright install chromium")
            if update_env_file({"DICE_ALLOW_BROWSER_SEARCH": "true" if enabled else "false"}):
                UI.ok("Setting saved.")
        elif choice == 3:
            headless = UI.ask_yes_no("Run the browser without a visible window (headless)?",
                                     default=False)
            if update_env_file({"BROWSER_HEADLESS": "true" if headless else "false"}):
                UI.ok("Setting saved. Headless makes sign-in and reviews harder to follow.")
        elif choice == 4:
            sign_in_now(config)
        elif choice == 5:
            environment_report(config, deep=True)
        elif choice == 6:
            if BROWSER_STATE_FILE.exists() and UI.ask_yes_no(
                    "Delete the saved Dice session (data/browser_state.json)?", default=False):
                try:
                    BROWSER_STATE_FILE.unlink()
                    UI.ok("Saved session deleted. You will sign in again next time.")
                except OSError as exc:
                    UI.error(f"Could not delete the session file: {exc}")
        elif choice == 7:
            show_logs()


# ===========================================================================
# SECTION 19 - first-run checks and main()
# ===========================================================================


def first_run_checks(config: Config) -> None:
    if SETUP_MARKER.exists():
        return
    UI.banner(f"Welcome to {APP_NAME}", APP_TAGLINE)
    UI.print("First run: checking your environment. Nothing is sent anywhere.")
    UI.print("")
    ok = environment_report(config, deep=False)
    UI.print("")
    UI.print("Quick setup:")
    UI.print("  1. python -m venv .venv")
    UI.print("  2. Windows: .venv\\Scripts\\activate     macOS/Linux: source .venv/bin/activate")
    UI.print("  3. pip install -r requirements.txt")
    UI.print("  4. playwright install chromium")
    UI.print("  5. copy .env.example .env, then fill in DICE_API_KEY (or enable browser search)")
    UI.print("  6. put your resume in uploads/ and use menu 2 to parse it")
    UI.print("")
    if not ok:
        UI.warn("Some checks failed, but the program continues. Each feature explains what it needs.")
    try:
        ensure_dirs()
        SETUP_MARKER.write_text(json.dumps(
            {"first_run_at": now_iso(), "version": VERSION, "python": sys.version.split()[0]},
            indent=2), encoding="utf-8")
    except OSError:
        pass
    log_event("first_run", "ok", extra={"python": sys.version.split()[0]})


def print_main_menu(config: Config) -> None:
    candidate = load_candidate()
    jobs = load_jobs()
    apps = load_applications()
    who = candidate.get("email") or (f"{candidate.get('first_name','')} "
                                     f"{candidate.get('last_name','')}".strip() or "empty")
    print()
    print("===============================================")
    print(f"                 {APP_NAME}")
    print("===============================================")
    print()
    print("1. Setup Candidate")
    print("2. Upload / Parse Resume")
    print("3. Search Dice Jobs")
    print("4. Analyze Jobs")
    print("5. Apply to Job")
    print("6. Application History")
    print("7. Settings")
    print("8. Exit")
    print()
    print(f"   profile: {who} | jobs: {len(jobs)} | applications: {len(apps)}"
          + (" | DEMO MODE" if config.demo else ""))
    print()


def main_menu(config: Config) -> None:
    while True:
        print_main_menu(config)
        choice = UI.ask("Select", allow_empty=True)
        if choice == "" and UI.eof_seen:
            flush_logs()
            UI.print("")
            UI.print("Input ended - exiting.")
            return
        try:
            if choice == "1":
                menu_setup_candidate(config)
            elif choice == "2":
                import_from_resume(config)
            elif choice == "3":
                menu_search_jobs(config)
            elif choice == "4":
                menu_analyze_jobs(config)
            elif choice == "5":
                menu_apply(config)
            elif choice == "6":
                menu_history(config)
            elif choice == "7":
                menu_settings(config)
            elif choice in ("8", "q", "quit", "exit"):
                flush_logs()
                UI.print("")
                UI.print("Goodbye. Nothing is submitted without your explicit YES.")
                return
            else:
                UI.warn("Please enter a number from 1 to 8.")
        except KeyboardInterrupt:
            print()
            UI.warn("Cancelled. Returning to the menu.")
        except Exception as exc:
            print()
            UI.error("That menu action failed unexpectedly.")
            UI.print(f"Details: {first_line(str(exc), 300)}")
            UI.print("The program is still running; nothing was submitted.")
            log_event("menu_action", "error", error=first_line(str(exc), 400))
            if DEBUG:
                traceback.print_exc()


def preflight() -> bool:
    if sys.version_info < MIN_PYTHON:
        print(f"{APP_NAME} needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer. "
              f"You are running {sys.version.split()[0]}.")
        return False
    ensure_dirs()
    UI.init()
    return True


def data_summary() -> int:
    candidate = load_candidate()
    jobs = load_jobs()
    apps = load_applications()
    UI.table(["Store", "Records", "File"], [
        ["candidate", "1 profile" if candidate_has_profile(candidate) else "empty", str(CANDIDATE_FILE)],
        ["jobs", str(len(jobs)), str(JOBS_FILE)],
        ["applications", str(len(apps)), str(APPLICATIONS_FILE)],
        ["logs", str(len(_logs())), str(LOGS_FILE)],
    ])
    for state in ALL_STATES:
        count = len([app for app in apps if app.get("state") == state])
        if count:
            UI.print(f"    {state}: {count}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="jobpilot.py",
                                     description=f"{APP_NAME} - {APP_TAGLINE}")
    parser.add_argument("--demo", action="store_true",
                        help="use clearly labelled demo records and a local demo form (opt-in)")
    parser.add_argument("--check", action="store_true",
                        help="run the environment check, then exit")
    parser.add_argument("--state", action="store_true",
                        help="print a summary of the local JSON stores, then exit")
    parser.add_argument("--version", action="store_true", help="print the version, then exit")
    args = parser.parse_args(argv)

    if not preflight():
        return 1

    if args.version:
        print(f"{APP_NAME} {VERSION}")
        return 0

    config = load_config(demo=args.demo)

    if args.check:
        return 0 if environment_report(config, deep=True) else 1
    if args.state:
        return data_summary()

    if args.demo:
        demo_note()
    first_run_checks(config)
    log_event("app_start", "ok", extra={"demo": args.demo, "version": VERSION})
    try:
        main_menu(config)
    except KeyboardInterrupt:
        print()
        UI.warn("Interrupted. Nothing was submitted.")
    finally:
        flush_logs()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as _fatal_error:
        print("JOBPILOT hit an unexpected fatal error and stopped.")
        print(f"Details: {_fatal_error}")
        if DEBUG:
            traceback.print_exc()
        sys.exit(1)
