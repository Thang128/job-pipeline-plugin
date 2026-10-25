import copy
import json

import pytest

from Pipeline.pipeline_settings import (
    DEFAULT_SETTINGS,
    SettingsError,
    classify_job,
    evaluate_job,
    rank_jobs,
    settings_from_preferences,
    validate_settings,
)


def configured_settings(**overrides):
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings.update(
        {
            "role_families": [
                {
                    "id": "synthetic",
                    "label": "Synthetic",
                    "keywords": ["synthetic"],
                    "enabled": True,
                    "priority": 1,
                }
            ],
            "countries": [
                {
                    "code": "ZZ",
                    "label": "Synthetic Country",
                    "enabled": True,
                    "priority": 1,
                }
            ],
        }
    )
    settings.update(overrides)
    return settings


def valid_job(**overrides):
    job = {
        "id": "A",
        "title": "Synthetic Analyst",
        "company": "Example Org",
        "country": "ZZ",
        "score": 80,
        "status": "docs_ready",
        "detected_seniority": "entry",
        "job_description": "Synthetic analysis",
    }
    job.update(overrides)
    return job


def test_defaults_produce_safe_review_queue_without_candidate_values():
    assert DEFAULT_SETTINGS == {
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
    serialized = json.dumps(DEFAULT_SETTINGS).casefold()
    assert "current employer" not in serialized
    assert "candidate" not in serialized


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra=True),
        lambda value: value.update(schema_version=True),
        lambda value: value.update(score_threshold=-1),
        lambda value: value.update(score_threshold=101),
        lambda value: value.update(daily_submission_cap=-1),
        lambda value: value.update(daily_submission_cap=51),
        lambda value: value.update(daily_submission_cap=1.0),
        lambda value: value.update(minimum_seniority="unknown"),
        lambda value: value.update(minimum_seniority="senior", maximum_seniority="entry"),
        lambda value: value.update(prioritize_large_groups=1),
        lambda value: value.update(vie_enabled=1),
        lambda value: value.update(minimum_salary={"amount": -1, "currency": "EUR"}),
        lambda value: value.update(minimum_salary={"amount": 10**10_000, "currency": "EUR"}),
        lambda value: value.update(minimum_salary={"amount": 1, "currency": "eur"}),
        lambda value: value.update(include_keywords=[""]),
    ],
)
def test_settings_reject_wrong_types_bounds_unknown_keys_and_invalid_rows(mutation):
    value = configured_settings()
    mutation(value)
    with pytest.raises(SettingsError):
        validate_settings(value)


@pytest.mark.parametrize(
    ("field", "rows"),
    [
        (
            "role_families",
            [
                {"id": "same", "label": "One", "keywords": ["one"], "enabled": True, "priority": 1},
                {"id": "same", "label": "Two", "keywords": ["two"], "enabled": True, "priority": 2},
            ],
        ),
        (
            "role_families",
            [{"id": "bad_slug", "label": "One", "keywords": ["one"], "enabled": True, "priority": 1}],
        ),
        (
            "role_families",
            [{"id": "one", "label": "One", "keywords": [], "enabled": True, "priority": 1}],
        ),
        (
            "countries",
            [
                {"code": "AA", "label": "One", "enabled": True, "priority": 1},
                {"code": "AA", "label": "Two", "enabled": True, "priority": 2},
            ],
        ),
        (
            "countries",
            [{"code": "aa", "label": "One", "enabled": True, "priority": 1}],
        ),
        (
            "countries",
            [
                {"code": "AA", "label": "One", "enabled": True, "priority": 1},
                {"code": "BB", "label": "Two", "enabled": True, "priority": 3},
            ],
        ),
    ],
)
def test_settings_reject_duplicate_noncanonical_and_noncontiguous_priority_rows(field, rows):
    value = configured_settings()
    value[field] = rows
    with pytest.raises(SettingsError, match=field):
        validate_settings(value)


def test_validation_returns_normalized_deep_copy():
    source = configured_settings(include_keywords=["  Useful  "])
    source["role_families"][0]["label"] = "  Synthetic  "
    normalized = validate_settings(source)
    assert normalized["include_keywords"] == ["Useful"]
    assert normalized["role_families"][0]["label"] == "Synthetic"
    normalized["role_families"][0]["keywords"].append("changed")
    assert source["role_families"][0]["keywords"] == ["synthetic"]


def test_preferences_create_canonical_rows_cover_titles_and_keep_safe_controls():
    preferences = {
        "schema_version": 1,
        "target_titles": ["Synthetic Analyst"],
        "role_families": [
            {"id": "analysis", "label": "Analysis", "keywords": ["analyst"], "priority": 1}
        ],
        "industries": ["Synthetic Industry"],
        "seniority": {"minimum": "entry", "maximum": "mid"},
        "countries": [{"code": "ZZ", "label": "Synthetic Country", "priority": 1}],
        "work_modes": ["hybrid"],
        "salary": {"minimum": 50000, "currency": "EUR"},
        "include_keywords": ["pricing"],
        "exclude_keywords": ["commission only"],
        "excluded_employers": ["Excluded Org"],
        "preferred_company_types": ["large"],
        "vie": {"enabled": True, "eligible": True},
    }
    settings = settings_from_preferences(preferences)
    assert settings["role_families"] == [
        {
            "id": "analysis",
            "label": "Analysis",
            "keywords": ["analyst", "Synthetic Analyst"],
            "enabled": True,
            "priority": 1,
        }
    ]
    assert settings["countries"] == [
        {"code": "ZZ", "label": "Synthetic Country", "enabled": True, "priority": 1}
    ]
    assert settings["minimum_salary"] == {"amount": 50000, "currency": "EUR"}
    assert settings["minimum_seniority"] == "entry"
    assert settings["maximum_seniority"] == "mid"
    assert settings["daily_submission_cap"] == 0
    assert settings["score_threshold"] == 75
    assert settings["vie_enabled"] is True
    assert "industries" not in settings
    assert "work_modes" not in settings
    assert settings["prioritize_large_groups"] is False


def test_legacy_onboarding_family_names_map_without_relaxing_settings_schema():
    preferences = {
        "schema_version": 1,
        "target_titles": ["Analyst"],
        "role_families": ["analytics"],
        "industries": [],
        "seniority": {"minimum": "junior", "maximum": "senior"},
        "countries": [{"code": "ZZ", "priority": 1}],
        "work_modes": [],
        "salary": {"minimum": 0, "currency": "EUR"},
        "include_keywords": [],
        "exclude_keywords": [],
        "excluded_employers": [],
        "preferred_company_types": [],
        "vie": {"enabled": False, "eligible": False},
    }
    settings = settings_from_preferences(preferences)
    assert settings["role_families"][0]["keywords"] == ["analytics", "Analyst"]
    assert settings["minimum_seniority"] == "entry"
    assert settings["maximum_seniority"] == "senior"


def test_preferences_mapping_rejects_unknown_keys_and_invalid_private_hints():
    base = {
        "schema_version": 1,
        "target_titles": ["Analyst"],
        "role_families": ["analytics"],
        "industries": [],
        "seniority": {"minimum": "entry", "maximum": "mid"},
        "countries": [{"code": "ZZ", "priority": 1}],
        "work_modes": [],
        "salary": {"minimum": 0, "currency": "EUR"},
        "include_keywords": [],
        "exclude_keywords": [],
        "excluded_employers": [],
        "preferred_company_types": [],
        "vie": {"enabled": False, "eligible": False},
    }
    unknown = copy.deepcopy(base)
    unknown["private_extra"] = True
    with pytest.raises(SettingsError, match="preferences"):
        settings_from_preferences(unknown)
    invalid_hint = copy.deepcopy(base)
    invalid_hint["industries"] = "not-a-list"
    with pytest.raises(SettingsError, match="preferences.industries"):
        settings_from_preferences(invalid_hint)


def test_classification_prefers_higher_role_priority_then_title_before_description():
    settings = configured_settings()
    settings["role_families"] = [
        {"id": "data", "label": "Data", "keywords": ["data"], "enabled": True, "priority": 1},
        {"id": "risk", "label": "Risk", "keywords": ["risk"], "enabled": True, "priority": 2},
    ]
    family, reason = classify_job(
        valid_job(title="Data Analyst", job_description="Risk modelling"), settings
    )
    assert family == "risk"
    assert reason == "description matched 'risk'"


def test_classification_accepts_title_or_description_only_inputs():
    settings = configured_settings()
    assert classify_job({"title": "Synthetic Analyst"}, settings) == (
        "synthetic",
        "title matched 'synthetic'",
    )
    assert classify_job({"job_description": "Synthetic analysis"}, settings) == (
        "synthetic",
        "description matched 'synthetic'",
    )


@pytest.mark.parametrize("job", [{"title": []}, {"job_description": 1}, []])
def test_classification_rejects_only_non_object_or_wrong_classification_field_types(job):
    with pytest.raises(SettingsError, match="job"):
        classify_job(job, configured_settings())


@pytest.mark.parametrize(
    ("settings_change", "job_change", "reason_fragment"),
    [
        ({"role_families": [{"id": "other", "label": "Other", "keywords": ["other"], "enabled": True, "priority": 1}]}, {}, "role family"),
        ({"countries": [{"code": "AA", "label": "Other", "enabled": True, "priority": 1}]}, {}, "country"),
        ({}, {"detected_seniority": None}, "seniority"),
        ({}, {"detected_seniority": "senior"}, "seniority"),
        ({}, {"score": 74.9}, "score"),
        ({"minimum_salary": {"amount": 50000, "currency": "EUR"}}, {"salary_amount": 49999, "salary_currency": "EUR"}, "salary"),
        ({"minimum_salary": {"amount": 50000, "currency": "EUR"}}, {"salary_amount": 60000, "salary_currency": "USD"}, "salary currency"),
        ({"include_keywords": ["required"]}, {}, "include keyword"),
        ({"exclude_keywords": ["synthetic"]}, {}, "exclude keyword"),
        ({"excluded_employers": ["Example Org"]}, {}, "employer"),
        ({}, {"is_vie": True}, "VIE"),
    ],
)
def test_evaluation_fails_each_configured_gate(settings_change, job_change, reason_fragment):
    settings = configured_settings()
    settings.update(settings_change)
    ok, reasons = evaluate_job(valid_job(**job_change), settings)
    assert ok is False
    assert any(reason_fragment.casefold() in reason.casefold() for reason in reasons)


def test_evaluation_does_not_infer_work_authorization_and_accepts_unstated_salary():
    job = valid_job(work_authorization="unknown", sponsorship_status="unknown")
    ok, reasons = evaluate_job(job, configured_settings(minimum_salary={"amount": 50000, "currency": "EUR"}))
    assert ok is True
    assert reasons == []


def test_ranking_filters_ineligible_and_uses_complete_stable_order():
    settings = configured_settings(vie_enabled=True)
    settings["role_families"] = [
        {"id": "data", "label": "Data", "keywords": ["data"], "enabled": True, "priority": 2},
        {"id": "risk", "label": "Risk", "keywords": ["risk"], "enabled": True, "priority": 1},
    ]
    settings["countries"] = [
        {"code": "AA", "label": "A", "enabled": True, "priority": 1},
        {"code": "BB", "label": "B", "enabled": True, "priority": 2},
    ]
    jobs = [
        valid_job(id="Z", title="Risk Analyst", country="AA", score=99, deadline="2030-01-01"),
        valid_job(id="E", title="Data Analyst", country="BB", score=90, deadline="2030-01-03"),
        valid_job(id="D", title="Data Analyst", country="AA", score=80, deadline="2030-01-02"),
        valid_job(id="C", title="Data Analyst", country="AA", score=90, deadline="2030-01-03"),
        valid_job(id="B", title="Data Analyst", country="AA", score=90, deadline="2030-01-02"),
        valid_job(id="A", title="Data Analyst", country="AA", score=90, deadline="2030-01-02"),
        valid_job(id="V", title="Risk Analyst", country="BB", score=75, is_vie=True),
        valid_job(id="X", title="Data Analyst", country="AA", score=10),
    ]
    ranked = rank_jobs(jobs, settings)
    assert [job["id"] for job in ranked] == ["V", "A", "B", "C", "D", "E", "Z"]
    assert ranked[1]["role_family"] == "data"
    assert ranked[1]["classification_reason"] == "title matched 'data'"
    assert jobs[1].get("role_family") is None


def test_large_group_preference_is_exact_optional_tie_break_after_country_before_score():
    jobs = [
        valid_job(id="high", company="Small Org", score=95),
        valid_job(id="group", company="Large Org", score=80),
        valid_job(id="partial", company="Large Org Subsidiary", score=99),
    ]
    off = configured_settings(
        prioritize_large_groups=False,
        large_group_employers=["Large Org"],
    )
    assert [row["id"] for row in rank_jobs(jobs, off)] == ["partial", "high", "group"]
    on = configured_settings(
        prioritize_large_groups=True,
        large_group_employers=["Large Org"],
    )
    assert [row["id"] for row in rank_jobs(jobs, on)] == ["group", "partial", "high"]


@pytest.mark.parametrize(
    "job_change",
    [
        {"is_vie": 1},
        {"deadline": 20300101},
        {"job_description": []},
        {"salary_amount": True, "salary_currency": "EUR"},
        {"salary_amount": 50_000, "salary_currency": []},
        {"company": []},
    ],
)
def test_evaluation_fails_closed_for_wrong_optional_types(job_change):
    ok, reasons = evaluate_job(valid_job(**job_change), configured_settings())
    assert ok is False
    assert any("type" in reason for reason in reasons)
