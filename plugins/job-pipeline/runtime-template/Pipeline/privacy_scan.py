from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
import stat
import struct
import sys
import unicodedata
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import yaml

sys.dont_write_bytecode = True

from Pipeline.io_utils import FILE_ATTRIBUTE_REPARSE_POINT, is_symlink_or_reparse
from Pipeline.pipeline_settings import DEFAULT_SETTINGS, SettingsError, validate_settings


MAX_STATIC_FILE_BYTES = 4 * 1024 * 1024
MAX_SCHEMA_BYTES = 512 * 1024
MAX_ZIP_MEMBER_BYTES = 25 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 100 * 1024 * 1024
MAX_ZIP_MEMBERS = 2_000
MAX_ZIP_DEPTH = 3
MAX_DENY_STDIN_BYTES = 1024 * 1024
MAX_DENY_TOKENS = 10_000
MAX_DENY_TOKEN_BYTES = 4_096
READ_CHUNK_BYTES = 64 * 1024

TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".ini",
    ".jinja2",
    ".js",
    ".j2",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".txt",
    ".yaml",
    ".yml",
}
TEXT_NAMES = {".gitignore", "license", "manifest.sha256", "workspace_manifest.sha256", "requirements.txt", "pytest.ini"}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".doc",
    ".docm",
    ".docx",
    ".env",
    ".gif",
    ".jpeg",
    ".jpg",
    ".key",
    ".pdf",
    ".pem",
    ".pfx",
    ".png",
    ".sqlite",
    ".webp",
}
FORBIDDEN_NAMES = {
    ".env",
    "accounts.json",
    "pipeline.db",
    "pipeline.db-shm",
    "pipeline.db-wal",
    "pipeline_state.json",
    "settings.local.json",
}
FORBIDDEN_PARTS = {"artifacts", "backups", "private", "tmp"}
CACHE_OR_DEPENDENCY_PARTS = {".pytest_cache", ".venv", "__pycache__", "node_modules"}
ALLOWED_STATIC_TOP_DIRECTORIES = {".claude", "config", "desktop-import", "documents", "pipeline"}
ALLOWED_STATIC_SUBDIRECTORIES = {
    (".claude", "skills"),
    (".claude", "skills", "job-pipeline-apply"),
    (".claude", "skills", "job-pipeline-onboard"),
    (".claude", "skills", "vie-apply"),
    ("documents", "templates"),
    ("pipeline", "tests"),
}

EMAIL_PATTERN = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@([a-z0-9.-]+\.[a-z]{2,})(?![\w.-])")
PHONE_PATTERN = re.compile(r"(?<!\d)\+[1-9]\d{7,14}(?!\d)")
WINDOWS_USER_PATH_PATTERN = re.compile(r"(?i)[a-z]:[\\/]+users[\\/]+[^\\/\r\n]+")
PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
OAUTH_PATTERN = re.compile(r"(?i)(?:client_secret|refresh_token|access_token|api[_-]?key)\s*[:=]")
SECRET_PATTERN = re.compile(r"(?i)\b(?:xox[baprs]|ghp)_[a-z0-9_-]{20,}\b|\bsk-[a-z0-9_-]{20,}\b")
STREET_ADDRESS_PATTERN = re.compile(
    r"""(?ix)
    (?:
        \b(?:street\s+address|home\s+address|residence\s+address|postal\s+address|address|adresse)\b
        \s*:\s*\d{1,6}[a-z]?(?:\s+[\w'’.-]+){1,10}
    )
    |
    (?:
        \b\d{1,6}[a-z]?\s+
        (?:(?:[\w'’.-]+\s+){0,6})
        (?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|way|place|plaza|square|route|rue|chemin|impasse|all[eé]e|quai|cours)
        \b(?:\s+[\w'’.-]+){0,6}
    )
    """
)
DOB_LABEL = r"(?:date\s+of\s+birth|birth\s+date|dob|date\s+de\s+naissance|n[ée]e?\s+le|born\s+on)"
DOB_NUMERIC_PATTERN = re.compile(
    rf"(?ix)\b{DOB_LABEL}\b\s*:?\s*(?P<day>\d{{1,2}})[./-](?P<month>\d{{1,2}})[./-](?P<year>\d{{4}})\b"
)
DOB_US_NUMERIC_PATTERN = re.compile(
    rf"(?ix)\b{DOB_LABEL}\b\s*:?\s*(?P<month>\d{{1,2}})[./-](?P<day>\d{{1,2}})[./-](?P<year>\d{{4}})\b"
)
DOB_ISO_PATTERN = re.compile(
    rf"(?ix)\b{DOB_LABEL}\b\s*:?\s*(?P<year>\d{{4}})[./-](?P<month>\d{{1,2}})[./-](?P<day>\d{{1,2}})\b"
)
DOB_NAMED_PATTERN = re.compile(
    rf"""(?ix)\b{DOB_LABEL}\b\s*:?\s*
    (?P<day>\d{{1,2}})\s+
    (?P<month>jan(?:uary|vier)?|feb(?:ruary)?|f[eé]v(?:rier)?|mar(?:ch|s)?|apr(?:il)?|avr(?:il)?|
    may|mai|jun(?:e)?|juin|jul(?:y|let)?|aug(?:ust)?|ao[uû]t|sep(?:t(?:ember|embre)?)?|
    oct(?:ober|obre)?|nov(?:ember|embre)?|dec(?:ember)?|d[eé]c(?:embre)?)\s+
    (?P<year>\d{{4}})\b
    """
)
DOB_NAMED_MONTH_FIRST_PATTERN = re.compile(
    rf"""(?ix)\b{DOB_LABEL}\b\s*:?\s*
    (?P<month>jan(?:uary|vier)?|feb(?:ruary)?|f[eé]v(?:rier)?|mar(?:ch|s)?|apr(?:il)?|avr(?:il)?|
    may|mai|jun(?:e)?|juin|jul(?:y|let)?|aug(?:ust)?|ao[uû]t|sep(?:t(?:ember|embre)?)?|
    oct(?:ober|obre)?|nov(?:ember|embre)?|dec(?:ember)?|d[eé]c(?:embre)?)\s+
    (?P<day>\d{{1,2}})(?:\s*,\s*|\s+)
    (?P<year>\d{{4}})\b
    """
)
RESERVED_EMAIL_DOMAINS = {"example.com"}
RESERVED_EMAIL_SUFFIXES = (".invalid", ".test", ".example")
WINDOWS_RESERVED = {
    "aux",
    "clock$",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
BINARY_PREFIXES = (
    b"SQLite format 3\x00",
    b"%PDF-",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
    b"\x7fELF",
    b"MZ",
    b"\x1f\x8b",
    b"BZh",
    b"\xfd7zXZ\x00",
    b"7z\xbc\xaf'\x1c",
    b"Rar!\x1a\x07\x00",
    b"Rar!\x1a\x07\x01\x00",
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"MSCF",
)
ALLOWED_TEXT_CONTROLS = {9, 10, 12, 13}
MONTH_NUMBERS = {
    "jan": 1,
    "january": 1,
    "janvier": 1,
    "feb": 2,
    "february": 2,
    "fev": 2,
    "fevrier": 2,
    "mar": 3,
    "march": 3,
    "mars": 3,
    "apr": 4,
    "april": 4,
    "avr": 4,
    "avril": 4,
    "may": 5,
    "mai": 5,
    "jun": 6,
    "june": 6,
    "juin": 6,
    "jul": 7,
    "july": 7,
    "juillet": 7,
    "aug": 8,
    "august": 8,
    "aout": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "septembre": 9,
    "oct": 10,
    "october": 10,
    "octobre": 10,
    "nov": 11,
    "november": 11,
    "novembre": 11,
    "dec": 12,
    "december": 12,
    "decembre": 12,
}
MAX_SQLITE_SCHEMA_OBJECTS = 16
MAX_SQLITE_VM_STEPS = 250_000
EXPECTED_SQLITE_SCHEMA = {
    ("index", "idx_jobs_country", "jobs"): "create index idx_jobs_country on jobs(country)",
    ("index", "idx_jobs_score", "jobs"): "create index idx_jobs_score on jobs(score desc)",
    ("index", "idx_jobs_status", "jobs"): "create index idx_jobs_status on jobs(status)",
    ("index", "sqlite_autoindex_jobs_1", "jobs"): None,
    ("index", "sqlite_autoindex_meta_1", "meta"): None,
    ("table", "jobs", "jobs"): (
        "create table jobs ( id text primary key, payload_json text not null, status text not null, "
        "country text not null, score real not null, updated_at text not null )"
    ),
    ("table", "meta", "meta"): "create table meta ( key text primary key, value_json text not null )",
    ("table", "settings", "settings"): (
        "create table settings ( singleton integer primary key check(singleton = 1), version integer not null, "
        "payload_json text not null, updated_at text )"
    ),
    ("table", "settings_history", "settings_history"): (
        "create table settings_history ( version integer primary key, payload_json text not null, "
        "reason text not null, created_at text not null )"
    ),
    ("trigger", "settings_history_no_delete", "settings_history"): (
        "create trigger settings_history_no_delete before delete on settings_history begin "
        "select raise(abort, 'settings_history is immutable'); end"
    ),
    ("trigger", "settings_history_no_update", "settings_history"): (
        "create trigger settings_history_no_update before update on settings_history begin "
        "select raise(abort, 'settings_history is immutable'); end"
    ),
}
EXPECTED_SQLITE_COLUMNS = {
    "meta": (
        (0, "key", "TEXT", 0, None, 1),
        (1, "value_json", "TEXT", 1, None, 0),
    ),
    "jobs": (
        (0, "id", "TEXT", 0, None, 1),
        (1, "payload_json", "TEXT", 1, None, 0),
        (2, "status", "TEXT", 1, None, 0),
        (3, "country", "TEXT", 1, None, 0),
        (4, "score", "REAL", 1, None, 0),
        (5, "updated_at", "TEXT", 1, None, 0),
    ),
    "settings": (
        (0, "singleton", "INTEGER", 0, None, 1),
        (1, "version", "INTEGER", 1, None, 0),
        (2, "payload_json", "TEXT", 1, None, 0),
        (3, "updated_at", "TEXT", 0, None, 0),
    ),
    "settings_history": (
        (0, "version", "INTEGER", 0, None, 1),
        (1, "payload_json", "TEXT", 1, None, 0),
        (2, "reason", "TEXT", 1, None, 0),
        (3, "created_at", "TEXT", 1, None, 0),
    ),
}


BLANK_CANDIDATE = {
    "schema_version": 1,
    "identity": {"full_name": "", "email": "", "phone": "", "residence_country": ""},
    "current_employer": "",
    "employment_status": "",
    "work_authorization": [],
    "languages": [],
    "notice_period": "",
    "known_answers": {},
    "career_evidence": [],
}
BLANK_PREFERENCES = {
    "schema_version": 1,
    "target_titles": [],
    "role_families": [],
    "industries": [],
    "seniority": {"minimum": "", "maximum": ""},
    "countries": [],
    "work_modes": [],
    "salary": {"minimum": None, "currency": ""},
    "include_keywords": [],
    "exclude_keywords": [],
    "excluded_employers": [],
    "preferred_company_types": [],
    "vie": {"enabled": False, "eligible": False},
}
BLANK_ACCOUNTS = {
    "schema_version": 1,
    "identity_providers": [],
    "employer_tenants": [],
    "no_account_platforms": [],
}
BLANK_RUNTIME = {
    "schema_version": 1,
    "submission_mode": "review_only",
    "browser_profile": "",
    "master_cv_path": "",
    "master_cv_sha256": "",
    "onboarding": {"complete": False, "creator_ready": False},
    "auto_apply_approved_at": None,
}


@dataclass(frozen=True, order=True)
class Finding:
    category: str
    path: str


class PrivacyScanError(RuntimeError):
    """Fail-closed scan error containing category and relative path only."""


class _SafeArgumentError(Exception):
    pass


class _SafeHelpExit(Exception):
    pass


class _RedactingArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _SafeArgumentError

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status == 0:
            raise _SafeHelpExit
        raise _SafeArgumentError


class _Collector:
    def __init__(self, deny_tokens: tuple[str, ...]) -> None:
        self._items: set[Finding] = set()
        self.deny_tokens = deny_tokens

    def add(self, category: str, path: str) -> None:
        self._items.add(Finding(category, _safe_display(path, self.deny_tokens)))

    def findings(self) -> list[Finding]:
        return sorted(self._items, key=lambda item: (item.category, item.path.casefold(), item.path))


@dataclass
class _ZipBudget:
    members: int = 0
    declared_bytes: int = 0
    actual_bytes: int = 0


@dataclass(frozen=True)
class _RawZipMember:
    name: str
    raw_name: bytes
    flags: int
    method: int
    crc: int
    compressed_size: int
    file_size: int
    external_attr: int
    create_system: int
    local_offset: int
    data_offset: int


@dataclass(frozen=True)
class _RawZipLayout:
    members: tuple[_RawZipMember, ...]


@dataclass(frozen=True)
class _PathGuardEntry:
    path: Path
    identity: tuple[int, int, int, int]


@dataclass(frozen=True)
class _PathGuard:
    path: Path
    metadata: os.stat_result
    entries: tuple[_PathGuardEntry, ...]
    target_fingerprint: tuple[int, int, int, int, int, int]


class _StrictYAMLLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise ValueError("schema")
        return super().compose_node(parent, index)


def _strict_yaml_mapping(loader: _StrictYAMLLoader, node: Any, deep: bool = False) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int, float, bool, type(None))) or key in result:
            raise ValueError("schema")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictYAMLLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_yaml_mapping)


def _contains_format_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cf" for character in value)


def _matching_text(value: str) -> str:
    without_format_controls = "".join(
        character for character in value if unicodedata.category(character) != "Cf"
    )
    return unicodedata.normalize("NFKC", without_format_controls)


def _normalize_text(value: str) -> str:
    return " ".join(_matching_text(value).casefold().split())


def _normalized_deny_tokens(values: Sequence[str]) -> tuple[str, ...]:
    normalized = {
        item
        for value in values
        if isinstance(value, str)
        for item in (_normalize_text(value),)
        if len(item) >= 6
    }
    return tuple(sorted(normalized))


def _reserved_email(domain: str) -> bool:
    normalized = unicodedata.normalize("NFKC", domain).casefold().rstrip(".")
    return normalized in RESERVED_EMAIL_DOMAINS or normalized.endswith(RESERVED_EMAIL_SUFFIXES)


def _ascii_fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _plausible_date(year: str, month: str | int, day: str) -> bool:
    try:
        numeric_month = month if isinstance(month, int) else int(month)
        value = date(int(year), numeric_month, int(day))
    except (TypeError, ValueError):
        return False
    return 1900 <= value.year <= date.today().year


def _contains_labeled_birth_date(value: str) -> bool:
    for pattern in (DOB_NUMERIC_PATTERN, DOB_US_NUMERIC_PATTERN, DOB_ISO_PATTERN):
        for match in pattern.finditer(value):
            if _plausible_date(match.group("year"), match.group("month"), match.group("day")):
                return True
    for pattern in (DOB_NAMED_PATTERN, DOB_NAMED_MONTH_FIRST_PATTERN):
        for match in pattern.finditer(value):
            month = MONTH_NUMBERS.get(_ascii_fold(match.group("month")))
            if month is not None and _plausible_date(match.group("year"), month, match.group("day")):
                return True
    return False


def _binary_text_payload(payload: bytes) -> bool:
    if any(payload.startswith(signature) for signature in BINARY_PREFIXES):
        return True
    if len(payload) >= 262 and payload[257:262] == b"ustar":
        return True
    if len(payload) >= 12 and payload[:4] in {b"RIFF", b"RIFX"} and payload[8:12] in {b"WEBP", b"AVI ", b"WAVE"}:
        return True
    if any(byte < 32 and byte not in ALLOWED_TEXT_CONTROLS or byte == 127 for byte in payload):
        return True
    return False


def _path_contains_sensitive_text(path: str, deny_tokens: Sequence[str]) -> bool:
    if _contains_format_control(path):
        return True
    normalized = _matching_text(path)
    folded = _normalize_text(path)
    if any(token in folded for token in deny_tokens):
        return True
    if (
        SECRET_PATTERN.search(normalized)
        or WINDOWS_USER_PATH_PATTERN.search(normalized)
        or PRIVATE_KEY_PATTERN.search(normalized)
        or OAUTH_PATTERN.search(normalized)
        or STREET_ADDRESS_PATTERN.search(normalized)
        or _contains_labeled_birth_date(normalized)
    ):
        return True
    if any(match.group(0) != "+10000000000" for match in PHONE_PATTERN.finditer(normalized)):
        return True
    if any(not _reserved_email(match.group(1)) for match in EMAIL_PATTERN.finditer(normalized)):
        return True
    return False


def _safe_display(path: str, deny_tokens: Sequence[str]) -> str:
    candidate = path.replace("\\", "/") or "."
    if Path(candidate).is_absolute() or DRIVE_PREFIX.match(candidate) or _path_contains_sensitive_text(candidate, deny_tokens):
        return "<redacted-path>"
    if len(candidate) > 512:
        return "<long-path>"
    return candidate


def _is_link_like(path: Path, metadata: os.stat_result | None = None) -> bool:
    if is_symlink_or_reparse(path):
        return True
    if metadata is None:
        try:
            metadata = path.lstat()
        except OSError:
            return False
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & FILE_ATTRIBUTE_REPARSE_POINT)


def _path_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(stat.S_IFMT(metadata.st_mode)),
        int(getattr(metadata, "st_file_attributes", 0)),
    )


def _target_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return _path_identity(metadata) + (int(metadata.st_size), int(metadata.st_mtime_ns))


def _has_lexical_parent(path: Path) -> bool:
    raw = unicodedata.normalize("NFKC", os.fspath(path)).replace("\\", "/")
    return any(part == ".." for part in raw.split("/"))


def _prepare_path_guard(path: Path) -> tuple[_PathGuard | None, str | None]:
    candidate = Path(path)
    if _has_lexical_parent(candidate):
        return None, "unsafe_path"
    try:
        absolute = Path(os.path.abspath(candidate))
    except (OSError, TypeError, ValueError):
        return None, "scan_error"
    chain = list(reversed(absolute.parents)) + [absolute]
    entries: list[_PathGuardEntry] = []
    target_metadata: os.stat_result | None = None
    for index, component in enumerate(chain):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            return None, "missing_path" if index == len(chain) - 1 else "scan_error"
        except OSError:
            return None, "scan_error"
        if _is_link_like(component, metadata):
            return None, "link_or_reparse"
        if index < len(chain) - 1 and not stat.S_ISDIR(metadata.st_mode):
            return None, "scan_error"
        entries.append(_PathGuardEntry(component, _path_identity(metadata)))
        target_metadata = metadata
    assert target_metadata is not None
    return _PathGuard(absolute, target_metadata, tuple(entries), _target_fingerprint(target_metadata)), None


def _path_guard_stable(guard: _PathGuard) -> bool:
    for entry in guard.entries:
        try:
            metadata = entry.path.lstat()
        except OSError:
            return False
        if _is_link_like(entry.path, metadata) or _path_identity(metadata) != entry.identity:
            return False
    try:
        target_metadata = guard.path.lstat()
    except OSError:
        return False
    return not _is_link_like(guard.path, target_metadata) and _target_fingerprint(target_metadata) == guard.target_fingerprint


def _directory_fingerprint(path: Path) -> tuple[int, int, int, int, int, int] | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if _is_link_like(path, metadata) or not stat.S_ISDIR(metadata.st_mode):
        return None
    return _target_fingerprint(metadata)


def _component_analysis(component: str) -> tuple[str, bool, bool]:
    if not component or "\x00" in component or "/" in component or "\\" in component or ":" in component:
        return "", True, False
    nfkc = _matching_text(component)
    trimmed = nfkc.rstrip(" .")
    unsafe = not trimmed or trimmed in {".", ".."} or any(character in trimmed for character in ("\x00", "/", "\\", ":"))
    base = trimmed.split(".", 1)[0].casefold()
    alias = _contains_format_control(component) or nfkc != component or trimmed != nfkc or base in WINDOWS_RESERVED
    return trimmed.casefold(), unsafe, alias


def _relative_analysis(relative: str) -> tuple[str, bool, bool]:
    parts = PurePosixPath(relative).parts
    keys: list[str] = []
    unsafe = False
    alias = False
    for part in parts:
        key, bad, is_alias = _component_analysis(part)
        keys.append(key)
        unsafe = unsafe or bad
        alias = alias or is_alias
    return "/".join(keys), unsafe, alias


def _source_directory_allowed(parts: tuple[str, ...]) -> bool:
    folded = tuple(part.casefold() for part in parts)
    if len(folded) == 1:
        return folded[0] in ALLOWED_STATIC_TOP_DIRECTORIES
    if folded in ALLOWED_STATIC_SUBDIRECTORIES:
        return True
    return len(folded) == 4 and folded[:2] == (".claude", "skills") and folded[2] in {
        "job-pipeline-apply",
        "job-pipeline-onboard",
        "vie-apply",
    }


def _static_policy(relative: str, *, is_directory: bool) -> str | None:
    parts = tuple(part.casefold() for part in PurePosixPath(relative).parts)
    if any(part in CACHE_OR_DEPENDENCY_PARTS for part in parts):
        return "cache_or_dependency"
    if any(part in FORBIDDEN_PARTS for part in parts):
        return "forbidden_path"
    if is_directory:
        return None if _source_directory_allowed(tuple(PurePosixPath(relative).parts)) else "unexpected_source_path"
    name = parts[-1] if parts else ""
    if name in FORBIDDEN_NAMES:
        return "forbidden_name"
    suffix = Path(name).suffix.casefold()
    if name.endswith(('.db-shm', '.db-wal')) or suffix in FORBIDDEN_SUFFIXES:
        return "forbidden_suffix"
    if suffix == ".zip":
        return None
    if suffix in TEXT_SUFFIXES or name in TEXT_NAMES:
        return None
    return "unsupported_file"


def _stable_read(path: Path, maximum: int) -> tuple[bytes | None, str | None]:
    try:
        before = path.lstat()
        if _is_link_like(path, before) or not stat.S_ISREG(before.st_mode):
            return None, "link_or_reparse"
        if before.st_nlink != 1:
            return None, "hardlink"
        if before.st_size > maximum:
            return None, "file_size"
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = handle.read(min(READ_CHUNK_BYTES, maximum + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum:
                    return None, "file_size"
        after = path.lstat()
    except OSError:
        return None, "scan_error"
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_open = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_open or identity_before != identity_after or _is_link_like(path, after) or after.st_nlink != 1:
        return None, "scan_error"
    return b"".join(chunks), None


def _bounded_structure(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [0]
    budget[0] += 1
    if budget[0] > 10_000 or depth > 32:
        raise ValueError("schema")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 512:
                raise ValueError("schema")
            _bounded_structure(child, depth=depth + 1, budget=budget)
    elif isinstance(value, list):
        for child in value:
            _bounded_structure(child, depth=depth + 1, budget=budget)
    elif isinstance(value, str):
        if len(value) > MAX_SCHEMA_BYTES:
            raise ValueError("schema")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError("schema")


def _strict_json(payload: bytes) -> Any:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in rows:
            if key in result:
                raise ValueError("schema")
            result[key] = value
        return result

    text = payload.decode("utf-8")
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("schema")))
    _bounded_structure(value)
    return value


def _strict_yaml(payload: bytes) -> Any:
    text = payload.decode("utf-8")
    value = yaml.load(text, Loader=_StrictYAMLLoader)
    _bounded_structure(value)
    return value


def _scan_text(payload: bytes, display: str, collector: _Collector, logical_path: str) -> None:
    if _binary_text_payload(payload):
        collector.add("binary_content", display)
        return
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        collector.add("invalid_utf8", display)
        return
    if _contains_format_control(text):
        collector.add("format_control", display)
    if any(unicodedata.category(character) == "Cc" and ord(character) not in ALLOWED_TEXT_CONTROLS for character in text):
        collector.add("binary_content", display)
        return
    normalized = _matching_text(text)
    for match in EMAIL_PATTERN.finditer(normalized):
        if not _reserved_email(match.group(1)):
            collector.add("email", display)
            break
    for match in PHONE_PATTERN.finditer(normalized):
        if match.group(0) != "+10000000000":
            collector.add("phone", display)
            break
    if WINDOWS_USER_PATH_PATTERN.search(normalized):
        collector.add("windows_user_path", display)
    if PRIVATE_KEY_PATTERN.search(normalized):
        collector.add("private_key", display)
    if OAUTH_PATTERN.search(normalized):
        collector.add("oauth_or_token", display)
    if SECRET_PATTERN.search(normalized):
        collector.add("secret", display)
    if STREET_ADDRESS_PATTERN.search(normalized):
        collector.add("street_address", display)
    if _contains_labeled_birth_date(normalized):
        collector.add("date_of_birth", display)
    folded = _normalize_text(text)
    if any(token in folded for token in collector.deny_tokens):
        collector.add("private_identifier", display)
    if logical_path.casefold().endswith("config/candidate.example.yml"):
        try:
            candidate = _strict_yaml(payload)
            identity = candidate.get("identity") if isinstance(candidate, dict) else None
            if not isinstance(identity, dict) or set(identity) != {"full_name", "email", "phone", "residence_country"}:
                raise ValueError("schema")
            if any(not isinstance(value, str) or value for value in identity.values()):
                raise ValueError("schema")
        except (UnicodeDecodeError, ValueError, TypeError, yaml.YAMLError, RecursionError):
            collector.add("structured_identity", display)


def _scan_logical_path_text(path: str, collector: _Collector) -> None:
    display = _safe_display(path, collector.deny_tokens)
    if _contains_format_control(path):
        collector.add("format_control", display)
    normalized = _matching_text(path)
    for match in EMAIL_PATTERN.finditer(normalized):
        if not _reserved_email(match.group(1)):
            collector.add("email", display)
            break
    for match in PHONE_PATTERN.finditer(normalized):
        if match.group(0) != "+10000000000":
            collector.add("phone", display)
            break
    if WINDOWS_USER_PATH_PATTERN.search(normalized):
        collector.add("windows_user_path", display)
    if PRIVATE_KEY_PATTERN.search(normalized):
        collector.add("private_key", display)
    if OAUTH_PATTERN.search(normalized):
        collector.add("oauth_or_token", display)
    if SECRET_PATTERN.search(normalized):
        collector.add("secret", display)
    if STREET_ADDRESS_PATTERN.search(normalized):
        collector.add("street_address", display)
    if _contains_labeled_birth_date(normalized):
        collector.add("date_of_birth", display)
    folded = _normalize_text(path)
    if any(token in folded for token in collector.deny_tokens):
        collector.add("private_identifier", display)


def _scan_static_file(path: Path, relative: str, collector: _Collector) -> None:
    policy = _static_policy(relative, is_directory=False)
    if policy:
        collector.add(policy, relative)
        return
    suffix = path.suffix.casefold()
    maximum = MAX_ZIP_TOTAL_BYTES + 1024 * 1024 if suffix == ".zip" else MAX_STATIC_FILE_BYTES
    payload, error = _stable_read(path, maximum)
    if error:
        category = "zip_total_size" if suffix == ".zip" and error == "file_size" else error
        collector.add(category, relative)
        return
    assert payload is not None
    if suffix == ".zip":
        _scan_zip_bytes(payload, relative, 1, _ZipBudget(), collector)
    else:
        _scan_text(payload, relative, collector, relative)


def _directory_entries(path: Path) -> list[os.DirEntry[str]] | None:
    try:
        before = path.lstat()
        if _is_link_like(path, before) or not stat.S_ISDIR(before.st_mode):
            return None
        with os.scandir(path) as iterator:
            entries = sorted(list(iterator), key=lambda item: (unicodedata.normalize("NFKC", item.name).casefold(), item.name))
        after = path.lstat()
    except OSError:
        return None
    if (
        _is_link_like(path, after)
        or not stat.S_ISDIR(after.st_mode)
        or (before.st_dev, before.st_ino, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns)
    ):
        return None
    return entries


def _scan_source_directory(root: Path, collector: _Collector) -> None:
    try:
        metadata = root.lstat()
    except OSError:
        collector.add("scan_error", ".")
        return
    if _is_link_like(root, metadata) or not stat.S_ISDIR(metadata.st_mode):
        collector.add("link_or_reparse", ".")
        return

    def walk(directory: Path, relative_parts: tuple[str, ...]) -> None:
        fingerprint = _directory_fingerprint(directory)
        if fingerprint is None:
            collector.add("scan_error", "/".join(relative_parts) or ".")
            return
        entries = _directory_entries(directory)
        if entries is None:
            collector.add("scan_error", "/".join(relative_parts) or ".")
            return
        seen: dict[str, str] = {}
        for entry in entries:
            relative = "/".join(relative_parts + (entry.name,))
            _scan_logical_path_text(relative, collector)
            key, unsafe, alias = _component_analysis(entry.name)
            if unsafe:
                collector.add("unsafe_path", relative)
                continue
            if alias:
                collector.add("windows_path_alias", relative)
            if key in seen:
                collector.add("duplicate_path_alias", relative)
                continue
            seen[key] = entry.name
            path = Path(entry.path)
            try:
                metadata = path.lstat()
            except OSError:
                collector.add("scan_error", relative)
                continue
            if _is_link_like(path, metadata):
                collector.add("link_or_reparse", relative)
                continue
            if stat.S_ISDIR(metadata.st_mode):
                policy = _static_policy(relative, is_directory=True)
                if policy:
                    collector.add(policy, relative)
                    continue
                walk(path, relative_parts + (entry.name,))
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    collector.add("hardlink", relative)
                    continue
                _scan_static_file(path, relative, collector)
            else:
                collector.add("unsupported_file", relative)
        if _directory_fingerprint(directory) != fingerprint:
            collector.add("scan_error", "/".join(relative_parts) or ".")

    walk(root, ())


def _decode_zip_name(raw_name: bytes, flags: int) -> str:
    return raw_name.decode("utf-8" if flags & 0x800 else "cp437")


def _zip_metadata_error(flags: int, method: int) -> str | None:
    if flags & 0x1:
        return "zip_encrypted"
    if flags & ~0x800:
        return "zip_invalid"
    if method not in {ZIP_STORED, ZIP_DEFLATED}:
        return "zip_unsupported_compression"
    return None


def _parse_raw_zip(payload: bytes) -> tuple[_RawZipLayout | None, str | None]:
    if len(payload) < 52 or payload[:4] != b"PK\x03\x04":
        return None, "zip_invalid"
    eocd_offset = len(payload) - 22
    try:
        (
            signature,
            disk,
            central_disk,
            disk_entries,
            total_entries,
            central_size,
            central_offset,
            archive_comment_size,
        ) = struct.unpack_from("<I4H2IH", payload, eocd_offset)
    except struct.error:
        return None, "zip_invalid"
    if (
        signature != 0x06054B50
        or archive_comment_size != 0
        or disk != 0
        or central_disk != 0
        or disk_entries != total_entries
        or total_entries in {0, 0xFFFF}
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
        or central_offset <= 0
        or central_offset + central_size != eocd_offset
    ):
        return None, "zip_invalid"
    if total_entries > MAX_ZIP_MEMBERS:
        return None, "zip_member_limit"

    local_records: dict[int, tuple[int, bytes, str, int, int, int, int, int, int, int]] = {}
    offset = 0
    try:
        while offset < central_offset:
            if offset + 30 > central_offset:
                return None, "zip_invalid"
            (
                local_signature,
                version_needed,
                flags,
                method,
                _modified_time,
                _modified_date,
                crc,
                compressed_size,
                file_size,
                name_size,
                extra_size,
            ) = struct.unpack_from("<I5H3I2H", payload, offset)
            if local_signature != 0x04034B50 or not name_size or extra_size:
                return None, "zip_invalid"
            metadata_error = _zip_metadata_error(flags, method)
            if metadata_error:
                return None, metadata_error
            if compressed_size == 0xFFFFFFFF or file_size == 0xFFFFFFFF:
                return None, "zip_invalid"
            name_start = offset + 30
            name_end = name_start + name_size
            data_start = name_end
            data_end = data_start + compressed_size
            if name_end > central_offset or data_end > central_offset:
                return None, "zip_invalid"
            raw_name = payload[name_start:name_end]
            name = _decode_zip_name(raw_name, flags)
            if offset in local_records:
                return None, "zip_invalid"
            local_records[offset] = (
                version_needed,
                raw_name,
                name,
                flags,
                method,
                crc,
                compressed_size,
                file_size,
                data_start,
                data_end,
            )
            offset = data_end
    except (UnicodeDecodeError, struct.error):
        return None, "zip_invalid"
    if offset != central_offset or len(local_records) != total_entries:
        return None, "zip_invalid"

    members: list[_RawZipMember] = []
    used_local_offsets: set[int] = set()
    offset = central_offset
    central_end = central_offset + central_size
    try:
        for _index in range(total_entries):
            if offset + 46 > central_end:
                return None, "zip_invalid"
            (
                central_signature,
                version_made,
                version_needed,
                flags,
                method,
                _modified_time,
                _modified_date,
                crc,
                compressed_size,
                file_size,
                name_size,
                extra_size,
                member_comment_size,
                member_disk,
                _internal_attr,
                external_attr,
                local_offset,
            ) = struct.unpack_from("<I6H3I5H2I", payload, offset)
            if (
                central_signature != 0x02014B50
                or not name_size
                or extra_size
                or member_comment_size
                or member_disk != 0
                or compressed_size == 0xFFFFFFFF
                or file_size == 0xFFFFFFFF
                or local_offset == 0xFFFFFFFF
            ):
                return None, "zip_invalid"
            metadata_error = _zip_metadata_error(flags, method)
            if metadata_error:
                return None, metadata_error
            record_end = offset + 46 + name_size
            if record_end > central_end:
                return None, "zip_invalid"
            raw_name = payload[offset + 46 : record_end]
            name = _decode_zip_name(raw_name, flags)
            local = local_records.get(local_offset)
            if local is None or local_offset in used_local_offsets:
                return None, "zip_invalid"
            (
                local_version_needed,
                local_raw_name,
                local_name,
                local_flags,
                local_method,
                local_crc,
                local_compressed_size,
                local_file_size,
                local_data_start,
                _local_end,
            ) = local
            if (
                version_needed != local_version_needed
                or raw_name != local_raw_name
                or name != local_name
                or flags != local_flags
                or method != local_method
                or crc != local_crc
                or compressed_size != local_compressed_size
                or file_size != local_file_size
            ):
                return None, "zip_invalid"
            used_local_offsets.add(local_offset)
            members.append(
                _RawZipMember(
                    name=name,
                    raw_name=raw_name,
                    flags=flags,
                    method=method,
                    crc=crc,
                    compressed_size=compressed_size,
                    file_size=file_size,
                    external_attr=external_attr,
                    create_system=(version_made >> 8) & 0xFF,
                    local_offset=local_offset,
                    data_offset=local_data_start,
                )
            )
            offset = record_end
    except (UnicodeDecodeError, struct.error):
        return None, "zip_invalid"
    if offset != central_end or offset != eocd_offset or used_local_offsets != set(local_records):
        return None, "zip_invalid"
    return _RawZipLayout(tuple(members)), None


def _member_analysis(name: str) -> tuple[str | None, str | None, bool, bool]:
    if not name or "\x00" in name or "\\" in name or name.startswith("//") or DRIVE_PREFIX.match(name) or ":" in name:
        category = "zip_path_traversal" if name.startswith(("/", "//")) or DRIVE_PREFIX.match(name) else "zip_unsafe_name"
        return None, category, False, False
    is_directory = name.endswith("/")
    body = name[:-1] if is_directory else name
    if not body or body.startswith("/"):
        return None, "zip_path_traversal", is_directory, False
    raw_parts = body.split("/")
    if any(part == ".." for part in raw_parts):
        return None, "zip_path_traversal", is_directory, False
    if any(not part or part == "." for part in raw_parts):
        return None, "zip_unsafe_name", is_directory, False
    normalized_parts: list[str] = []
    alias = False
    for part in raw_parts:
        key, unsafe, component_alias = _component_analysis(part)
        if unsafe:
            return None, "zip_unsafe_name", is_directory, False
        normalized_parts.append(key)
        alias = alias or component_alias
    return "/".join(normalized_parts), None, is_directory, alias


def _zip_link_or_device(info: ZipInfo) -> bool:
    attributes = info.external_attr & 0xFFFF
    if attributes & (FILE_ATTRIBUTE_REPARSE_POINT | 0x40):
        return True
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    return kind not in {0, stat.S_IFREG, stat.S_IFDIR}


def _zip_attribute_mismatch(info: ZipInfo, is_directory: bool) -> bool:
    if info.create_system not in {0, 3}:
        return True
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    marked_directory = bool((info.external_attr & 0xFFFF) & 0x10)
    if marked_directory and not is_directory:
        return True
    if kind == stat.S_IFDIR and not is_directory:
        return True
    if kind == stat.S_IFREG and is_directory:
        return True
    if is_directory and not marked_directory and kind != stat.S_IFDIR:
        return True
    if info.create_system == 0 and marked_directory != is_directory:
        return True
    return False


def _member_display(logical: str, normalized_name: str | None, index: int, collector: _Collector) -> str:
    suffix = normalized_name if normalized_name is not None else f"<member-{index:04d}>"
    return _safe_display(f"{logical}!/{suffix}", collector.deny_tokens)


def _zip_output_limit(budget: _ZipBudget, member_total: int) -> tuple[int, str | None]:
    if budget.actual_bytes > MAX_ZIP_TOTAL_BYTES:
        return 0, "zip_total_size"
    if member_total > MAX_ZIP_MEMBER_BYTES:
        return 0, "zip_member_size"
    return min(
        MAX_ZIP_TOTAL_BYTES - budget.actual_bytes,
        MAX_ZIP_MEMBER_BYTES - member_total,
    ) + 1, None


def _charge_zip_output(budget: _ZipBudget, member_total: int, produced: int) -> tuple[int, str | None]:
    budget.actual_bytes += produced
    member_total += produced
    if budget.actual_bytes > MAX_ZIP_TOTAL_BYTES:
        return member_total, "zip_total_size"
    if member_total > MAX_ZIP_MEMBER_BYTES:
        return member_total, "zip_member_size"
    return member_total, None


def _read_zip_member(
    payload: bytes,
    member: _RawZipMember,
    budget: _ZipBudget,
) -> tuple[bytes | None, str | None]:
    compressed = memoryview(payload)[member.data_offset : member.data_offset + member.compressed_size]
    if len(compressed) != member.compressed_size:
        return None, "zip_invalid"
    chunks: list[bytes] = []
    member_total = 0
    if member.method == ZIP_STORED:
        cursor = 0
        while cursor < len(compressed):
            output_limit, error = _zip_output_limit(budget, member_total)
            if error:
                return None, error
            output = compressed[cursor : cursor + min(READ_CHUNK_BYTES, output_limit)].tobytes()
            cursor += len(output)
            member_total, error = _charge_zip_output(budget, member_total, len(output))
            if error:
                return None, error
            chunks.append(output)
    else:
        decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
        cursor = 0
        try:
            while cursor < len(compressed):
                pending = compressed[cursor : cursor + READ_CHUNK_BYTES].tobytes()
                cursor += len(pending)
                while pending:
                    output_limit, error = _zip_output_limit(budget, member_total)
                    if error:
                        return None, error
                    output = decompressor.decompress(pending, output_limit)
                    if output:
                        member_total, error = _charge_zip_output(budget, member_total, len(output))
                        if error:
                            return None, error
                        chunks.append(output)
                    tail = decompressor.unconsumed_tail
                    if tail and len(tail) == len(pending) and not output:
                        return None, "zip_invalid"
                    pending = tail
                if decompressor.eof:
                    if decompressor.unused_data or cursor != len(compressed):
                        return None, "zip_invalid"
                    break
            if not decompressor.eof:
                return None, "zip_crc"
            output_limit, error = _zip_output_limit(budget, member_total)
            if error:
                return None, error
            flushed = decompressor.flush(output_limit)
            if flushed:
                member_total, error = _charge_zip_output(budget, member_total, len(flushed))
                if error:
                    return None, error
                chunks.append(flushed)
        except (MemoryError, zlib.error):
            return None, "zip_crc"
    result = b"".join(chunks)
    if len(result) != member.file_size:
        return None, "zip_size_mismatch"
    if zlib.crc32(result) & 0xFFFFFFFF != member.crc:
        return None, "zip_crc"
    return result, None


def _scan_zip_bytes(payload: bytes, logical: str, depth: int, budget: _ZipBudget, collector: _Collector) -> None:
    if depth > MAX_ZIP_DEPTH:
        collector.add("zip_depth", logical)
        return
    if len(payload) > MAX_ZIP_TOTAL_BYTES + 1024 * 1024:
        collector.add("zip_total_size", logical)
        return
    layout, layout_error = _parse_raw_zip(payload)
    if layout_error:
        collector.add(layout_error, logical)
        return
    assert layout is not None
    declared_count = len(layout.members)
    if declared_count > MAX_ZIP_MEMBERS - budget.members:
        collector.add("zip_member_limit", logical)
        return
    try:
        archive = ZipFile(io.BytesIO(payload), "r")
    except (BadZipFile, EOFError, OSError, UnicodeError, ValueError):
        collector.add("zip_invalid", logical)
        return
    with archive:
        infos = archive.infolist()
        if len(infos) != declared_count:
            collector.add("zip_invalid", logical)
            return
        budget.members += len(infos)
        names: dict[str, bool] = {}
        analysed: list[tuple[ZipInfo, _RawZipMember, str | None, bool, str]] = []
        for index, (info, raw_member) in enumerate(zip(infos, layout.members), 1):
            interpreted_name = getattr(info, "orig_filename", info.filename)
            if (
                interpreted_name != raw_member.name
                or info.header_offset != raw_member.local_offset
                or info.flag_bits != raw_member.flags
                or info.compress_type != raw_member.method
                or info.CRC != raw_member.crc
                or info.compress_size != raw_member.compressed_size
                or info.file_size != raw_member.file_size
                or info.external_attr != raw_member.external_attr
                or info.create_system != raw_member.create_system
                or info.extra
                or info.comment
            ):
                collector.add("zip_invalid", logical)
                return
            original = raw_member.name
            _scan_logical_path_text(f"{logical}!/{original}", collector)
            normalized_name, name_error, is_directory, alias = _member_analysis(original)
            display = _member_display(logical, normalized_name, index, collector)
            if name_error:
                collector.add(name_error, display)
                analysed.append((info, raw_member, None, is_directory, display))
                continue
            assert normalized_name is not None
            if alias:
                collector.add("zip_windows_alias", display)
            if normalized_name in names:
                collector.add("zip_duplicate_name", display)
            for segment_index in range(1, len(normalized_name.split("/"))):
                prefix = "/".join(normalized_name.split("/")[:segment_index])
                if prefix in names and not names[prefix]:
                    collector.add("zip_prefix_conflict", display)
            if not is_directory and any(existing.startswith(normalized_name + "/") for existing in names):
                collector.add("zip_prefix_conflict", display)
            names.setdefault(normalized_name, is_directory)
            analysed.append((info, raw_member, normalized_name, is_directory, display))

        for info, raw_member, normalized_name, is_directory, display in analysed:
            if info.flag_bits & 0x1:
                collector.add("zip_encrypted", display)
                continue
            if info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
                collector.add("zip_unsupported_compression", display)
                continue
            if _zip_link_or_device(info):
                collector.add("zip_link_or_device", display)
                continue
            if _zip_attribute_mismatch(info, is_directory):
                collector.add("zip_invalid", display)
                continue
            if info.file_size < 0 or info.compress_size < 0:
                collector.add("zip_invalid", display)
                continue
            budget.declared_bytes += info.file_size
            if info.file_size > MAX_ZIP_MEMBER_BYTES:
                collector.add("zip_member_size", display)
                continue
            if budget.declared_bytes > MAX_ZIP_TOTAL_BYTES:
                collector.add("zip_total_size", display)
                return
            if normalized_name is None:
                continue
            policy = _static_policy(normalized_name, is_directory=is_directory)
            if policy and policy != "unexpected_source_path":
                collector.add(policy, display)
            if is_directory:
                if info.file_size != 0 or info.compress_size != 0 or info.CRC != 0:
                    collector.add("zip_invalid", display)
                continue
            if budget.actual_bytes >= MAX_ZIP_TOTAL_BYTES:
                collector.add("zip_total_size", display)
                return
            member_payload, error = _read_zip_member(payload, raw_member, budget)
            if error:
                collector.add(error, display)
                if error == "zip_total_size":
                    return
                continue
            assert member_payload is not None
            if normalized_name.endswith(".zip"):
                _scan_zip_bytes(member_payload, display, depth + 1, budget, collector)
                if budget.actual_bytes > MAX_ZIP_TOTAL_BYTES:
                    return
            elif Path(normalized_name).suffix.casefold() in TEXT_SUFFIXES or Path(normalized_name).name.casefold() in TEXT_NAMES:
                _scan_text(member_payload, display, collector, normalized_name)


def _mutable_policy(parts: tuple[str, ...], *, is_directory: bool) -> str | None:
    folded = tuple(part.casefold() for part in parts)
    if not folded:
        return "unexpected_mutable_path"
    if folded[0] == "private":
        if len(folded) == 1:
            return None if is_directory else "unexpected_mutable_path"
        if len(folded) == 2:
            if folded[1] in {"cv-creator", "imports", "jobs"}:
                return None if is_directory else "unexpected_mutable_path"
            if folded[1] in {
                "accounts.json",
                "candidate.yml",
                "job-preferences.yml",
                "master-cv.docx",
                "master-cv.pdf",
                "master-profile.md",
                "run.lock",
                ".run-check.json",
                ".run-ack.json",
                ".run-release.json",
                "runtime.json",
            }:
                return None if not is_directory else "unexpected_mutable_path"
            return "unexpected_mutable_path"
        if folded[1] == "imports":
            return None if len(folded) == 3 and not is_directory and Path(folded[-1]).suffix == ".json" else (
                "unsafe_mutable_type" if not is_directory else "unexpected_mutable_path"
            )
        if folded[1] == "jobs":
            return None if len(folded) == 3 and not is_directory and Path(folded[-1]).suffix in {".yml", ".yaml"} else (
                "unsafe_mutable_type" if not is_directory else "unexpected_mutable_path"
            )
        if folded[1] == "cv-creator":
            if len(folded) == 3 and folded[2] in {"assets", "baseline"}:
                return None if is_directory else "unexpected_mutable_path"
            if len(folded) == 3 and folded[2] in {"creator.json", "layout.json", "style.css", "template.html.j2"}:
                return None if not is_directory else "unexpected_mutable_path"
            if len(folded) == 4 and folded[2] == "assets" and Path(folded[3]).suffix in {".jpeg", ".jpg", ".png", ".webp"}:
                return None if not is_directory else "unexpected_mutable_path"
            if len(folded) == 4 and folded[2] == "baseline":
                name = folded[3]
                allowed = name in {"baseline.pdf", "recreated-baseline.pdf"} or re.fullmatch(
                    r"(?:page|source-page|recreated-page|comparison-page)-\d{3}\.png", name
                )
                return None if allowed and not is_directory else (
                    "unsafe_mutable_type" if not is_directory else "unexpected_mutable_path"
                )
            return "unsafe_mutable_type" if not is_directory else "unexpected_mutable_path"
    if folded[0] == "pipeline" and len(folded) == 2 and folded[1] in {
        "pipeline.db",
        "pipeline.db-shm",
        "pipeline.db-wal",
        "pipeline_state.json",
    }:
        return None if not is_directory else "unexpected_mutable_path"
    if folded[:2] == ("documents", "generated"):
        if len(folded) == 2:
            return None if is_directory else "unexpected_mutable_path"
        if len(folded) == 3 and folded[2] == "previews":
            return None if is_directory else "unexpected_mutable_path"
        if len(folded) == 3 and not is_directory and Path(folded[2]).suffix in {".md", ".pdf"}:
            return None
        if len(folded) == 4 and folded[2] == "previews" and not is_directory and Path(folded[3]).suffix == ".png":
            return None
        return "unsafe_mutable_type" if not is_directory else "unexpected_mutable_path"
    return "unexpected_mutable_path"


def _is_mutable(parts: tuple[str, ...]) -> bool:
    folded = tuple(part.casefold() for part in parts)
    return bool(folded) and (
        folded[0] == "private"
        or folded[:2] == ("documents", "generated")
        or (folded[0] == "pipeline" and len(folded) >= 2 and folded[1] in {
            "pipeline.db",
            "pipeline.db-shm",
            "pipeline.db-wal",
            "pipeline_state.json",
        })
    )


def _scan_installed_tree(root: Path, collector: _Collector) -> None:
    try:
        root_metadata = root.lstat()
    except OSError:
        collector.add("scan_error", ".")
        return
    if _is_link_like(root, root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
        collector.add("link_or_reparse", ".")
        return

    master_cv_count = 0

    def walk(directory: Path, relative_parts: tuple[str, ...]) -> None:
        nonlocal master_cv_count
        fingerprint = _directory_fingerprint(directory)
        if fingerprint is None:
            collector.add("scan_error", "/".join(relative_parts) or ".")
            return
        entries = _directory_entries(directory)
        if entries is None:
            collector.add("scan_error", "/".join(relative_parts) or ".")
            return
        seen: set[str] = set()
        for entry in entries:
            parts = relative_parts + (entry.name,)
            relative = "/".join(parts)
            _scan_logical_path_text(relative, collector)
            key, unsafe, alias = _component_analysis(entry.name)
            if unsafe:
                collector.add("unsafe_path", relative)
                continue
            if alias:
                collector.add("windows_path_alias", relative)
            if key in seen:
                collector.add("duplicate_path_alias", relative)
                continue
            seen.add(key)
            path = Path(entry.path)
            try:
                metadata = path.lstat()
            except OSError:
                collector.add("scan_error", relative)
                continue
            if _is_link_like(path, metadata):
                collector.add("link_or_reparse", relative)
                continue
            is_directory = stat.S_ISDIR(metadata.st_mode)
            is_file = stat.S_ISREG(metadata.st_mode)
            if parts == (".venv",):
                if not is_directory:
                    collector.add("unexpected_mutable_path", relative)
                continue
            if parts and parts[0].casefold() == ".venv":
                collector.add("unexpected_source_path", relative)
                continue
            mutable = _is_mutable(parts)
            if mutable:
                policy = _mutable_policy(parts, is_directory=is_directory)
                if policy:
                    collector.add(policy, relative)
                    continue
                if is_directory:
                    walk(path, parts)
                    continue
                if not is_file:
                    collector.add("unsafe_mutable_type", relative)
                    continue
                if metadata.st_nlink != 1:
                    collector.add("hardlink", relative)
                    continue
                if tuple(part.casefold() for part in parts) in {
                    ("private", "master-cv.pdf"),
                    ("private", "master-cv.docx"),
                }:
                    master_cv_count += 1
                continue
            if is_directory:
                policy = _static_policy(relative, is_directory=True)
                if policy:
                    collector.add(policy, relative)
                    continue
                walk(path, parts)
            elif is_file:
                if metadata.st_nlink != 1:
                    collector.add("hardlink", relative)
                else:
                    _scan_static_file(path, relative, collector)
            else:
                collector.add("unsupported_file", relative)
        if _directory_fingerprint(directory) != fingerprint:
            collector.add("scan_error", "/".join(relative_parts) or ".")

    walk(root, ())
    if master_cv_count > 1:
        collector.add("mutable_conflict", "private/master-cv.*")


def _read_schema_file(root: Path, relative: str, collector: _Collector, *, yaml_format: bool) -> Any | None:
    path = root / Path(relative)
    payload, error = _stable_read(path, MAX_SCHEMA_BYTES)
    if error:
        collector.add("invalid_blank_schema", relative)
        return None
    assert payload is not None
    try:
        return _strict_yaml(payload) if yaml_format else _strict_json(payload)
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError, yaml.YAMLError, RecursionError):
        collector.add("invalid_blank_schema", relative)
        return None


def _directory_empty(path: Path) -> bool:
    entries = _directory_entries(path)
    return entries == []


def _normalized_sql(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise ValueError("database schema")
    return " ".join(value.split()).casefold()


def _sqlite_json(value: object) -> Any:
    if not isinstance(value, str):
        raise ValueError("database value")
    payload = value.encode("utf-8")
    if len(payload) > MAX_SCHEMA_BYTES:
        raise ValueError("database value")
    return _strict_json(payload)


def _valid_utc_timestamp(value: object, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    if not isinstance(value, str) or len(value) > 64 or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _read_blank_database(database_path: Path) -> tuple[dict[str, Any], bool, bool]:
    connection: sqlite3.Connection | None = None
    try:
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = database_path.with_name(database_path.name + suffix)
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ValueError("database sidecar") from error
            raise ValueError("database sidecar")
        connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True, timeout=5)
        if hasattr(connection, "setlimit"):
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_SCHEMA_BYTES)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 64 * 1024)
            connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 64)
        connection.execute("PRAGMA query_only = ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only != (1,):
            raise ValueError("database mode")
        connection.execute("PRAGMA trusted_schema = OFF")
        trusted_schema = connection.execute("PRAGMA trusted_schema").fetchone()
        if trusted_schema is not None and trusted_schema != (0,):
            raise ValueError("database mode")
        progress = [0]

        def bounded_progress() -> int:
            progress[0] += 1_000
            return int(progress[0] > MAX_SQLITE_VM_STEPS)

        connection.set_progress_handler(bounded_progress, 1_000)
        connection.execute("BEGIN")

        schema_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name LIMIT ?",
            (MAX_SQLITE_SCHEMA_OBJECTS + 1,),
        ).fetchall()
        if len(schema_rows) > MAX_SQLITE_SCHEMA_OBJECTS:
            raise ValueError("database schema")
        schema = {
            (kind, name, table): _normalized_sql(sql)
            for kind, name, table, sql in schema_rows
        }
        if schema != EXPECTED_SQLITE_SCHEMA:
            raise ValueError("database schema")
        for table, expected_columns in EXPECTED_SQLITE_COLUMNS.items():
            columns = tuple(connection.execute(f"PRAGMA table_info({table})").fetchall())
            if columns != expected_columns:
                raise ValueError("database schema")

        meta_lengths = connection.execute(
            "SELECT key, length(CAST(value_json AS BLOB)) FROM meta ORDER BY key LIMIT 4"
        ).fetchall()
        if len(meta_lengths) != 3 or {row[0] for row in meta_lengths} != {"created_at", "last_run", "run_count"}:
            raise ValueError("database meta")
        if any(type(row[1]) is not int or not 0 <= row[1] <= MAX_SCHEMA_BYTES for row in meta_lengths):
            raise ValueError("database meta")
        meta_rows = connection.execute("SELECT key, value_json FROM meta ORDER BY key LIMIT 4").fetchall()
        if len(meta_rows) != 3:
            raise ValueError("database meta")
        meta = {key: _sqlite_json(payload) for key, payload in meta_rows}
        if (
            set(meta) != {"created_at", "last_run", "run_count"}
            or type(meta["run_count"]) is not int
            or meta["run_count"] < 0
            or not _valid_utc_timestamp(meta["created_at"])
            or not _valid_utc_timestamp(meta["last_run"], nullable=True)
        ):
            raise ValueError("database meta")

        settings_rows = connection.execute(
            "SELECT singleton, version, length(CAST(payload_json AS BLOB)), updated_at "
            "FROM settings ORDER BY singleton LIMIT 2"
        ).fetchall()
        if len(settings_rows) != 1:
            raise ValueError("database settings")
        singleton, version, payload_size, updated_at = settings_rows[0]
        if (
            singleton != 1
            or type(version) is not int
            or version < 0
            or type(payload_size) is not int
            or not 0 <= payload_size <= MAX_SCHEMA_BYTES
            or not _valid_utc_timestamp(updated_at, nullable=True)
        ):
            raise ValueError("database settings")
        setting_payload = connection.execute(
            "SELECT payload_json FROM settings WHERE singleton = 1"
        ).fetchone()
        if setting_payload is None:
            raise ValueError("database settings")
        settings = validate_settings(_sqlite_json(setting_payload[0]))
        job_exists = connection.execute("SELECT 1 FROM jobs LIMIT 1").fetchone() is not None
        history_exists = connection.execute("SELECT 1 FROM settings_history LIMIT 1").fetchone() is not None
        nonblank = bool(
            job_exists
            or history_exists
            or version != 0
            or settings != validate_settings(DEFAULT_SETTINGS)
            or updated_at is not None
            or meta["run_count"] != 0
            or meta["last_run"] is not None
        )
        state = {
            "meta": meta,
            "settings": settings,
            "settings_version": version,
            "jobs": [],
        }
        return state, nonblank, job_exists
    finally:
        if connection is not None:
            connection.close()


def _expect_blank(root: Path, collector: _Collector) -> None:
    checks = (
        ("private/candidate.yml", BLANK_CANDIDATE, True, "nonblank_candidate"),
        ("private/job-preferences.yml", BLANK_PREFERENCES, True, "nonblank_preferences"),
        ("private/accounts.json", BLANK_ACCOUNTS, False, "nonblank_accounts"),
        ("private/runtime.json", BLANK_RUNTIME, False, "nonblank_runtime"),
    )
    for relative, expected, yaml_format, category in checks:
        value = _read_schema_file(root, relative, collector, yaml_format=yaml_format)
        if value is not None and value != expected:
            collector.add(category, relative)

    for relative in ("private/run.lock", "private/master-profile.md", "private/master-cv.pdf", "private/master-cv.docx"):
        path = root / Path(relative)
        try:
            exists = path.exists() or path.is_symlink()
        except OSError:
            exists = True
        if exists:
            collector.add("nonblank_private_runtime", relative)

    for relative in ("private/cv-creator", "private/imports", "private/jobs", "Documents/generated"):
        path = root / Path(relative)
        if not path.is_dir() or not _directory_empty(path):
            collector.add("nonblank_private_runtime", relative)

    database_path = root / "Pipeline/pipeline.db"
    state_path = root / "Pipeline/pipeline_state.json"
    try:
        database_metadata = database_path.lstat()
        state_metadata = state_path.lstat()
        if (
            _is_link_like(database_path, database_metadata)
            or _is_link_like(state_path, state_metadata)
            or database_metadata.st_nlink != 1
            or state_metadata.st_nlink != 1
        ):
            raise ValueError("database")
        database_state, nonblank, job_exists = _read_blank_database(database_path)
        if nonblank:
            collector.add("nonblank_database", "Pipeline/pipeline.db")
        exported = _read_schema_file(root, "Pipeline/pipeline_state.json", collector, yaml_format=False)
        if exported is not None:
            mismatch = (
                not isinstance(exported, dict)
                or set(exported) != {"meta", "settings", "settings_version", "jobs"}
                or exported.get("meta") != database_state["meta"]
                or exported.get("settings") != database_state["settings"]
                or exported.get("settings_version") != database_state["settings_version"]
                or not isinstance(exported.get("jobs"), list)
                or exported.get("jobs") != []
                or job_exists
            )
            if mismatch:
                collector.add("database_state_mismatch", "Pipeline/pipeline_state.json")
    except (OSError, ValueError, TypeError, sqlite3.Error, SettingsError, UnicodeError, RecursionError):
        collector.add("invalid_blank_schema", "Pipeline/pipeline.db")


def scan_path(path: Path, deny_tokens: Sequence[str] = ()) -> list[Finding]:
    tokens = _normalized_deny_tokens(deny_tokens)
    collector = _Collector(tokens)
    candidate = Path(path)
    guard, guard_error = _prepare_path_guard(candidate)
    if guard_error:
        collector.add(guard_error, ".")
        return collector.findings()
    assert guard is not None
    candidate = guard.path
    metadata = guard.metadata
    if stat.S_ISDIR(metadata.st_mode):
        _scan_source_directory(candidate, collector)
    elif stat.S_ISREG(metadata.st_mode) and candidate.suffix.casefold() == ".zip":
        payload, error = _stable_read(candidate, MAX_ZIP_TOTAL_BYTES + 1024 * 1024)
        if error:
            collector.add("zip_total_size" if error == "file_size" else error, candidate.name)
        else:
            assert payload is not None
            _scan_zip_bytes(payload, candidate.name, 1, _ZipBudget(), collector)
    else:
        collector.add("unsupported_input", candidate.name or ".")
    if not _path_guard_stable(guard):
        collector.add("scan_error", ".")
    return collector.findings()


def assert_clean(path: Path, deny_tokens: Sequence[str] = ()) -> None:
    findings = scan_path(path, deny_tokens)
    if findings:
        first = findings[0]
        raise PrivacyScanError(f"privacy scan failed: {first.category} at {first.path}")


def scan_installed_project(
    root: Path,
    deny_tokens: Sequence[str] = (),
    expect_blank: bool = False,
) -> list[Finding]:
    tokens = _normalized_deny_tokens(deny_tokens)
    collector = _Collector(tokens)
    guard, guard_error = _prepare_path_guard(Path(root))
    if guard_error:
        collector.add(guard_error, ".")
        return collector.findings()
    assert guard is not None
    project_root = guard.path
    _scan_installed_tree(project_root, collector)
    if expect_blank:
        _expect_blank(project_root, collector)
    if not _path_guard_stable(guard):
        collector.add("scan_error", ".")
    return collector.findings()


def _read_deny_stdin() -> tuple[str, ...]:
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        raise ValueError("deny input unavailable")
    payload = stream.read(MAX_DENY_STDIN_BYTES + 1)
    if len(payload) > MAX_DENY_STDIN_BYTES:
        raise ValueError("deny input exceeds safe limits")
    lines = payload.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if len(lines) > MAX_DENY_TOKENS:
        raise ValueError("deny input exceeds safe limits")
    values: list[str] = []
    for line in lines:
        if line.endswith(b"\r"):
            line = line[:-1]
        if len(line) > MAX_DENY_TOKEN_BYTES:
            raise ValueError("deny input exceeds safe limits")
        values.append(line.decode("utf-8"))
    return tuple(values)


def _render_findings(findings: Iterable[Finding]) -> str:
    return json.dumps(
        {"findings": [{"category": item.category, "path": item.path} for item in findings]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _RedactingArgumentParser(
        prog="privacy-scan",
        description="Scan a clean source package, ZIP, or installed local project.",
        allow_abbrev=False,
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--expect-blank", action="store_true")
    parser.add_argument("--deny-stdin", action="store_true")
    try:
        arguments = parser.parse_args(argv)
        if arguments.expect_blank and not arguments.installed:
            raise _SafeArgumentError
    except _SafeHelpExit:
        return 0
    except (argparse.ArgumentError, _SafeArgumentError):
        print("privacy-scan: invalid arguments.", file=sys.stderr)
        return 2
    try:
        deny = _read_deny_stdin() if arguments.deny_stdin else ()
        findings = (
            scan_installed_project(arguments.path, deny, expect_blank=arguments.expect_blank)
            if arguments.installed
            else scan_path(arguments.path, deny)
        )
    except Exception:
        print('{"findings":[{"category":"scan_error","path":"."}]}')
        return 2
    if findings:
        print(_render_findings(findings))
        return 1
    print("Privacy scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
