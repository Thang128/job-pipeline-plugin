from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import stat
import sys
import unicodedata
import zipfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

import fitz
import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Pipeline.io_utils import (
    ConfinementError,
    atomic_write_json,
    confined_path,
    exclusive_project_lock,
    is_symlink_or_reparse,
    path_without_link_ancestors,
    publish_bytes_no_overwrite,
)
from Pipeline.pipeline_settings import SettingsError, settings_from_preferences
from Pipeline.pipeline_store import PipelineStore, StoreError


class OnboardingError(ValueError):
    """Safe validation error. Messages contain field paths/categories only."""


MAX_CV_BYTES = 15 * 1024 * 1024
MAX_PROFILE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 10_000
MAX_LIST_ITEMS = 100
MAX_MAPPING_ITEMS = 100
MAX_CAREER_ROWS = 100
MAX_PREFERENCE_ROWS = 100
MAX_DOCX_ENTRIES = 256
MAX_DOCX_UNCOMPRESSED = 8 * 1024 * 1024
MAX_DOCX_RATIO = 100
MAX_PDF_PAGES = 20

RUNTIME_KEYS = {"schema_version", "submission_mode", "browser_profile", "master_cv_path", "master_cv_sha256", "onboarding", "auto_apply_approved_at"}
ONBOARDING_KEYS = {"complete", "creator_ready"}
CANDIDATE_KEYS = {"schema_version", "identity", "current_employer", "employment_status", "work_authorization", "languages", "notice_period", "known_answers", "career_evidence"}
IDENTITY_KEYS = {"full_name", "email", "phone", "residence_country"}
PREFERENCE_KEYS = {"schema_version", "target_titles", "role_families", "industries", "seniority", "countries", "work_modes", "salary", "include_keywords", "exclude_keywords", "excluded_employers", "preferred_company_types", "vie"}
SENIORITY_ORDER = {name: index for index, name in enumerate(("intern", "entry", "junior", "mid", "senior", "lead", "manager", "director", "executive"))}
COUNTRY_CODE = re.compile(r"[A-Z]{2}")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SHA256 = re.compile(r"[0-9a-f]{64}")
UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
PDF_FORBIDDEN_KEYS = {
    "AA",
    "AF",
    "Collection",
    "EF",
    "EmbeddedFile",
    "EmbeddedFiles",
    "FS",
    "JS",
    "JavaScript",
    "Next",
    "OpenAction",
    "RichMedia",
    "RichMediaContent",
    "RichMediaSettings",
}
PDF_FORBIDDEN_NAMES = {"/EmbeddedFile", "/FileAttachment", "/Filespec", "/JavaScript", "/Launch", "/RichMedia"}
PDF_FORBIDDEN_NESTED_PATHS = {"Names/EmbeddedFiles", "Names/JavaScript"}


def _error(path: str) -> None:
    raise OnboardingError(path)


def _bounded_string(value: object, path: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT_CHARS or (nonempty and not value.strip()):
        _error(path)
    return value


def _expect_exact_keys(value: object, keys: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > MAX_MAPPING_ITEMS or set(value) != keys:
        _error(path)
    return value


def _project_root(project_root: Path) -> Path:
    try:
        return confined_path(project_root, project_root)
    except (OSError, ValueError) as error:
        raise OnboardingError("confinement") from error


def _private_path(project_root: Path, name: str) -> Path:
    root = _project_root(project_root)
    try:
        return confined_path(root, root / "private" / name)
    except OSError as error:
        raise OnboardingError("confinement") from error


def _source_bytes(source: Path, *, extensions: set[str], maximum_size: int, label: str) -> tuple[str, bytes]:
    source = Path(source)
    try:
        source = path_without_link_ancestors(source)
        before = source.lstat()
        if is_symlink_or_reparse(source):
            raise OnboardingError(f"{label}.source: symlink or reparse")
        if not stat.S_ISREG(before.st_mode):
            raise OnboardingError(f"{label}.source: regular file")
        extension = source.suffix.casefold()
        if extension not in extensions:
            detail = "PDF or DOCX" if label == "master CV" else "Markdown or text"
            raise OnboardingError(f"{label}.extension: {detail}")
        with source.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            payload = handle.read(maximum_size + 1)
        after = source.lstat()
        if is_symlink_or_reparse(source) or not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise OnboardingError(f"{label}.source: symlink or reparse")
    except OnboardingError:
        raise
    except ConfinementError:
        raise OnboardingError(f"{label}.source: symlink or reparse") from None
    except OSError:
        raise OnboardingError(f"{label}.source: regular file") from None
    if not payload or len(payload) > maximum_size:
        raise OnboardingError(f"{label}.size")
    return extension, payload


def _validate_pdf(payload: bytes) -> None:
    if not payload.startswith(b"%PDF-") or b"%%EOF" not in payload or payload.rsplit(b"%%EOF", 1)[1].strip():
        _error("master CV.pdf")
    try:
        with fitz.open(stream=payload, filetype="pdf") as document:
            if document.needs_pass or not 1 <= document.page_count <= MAX_PDF_PAGES or document.embfile_names():
                _error("master CV.pdf")
            for xref in range(1, document.xref_length()):
                keys = document.xref_get_keys(xref)
                if not isinstance(keys, (list, tuple)) or any(not isinstance(key, str) for key in keys):
                    _error("master CV.pdf")
                if PDF_FORBIDDEN_KEYS.intersection(keys):
                    _error("master CV.pdf")
                for key in keys:
                    value_type, value = document.xref_get_key(xref, key)
                    if value_type == "name" and value in PDF_FORBIDDEN_NAMES:
                        _error("master CV.pdf")
                action_type, action_name = document.xref_get_key(xref, "A/S")
                if action_type == "name" and action_name in {"/JavaScript", "/Launch"}:
                    _error("master CV.pdf")
                for path in PDF_FORBIDDEN_NESTED_PATHS:
                    value_type, _ = document.xref_get_key(xref, path)
                    if value_type != "null":
                        _error("master CV.pdf")
            safe_link_kinds = {fitz.LINK_GOTO, fitz.LINK_URI}
            for page in document:
                if any(link.get("kind") not in safe_link_kinds for link in page.get_links()):
                    _error("master CV.pdf")
    except OnboardingError:
        raise
    except Exception:
        _error("master CV.pdf")


def _unsafe_docx_name(name: str) -> bool:
    pure = PurePosixPath(name)
    lowered = name.casefold()
    return not name or "\\" in name or pure.is_absolute() or ".." in pure.parts or bool(re.match(r"^[a-z]:", name, re.I)) or lowered.startswith(("word/activex/", "word/embeddings/")) or lowered in {"word/vbaproject.bin", "word/vbadata.xml"}


def _validate_docx(payload: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_DOCX_ENTRIES:
                _error("master CV.docx")
            total = 0
            for info in infos:
                if info.flag_bits & 1 or _unsafe_docx_name(info.filename) or stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
                    _error("master CV.docx")
                total += info.file_size
                if total > MAX_DOCX_UNCOMPRESSED or (info.file_size and (not info.compress_size or info.file_size / info.compress_size > MAX_DOCX_RATIO)):
                    _error("master CV.docx")
            names = {info.filename for info in infos}
            if not {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}.issubset(names):
                _error("master CV.docx")
            content_types = archive.read("[Content_Types].xml")
            document_xml = archive.read("word/document.xml")
            content_root = ElementTree.fromstring(content_types)
            ElementTree.fromstring(document_xml)
            content_namespace = "{http://schemas.openxmlformats.org/package/2006/content-types}"
            expected_content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
            if content_root.tag != content_namespace + "Types" or not any(
                child.tag == content_namespace + "Override"
                and child.attrib.get("PartName") == "/word/document.xml"
                and child.attrib.get("ContentType") == expected_content_type
                for child in content_root
            ):
                _error("master CV.docx")
            if b"macroenabled" in content_types.lower() or b"vbaproject" in content_types.lower():
                _error("master CV.docx")
            for name in names:
                if name.casefold().endswith(".rels"):
                    relationships = ElementTree.fromstring(archive.read(name))
                    for relationship in relationships:
                        relation_type = relationship.attrib.get("Type", "").casefold()
                        if any(relation_type.endswith("/" + suffix) for suffix in ("attachedtemplate", "oleobject", "package", "vbaproject")):
                            _error("master CV.docx")
    except OnboardingError:
        raise
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError):
        _error("master CV.docx")


def _validate_cv(extension: str, payload: bytes) -> None:
    if extension == ".pdf":
        _validate_pdf(payload)
    elif extension == ".docx":
        _validate_docx(payload)
    else:
        _error("master CV.extension")


def _profile_text(payload: bytes) -> str:
    if not payload or len(payload) > MAX_PROFILE_BYTES:
        _error("master_profile.size")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        _error("master_profile.UTF-8")
    if any(unicodedata.category(character) == "Cc" and character not in "\n\r\t" for character in text):
        _error("master_profile.control")
    return text


def _write_profile(project_root: Path, content: str) -> Path:
    if not isinstance(content, str) or not content.strip():
        _error("master_profile")
    try:
        payload = content.encode("utf-8")
    except UnicodeEncodeError:
        _error("master_profile.UTF-8")
    _profile_text(payload)
    destination = _private_path(project_root, "master-profile.md")
    if destination.exists() or is_symlink_or_reparse(destination):
        raise OnboardingError("master_profile.overwrite: already exists")
    try:
        publish_bytes_no_overwrite(_project_root(project_root), destination, payload)
    except (OSError, ValueError) as error:
        raise OnboardingError("master_profile.destination") from error
    return destination


def register_master_cv(project_root: Path, source: Path) -> dict[str, str]:
    root = _project_root(project_root)
    try:
        with exclusive_project_lock(root):
            if any(_private_path(root, f"master-cv{suffix}").exists() for suffix in (".pdf", ".docx")):
                raise OnboardingError("master CV.overwrite: already exists")
            extension, payload = _source_bytes(source, extensions={".pdf", ".docx"}, maximum_size=MAX_CV_BYTES, label="master CV")
            _validate_cv(extension, payload)
            destination = _private_path(root, f"master-cv{extension}")
            runtime = load_runtime(root)
            publish_bytes_no_overwrite(root, destination, payload)
            digest = hashlib.sha256(payload).hexdigest()
            runtime["master_cv_path"] = destination.relative_to(root).as_posix()
            runtime["master_cv_sha256"] = digest
            try:
                save_runtime(root, runtime)
            except Exception:
                try:
                    confined_path(root, destination)
                    destination.unlink()
                except FileNotFoundError:
                    pass
                raise
    except OnboardingError:
        raise
    except (OSError, ValueError) as error:
        raise OnboardingError("master CV.destination") from error
    return {"path": destination.relative_to(root).as_posix(), "sha256": digest}


def register_master_profile(project_root: Path, source: Path) -> Path:
    _, payload = _source_bytes(source, extensions={".md", ".txt"}, maximum_size=MAX_PROFILE_BYTES, label="master profile")
    return _write_profile(project_root, _profile_text(payload))


def write_master_profile_draft(project_root: Path, content: str) -> Path:
    return _write_profile(project_root, content)


def _validate_runtime(runtime: object) -> dict[str, Any]:
    value = _expect_exact_keys(runtime, RUNTIME_KEYS, "runtime")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _error("runtime.schema_version")
    mode = value["submission_mode"]
    if not isinstance(mode, str) or mode not in {"review_only", "auto_apply"}:
        _error("runtime.submission_mode")
    _bounded_string(value["browser_profile"], "runtime.browser_profile")
    path, digest = value["master_cv_path"], value["master_cv_sha256"]
    if not isinstance(path, str) or not isinstance(digest, str) or len(path) > MAX_TEXT_CHARS or len(digest) > MAX_TEXT_CHARS or bool(path) != bool(digest):
        _error("runtime.master_cv_path")
    if path:
        relative = PurePosixPath(path)
        if relative.is_absolute() or relative.parts[:1] != ("private",) or ".." in relative.parts or len(relative.parts) < 2:
            _error("runtime.master_cv_path")
    if digest and not SHA256.fullmatch(digest):
        _error("runtime.master_cv_sha256")
    onboarding = _expect_exact_keys(value["onboarding"], ONBOARDING_KEYS, "runtime.onboarding")
    if any(type(onboarding[name]) is not bool for name in ONBOARDING_KEYS):
        _error("runtime.onboarding")
    approval = value["auto_apply_approved_at"]
    if mode == "review_only" and approval is not None:
        _error("runtime.approval")
    if mode == "auto_apply":
        if not isinstance(approval, str) or not UTC_TIMESTAMP.fullmatch(approval):
            _error("runtime.approval")
        try:
            datetime.fromisoformat(approval.replace("Z", "+00:00"))
        except ValueError:
            _error("runtime.approval")
    return deepcopy(value)


def load_runtime(project_root: Path) -> dict:
    path = _private_path(project_root, "runtime.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _error("runtime")
    return _validate_runtime(value)


def save_runtime(project_root: Path, runtime: dict) -> None:
    valid = _validate_runtime(runtime)
    try:
        atomic_write_json(_private_path(project_root, "runtime.json"), valid, _project_root(project_root))
    except OSError as error:
        raise OnboardingError("runtime") from error


def _validate_string_list(value: object, path: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_LIST_ITEMS or (required and not value):
        _error(path)
    for item in value:
        _bounded_string(item, path, nonempty=True)
    return value


def validate_candidate(value: object) -> dict:
    candidate = _expect_exact_keys(value, CANDIDATE_KEYS, "candidate")
    if type(candidate["schema_version"]) is not int or candidate["schema_version"] != 1:
        _error("candidate.schema_version")
    identity = _expect_exact_keys(candidate["identity"], IDENTITY_KEYS, "candidate.identity")
    for field in IDENTITY_KEYS:
        _bounded_string(identity[field], f"candidate.identity.{field}", nonempty=True)
    status = candidate["employment_status"]
    employer = candidate["current_employer"]
    if not isinstance(status, str) or status not in {"employed", "not_employed"}:
        _error("candidate.employment_status")
    _bounded_string(employer, "candidate.current_employer")
    if (status == "employed" and not employer.strip()) or (status == "not_employed" and employer.strip()):
        _error("candidate.current_employer")
    authorization = candidate["work_authorization"]
    if not isinstance(authorization, list) or not authorization or len(authorization) > MAX_LIST_ITEMS:
        _error("candidate.work_authorization")
    for row in authorization:
        row = _expect_exact_keys(row, {"countries", "status"}, "candidate.work_authorization")
        countries, row_status = row["countries"], row["status"]
        if not isinstance(countries, list) or not countries or len(countries) > MAX_LIST_ITEMS or any(not isinstance(country, str) or not COUNTRY_CODE.fullmatch(country) for country in countries) or len(set(countries)) != len(countries):
            _error("candidate.work_authorization")
        if not isinstance(row_status, str) or row_status not in {"authorized", "sponsorship_required"}:
            _error("candidate.work_authorization")
    _validate_string_list(candidate["languages"], "candidate.languages")
    _bounded_string(candidate["notice_period"], "candidate.notice_period", nonempty=True)
    answers = candidate["known_answers"]
    if not isinstance(answers, dict) or len(answers) > MAX_MAPPING_ITEMS:
        _error("candidate.known_answers")
    for key, answer in answers.items():
        _bounded_string(key, "candidate.known_answers", nonempty=True)
        _bounded_string(answer, "candidate.known_answers", nonempty=True)
    evidence = candidate["career_evidence"]
    if not isinstance(evidence, list) or not evidence or len(evidence) > MAX_CAREER_ROWS:
        _error("candidate.career_evidence")
    identifiers: set[str] = set()
    for row in evidence:
        row = _expect_exact_keys(row, {"id", "text"}, "candidate.career_evidence")
        identifier = row["id"]
        if not isinstance(identifier, str) or not SLUG.fullmatch(identifier) or identifier in identifiers:
            _error("candidate.career_evidence")
        _bounded_string(row["text"], "candidate.career_evidence", nonempty=True)
        identifiers.add(identifier)
    return deepcopy(candidate)


def validate_preferences(value: object) -> dict:
    preferences = _expect_exact_keys(value, PREFERENCE_KEYS, "preferences")
    if type(preferences["schema_version"]) is not int or preferences["schema_version"] != 1:
        _error("preferences.schema_version")
    _validate_string_list(preferences["target_titles"], "preferences.target_titles", required=True)
    role_families = preferences["role_families"]
    if not isinstance(role_families, list) or not role_families or len(role_families) > MAX_PREFERENCE_ROWS:
        _error("preferences.role_families")
    if all(isinstance(row, str) for row in role_families):
        _validate_string_list(role_families, "preferences.role_families", required=True)
        if len(set(role_families)) != len(role_families):
            _error("preferences.role_families")
    elif all(isinstance(row, dict) for row in role_families):
        identifiers: set[str] = set()
        priorities: list[int] = []
        for row in role_families:
            row = _expect_exact_keys(row, {"id", "label", "keywords", "priority"}, "preferences.role_families")
            identifier, priority = row["id"], row["priority"]
            if not isinstance(identifier, str) or not SLUG.fullmatch(identifier) or identifier in identifiers or type(priority) is not int or priority < 1:
                _error("preferences.role_families")
            _bounded_string(row["label"], "preferences.role_families", nonempty=True)
            _validate_string_list(row["keywords"], "preferences.role_families", required=True)
            identifiers.add(identifier)
            priorities.append(priority)
        if sorted(priorities) != list(range(1, len(role_families) + 1)):
            _error("preferences.role_families")
    else:
        _error("preferences.role_families")
    for field in ("industries", "work_modes", "include_keywords", "exclude_keywords", "excluded_employers", "preferred_company_types"):
        _validate_string_list(preferences[field], f"preferences.{field}")
    seniority = _expect_exact_keys(preferences["seniority"], {"minimum", "maximum"}, "preferences.seniority")
    minimum, maximum = seniority["minimum"], seniority["maximum"]
    if not isinstance(minimum, str) or not isinstance(maximum, str) or minimum not in SENIORITY_ORDER or maximum not in SENIORITY_ORDER or SENIORITY_ORDER[minimum] > SENIORITY_ORDER[maximum]:
        _error("preferences.seniority")
    countries = preferences["countries"]
    if not isinstance(countries, list) or not countries or len(countries) > MAX_PREFERENCE_ROWS:
        _error("preferences.countries")
    codes: set[str] = set()
    priorities: list[int] = []
    for row in countries:
        if not isinstance(row, dict) or set(row) not in ({"code", "priority"}, {"code", "label", "priority"}):
            _error("preferences.countries")
        code, priority = row["code"], row["priority"]
        if not isinstance(code, str) or not COUNTRY_CODE.fullmatch(code) or type(priority) is not int or priority < 1 or code in codes:
            _error("preferences.countries")
        if "label" in row:
            _bounded_string(row["label"], "preferences.countries", nonempty=True)
        codes.add(code)
        priorities.append(priority)
    if sorted(priorities) != list(range(1, len(countries) + 1)):
        _error("preferences.countries")
    salary = _expect_exact_keys(preferences["salary"], {"minimum", "currency"}, "preferences.salary")
    amount, currency = salary["minimum"], salary["currency"]
    if type(amount) not in {int, float} or not math.isfinite(amount) or amount < 0:
        _error("preferences.salary.minimum")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        _error("preferences.salary.currency")
    vie = _expect_exact_keys(preferences["vie"], {"enabled", "eligible"}, "preferences.vie")
    if type(vie["enabled"]) is not bool or type(vie["eligible"]) is not bool or (vie["enabled"] and not vie["eligible"]):
        _error("preferences.vie")
    return deepcopy(preferences)


def _load_yaml(path: Path, label: str) -> dict:
    if is_symlink_or_reparse(path):
        _error(label)
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        _error(label)
    if not isinstance(value, dict):
        _error(label)
    return value


def _validate_authorization_coverage(candidate: dict, preferences: dict) -> None:
    counts = {row["code"]: 0 for row in preferences["countries"]}
    for row in candidate["work_authorization"]:
        for country in row["countries"]:
            if country in counts:
                counts[country] += 1
    if any(count != 1 for count in counts.values()):
        _error("candidate.work_authorization")


def onboarding_status(project_root: Path) -> dict[str, object]:
    root = _project_root(project_root)
    private = _private_path(root, "")
    master_cv = master_profile = candidate_ready = preferences_ready = creator_ready = complete = False
    try:
        runtime = load_runtime(root)
        cv_path = confined_path(root, root.joinpath(*PurePosixPath(runtime["master_cv_path"]).parts)) if runtime["master_cv_path"] else None
        if cv_path and cv_path.is_file() and not is_symlink_or_reparse(cv_path):
            master_cv = hashlib.sha256(cv_path.read_bytes()).hexdigest() == runtime["master_cv_sha256"]
        profile = _private_path(root, "master-profile.md")
        if profile.is_file() and not is_symlink_or_reparse(profile):
            master_profile = bool(_profile_text(profile.read_bytes()).strip())
        candidate = validate_candidate(_load_yaml(_private_path(root, "candidate.yml"), "candidate"))
        preferences = validate_preferences(_load_yaml(_private_path(root, "job-preferences.yml"), "preferences"))
        _validate_authorization_coverage(candidate, preferences)
        candidate_ready = preferences_ready = True
        creator_ready = runtime["onboarding"]["creator_ready"] and master_cv
        complete = runtime["onboarding"]["complete"]
    except (OnboardingError, OSError, ValueError):
        pass
    requirements = {"master_cv": master_cv, "master_profile": master_profile, "candidate": candidate_ready, "job_preferences": preferences_ready, "creator_ready": creator_ready}
    return {"ready": all(requirements.values()) and complete, "requirements": requirements}


def mark_onboarding_complete(project_root: Path) -> dict:
    root = _project_root(project_root)
    candidate = validate_candidate(_load_yaml(_private_path(root, "candidate.yml"), "candidate"))
    preferences = validate_preferences(_load_yaml(_private_path(root, "job-preferences.yml"), "preferences"))
    _validate_authorization_coverage(candidate, preferences)
    status = onboarding_status(root)
    if not status["requirements"]["master_cv"] or not status["requirements"]["master_profile"]:
        _error("onboarding.master_files")
    runtime = load_runtime(root)
    if not runtime["onboarding"]["creator_ready"]:
        _error("onboarding.creator_ready")
    try:
        store = PipelineStore(confined_path(root, root / "Pipeline" / "pipeline.db"))
        current = store.get_settings()
        settings = settings_from_preferences(preferences)
        store.save_settings(
            settings,
            expected_version=current["version"],
            reason="onboarding",
        )
    except (SettingsError, StoreError, OSError, ValueError) as error:
        raise OnboardingError("onboarding.settings") from error
    runtime["onboarding"]["complete"] = True
    save_runtime(root, runtime)
    return load_runtime(root)


def _status_for_cli(project_root: Path) -> dict[str, object]:
    status = onboarding_status(project_root)
    return {"ready": status["ready"], "missing": [name for name, present in status["requirements"].items() if not present]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage private onboarding state.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    register_cv = commands.add_parser("register-cv")
    register_cv.add_argument("--source", type=Path, required=True)
    register_profile = commands.add_parser("register-profile")
    register_profile.add_argument("--source", type=Path, required=True)
    commands.add_parser("validate")
    commands.add_parser("complete")
    arguments = parser.parse_args()
    try:
        if arguments.command == "status":
            output: object = _status_for_cli(arguments.root)
        elif arguments.command == "register-cv":
            output = register_master_cv(arguments.root, arguments.source)
        elif arguments.command == "register-profile":
            register_master_profile(arguments.root, arguments.source)
            output = {"registered": True}
        elif arguments.command == "validate":
            root = _project_root(arguments.root)
            candidate = validate_candidate(_load_yaml(_private_path(root, "candidate.yml"), "candidate"))
            preferences = validate_preferences(_load_yaml(_private_path(root, "job-preferences.yml"), "preferences"))
            _validate_authorization_coverage(candidate, preferences)
            output = {"valid": True}
        else:
            mark_onboarding_complete(arguments.root)
            output = {"ready": True}
    except (OnboardingError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
