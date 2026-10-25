from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Pipeline.io_utils import (
    ConfinementError,
    confined_path,
    exclusive_project_lock,
    is_symlink_or_reparse,
    path_without_link_ancestors,
)
from Pipeline.onboarding import (
    MAX_CV_BYTES,
    MAX_PROFILE_BYTES,
    OnboardingError,
    _profile_text,
    _validate_cv,
    load_runtime,
    onboarding_status,
    save_runtime,
    validate_candidate,
    validate_preferences,
)
from Pipeline.pipeline_settings import MAXIMUM_DAILY_SUBMISSIONS, SettingsError, evaluate_job, fold, validate_settings
from Pipeline.pipeline_store import PipelineStore, StoreError, _validate_confirmation


class PolicyError(ValueError):
    """Application policy failure whose message contains no private values."""


EMPLOYER_FIELDS = ("company", "employer", "end_employer")
COUNTRY_CODE = re.compile(r"[A-Z]{2}")
SHA256 = re.compile(r"[0-9a-f]{64}")
UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
MAX_CREATOR_BYTES = 256 * 1024
MAX_CREATOR_ASSETS = 100
MAX_CREATOR_CONTENT_BYTES = 15 * 1024 * 1024
APPROVED_CREATOR_ZONES = {"headline", "summary", "experience_bullets", "skills"}
ALLOWED_CREATOR_ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
CREATOR_KEYS = {
    "schema_version",
    "source_sha256",
    "creator_version",
    "template",
    "stylesheet",
    "approved_zones",
    "automated_validation",
    "approved_at",
    "font_substitutions",
    "validation",
    "creator_content_sha256",
    "approved_creator_sha256",
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


@contextmanager
def application_policy_lock(project_root: Path):
    root = _root(project_root)
    try:
        with exclusive_project_lock(root, name=".application-policy.lock"):
            yield
    except ConfinementError as error:
        raise PolicyError("application policy lock is already held") from error


def _stable_single_link_bytes(path: Path, maximum_size: int) -> bytes:
    try:
        checked = path_without_link_ancestors(path)
        before = checked.lstat()
        if is_symlink_or_reparse(checked) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise PolicyError("unsafe file")
        with checked.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            payload = handle.read(maximum_size + 1)
        after = checked.lstat()
        resolved = checked.stat()
        snapshots = (before, opened, after, resolved)
        identities = {(item.st_dev, item.st_ino) for item in snapshots}
        if (
            len(identities) != 1
            or any(not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 for item in snapshots)
            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            or len(payload) > maximum_size
            or not payload
            or is_symlink_or_reparse(checked)
        ):
            raise PolicyError("unsafe file")
        path_without_link_ancestors(checked)
        return payload
    except PolicyError:
        raise
    except (OSError, ValueError) as error:
        raise PolicyError("unsafe file") from error


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    if depth > 12:
        raise PolicyError("CV creator is not approved")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > 10**18:
            raise PolicyError("CV creator is not approved")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise PolicyError("CV creator is not approved")
        return
    if isinstance(value, str):
        if len(value) > 10_000:
            raise PolicyError("CV creator is not approved")
        return
    if isinstance(value, list):
        if len(value) > 100:
            raise PolicyError("CV creator is not approved")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 100 or any(not isinstance(key, str) or not key or len(key) > 10_000 for key in value):
            raise PolicyError("CV creator is not approved")
        for item in value.values():
            _validate_json_value(item, depth=depth + 1)
        return
    raise PolicyError("CV creator is not approved")


def compute_creator_content_sha256(creator_dir: Path, font_substitutions: dict[str, str]) -> str:
    if not isinstance(font_substitutions, dict):
        raise PolicyError("CV creator is not approved")
    creator_dir = path_without_link_ancestors(Path(creator_dir))
    if not creator_dir.is_dir() or is_symlink_or_reparse(creator_dir):
        raise PolicyError("CV creator is not approved")
    entries = [
        ("template.html.j2", _stable_single_link_bytes(creator_dir / "template.html.j2", MAX_CREATOR_CONTENT_BYTES)),
        ("style.css", _stable_single_link_bytes(creator_dir / "style.css", MAX_CREATOR_CONTENT_BYTES)),
        (
            "font_substitutions.json",
            json.dumps(font_substitutions, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ),
    ]
    assets = creator_dir / "assets"
    asset_paths: list[Path] = []
    if assets.exists():
        assets = path_without_link_ancestors(assets)
        if not assets.is_dir() or is_symlink_or_reparse(assets):
            raise PolicyError("CV creator is not approved")
        for item in assets.rglob("*"):
            if is_symlink_or_reparse(item):
                raise PolicyError("CV creator is not approved")
            if item.is_file():
                if item.suffix.casefold() not in ALLOWED_CREATOR_ASSET_EXTENSIONS:
                    raise PolicyError("CV creator is not approved")
                asset_paths.append(item)
            elif not item.is_dir():
                raise PolicyError("CV creator is not approved")
        if len(asset_paths) > MAX_CREATOR_ASSETS:
            raise PolicyError("CV creator is not approved")
        for path in sorted(asset_paths, key=lambda item: item.relative_to(assets).as_posix()):
            entries.append(
                (
                    "assets/" + path.relative_to(assets).as_posix(),
                    _stable_single_link_bytes(path, MAX_CREATOR_CONTENT_BYTES),
                )
            )
    if sum(len(payload) for _, payload in entries) > MAX_CREATOR_CONTENT_BYTES:
        raise PolicyError("CV creator is not approved")
    digest = hashlib.sha256()
    for name, payload in entries:
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(str(len(payload)).encode("ascii") + b"\0")
        digest.update(payload)
    return digest.hexdigest()


def _root(project_root: Path) -> Path:
    try:
        return confined_path(project_root, project_root)
    except (OSError, ValueError) as error:
        raise PolicyError("confinement") from error


def _private_file(project_root: Path, name: str) -> Path:
    root = _root(project_root)
    try:
        return confined_path(root, root / "private" / name)
    except (OSError, ValueError) as error:
        raise PolicyError("confinement") from error


def _load_candidate(project_root: Path) -> dict:
    path = _private_file(project_root, "candidate.yml")
    if is_symlink_or_reparse(path):
        raise PolicyError("candidate profile is invalid")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        return validate_candidate(value)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, OnboardingError, ValueError, TypeError) as error:
        raise PolicyError("candidate profile is invalid") from error


def _valid_utc(value: object) -> bool:
    if not isinstance(value, str) or UTC_TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def _load_creator(project_root: Path) -> dict[str, Any]:
    path = _private_file(project_root, "cv-creator/creator.json")
    if is_symlink_or_reparse(path):
        raise PolicyError("CV creator is not approved")
    try:
        payload = path.read_bytes()
        if not payload or len(payload) > MAX_CREATOR_BYTES:
            raise ValueError
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise PolicyError("CV creator is not approved") from None
    zones = value.get("approved_zones") if isinstance(value, dict) else None
    substitutions = value.get("font_substitutions") if isinstance(value, dict) else None
    validation = value.get("validation") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != CREATOR_KEYS
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or not isinstance(value.get("source_sha256"), str)
        or SHA256.fullmatch(value["source_sha256"]) is None
        or type(value.get("creator_version")) is not int
        or not 1 <= value["creator_version"] <= 1_000_000
        or value.get("template") != "template.html.j2"
        or value.get("stylesheet") != "style.css"
        or not isinstance(zones, list)
        or not zones
        or len(zones) > 100
        or any(not isinstance(zone, str) or not zone.strip() or len(zone) > 100 for zone in zones)
        or len(set(zones)) != len(zones)
        or not set(zones).issubset(APPROVED_CREATOR_ZONES)
        or value.get("automated_validation") is not True
        or not _valid_utc(value.get("approved_at"))
        or not isinstance(substitutions, dict)
        or len(substitutions) > 100
        or any(
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 200
            or not isinstance(replacement, str)
            or not replacement.strip()
            or len(replacement) > 200
            for name, replacement in substitutions.items()
        )
        or not isinstance(validation, dict)
        or not validation
        or not isinstance(value.get("creator_content_sha256"), str)
        or SHA256.fullmatch(value["creator_content_sha256"]) is None
        or not isinstance(value.get("approved_creator_sha256"), str)
        or SHA256.fullmatch(value["approved_creator_sha256"]) is None
    ):
        raise PolicyError("CV creator is not approved")
    _validate_json_value(validation)
    try:
        current_hash = compute_creator_content_sha256(path.parent, substitutions)
    except (OSError, ValueError, PolicyError) as error:
        raise PolicyError("CV creator is not approved") from error
    if value["creator_content_sha256"] != current_hash:
        raise PolicyError("CV creator is not approved")
    return value


def _verify_strict_creator_binding(project_root: Path) -> dict[str, Any]:
    try:
        from Pipeline.document_builder import DocumentBuildError, verify_approved_creator_binding

        return verify_approved_creator_binding(project_root)
    except (ImportError, OSError, ValueError, PolicyError) as error:
        raise PolicyError("CV creator is not approved") from error


def work_authorization_blocker(job: dict, candidate: dict) -> str | None:
    if not isinstance(job, dict):
        return "job"
    raw_country = job.get("country")
    if not isinstance(raw_country, str) or COUNTRY_CODE.fullmatch(raw_country.strip().upper()) is None:
        return "job.country"
    sponsorship = job.get("sponsorship_status", "unknown")
    if sponsorship not in {"unknown", "offered", "not_offered", "not_required"}:
        return "job.sponsorship_status"
    if not isinstance(candidate, dict):
        return "candidate.work_authorization"
    rows = candidate.get("work_authorization")
    if not isinstance(rows, list) or not rows:
        return "candidate.work_authorization"
    country = raw_country.strip().upper()
    matches: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"countries", "status"}:
            return "candidate.work_authorization"
        countries = row.get("countries")
        status = row.get("status")
        if (
            not isinstance(countries, list)
            or not countries
            or status not in {"authorized", "sponsorship_required"}
            or any(not isinstance(code, str) or COUNTRY_CODE.fullmatch(code) is None for code in countries)
            or len(set(countries)) != len(countries)
            or seen.intersection(countries)
        ):
            return "candidate.work_authorization"
        seen.update(countries)
        if country in countries:
            matches.append(row)
    if len(matches) != 1:
        return "work authorization is not configured for the job country"
    if matches[0]["status"] == "authorized" or sponsorship in {"offered", "unknown"}:
        return None
    return "sponsorship is required but posting explicitly requires existing authorization"


def is_current_employer_job(job: dict, candidate: dict) -> bool:
    if not isinstance(job, dict) or not isinstance(candidate, dict):
        return False
    employer = fold(candidate.get("current_employer"))
    if not employer:
        return False
    return any(fold(job.get(field)) == employer for field in EMPLOYER_FIELDS if isinstance(job.get(field), str))


def enforce_current_employer_exclusion(state: dict, candidate: dict) -> list[str]:
    if not isinstance(state, dict) or not isinstance(state.get("jobs"), list):
        raise PolicyError("state.jobs")
    if not isinstance(candidate, dict) or not isinstance(candidate.get("current_employer"), str):
        raise PolicyError("candidate.current_employer")
    changed: list[str] = []
    for job in state["jobs"]:
        if not isinstance(job, dict):
            raise PolicyError("state.jobs")
        if not is_current_employer_job(job, candidate):
            continue
        if job.get("status") == "applied" or job.get("applied"):
            raise PolicyError("current-employer applied job requires manual review")
        if not isinstance(job.get("id"), str) or not job["id"].strip():
            raise PolicyError("state.jobs.id")
        job.update({"status": "skipped", "score": 0})
        job.pop("blocker", None)
        job.pop("confirmation", None)
        changed.append(job["id"])
    return changed


def _master_and_creator_blockers(project_root: Path, runtime: dict) -> list[str]:
    blockers: list[str] = []
    try:
        creator = _verify_strict_creator_binding(project_root)
    except PolicyError:
        creator = None
        blockers.append("CV creator is not approved")
    raw_path = runtime.get("master_cv_path")
    raw_digest = runtime.get("master_cv_sha256")
    if not isinstance(raw_path, str) or not isinstance(raw_digest, str) or SHA256.fullmatch(raw_digest) is None:
        if "CV creator is not approved" not in blockers:
            blockers.append("CV creator is not approved")
        blockers.append("master CV hash has changed")
        return blockers
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or relative.parts[:1] != ("private",) or ".." in relative.parts or "\\" in raw_path:
        blockers.append("master CV path is unsafe")
        return blockers
    root = _root(project_root)
    try:
        path = confined_path(root, root.joinpath(*relative.parts))
        extension = path.suffix.casefold()
        if extension not in {".pdf", ".docx"}:
            raise OnboardingError("master CV.extension")
        payload = _stable_single_link_bytes(path, MAX_CV_BYTES)
        _validate_cv(extension, payload)
    except PolicyError:
        blockers.append("master CV path is unsafe")
        return blockers
    except (OSError, ValueError, OnboardingError):
        blockers.append("master CV is missing or invalid")
        return blockers
    actual = hashlib.sha256(payload).hexdigest()
    if actual != raw_digest or creator is None or creator["source_sha256"] != actual:
        blockers.append("master CV hash has changed")
    return blockers


def _document_blocker(project_root: Path, raw_path: object, *, cv: bool) -> str | None:
    label = "generated CV" if cv else "generated cover letter"
    if not isinstance(raw_path, str) or not raw_path.strip() or len(raw_path) > 10_000 or "\\" in raw_path:
        return f"{label} path is outside Documents/generated"
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:2] != ("Documents", "generated") or len(relative.parts) < 3:
        return f"{label} path is outside Documents/generated"
    allowed = {".pdf"} if cv else {".md", ".pdf", ".docx"}
    if relative.suffix.casefold() not in allowed:
        return f"{label} has unsupported type"
    root = _root(project_root)
    try:
        path = confined_path(root, root.joinpath(*relative.parts))
    except (OSError, ValueError):
        return f"{label} path is unsafe"
    if is_symlink_or_reparse(path):
        return f"{label} path is unsafe"
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return f"{label} path is unsafe"
        if not stat.S_ISREG(before.st_mode):
            return f"{label} is missing or invalid"
        extension = relative.suffix.casefold()
        payload = _stable_single_link_bytes(
            path, MAX_CV_BYTES if cv or extension != ".md" else MAX_PROFILE_BYTES
        )
        if extension in {".pdf", ".docx"}:
            _validate_cv(extension, payload)
        else:
            if not _profile_text(payload).strip():
                return f"{label} is missing or invalid"
    except PolicyError:
        return f"{label} path is unsafe"
    except OnboardingError as error:
        if "symlink or reparse" in str(error):
            return f"{label} path is unsafe"
        return f"{label} is missing or invalid"
    except (OSError, ValueError):
        return f"{label} is missing or invalid"
    return None


def _confirmation_valid(value: object) -> bool:
    try:
        _validate_confirmation(value)
        return True
    except (StoreError, ValueError, TypeError):
        return False


def _today_applied_count(project_root: Path) -> int:
    store = PipelineStore(_root(project_root) / "Pipeline" / "pipeline.db")
    today = datetime.now(timezone.utc).date()
    count = 0
    for row in store.list_jobs():
        if row.get("status") != "applied":
            continue
        observed = datetime.fromisoformat(row["confirmation"]["observed_at"].replace("Z", "+00:00"))
        if observed.astimezone(timezone.utc).date() == today:
            count += 1
    return count


def _revoke_creator_runtime(project_root: Path, runtime: dict) -> None:
    runtime["onboarding"]["creator_ready"] = False
    runtime["submission_mode"] = "review_only"
    runtime["auto_apply_approved_at"] = None
    try:
        save_runtime(_root(project_root), runtime)
    except (OnboardingError, OSError, ValueError) as error:
        raise PolicyError("runtime is invalid") from error


def selection_blockers(project_root: Path, job: dict, settings: dict) -> list[str]:
    if not isinstance(job, dict):
        return ["job is invalid"]
    selection_job = copy.deepcopy(job)
    if selection_job.get("status") in {"new", "applied"}:
        selection_job["status"] = "shortlisted"
    try:
        _, blockers = evaluate_job(selection_job, settings)
    except (SettingsError, ValueError, TypeError):
        return ["settings are invalid"]
    try:
        candidate = _load_candidate(project_root)
        authorization = work_authorization_blocker(job, candidate)
        if authorization:
            blockers.append(authorization)
        if is_current_employer_job(job, candidate):
            blockers.append("target matches current employer")
    except PolicyError:
        blockers.append("candidate profile is invalid")
    if job.get("is_vie") is True:
        try:
            preferences = validate_preferences(yaml.safe_load(_private_file(project_root, "job-preferences.yml").read_text(encoding="utf-8")))
            if preferences["vie"] != {"enabled": True, "eligible": True}:
                blockers.append("candidate VIE eligibility is not confirmed")
        except (OSError, ValueError, yaml.YAMLError):
            blockers.append("candidate VIE eligibility is not confirmed")
    return blockers


def _application_blockers_locked(project_root: Path, job: dict, settings: dict) -> list[str]:
    blockers: list[str] = []
    try:
        valid_settings = validate_settings(settings)
    except (SettingsError, ValueError, TypeError):
        return ["settings are invalid"]
    if not isinstance(job, dict):
        return ["job is invalid"]
    blockers.extend(selection_blockers(project_root, job, valid_settings))
    try:
        runtime = load_runtime(_root(project_root))
    except (OnboardingError, OSError, ValueError):
        runtime = None
        blockers.append("runtime is invalid")
    try:
        ready = onboarding_status(_root(project_root))["ready"]
    except (OSError, ValueError):
        ready = False
    if not ready:
        blockers.append("onboarding is incomplete")
    if runtime is None:
        blockers.append("CV creator is not approved")
    else:
        if not runtime["onboarding"]["creator_ready"]:
            blockers.append("CV creator is not approved")
        for blocker in _master_and_creator_blockers(project_root, runtime):
            if blocker not in blockers:
                blockers.append(blocker)
        if "CV creator is not approved" in blockers:
            _revoke_creator_runtime(project_root, runtime)
        if runtime["submission_mode"] != "auto_apply":
            blockers.append("submission mode is review_only")
        elif not _valid_utc(runtime.get("auto_apply_approved_at")):
            blockers.append("auto-apply approval timestamp is invalid")
    cap = valid_settings["daily_submission_cap"]
    if cap == 0:
        blockers.append("daily submission cap is 0")
    elif not 1 <= cap <= MAXIMUM_DAILY_SUBMISSIONS:
        blockers.append("daily submission cap is invalid")
    else:
        try:
            if _today_applied_count(project_root) >= cap:
                blockers.append("daily submission cap is reached")
        except (StoreError, OSError, ValueError, KeyError):
            blockers.append("canonical state is invalid")
    status = job.get("status")
    if status not in {"docs_ready", "applied"}:
        blockers.append("job is not docs_ready")
    score = job.get("score")
    if (type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= 100 or score < valid_settings["score_threshold"]) and "score is below threshold" not in blockers:
        blockers.append("score is below threshold")
    raw_blocker = job.get("blocker")
    if raw_blocker is not None:
        category = raw_blocker.get("category") if isinstance(raw_blocker, dict) else None
        if category in {"unknown_answer", "unknown_answer_email_verification_code"}:
            blockers.append("job has unresolved unknown answer")
        elif category == "captcha":
            blockers.append("job has unresolved CAPTCHA")
        else:
            blockers.append("job has unresolved blocker")
    for field, cv in (("cv", True), ("cover_letter", False)):
        blocker = _document_blocker(project_root, job.get(field), cv=cv)
        if blocker:
            blockers.append(blocker)
    from Pipeline.document_review import review_blocker
    review_issue = review_blocker(_root(project_root), job)
    if review_issue:
        blockers.append(review_issue)
    if status == "applied":
        if not _confirmation_valid(job.get("confirmation")):
            blockers.append("visible confirmation evidence is missing or invalid")
    elif "confirmation" in job:
        blockers.append("confirmation is invalid for docs_ready job")
    return blockers


def application_blockers(project_root: Path, job: dict, settings: dict) -> list[str]:
    root = _root(project_root)
    with application_policy_lock(root):
        return _application_blockers_locked(root, job, settings)


def assert_application_ready(project_root: Path, job: dict, settings: dict) -> None:
    blockers = application_blockers(project_root, job, settings)
    if blockers:
        raise PolicyError("application blocked: " + "; ".join(blockers))


def _assert_application_ready_locked(project_root: Path, job: dict, settings: dict) -> None:
    blockers = _application_blockers_locked(project_root, job, settings)
    if blockers:
        raise PolicyError("application blocked: " + "; ".join(blockers))


def enable_auto_apply(project_root: Path, *, explicit_user_approval: bool) -> dict:
    if explicit_user_approval is not True:
        raise PolicyError("explicit user approval is required")
    root = _root(project_root)
    with application_policy_lock(root):
        try:
            runtime = load_runtime(root)
            status = onboarding_status(root)
            creator = _load_creator(root)
            settings = PipelineStore(root / "Pipeline" / "pipeline.db").get_settings()["settings"]
        except (OnboardingError, StoreError, PolicyError, OSError, ValueError) as error:
            raise PolicyError("onboarding and CV creator approval are required") from error
        cap = settings["daily_submission_cap"]
        if not 1 <= cap <= MAXIMUM_DAILY_SUBMISSIONS:
            raise PolicyError("daily submission cap must be between 1 and 50")
        master_blockers = _master_and_creator_blockers(root, runtime)
        if "CV creator is not approved" in master_blockers:
            _revoke_creator_runtime(root, runtime)
        if not status["ready"] or not runtime["onboarding"]["complete"] or not runtime["onboarding"]["creator_ready"] or creator is None or master_blockers:
            raise PolicyError("onboarding and CV creator approval are required")
        runtime["submission_mode"] = "auto_apply"
        runtime["auto_apply_approved_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        try:
            save_runtime(root, runtime)
        except (OnboardingError, OSError, ValueError) as error:
            raise PolicyError("runtime is invalid") from error
        return runtime


def disable_auto_apply(project_root: Path) -> dict:
    root = _root(project_root)
    with application_policy_lock(root):
        try:
            runtime = load_runtime(root)
            runtime["submission_mode"] = "review_only"
            runtime["auto_apply_approved_at"] = None
            save_runtime(root, runtime)
            return runtime
        except (OnboardingError, OSError, ValueError) as error:
            raise PolicyError("runtime is invalid") from error


def record_applied(project_root: Path, job_id: str, confirmation: dict) -> dict:
    root = _root(project_root)
    try:
        evidence = _validate_confirmation(confirmation)
    except (StoreError, ValueError, TypeError) as error:
        raise PolicyError("visible confirmation evidence is missing or invalid") from error
    with application_policy_lock(root):
        store = PipelineStore(root / "Pipeline" / "pipeline.db")
        try:
            current = store.get_job(job_id)
            revision = store.get_settings()
            if current is None:
                raise PolicyError("job.id")
            expected_fingerprint = store.job_fingerprint(current)
            proposed = copy.deepcopy(current)
            proposed["status"] = "applied"
            proposed["confirmation"] = evidence
            proposed.pop("blocker", None)
            _assert_application_ready_locked(root, proposed, revision["settings"])

            def readiness_check(canonical_job: dict, canonical_settings: dict, settings_version: int) -> None:
                if settings_version != revision["version"]:
                    raise PolicyError("canonical settings changed")
                checked = copy.deepcopy(canonical_job)
                checked["status"] = "applied"
                checked["confirmation"] = evidence
                checked.pop("blocker", None)
                _assert_application_ready_locked(root, checked, canonical_settings)

            return store.record_applied(
                job_id,
                evidence,
                expected_job_fingerprint=expected_fingerprint,
                expected_settings_version=revision["version"],
                readiness_check=readiness_check,
            )
        except PolicyError:
            raise
        except (StoreError, OSError, ValueError) as error:
            raise PolicyError("canonical state transition failed") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage guarded application policy state.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest="command", required=True)
    enable = commands.add_parser("enable-auto-apply")
    enable.add_argument("--confirm-user-approval", action="store_true")
    commands.add_parser("disable-auto-apply")
    arguments = parser.parse_args()
    try:
        if arguments.command == "enable-auto-apply":
            runtime = enable_auto_apply(arguments.root, explicit_user_approval=arguments.confirm_user_approval)
        else:
            runtime = disable_auto_apply(arguments.root)
        output = {
            "ok": True,
            "submission_mode": runtime["submission_mode"],
            "auto_apply_approved_at": runtime["auto_apply_approved_at"],
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (PolicyError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error), "ok": False}, ensure_ascii=False, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
