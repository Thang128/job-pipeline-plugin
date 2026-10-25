from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import math
import os
import re
import sqlite3
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Pipeline.io_utils import confined_path, is_symlink_or_reparse, path_without_link_ancestors
from Pipeline.pipeline_settings import (
    DEFAULT_SETTINGS,
    MAXIMUM_DAILY_SUBMISSIONS,
    SENIORITY_ORDER,
    SettingsError,
    validate_settings,
)


class StoreError(ValueError):
    """Canonical store validation or concurrency failure."""


ALLOWED_STATUSES = frozenset({"new", "shortlisted", "docs_ready", "applied", "needs_user", "skipped", "interview", "offer", "rejected"})
BLOCKER_CATEGORIES = frozenset(
    {
        "needs_account",
        "unknown_answer",
        "captcha",
        "portal_broken",
        "unknown_answer_email_verification_code",
        "transient_no_browser",
        "transient_portal_error",
    }
)
SPONSORSHIP_STATUSES = frozenset({"unknown", "offered", "not_offered", "not_required"})
COUNTRY_CODE = re.compile(r"[A-Z]{2}")
UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_TEXT_CHARS = 10_000
MAX_CONFIRMATION_SUMMARY_CHARS = 2_000
MAX_IMPORT_BYTES = 1024 * 1024
MAX_SERIALIZED_JOB_BYTES = 256 * 1024
MAX_SERIALIZED_SETTINGS_BYTES = 512 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 2_048
MAX_JSON_COLLECTION_ITEMS = 200
MAX_JSON_INTEGER = 10**18
MAX_DASHBOARD_PAGE = 10_000
MAX_DASHBOARD_PAGE_SIZE = 100
REQUIRED_JOB_FIELDS = frozenset({"id", "title", "company", "country", "source_url", "status", "score", "detected_seniority"})
OPTIONAL_JOB_FIELDS = frozenset(
    {
        "employer",
        "end_employer",
        "job_description",
        "deadline",
        "salary_amount",
        "salary_currency",
        "salary_max_amount",
        "salary_period",
        "sponsorship_status",
        "is_vie",
        "blocker",
        "source",
        "source_job_id",
        "source_query",
        "location",
        "work_mode",
        "employment_type",
        "language",
        "posted_at",
        "found_at",
        "cv",
        "cover_letter",
        "role_family",
        "classification_reason",
        "application_url",
        "confirmation",
        "document_review",
        "portal_error_count",
        "last_portal_error_run",
        "tailoring_overlay",
    }
)
ALLOWED_JOB_FIELDS = REQUIRED_JOB_FIELDS | OPTIONAL_JOB_FIELDS
RESERVED_APPLICATION_FIELDS = frozenset(
    {"applied", "confirmation", "applied_at", "confirmation_kind", "confirmation_observed_at", "confirmation_summary", "confirmation_source_url"}
)
OPTIONAL_TEXT_FIELDS = frozenset(
    {
        "employer",
        "end_employer",
        "job_description",
        "deadline",
        "salary_period",
        "source",
        "source_job_id",
        "source_query",
        "location",
        "work_mode",
        "employment_type",
        "language",
        "posted_at",
        "found_at",
        "cv",
        "cover_letter",
        "role_family",
        "classification_reason",
        "last_portal_error_run",
        "tailoring_overlay",
    }
)
META_KEYS = frozenset({"run_count", "last_run", "created_at"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _error(path: str) -> None:
    raise StoreError(path)


def _text(value: object, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT_CHARS or (nonempty and not value.strip()):
        _error(path)
    return value.strip()


def _timestamp(value: object, path: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not UTC_TIMESTAMP.fullmatch(value):
        _error(path)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _error(path)
    return value


def _valid_http_url(value: object, path: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        _error(path)
    text = _text(value, path)
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in text) or "\\" in text:
        _error(path)
    try:
        parsed = urlsplit(text)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        _error(path)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username is not None or parsed.password is not None or hostname is None:
        _error(path)
    if port is not None and not 1 <= port <= 65535:
        _error(path)
    if parsed.netloc.endswith(":"):
        _error(path)
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        try:
            ascii_host = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            _error(path)
        labels = ascii_host.rstrip(".").split(".")
        if len(ascii_host) > 253 or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or re.fullmatch(r"[A-Za-z0-9-]+", label) is None
            for label in labels
        ):
            _error(path)
    return text


def _finite_number(value: object, path: str, *, minimum: float = 0, maximum: float = 10**12) -> int | float:
    if type(value) not in {int, float}:
        _error(path)
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if not finite or value < minimum or value > maximum:
        _error(path)
    return value


def _validate_json_tree(value: object, path: str) -> None:
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            _error(path)
        if current is None or type(current) is bool:
            continue
        if type(current) is int:
            if abs(current) > MAX_JSON_INTEGER:
                _error(path)
            continue
        if type(current) is float:
            if not math.isfinite(current):
                _error(path)
            continue
        if isinstance(current, str):
            if len(current) > MAX_TEXT_CHARS:
                _error(path)
            continue
        if isinstance(current, list):
            if len(current) > MAX_JSON_COLLECTION_ITEMS:
                _error(path)
            stack.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            if len(current) > MAX_JSON_COLLECTION_ITEMS:
                _error(path)
            for key, item in current.items():
                if not isinstance(key, str) or not key or len(key) > MAX_TEXT_CHARS:
                    _error(path)
                stack.append((item, depth + 1))
            continue
        _error(path)


def _validate_confirmation(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"kind", "observed_at", "summary", "source_url"}:
        _error("job.confirmation")
    kind = value.get("kind")
    if kind not in {"portal_success", "sent_message"}:
        _error("job.confirmation.kind")
    summary = _text(value.get("summary"), "job.confirmation.summary")
    if len(summary) > MAX_CONFIRMATION_SUMMARY_CHARS:
        _error("job.confirmation.summary")
    return {
        "kind": kind,
        "observed_at": _timestamp(value.get("observed_at"), "job.confirmation.observed_at"),
        "summary": summary,
        "source_url": _valid_http_url(value.get("source_url"), "job.confirmation.source_url"),
    }


def _validate_blocker(value: object, *, required: bool) -> dict[str, str] | None:
    if value is None and not required:
        return None
    if not isinstance(value, dict) or set(value) != {"category", "detail"}:
        _error("job.blocker")
    category = value.get("category")
    if category not in BLOCKER_CATEGORIES:
        _error("job.blocker.category")
    return {"category": category, "detail": _text(value.get("detail"), "job.blocker.detail")}


def _validate_job(value: object, *, stored: bool) -> dict[str, Any]:
    _validate_json_tree(value, "job")
    if not isinstance(value, dict) or not REQUIRED_JOB_FIELDS.issubset(value):
        _error("job")
    unknown = set(value) - ALLOWED_JOB_FIELDS
    reserved = set(value) & RESERVED_APPLICATION_FIELDS
    if not stored and reserved:
        _error("job.applied_confirmation")
    if unknown:
        _error("job.fields")
    result = copy.deepcopy(value)
    result["id"] = _text(result.get("id"), "job.id")
    result["title"] = _text(result.get("title"), "job.title")
    result["company"] = _text(result.get("company"), "job.company")
    country = result.get("country")
    if not isinstance(country, str) or not COUNTRY_CODE.fullmatch(country):
        _error("job.country")
    result["source_url"] = _valid_http_url(result.get("source_url"), "job.source_url")
    status = result.get("status")
    if status not in ALLOWED_STATUSES:
        _error("job.status")
    if status == "applied" and not stored:
        raise StoreError("job.status: use record_applied")
    result["score"] = _finite_number(result.get("score"), "job.score", maximum=100)
    if "portal_error_count" in result and (type(result["portal_error_count"]) is not int or not 0 <= result["portal_error_count"] <= 3):
        _error("job.portal_error_count")
    if "document_review" in result:
        from Pipeline.document_review import REVIEW_KEYS
        review = result["document_review"]
        if not isinstance(review, dict) or set(review) != REVIEW_KEYS or any(not isinstance(item, str) or SHA256.fullmatch(item) is None for item in review.values()):
            _error("job.document_review")
    seniority = result.get("detected_seniority")
    if seniority is not None and seniority not in SENIORITY_ORDER:
        _error("job.detected_seniority")
    for field in OPTIONAL_TEXT_FIELDS:
        if field in result:
            result[field] = _text(result[field], f"job.{field}")
    if "application_url" in result:
        result["application_url"] = _valid_http_url(result["application_url"], "job.application_url")
    if "is_vie" in result and type(result["is_vie"]) is not bool:
        _error("job.is_vie")
    salary_fields = {"salary_amount", "salary_currency"}
    if salary_fields.intersection(result) and not salary_fields.issubset(result):
        _error("job.salary")
    if "salary_amount" in result:
        result["salary_amount"] = _finite_number(result["salary_amount"], "job.salary_amount")
        currency = result["salary_currency"]
        if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
            _error("job.salary_currency")
    if "salary_max_amount" in result:
        result["salary_max_amount"] = _finite_number(result["salary_max_amount"], "job.salary_max_amount")
    sponsorship = result.get("sponsorship_status")
    if sponsorship is not None and sponsorship not in SPONSORSHIP_STATUSES:
        _error("job.sponsorship_status")
    blocker = _validate_blocker(result.get("blocker"), required=status == "needs_user")
    if status == "needs_user" and blocker is not None and blocker["category"] in {"transient_no_browser", "transient_portal_error"}:
        _error("job.blocker.category")
    if status == "docs_ready" and blocker is not None and blocker["category"] not in {"transient_no_browser", "transient_portal_error"}:
        _error("job.blocker.category")
    if status not in {"needs_user", "docs_ready"} and blocker is not None:
        _error("job.blocker")
    if blocker is None:
        result.pop("blocker", None)
    else:
        result["blocker"] = blocker
    if status == "applied":
        if "confirmation" not in result:
            _error("job.confirmation")
        result["confirmation"] = _validate_confirmation(result["confirmation"])
    elif "confirmation" in result:
        _error("job.confirmation")
    try:
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (OverflowError, TypeError, ValueError):
        _error("job")
    if len(payload) > MAX_SERIALIZED_JOB_BYTES:
        _error("job.size")
    return result


def _validate_meta(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != META_KEYS:
        _error("state.meta")
    run_count = value.get("run_count")
    if type(run_count) is not int or run_count < 0:
        _error("state.meta.run_count")
    return {
        "run_count": run_count,
        "last_run": _timestamp(value.get("last_run"), "state.meta.last_run", nullable=True),
        "created_at": _timestamp(value.get("created_at"), "state.meta.created_at"),
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _parse_json_int(text: str) -> int:
    if len(text.lstrip("-")) > 19:
        raise ValueError("integer bound")
    value = int(text)
    if abs(value) > MAX_JSON_INTEGER:
        raise ValueError("integer bound")
    return value


def _parse_json_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("number bound")
    return value


def _dashboard_pagination(page: object, page_size: object) -> tuple[int, int]:
    if (
        type(page) is not int
        or type(page_size) is not int
        or not 1 <= page <= MAX_DASHBOARD_PAGE
        or not 1 <= page_size <= MAX_DASHBOARD_PAGE_SIZE
    ):
        _error("dashboard.pagination")
    return page, page_size


def _strict_json_loads(text: str, path: str) -> Any:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_int=_parse_json_int,
            parse_float=_parse_json_float,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite")),
        )
        _validate_json_tree(value, path)
        return value
    except (OverflowError, RecursionError, TypeError, ValueError, json.JSONDecodeError):
        _error(path)


class PipelineStore:
    def __init__(self, path: Path):
        try:
            self.path = path_without_link_ancestors(Path(path))
        except OSError as error:
            raise StoreError("database.confinement") from error

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except sqlite3.Error as error:
            if connection is not None:
                connection.close()
            raise StoreError("database") from error

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        try:
            parent = path_without_link_ancestors(self.path.parent)
            parent.mkdir(parents=True, exist_ok=True)
            path_without_link_ancestors(parent)
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        country TEXT NOT NULL,
                        score REAL NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                    CREATE INDEX IF NOT EXISTS idx_jobs_country ON jobs(country);
                    CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC);
                    CREATE TABLE IF NOT EXISTS settings (
                        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                        version INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        updated_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS settings_history (
                        version INTEGER PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TRIGGER IF NOT EXISTS settings_history_no_update
                    BEFORE UPDATE ON settings_history
                    BEGIN
                        SELECT RAISE(ABORT, 'settings_history is immutable');
                    END;
                    CREATE TRIGGER IF NOT EXISTS settings_history_no_delete
                    BEFORE DELETE ON settings_history
                    BEGIN
                        SELECT RAISE(ABORT, 'settings_history is immutable');
                    END;
                    """
                )
                created_at = _now()
                defaults = {"run_count": 0, "last_run": None, "created_at": created_at}
                for key, value in defaults.items():
                    connection.execute(
                        "INSERT OR IGNORE INTO meta(key, value_json) VALUES (?, ?)",
                        (key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO settings(singleton, version, payload_json, updated_at) VALUES (1, 0, ?, NULL)",
                    (self._dump(DEFAULT_SETTINGS),),
                )
        except (OSError, sqlite3.Error) as error:
            raise StoreError("database.initialize") from error

    @staticmethod
    def _dump(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def job_fingerprint(value: dict) -> str:
        job = _validate_job(value, stored=True)
        return hashlib.sha256(PipelineStore._dump(job).encode("utf-8")).hexdigest()

    @staticmethod
    def _load(payload: str, path: str) -> Any:
        if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_SERIALIZED_SETTINGS_BYTES:
            _error(path)
        return _strict_json_loads(payload, path)

    def _serialized_settings(self, settings: dict) -> str:
        _validate_json_tree(settings, "settings")
        try:
            payload = self._dump(settings)
        except (OverflowError, TypeError, ValueError) as error:
            raise StoreError("settings") from error
        if len(payload.encode("utf-8")) > MAX_SERIALIZED_SETTINGS_BYTES:
            _error("settings.size")
        return payload

    def _ensure_initialized(self) -> None:
        try:
            checked = path_without_link_ancestors(self.path)
        except OSError as error:
            raise StoreError("database.confinement") from error
        if checked != self.path or not self.path.is_file() or is_symlink_or_reparse(self.path):
            _error("database.initialize")

    def _meta(self, connection: sqlite3.Connection) -> dict[str, Any]:
        rows = connection.execute("SELECT key, value_json FROM meta ORDER BY key").fetchall()
        return _validate_meta({row["key"]: self._load(row["value_json"], "state.meta") for row in rows})

    def get_settings(self) -> dict:
        self._ensure_initialized()
        try:
            with self._connection() as connection:
                row = connection.execute("SELECT version, payload_json FROM settings WHERE singleton = 1").fetchone()
            if row is None:
                _error("settings")
            return {"version": row["version"], "settings": validate_settings(self._load(row["payload_json"], "settings"))}
        except (sqlite3.Error, SettingsError) as error:
            raise StoreError("settings") from error

    def load_state(self) -> dict:
        self._ensure_initialized()
        try:
            with self._connection() as connection:
                meta = self._meta(connection)
                rows = connection.execute("SELECT payload_json FROM jobs ORDER BY id").fetchall()
                setting_row = connection.execute("SELECT version, payload_json FROM settings WHERE singleton = 1").fetchone()
            if setting_row is None:
                _error("settings")
            jobs = [_validate_job(self._load(row["payload_json"], "job"), stored=True) for row in rows]
            return {
                "meta": meta,
                "settings": validate_settings(self._load(setting_row["payload_json"], "settings")),
                "settings_version": setting_row["version"],
                "jobs": jobs,
            }
        except (sqlite3.Error, SettingsError) as error:
            raise StoreError("state") from error

    def list_jobs(self) -> list[dict]:
        return self.load_state()["jobs"]

    def dashboard_job_summary(self) -> dict:
        """Return bounded aggregate dashboard data without materializing job payloads."""
        self._ensure_initialized()
        try:
            with self._connection() as connection:
                meta = self._meta(connection)
                total = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                status_rows = connection.execute(
                    "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status ORDER BY status"
                ).fetchall()
                country_rows = connection.execute(
                    "SELECT country, COUNT(*) AS count FROM jobs GROUP BY country ORDER BY country"
                ).fetchall()
                today = datetime.now(timezone.utc).date().isoformat()
                applied_today = connection.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                    WHERE status = 'applied'
                      AND substr(json_extract(payload_json, '$.confirmation.observed_at'), 1, 10) = ?
                    """,
                    (today,),
                ).fetchone()[0]
            by_status = {}
            for row in status_rows:
                if row["status"] not in ALLOWED_STATUSES or type(row["count"]) is not int or row["count"] < 0:
                    _error("dashboard.summary")
                by_status[row["status"]] = row["count"]
            by_country = {}
            for row in country_rows:
                if not isinstance(row["country"], str) or COUNTRY_CODE.fullmatch(row["country"]) is None or type(row["count"]) is not int or row["count"] < 0:
                    _error("dashboard.summary")
                by_country[row["country"]] = row["count"]
            if any(type(value) is not int or value < 0 for value in (total, applied_today)):
                _error("dashboard.summary")
            return {
                "meta": meta,
                "summaries": {
                    "total_jobs": total,
                    "by_status": by_status,
                    "by_country": by_country,
                    "applied_today": applied_today,
                },
            }
        except StoreError:
            raise
        except sqlite3.Error as error:
            raise StoreError("dashboard.summary") from error

    def dashboard_jobs(self, *, page: int = 1, page_size: int = 50) -> dict:
        """Return only the fields needed to render a bounded jobs page."""
        page, page_size = _dashboard_pagination(page, page_size)
        self._ensure_initialized()
        try:
            with self._connection() as connection:
                total = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                rows = connection.execute(
                    """
                    SELECT id, status,
                           json_extract(payload_json, '$.id') AS payload_id,
                           json_extract(payload_json, '$.status') AS payload_status,
                           json_extract(payload_json, '$.title') AS title,
                           json_extract(payload_json, '$.company') AS company,
                           json_extract(payload_json, '$.source_url') AS source_url
                    FROM jobs ORDER BY id LIMIT ? OFFSET ?
                    """,
                    (page_size, (page - 1) * page_size),
                ).fetchall()
            items = []
            for row in rows:
                if row["id"] != row["payload_id"] or row["status"] != row["payload_status"] or row["status"] not in ALLOWED_STATUSES:
                    _error("dashboard.jobs")
                items.append(
                    {
                        "id": _text(row["id"], "dashboard.jobs.id"),
                        "title": _text(row["title"], "dashboard.jobs.title"),
                        "company": _text(row["company"], "dashboard.jobs.company"),
                        "status": row["status"],
                        "source_url": _valid_http_url(row["source_url"], "dashboard.jobs.source_url"),
                    }
                )
            pages = (total + page_size - 1) // page_size
            return {"items": items, "page": page, "page_size": page_size, "total": total, "pages": pages}
        except StoreError:
            raise
        except sqlite3.Error as error:
            raise StoreError("dashboard.jobs") from error

    def get_job(self, job_id: str) -> dict | None:
        identifier = _text(job_id, "job.id")
        self._ensure_initialized()
        try:
            with self._connection() as connection:
                row = connection.execute("SELECT payload_json FROM jobs WHERE id = ?", (identifier,)).fetchone()
            return None if row is None else _validate_job(self._load(row["payload_json"], "job"), stored=True)
        except sqlite3.Error as error:
            raise StoreError("job") from error

    def _write_job(self, connection: sqlite3.Connection, job: dict) -> None:
        connection.execute(
            """
            INSERT INTO jobs(id, payload_json, status, country, score, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload_json = excluded.payload_json,
                status = excluded.status,
                country = excluded.country,
                score = excluded.score,
                updated_at = excluded.updated_at
            """,
            (job["id"], self._dump(job), job["status"], job["country"], float(job["score"]), _now()),
        )

    def upsert_job(self, value: dict) -> dict:
        job = _validate_job(value, stored=False)
        self._ensure_initialized()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute("SELECT payload_json FROM jobs WHERE id = ?", (job["id"],)).fetchone()
                if existing is not None:
                    current = _validate_job(self._load(existing["payload_json"], "job"), stored=True)
                    if current["status"] == "applied":
                        raise StoreError("job.applied: immutable outside record_applied")
                self._write_job(connection, job)
        except StoreError:
            raise
        except sqlite3.Error as error:
            raise StoreError("job") from error
        return copy.deepcopy(job)

    def set_job_status(self, job_id: str, status: str, *, blocker: dict | None = None) -> dict:
        if status == "applied":
            raise StoreError("job.status: use record_applied")
        identifier = _text(job_id, "job.id")
        self._ensure_initialized()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT id, payload_json, status, country, score FROM jobs WHERE id = ?", (identifier,)
                ).fetchone()
                if row is None:
                    _error("job.id")
                current = _validate_job(self._load(row["payload_json"], "job"), stored=True)
                if (row["id"], row["status"], row["country"], row["score"]) != (
                    current["id"],
                    current["status"],
                    current["country"],
                    float(current["score"]),
                ):
                    raise StoreError("integrity.jobs")
                if current["status"] == "applied":
                    raise StoreError("job.applied: immutable outside record_applied")
                current["status"] = status
                if blocker is None:
                    current.pop("blocker", None)
                else:
                    current["blocker"] = blocker
                current = _validate_job(current, stored=False)
                self._write_job(connection, current)
            return copy.deepcopy(current)
        except StoreError:
            raise
        except sqlite3.Error as error:
            raise StoreError("job") from error

    def record_applied(
        self,
        job_id: str,
        confirmation: dict,
        *,
        expected_job_fingerprint: str,
        expected_settings_version: int,
        readiness_check: Callable[[dict, dict, int], None],
    ) -> dict:
        identifier = _text(job_id, "job.id")
        evidence = _validate_confirmation(confirmation)
        if not isinstance(expected_job_fingerprint, str) or SHA256.fullmatch(expected_job_fingerprint) is None:
            _error("job.fingerprint")
        if type(expected_settings_version) is not int or expected_settings_version < 0:
            _error("settings.version")
        if not callable(readiness_check):
            _error("job.readiness_check")
        self._ensure_initialized()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT payload_json FROM jobs WHERE id = ?", (identifier,)).fetchone()
                if row is None:
                    _error("job.id")
                current = _validate_job(self._load(row["payload_json"], "job"), stored=True)
                if self.job_fingerprint(current) != expected_job_fingerprint:
                    raise StoreError("job.fingerprint conflict")
                settings_row = connection.execute(
                    "SELECT version, payload_json FROM settings WHERE singleton = 1"
                ).fetchone()
                if settings_row is None or settings_row["version"] != expected_settings_version:
                    raise StoreError("settings.version conflict")
                settings = validate_settings(self._load(settings_row["payload_json"], "settings"))
                cap = settings["daily_submission_cap"]
                if not 1 <= cap <= MAXIMUM_DAILY_SUBMISSIONS:
                    raise StoreError("settings.daily_submission_cap")
                today = datetime.now(timezone.utc).date()
                applied_today = 0
                for applied_row in connection.execute(
                    "SELECT id, payload_json, status, country, score FROM jobs"
                ).fetchall():
                    applied_job = _validate_job(self._load(applied_row["payload_json"], "job"), stored=True)
                    if (applied_row["id"], applied_row["status"], applied_row["country"], applied_row["score"]) != (
                        applied_job["id"],
                        applied_job["status"],
                        applied_job["country"],
                        float(applied_job["score"]),
                    ):
                        raise StoreError("integrity.jobs")
                    if applied_job["status"] != "applied":
                        continue
                    observed = datetime.fromisoformat(
                        applied_job["confirmation"]["observed_at"].replace("Z", "+00:00")
                    )
                    if observed.astimezone(timezone.utc).date() == today:
                        applied_today += 1
                if applied_today >= cap:
                    raise StoreError("settings.daily_submission_cap reached")
                if current["status"] != "docs_ready":
                    _error("job.status.docs_ready")
                readiness_check(copy.deepcopy(current), copy.deepcopy(settings), settings_row["version"])
                current["status"] = "applied"
                current["confirmation"] = evidence
                current.pop("blocker", None)
                applied = _validate_job(current, stored=True)
                self._write_job(connection, applied)
            return copy.deepcopy(applied)
        except StoreError:
            raise
        except sqlite3.Error as error:
            raise StoreError("job.applied") from error

    def skip_jobs_if_unchanged(self, expected: list[dict[str, str]]) -> list[dict]:
        if not isinstance(expected, list) or len(expected) > MAX_JSON_COLLECTION_ITEMS:
            _error("jobs.skip")
        normalized: list[dict[str, str]] = []
        identifiers: set[str] = set()
        for item in expected:
            if not isinstance(item, dict) or set(item) != {"id", "status", "fingerprint"}:
                _error("jobs.skip")
            identifier = _text(item.get("id"), "jobs.skip.id")
            status = item.get("status")
            fingerprint = item.get("fingerprint")
            if (
                identifier in identifiers
                or status not in ALLOWED_STATUSES
                or status == "applied"
                or not isinstance(fingerprint, str)
                or SHA256.fullmatch(fingerprint) is None
            ):
                _error("jobs.skip")
            identifiers.add(identifier)
            normalized.append({"id": identifier, "status": status, "fingerprint": fingerprint})
        if not normalized:
            return []
        self._ensure_initialized()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current_jobs: list[dict] = []
                for item in normalized:
                    row = connection.execute(
                        "SELECT payload_json FROM jobs WHERE id = ?", (item["id"],)
                    ).fetchone()
                    if row is None:
                        raise StoreError("jobs.skip conflict")
                    current = _validate_job(self._load(row["payload_json"], "job"), stored=True)
                    if (
                        current["status"] != item["status"]
                        or current["status"] == "applied"
                        or self.job_fingerprint(current) != item["fingerprint"]
                    ):
                        raise StoreError("jobs.skip conflict")
                    current_jobs.append(current)
                changed: list[dict] = []
                for current in current_jobs:
                    current["status"] = "skipped"
                    current["score"] = 0
                    current.pop("blocker", None)
                    current.pop("confirmation", None)
                    skipped = _validate_job(current, stored=False)
                    self._write_job(connection, skipped)
                    changed.append(copy.deepcopy(skipped))
            return changed
        except StoreError:
            raise
        except sqlite3.Error as error:
            raise StoreError("jobs.skip") from error

    def replace_state(self, value: dict) -> dict:
        if not isinstance(value, dict) or not {"meta", "jobs"}.issubset(value):
            _error("state")
        meta = _validate_meta(value.get("meta"))
        raw_jobs = value.get("jobs")
        if not isinstance(raw_jobs, list):
            _error("state.jobs")
        jobs = [_validate_job(row, stored=True) for row in raw_jobs]
        identifiers = [row["id"] for row in jobs]
        if len(set(identifiers)) != len(identifiers):
            _error("state.jobs")
        self._ensure_initialized()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                stored_applied: dict[str, dict] = {}
                for row in connection.execute("SELECT id, payload_json FROM jobs WHERE status = 'applied'").fetchall():
                    stored_applied[row["id"]] = _validate_job(self._load(row["payload_json"], "job"), stored=True)
                proposed_applied = {job["id"]: job for job in jobs if job["status"] == "applied"}
                if proposed_applied != stored_applied:
                    raise StoreError("state.applied: public replace_state must preserve applied rows exactly")
                connection.execute("DELETE FROM jobs WHERE status <> 'applied'")
                for job in jobs:
                    if job["status"] != "applied":
                        self._write_job(connection, job)
                for key, item in meta.items():
                    connection.execute("UPDATE meta SET value_json = ? WHERE key = ?", (self._dump(item), key))
        except StoreError:
            raise
        except sqlite3.Error as error:
            raise StoreError("state") from error
        return self.load_state()

    def save_settings(self, value: dict, *, expected_version: int, reason: str) -> dict:
        try:
            settings = validate_settings(value)
        except SettingsError as error:
            raise StoreError(str(error)) from error
        if type(expected_version) is not int or expected_version < 0:
            _error("settings.version")
        normalized_reason = _text(reason, "settings.reason")
        payload = self._serialized_settings(settings)
        self._ensure_initialized()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT version, payload_json FROM settings WHERE singleton = 1").fetchone()
                if row is None or row["version"] != expected_version:
                    raise StoreError("settings.version conflict")
                current = validate_settings(self._load(row["payload_json"], "settings"))
                if current == settings:
                    return {"version": row["version"], "settings": copy.deepcopy(current)}
                version = row["version"] + 1
                created_at = _now()
                connection.execute(
                    "UPDATE settings SET version = ?, payload_json = ?, updated_at = ? WHERE singleton = 1",
                    (version, payload, created_at),
                )
                connection.execute(
                    "INSERT INTO settings_history(version, payload_json, reason, created_at) VALUES (?, ?, ?, ?)",
                    (version, payload, normalized_reason, created_at),
                )
            return {"version": version, "settings": copy.deepcopy(settings)}
        except StoreError:
            raise
        except (sqlite3.Error, SettingsError) as error:
            raise StoreError("settings") from error

    def settings_history(self) -> list[dict]:
        self._ensure_initialized()
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT version, payload_json, reason, created_at FROM settings_history ORDER BY version"
                ).fetchall()
            return [
                {
                    "version": row["version"],
                    "settings": validate_settings(self._load(row["payload_json"], "settings.history")),
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        except (sqlite3.Error, SettingsError) as error:
            raise StoreError("settings.history") from error

    def dashboard_settings_history(self, *, page: int = 1, page_size: int = 50) -> dict:
        """Return bounded settings audit metadata without settings payloads."""
        page, page_size = _dashboard_pagination(page, page_size)
        self._ensure_initialized()
        try:
            with self._connection() as connection:
                total = connection.execute("SELECT COUNT(*) FROM settings_history").fetchone()[0]
                rows = connection.execute(
                    """
                    SELECT version, reason, created_at FROM settings_history
                    ORDER BY version DESC LIMIT ? OFFSET ?
                    """,
                    (page_size, (page - 1) * page_size),
                ).fetchall()
            items = []
            for row in rows:
                if type(row["version"]) is not int or row["version"] < 1:
                    _error("dashboard.settings_history")
                items.append(
                    {
                        "version": row["version"],
                        "reason": _text(row["reason"], "dashboard.settings_history.reason"),
                        "created_at": _timestamp(row["created_at"], "dashboard.settings_history.created_at"),
                    }
                )
            pages = (total + page_size - 1) // page_size
            return {"items": items, "page": page, "page_size": page_size, "total": total, "pages": pages}
        except StoreError:
            raise
        except sqlite3.Error as error:
            raise StoreError("dashboard.settings_history") from error

    def rollback_settings(
        self,
        version: int | None = None,
        *,
        target_version: int | None = None,
        expected_version: int,
        reason: str = "rollback",
    ) -> dict:
        target = target_version if target_version is not None else version
        if type(target) is not int or target < 1 or (version is not None and target_version is not None):
            _error("settings.rollback.version")
        history = {row["version"]: row for row in self.settings_history()}
        if target not in history:
            _error("settings.rollback.version")
        return self.save_settings(history[target]["settings"], expected_version=expected_version, reason=reason)

    def export_state(self, destination: Path | None = None) -> Path:
        self._ensure_initialized()
        canonical = self.path.with_name("pipeline_state.json")
        if destination is not None:
            try:
                requested = path_without_link_ancestors(Path(destination))
            except OSError as error:
                raise StoreError("export.confinement") from error
            if requested != canonical:
                _error("export.destination")
        target = canonical
        if target == self.path or any(part.casefold() in {"private", "config"} for part in target.parts):
            _error("export.destination")
        try:
            target = path_without_link_ancestors(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            path_without_link_ancestors(target.parent)
            payload = (json.dumps(self.load_state(), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
            descriptor, name = tempfile.mkstemp(prefix=target.name + ".tmp-", dir=target.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                path_without_link_ancestors(target)
                path_without_link_ancestors(temporary)
                os.replace(temporary, target)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            return target
        except OSError as error:
            raise StoreError("export") from error

    def validate_integrity(self) -> dict:
        self._ensure_initialized()
        try:
            with self._connection() as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    _error("integrity.database")
                rows = connection.execute("SELECT id, payload_json, status, country, score FROM jobs ORDER BY id").fetchall()
                for row in rows:
                    job = _validate_job(self._load(row["payload_json"], "job"), stored=True)
                    if (row["id"], row["status"], row["country"], row["score"]) != (
                        job["id"],
                        job["status"],
                        job["country"],
                        float(job["score"]),
                    ):
                        _error("integrity.jobs")
                current = connection.execute("SELECT version, payload_json FROM settings WHERE singleton = 1").fetchone()
                if current is None:
                    _error("integrity.settings")
                current_settings = validate_settings(self._load(current["payload_json"], "settings"))
                if current["version"] == 0 and current_settings != validate_settings(DEFAULT_SETTINGS):
                    _error("integrity.settings_default")
                history_rows = connection.execute("SELECT version, payload_json, reason, created_at FROM settings_history ORDER BY version").fetchall()
                for row in history_rows:
                    validate_settings(self._load(row["payload_json"], "settings.history"))
                    _text(row["reason"], "settings.history.reason")
                    _timestamp(row["created_at"], "settings.history.created_at")
                versions = [row["version"] for row in history_rows]
                if versions != list(range(1, current["version"] + 1)):
                    _error("integrity.settings_history")
                if current["version"] and history_rows[-1]["payload_json"] != current["payload_json"]:
                    _error("integrity.settings_history")
            return {
                "ok": True,
                "job_count": len(rows),
                "settings_version": current["version"],
                "settings_history_count": len(history_rows),
            }
        except StoreError:
            raise
        except (sqlite3.Error, SettingsError) as error:
            raise StoreError("integrity") from error


def _project_root(root: Path) -> Path:
    try:
        return confined_path(root, root)
    except OSError as error:
        raise StoreError("confinement") from error


def _read_import(root: Path, source: Path) -> dict:
    project_root = _project_root(root)
    candidate = source if source.is_absolute() else project_root / source
    try:
        imports = confined_path(project_root, project_root / "private" / "imports")
        candidate = confined_path(project_root, candidate)
        candidate.relative_to(imports)
        if candidate == imports or is_symlink_or_reparse(candidate):
            raise StoreError("import.confinement")
        before = candidate.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise StoreError("import.file")
        with candidate.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            payload = handle.read(MAX_IMPORT_BYTES + 1)
        after = candidate.lstat()
        if is_symlink_or_reparse(candidate) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise StoreError("import.confinement")
    except StoreError:
        raise
    except (OSError, ValueError) as error:
        raise StoreError("import.confinement") from error
    if len(payload) > MAX_IMPORT_BYTES:
        _error("import.size")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise StoreError("import.UTF-8") from error
    value = _strict_json_loads(text, "import.job")
    if not isinstance(value, dict):
        _error("import.job")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage canonical local pipeline state.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("initialize")
    commands.add_parser("settings")
    commands.add_parser("validate")
    commands.add_parser("export")
    upsert = commands.add_parser("upsert-job")
    upsert.add_argument("--input", type=Path, required=True)
    status = commands.add_parser("set-status")
    status.add_argument("--job-id", required=True)
    status.add_argument("--status", required=True)
    status.add_argument("--blocker-category")
    status.add_argument("--blocker-detail")
    arguments = parser.parse_args()
    try:
        root = _project_root(arguments.root)
        store = PipelineStore(confined_path(root, root / "Pipeline" / "pipeline.db"))
        if arguments.command == "initialize":
            store.initialize()
            output: object = store.validate_integrity()
        elif arguments.command == "settings":
            output = store.get_settings()
        elif arguments.command == "validate":
            output = store.validate_integrity()
        elif arguments.command == "export":
            output = {"path": store.export_state().relative_to(root).as_posix()}
        elif arguments.command == "upsert-job":
            output = store.upsert_job(_read_import(root, arguments.input))
        else:
            blocker = None
            if arguments.blocker_category is not None or arguments.blocker_detail is not None:
                blocker = {"category": arguments.blocker_category, "detail": arguments.blocker_detail}
            output = store.set_job_status(arguments.job_id, arguments.status, blocker=blocker)
        print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (StoreError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
