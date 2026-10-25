from __future__ import annotations

import copy
import math
import re
import unicodedata
from typing import Any


class SettingsError(ValueError):
    """A settings or preferences field is invalid."""


DEFAULT_SETTINGS = {
    "schema_version": 1,
    "role_families": [],
    "countries": [],
    "minimum_salary": {"amount": 0, "currency": "EUR"},
    "score_threshold": 75,
    "daily_submission_cap": 0,
    "minimum_seniority": "entry",
    "maximum_seniority": "mid",
    "exclude_unknown_seniority": True,
    "include_keywords": [],
    "exclude_keywords": [],
    "excluded_employers": [],
    "prioritize_large_groups": False,
    "large_group_employers": [],
    "vie_enabled": False,
}

SENIORITY_ORDER = ("entry", "mid", "senior", "lead_manager", "director_executive")
MAXIMUM_DAILY_SUBMISSIONS = 50
ACTIONABLE_STATUSES = frozenset({"shortlisted", "docs_ready"})
MAX_TEXT_CHARS = 10_000
MAX_LIST_ITEMS = 100
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
COUNTRY_CODE = re.compile(r"[A-Z]{2}")
CURRENCY_CODE = re.compile(r"[A-Z]{3}")
SETTINGS_KEYS = frozenset(DEFAULT_SETTINGS)
ROLE_KEYS = frozenset({"id", "label", "keywords", "enabled", "priority"})
COUNTRY_KEYS = frozenset({"code", "label", "enabled", "priority"})
PREFERENCE_KEYS = frozenset(
    {
        "schema_version",
        "target_titles",
        "role_families",
        "industries",
        "seniority",
        "countries",
        "work_modes",
        "salary",
        "include_keywords",
        "exclude_keywords",
        "excluded_employers",
        "preferred_company_types",
        "vie",
    }
)


def _error(path: str) -> None:
    raise SettingsError(path)


def _exact_mapping(value: object, keys: frozenset[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _error(path)
    return value


def _text(value: object, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT_CHARS:
        _error(path)
    normalized = value.strip()
    if nonempty and not normalized:
        _error(path)
    return normalized


def _string_list(value: object, path: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_LIST_ITEMS or (nonempty and not value):
        _error(path)
    result = [_text(item, path) for item in value]
    if len({fold(item) for item in result}) != len(result):
        _error(path)
    return result


def _number(value: object, path: str, *, minimum: float, maximum: float | None = None) -> int | float:
    if type(value) not in {int, float}:
        _error(path)
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if not finite or value < minimum or (maximum is not None and value > maximum):
        _error(path)
    return value


def _validate_priority_rows(rows: object, path: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or len(rows) > MAX_LIST_ITEMS:
        _error(path)
    priorities: list[int] = []
    result: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for raw in rows:
        if path == "settings.role_families":
            row = _exact_mapping(raw, ROLE_KEYS, path)
            identifier = row.get("id")
            if not isinstance(identifier, str) or not SLUG.fullmatch(identifier) or identifier in identifiers:
                _error(path)
            normalized = {
                "id": identifier,
                "label": _text(row.get("label"), path),
                "keywords": _string_list(row.get("keywords"), path, nonempty=True),
                "enabled": row.get("enabled"),
                "priority": row.get("priority"),
            }
        else:
            row = _exact_mapping(raw, COUNTRY_KEYS, path)
            identifier = row.get("code")
            if not isinstance(identifier, str) or not COUNTRY_CODE.fullmatch(identifier) or identifier in identifiers:
                _error(path)
            normalized = {
                "code": identifier,
                "label": _text(row.get("label"), path),
                "enabled": row.get("enabled"),
                "priority": row.get("priority"),
            }
        if type(normalized["enabled"]) is not bool or type(normalized["priority"]) is not int or normalized["priority"] < 1:
            _error(path)
        identifiers.add(identifier)
        priorities.append(normalized["priority"])
        result.append(normalized)
    if sorted(priorities) != list(range(1, len(rows) + 1)):
        _error(path)
    return result


def validate_settings(value: object) -> dict:
    settings = _exact_mapping(value, SETTINGS_KEYS, "settings")
    if type(settings.get("schema_version")) is not int or settings["schema_version"] != 1:
        _error("settings.schema_version")
    salary = _exact_mapping(settings.get("minimum_salary"), frozenset({"amount", "currency"}), "settings.minimum_salary")
    amount = _number(salary.get("amount"), "settings.minimum_salary.amount", minimum=0)
    currency = salary.get("currency")
    if not isinstance(currency, str) or not CURRENCY_CODE.fullmatch(currency):
        _error("settings.minimum_salary.currency")
    threshold = _number(settings.get("score_threshold"), "settings.score_threshold", minimum=0, maximum=100)
    cap = settings.get("daily_submission_cap")
    if type(cap) is not int or not 0 <= cap <= MAXIMUM_DAILY_SUBMISSIONS:
        _error("settings.daily_submission_cap")
    minimum = settings.get("minimum_seniority")
    maximum = settings.get("maximum_seniority")
    if minimum not in SENIORITY_ORDER or maximum not in SENIORITY_ORDER or SENIORITY_ORDER.index(minimum) > SENIORITY_ORDER.index(maximum):
        _error("settings.seniority")
    for field in ("exclude_unknown_seniority", "prioritize_large_groups", "vie_enabled"):
        if type(settings.get(field)) is not bool:
            _error(f"settings.{field}")
    return {
        "schema_version": 1,
        "role_families": _validate_priority_rows(settings.get("role_families"), "settings.role_families"),
        "countries": _validate_priority_rows(settings.get("countries"), "settings.countries"),
        "minimum_salary": {"amount": amount, "currency": currency},
        "score_threshold": threshold,
        "daily_submission_cap": cap,
        "minimum_seniority": minimum,
        "maximum_seniority": maximum,
        "exclude_unknown_seniority": settings["exclude_unknown_seniority"],
        "include_keywords": _string_list(settings.get("include_keywords"), "settings.include_keywords"),
        "exclude_keywords": _string_list(settings.get("exclude_keywords"), "settings.exclude_keywords"),
        "excluded_employers": _string_list(settings.get("excluded_employers"), "settings.excluded_employers"),
        "prioritize_large_groups": settings["prioritize_large_groups"],
        "large_group_employers": _string_list(settings.get("large_group_employers"), "settings.large_group_employers"),
        "vie_enabled": settings["vie_enabled"],
    }


def _preference_seniority(value: object, path: str) -> str:
    mapping = {
        "intern": "entry",
        "entry": "entry",
        "junior": "entry",
        "mid": "mid",
        "senior": "senior",
        "lead": "lead_manager",
        "manager": "lead_manager",
        "lead_manager": "lead_manager",
        "director": "director_executive",
        "executive": "director_executive",
        "director_executive": "director_executive",
    }
    if not isinstance(value, str) or value not in mapping:
        _error(path)
    return mapping[value]


def settings_from_preferences(preferences: dict) -> dict:
    if not isinstance(preferences, dict) or set(preferences) != PREFERENCE_KEYS:
        _error("preferences")
    if type(preferences.get("schema_version")) is not int or preferences["schema_version"] != 1:
        _error("preferences.schema_version")
    for field in ("industries", "work_modes", "preferred_company_types"):
        _string_list(preferences.get(field), f"preferences.{field}")
    target_titles = _string_list(preferences.get("target_titles"), "preferences.target_titles", nonempty=True)
    raw_families = preferences.get("role_families")
    if not isinstance(raw_families, list) or not raw_families or len(raw_families) > MAX_LIST_ITEMS:
        _error("preferences.role_families")
    families: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_families, start=1):
        if isinstance(raw, str):
            identifier = _text(raw, "preferences.role_families")
            if not SLUG.fullmatch(identifier):
                _error("preferences.role_families")
            row = {"id": identifier, "label": identifier.replace("-", " ").title(), "keywords": [identifier], "priority": index}
        elif isinstance(raw, dict) and set(raw) == {"id", "label", "keywords", "priority"}:
            row = raw
        else:
            _error("preferences.role_families")
        keywords = _string_list(row.get("keywords"), "preferences.role_families", nonempty=True)
        families.append(
            {
                "id": row.get("id"),
                "label": row.get("label"),
                "keywords": keywords,
                "enabled": True,
                "priority": row.get("priority"),
            }
        )
    for title in target_titles:
        if not any(fold(title) == fold(keyword) for family in families for keyword in family["keywords"]):
            families[0]["keywords"].append(title)

    raw_countries = preferences.get("countries")
    if not isinstance(raw_countries, list) or not raw_countries or len(raw_countries) > MAX_LIST_ITEMS:
        _error("preferences.countries")
    countries = []
    for raw in raw_countries:
        if not isinstance(raw, dict) or set(raw) not in ({"code", "priority"}, {"code", "label", "priority"}):
            _error("preferences.countries")
        code = raw.get("code")
        countries.append(
            {
                "code": code,
                "label": raw.get("label", code),
                "enabled": True,
                "priority": raw.get("priority"),
            }
        )
    salary = preferences.get("salary")
    seniority = preferences.get("seniority")
    vie = preferences.get("vie")
    if not isinstance(salary, dict) or set(salary) != {"minimum", "currency"}:
        _error("preferences.salary")
    if not isinstance(seniority, dict) or set(seniority) != {"minimum", "maximum"}:
        _error("preferences.seniority")
    if not isinstance(vie, dict) or set(vie) != {"enabled", "eligible"} or type(vie.get("enabled")) is not bool or type(vie.get("eligible")) is not bool or (vie["enabled"] and not vie["eligible"]):
        _error("preferences.vie")
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings.update(
        {
            "role_families": families,
            "countries": countries,
            "minimum_salary": {"amount": salary.get("minimum"), "currency": salary.get("currency")},
            "minimum_seniority": _preference_seniority(seniority.get("minimum"), "preferences.seniority.minimum"),
            "maximum_seniority": _preference_seniority(seniority.get("maximum"), "preferences.seniority.maximum"),
            "include_keywords": copy.deepcopy(preferences.get("include_keywords")),
            "exclude_keywords": copy.deepcopy(preferences.get("exclude_keywords")),
            "excluded_employers": copy.deepcopy(preferences.get("excluded_employers")),
            "vie_enabled": vie["enabled"] and vie["eligible"],
        }
    )
    return validate_settings(settings)


def fold(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    return " ".join("".join(character for character in normalized if not unicodedata.combining(character)).casefold().split())


def _classify(job: dict, settings: dict) -> tuple[str, str]:
    title = fold(job.get("title"))
    description = fold(job.get("job_description"))
    families = sorted(
        (row for row in settings["role_families"] if row["enabled"]),
        key=lambda row: (-row["priority"], row["id"]),
    )
    for family in families:
        for keyword in family["keywords"]:
            if fold(keyword) in title:
                return family["id"], f"title matched '{fold(keyword)}'"
        for keyword in family["keywords"]:
            if fold(keyword) in description:
                return family["id"], f"description matched '{fold(keyword)}'"
    return "other", "no configured role-family keyword matched"


def _job_type_reasons(job: dict) -> list[str]:
    reasons: list[str] = []
    for field in ("id", "title", "country", "status"):
        if field not in job or not isinstance(job[field], str) or not job[field].strip():
            reasons.append(f"{field} has invalid type")
    if "detected_seniority" not in job or (job["detected_seniority"] is not None and not isinstance(job["detected_seniority"], str)):
        reasons.append("detected_seniority has invalid type")
    for field in ("company", "employer", "end_employer", "job_description", "deadline", "salary_currency"):
        if field in job and job[field] is not None and not isinstance(job[field], str):
            reasons.append(f"{field} has invalid type")
    if "is_vie" in job and type(job["is_vie"]) is not bool:
        reasons.append("is_vie has invalid type")
    for field in ("score", "salary_amount"):
        if field not in job:
            if field == "score":
                reasons.append("score has invalid type")
            continue
        value = job[field]
        if type(value) not in {int, float}:
            reasons.append(f"{field} has invalid type")
            continue
        try:
            finite = math.isfinite(value)
        except (OverflowError, TypeError):
            finite = False
        if not finite:
            reasons.append(f"{field} has invalid type")
    if ("salary_amount" in job) != ("salary_currency" in job):
        reasons.append("salary fields have invalid type")
    if "salary_currency" in job and (
        not isinstance(job["salary_currency"], str) or re.fullmatch(r"[A-Z]{3}", job["salary_currency"]) is None
    ):
        reasons.append("salary_currency has invalid type")
    return reasons


def classify_job(job: dict, settings: dict) -> tuple[str, str]:
    if not isinstance(job, dict):
        _error("job")
    for field in ("title", "job_description"):
        if field in job and not isinstance(job[field], str):
            _error(f"job.{field}.type")
    return _classify(job, validate_settings(settings))


def evaluate_job(job: dict, settings: dict) -> tuple[bool, list[str]]:
    if not isinstance(job, dict):
        return False, ["job is not an object"]
    settings = validate_settings(settings)
    reasons = _job_type_reasons(job)
    if reasons:
        return False, reasons
    family, _ = _classify(job, settings)
    if family == "other":
        reasons.append("no enabled role family matched")
    enabled_countries = {row["code"] for row in settings["countries"] if row["enabled"]}
    is_vie = job.get("is_vie") is True
    country = str(job.get("country") or "").upper()
    if not is_vie and country not in enabled_countries:
        reasons.append("country is not enabled")
    if is_vie and country == "BE":
        reasons.append("Belgium VIE is excluded")
    seniority = job.get("detected_seniority")
    if seniority not in SENIORITY_ORDER:
        if settings["exclude_unknown_seniority"]:
            reasons.append("seniority is unknown")
    elif not SENIORITY_ORDER.index(settings["minimum_seniority"]) <= SENIORITY_ORDER.index(seniority) <= SENIORITY_ORDER.index(settings["maximum_seniority"]):
        reasons.append("seniority is outside configured bounds")
    score = job.get("score")
    if type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= 100 or score < settings["score_threshold"]:
        reasons.append("score is below threshold")
    salary_amount = job.get("salary_amount")
    if salary_amount is not None and not is_vie:
        if type(salary_amount) not in {int, float} or not math.isfinite(salary_amount) or salary_amount < settings["minimum_salary"]["amount"]:
            reasons.append("stated salary is below minimum")
        salary_currency = job.get("salary_currency")
        if salary_currency != settings["minimum_salary"]["currency"]:
            reasons.append("stated salary currency does not match")
    combined = fold(" ".join(str(job.get(field) or "") for field in ("title", "job_description")))
    if settings["include_keywords"] and not any(fold(keyword) in combined for keyword in settings["include_keywords"]):
        reasons.append("no include keyword matched")
    if any(fold(keyword) in combined for keyword in settings["exclude_keywords"]):
        reasons.append("exclude keyword matched")
    excluded = {fold(value) for value in settings["excluded_employers"]}
    if any(fold(job.get(field)) in excluded for field in ("company", "employer", "end_employer") if job.get(field)):
        reasons.append("employer is excluded")
    if bool(job.get("is_vie")) and not settings["vie_enabled"]:
        reasons.append("VIE is disabled")
    if job.get("status") not in ACTIONABLE_STATUSES:
        reasons.append("job status is not actionable")
    return not reasons, reasons


def rank_jobs(jobs: list[dict], settings: dict) -> list[dict]:
    if not isinstance(jobs, list):
        _error("jobs")
    settings = validate_settings(settings)
    role_priority = {row["id"]: row["priority"] for row in settings["role_families"]}
    country_priority = {row["code"]: row["priority"] for row in settings["countries"]}
    large_groups = {fold(value) for value in settings["large_group_employers"]}
    eligible = []
    for source in jobs:
        ok, _ = evaluate_job(source, settings)
        if ok:
            job = copy.deepcopy(source)
            family, reason = _classify(job, settings)
            job.update({"role_family": family, "classification_reason": reason})
            eligible.append(job)
    eligible.sort(
        key=lambda job: (
            not (settings["vie_enabled"] and bool(job.get("is_vie"))),
            -role_priority.get(job["role_family"], 0),
            country_priority.get(str(job.get("country", "")).upper(), 10_000),
            not (
                settings["prioritize_large_groups"]
                and any(fold(job.get(field)) in large_groups for field in ("company", "employer", "end_employer"))
            ),
            -float(job.get("score") or 0),
            str(job.get("deadline") or "9999-12-31"),
            str(job["id"]),
        )
    )
    return eligible
