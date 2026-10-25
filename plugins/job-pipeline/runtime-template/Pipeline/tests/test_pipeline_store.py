import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import fitz
import pytest
import yaml

from Pipeline.initialize import initialize_project
from Pipeline.onboarding import (
    load_runtime,
    mark_onboarding_complete,
    register_master_cv,
    save_runtime,
    write_master_profile_draft,
)
from Pipeline.pipeline_settings import DEFAULT_SETTINGS
from Pipeline.pipeline_store import PipelineStore, StoreError


def job(**overrides):
    value = {
        "id": "synthetic-1",
        "title": "Synthetic Analyst",
        "company": "Synthetic Org",
        "country": "ZZ",
        "source_url": "https://jobs.invalid/synthetic-1",
        "status": "new",
        "score": 80,
        "detected_seniority": "entry",
    }
    value.update(overrides)
    return value


def configured_settings(**overrides):
    value = copy.deepcopy(DEFAULT_SETTINGS)
    value["role_families"] = [
        {"id": "synthetic", "label": "Synthetic", "keywords": ["synthetic"], "enabled": True, "priority": 1}
    ]
    value["countries"] = [
        {"code": "ZZ", "label": "Synthetic Country", "enabled": True, "priority": 1}
    ]
    value.update(overrides)
    return value


def make_pdf(path: Path) -> bytes:
    document = fitz.open()
    document.new_page().insert_text((72, 72), "Synthetic CV")
    document.save(path)
    document.close()
    return path.read_bytes()


def onboarding_candidate():
    return {
        "schema_version": 1,
        "identity": {"full_name": "Synthetic User", "email": "synthetic@example.test", "phone": "+10000000000", "residence_country": "ZZ"},
        "current_employer": "Synthetic Current Org",
        "employment_status": "employed",
        "work_authorization": [{"countries": ["ZZ"], "status": "authorized"}],
        "languages": ["English"],
        "notice_period": "30 days",
        "known_answers": {},
        "career_evidence": [{"id": "project-1", "text": "Built a synthetic project."}],
    }


def onboarding_preferences():
    return {
        "schema_version": 1,
        "target_titles": ["Synthetic Analyst"],
        "role_families": ["synthetic"],
        "industries": [],
        "seniority": {"minimum": "entry", "maximum": "mid"},
        "countries": [{"code": "ZZ", "priority": 1}],
        "work_modes": [],
        "salary": {"minimum": 40000, "currency": "EUR"},
        "include_keywords": [],
        "exclude_keywords": [],
        "excluded_employers": [],
        "preferred_company_types": [],
        "vie": {"enabled": False, "eligible": False},
    }


def test_initialize_creates_idempotent_zero_state_schema_indexes_and_meta(tmp_path: Path):
    database = tmp_path / "pipeline.db"
    store = PipelineStore(database)
    store.initialize()
    before = database.read_bytes()
    store.initialize()
    state = store.load_state()
    assert state["jobs"] == []
    assert state["meta"]["run_count"] == 0
    assert state["meta"]["last_run"] is None
    assert state["meta"]["created_at"].endswith("Z")
    assert store.get_settings() == {"version": 0, "settings": DEFAULT_SETTINGS}
    assert store.settings_history() == []
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"jobs", "meta", "settings", "settings_history"}.issubset(tables)
    assert {"idx_jobs_status", "idx_jobs_country", "idx_jobs_score"}.issubset(indexes)
    assert database.read_bytes() == before


def test_upsert_projects_validated_job_fields_and_returns_deep_copy(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    source = job(sponsorship_status="offered", blocker=None, source="synthetic-source")
    stored = store.upsert_job(source)
    assert stored["id"] == "synthetic-1"
    source["source"] = "changed"
    assert store.get_job("synthetic-1")["source"] == "synthetic-source"
    assert store.list_jobs()[0]["country"] == "ZZ"
    with sqlite3.connect(store.path) as connection:
        projection = connection.execute("SELECT status, country, score FROM jobs WHERE id = ?", ("synthetic-1",)).fetchone()
    assert projection == ("new", "ZZ", 80.0)


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        job(id=""),
        job(title=""),
        job(company=""),
        job(country="zz"),
        job(source_url="file:///private"),
        job(status="unknown"),
        job(score=True),
        job(score=-1),
        job(score=101),
        job(detected_seniority="unknown"),
        job(sponsorship_status="inferred"),
        job(status="needs_user"),
        job(status="new", blocker={"category": "captcha", "detail": "x"}),
        job(status="needs_user", blocker={"category": "unknown", "detail": "x"}),
        job(status="needs_user", blocker={"category": "captcha", "detail": ""}),
        job(private_blob={"secret": "must not persist"}),
        job(is_vie=1),
        job(deadline=1),
        job(job_description=[]),
        job(salary_amount=True, salary_currency="EUR"),
        job(salary_amount=50_000, salary_currency="eur"),
    ],
)
def test_job_schema_and_blockers_fail_closed(tmp_path: Path, invalid):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    with pytest.raises(StoreError):
        store.upsert_job(invalid)
    assert store.list_jobs() == []


def test_status_changes_require_valid_blocker_and_refuse_direct_applied(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    store.upsert_job(job())
    blocked = store.set_job_status(
        "synthetic-1", "needs_user", blocker={"category": "captcha", "detail": "Visible CAPTCHA"}
    )
    assert blocked["status"] == "needs_user"
    assert blocked["blocker"]["category"] == "captcha"
    with pytest.raises(StoreError, match="record_applied"):
        store.set_job_status("synthetic-1", "applied")
    with pytest.raises(StoreError, match="blocker"):
        store.set_job_status("synthetic-1", "shortlisted", blocker={"category": "captcha", "detail": "x"})
    assert store.get_job("synthetic-1")["status"] == "needs_user"


def test_status_taxonomy_is_generic_and_complete(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    accepted = {"new", "shortlisted", "docs_ready", "needs_user", "skipped", "interview", "offer", "rejected"}
    for index, status in enumerate(sorted(accepted)):
        blocker = {"category": "unknown_answer", "detail": "Needs user input"} if status == "needs_user" else None
        store.upsert_job(job(id=f"job-{index}", source_url=f"https://jobs.invalid/{index}", status=status, blocker=blocker))
    assert {row["status"] for row in store.list_jobs()} == accepted


def test_transient_browser_blocker_remains_docs_ready_and_retryable(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    store.upsert_job(job())
    result = store.set_job_status(
        "synthetic-1",
        "docs_ready",
        blocker={"category": "transient_no_browser", "detail": "Browser unavailable"},
    )
    assert result["status"] == "docs_ready"
    assert result["blocker"]["category"] == "transient_no_browser"


def test_replace_state_is_transactional_and_replaces_all_jobs_and_meta(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    store.upsert_job(job())
    state = store.load_state()
    state["meta"]["run_count"] = 2
    state["meta"]["last_run"] = "2030-01-01T00:00:00Z"
    state["jobs"] = [job(id="synthetic-2", source_url="https://jobs.invalid/synthetic-2", status="shortlisted")]
    store.replace_state(state)
    assert [row["id"] for row in store.list_jobs()] == ["synthetic-2"]
    assert store.load_state()["meta"]["run_count"] == 2

    invalid = store.load_state()
    invalid["jobs"].append(job(id="", source_url="https://jobs.invalid/bad"))
    with pytest.raises(StoreError):
        store.replace_state(invalid)
    assert [row["id"] for row in store.list_jobs()] == ["synthetic-2"]


def test_settings_use_optimistic_versions_history_noop_and_rollback(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    first = store.save_settings(configured_settings(), expected_version=0, reason="onboarding")
    assert first["version"] == 1
    assert len(store.settings_history()) == 1
    same = store.save_settings(configured_settings(), expected_version=1, reason="same")
    assert same["version"] == 1
    assert len(store.settings_history()) == 1
    with pytest.raises(StoreError, match="version"):
        store.save_settings(configured_settings(score_threshold=80), expected_version=0, reason="stale")
    second = store.save_settings(configured_settings(score_threshold=80), expected_version=1, reason="friend change")
    assert second["version"] == 2
    rolled_back = store.rollback_settings(1, expected_version=2, reason="friend rollback")
    assert rolled_back["version"] == 3
    assert rolled_back["settings"]["score_threshold"] == 75
    history = store.settings_history()
    assert [row["version"] for row in history] == [1, 2, 3]
    assert history[-1]["reason"] == "friend rollback"


def large_settings(keyword_count: int):
    value = configured_settings()
    value["include_keywords"] = [f"{index:02d}-" + "x" * 9_997 for index in range(keyword_count)]
    return value


def test_settings_near_serialized_limit_round_trip_through_save_get_history_and_rollback(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    near_limit = large_settings(52)
    first = store.save_settings(near_limit, expected_version=0, reason="near limit")
    assert first["version"] == 1
    assert store.get_settings() == first
    assert store.settings_history()[0]["settings"] == near_limit

    second = store.save_settings(configured_settings(score_threshold=80), expected_version=1, reason="smaller")
    rolled_back = store.rollback_settings(1, expected_version=second["version"], reason="restore near limit")
    assert rolled_back["settings"] == near_limit
    assert store.get_settings() == rolled_back
    assert store.settings_history()[-1]["settings"] == near_limit


def test_oversized_settings_save_is_rejected_before_transaction_without_state_change(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    before = store.get_settings()
    with pytest.raises(StoreError, match="settings"):
        store.save_settings(large_settings(53), expected_version=0, reason="too large")
    assert store.get_settings() == before
    assert store.settings_history() == []
    assert store.validate_integrity()["settings_version"] == 0


def test_export_is_deterministic_atomic_canonical_and_leaves_no_temporary_files(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    store.upsert_job(job())
    destination = tmp_path / "pipeline_state.json"
    first = store.export_state()
    first_bytes = destination.read_bytes()
    second = store.export_state()
    assert first == second == destination
    assert destination.read_bytes() == first_bytes
    assert json.loads(first_bytes.decode("utf-8")) == store.load_state()
    assert not list(tmp_path.glob("pipeline_state.json.tmp-*"))
    for forbidden in (tmp_path / "private" / "state.json", tmp_path / "config" / "state.json", store.path, tmp_path / "other.json"):
        with pytest.raises(StoreError, match="export"):
            store.export_state(forbidden)


def test_integrity_reports_counts_and_detects_projection_or_json_corruption(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    store.upsert_job(job())
    assert store.validate_integrity()["job_count"] == 1
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE jobs SET country = 'AA' WHERE id = 'synthetic-1'")
    with pytest.raises(StoreError, match="integrity"):
        store.validate_integrity()


def test_integrity_detects_missing_settings_history_revision(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    store.save_settings(configured_settings(), expected_version=0, reason="first")
    store.save_settings(configured_settings(score_threshold=80), expected_version=1, reason="second")
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER settings_history_no_delete")
        connection.execute("DELETE FROM settings_history WHERE version = 1")
    with pytest.raises(StoreError, match="integrity"):
        store.validate_integrity()


def test_settings_history_is_sql_immutable_and_version_zero_is_anchored(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE settings SET payload_json = '{}' WHERE singleton = 1")
    with pytest.raises(StoreError, match="integrity"):
        store.validate_integrity()

    anchored = PipelineStore(tmp_path / "anchored.db")
    anchored.initialize()
    anchored.save_settings(configured_settings(), expected_version=0, reason="first")
    with sqlite3.connect(anchored.path) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE settings_history SET reason = 'changed' WHERE version = 1")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM settings_history WHERE version = 1")
    assert anchored.validate_integrity()["settings_history_count"] == 1


def valid_confirmation(**overrides):
    value = {
        "kind": "portal_success",
        "observed_at": "2030-01-02T03:04:05Z",
        "summary": "Portal displayed a submitted confirmation.",
        "source_url": "https://jobs.invalid/application/confirmation",
    }
    value.update(overrides)
    return value


def expected_job_fingerprint(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def enable_store_cap(store: PipelineStore, cap: int = 1) -> dict:
    current = store.get_settings()
    settings = copy.deepcopy(current["settings"])
    settings["daily_submission_cap"] = cap
    return store.save_settings(settings, expected_version=current["version"], reason="test application cap")


def guarded_store_apply(store: PipelineStore, job_id: str, confirmation: dict) -> dict:
    revision = store.get_settings()
    if revision["settings"]["daily_submission_cap"] == 0:
        revision = enable_store_cap(store, 1)
    current = store.get_job(job_id)
    return store.record_applied(
        job_id,
        confirmation,
        expected_job_fingerprint=expected_job_fingerprint(current),
        expected_settings_version=revision["version"],
        readiness_check=lambda *_args: None,
    )


@pytest.mark.parametrize(
    "confirmation",
    [
        {},
        valid_confirmation(extra=True),
        valid_confirmation(kind="unknown"),
        valid_confirmation(observed_at="2030-01-02 03:04:05"),
        valid_confirmation(summary=""),
        valid_confirmation(source_url="file:///confirmation"),
    ],
)
def test_record_applied_requires_docs_ready_and_exact_visible_evidence(tmp_path: Path, confirmation):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    store.upsert_job(job(status="docs_ready"))
    before = store.get_job("synthetic-1")
    with pytest.raises(StoreError):
        guarded_store_apply(store, "synthetic-1", confirmation)
    assert store.get_job("synthetic-1") == before


def test_record_applied_is_only_atomic_transition_and_ordinary_apis_cannot_mutate_it(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    confirmation = valid_confirmation()
    store.upsert_job(job(status="new"))
    with pytest.raises(StoreError, match="docs_ready"):
        guarded_store_apply(store, "synthetic-1", confirmation)
    store.set_job_status("synthetic-1", "docs_ready")
    applied = guarded_store_apply(store, "synthetic-1", confirmation)
    assert applied["status"] == "applied"
    assert applied["confirmation"] == confirmation

    for forbidden in (
        job(id="new-applied", source_url="https://jobs.invalid/new-applied", status="applied", confirmation=confirmation),
        job(id="marker", source_url="https://jobs.invalid/marker", applied=False),
        job(id="reserved", source_url="https://jobs.invalid/reserved", confirmation=confirmation),
    ):
        with pytest.raises(StoreError, match="applied|confirmation"):
            store.upsert_job(forbidden)
    with pytest.raises(StoreError, match="record_applied"):
        store.set_job_status("synthetic-1", "rejected")

    state = store.load_state()
    original_applied = copy.deepcopy(state["jobs"][0])
    changed = copy.deepcopy(state)
    changed["jobs"][0]["title"] = "Edited"
    with pytest.raises(StoreError, match="applied"):
        store.replace_state(changed)
    removed = copy.deepcopy(state)
    removed["jobs"] = []
    with pytest.raises(StoreError, match="applied"):
        store.replace_state(removed)
    created = copy.deepcopy(state)
    created["jobs"].append(job(id="forged", source_url="https://jobs.invalid/forged", status="applied", confirmation=confirmation))
    with pytest.raises(StoreError, match="applied"):
        store.replace_state(created)
    assert store.get_job("synthetic-1") == original_applied


def test_guarded_record_applied_rechecks_job_settings_cap_and_callback_inside_one_transaction(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    store.upsert_job(job(status="docs_ready"))
    revision = enable_store_cap(store, 1)
    current = store.get_job("synthetic-1")
    callback_observation = {}

    def readiness_check(canonical_job, canonical_settings, settings_version):
        callback_observation.update(
            {
                "job": canonical_job,
                "settings": canonical_settings,
                "version": settings_version,
            }
        )
        with sqlite3.connect(store.path, timeout=0.05) as competing:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("BEGIN IMMEDIATE")

    applied = store.record_applied(
        "synthetic-1",
        valid_confirmation(),
        expected_job_fingerprint=expected_job_fingerprint(current),
        expected_settings_version=revision["version"],
        readiness_check=readiness_check,
    )
    assert applied["status"] == "applied"
    assert callback_observation["job"] == current
    assert callback_observation["settings"]["daily_submission_cap"] == 1
    assert callback_observation["version"] == revision["version"]


@pytest.mark.parametrize("conflict", ["fingerprint", "version", "cap"])
def test_guarded_record_applied_conflicts_leave_job_unchanged(tmp_path: Path, conflict: str):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    store.upsert_job(job(status="docs_ready"))
    revision = enable_store_cap(store, 1)
    current = store.get_job("synthetic-1")
    fingerprint = expected_job_fingerprint(current)
    version = revision["version"]
    if conflict == "fingerprint":
        fingerprint = "0" * 64
    elif conflict == "version":
        version -= 1
    else:
        latest = store.get_settings()
        settings = copy.deepcopy(latest["settings"])
        settings["daily_submission_cap"] = 0
        store.save_settings(settings, expected_version=latest["version"], reason="disable application")
        version += 1
    before = store.get_job("synthetic-1")
    with pytest.raises(StoreError):
        store.record_applied(
            "synthetic-1",
            valid_confirmation(),
            expected_job_fingerprint=fingerprint,
            expected_settings_version=version,
            readiness_check=lambda *_args: None,
        )
    assert store.get_job("synthetic-1") == before


def test_guarded_record_applied_fails_closed_on_projection_corruption_before_cap_count(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    store.upsert_job(job(id="first", source_url="https://jobs.invalid/first", status="docs_ready"))
    enable_store_cap(store, 1)
    guarded_store_apply(store, "first", valid_confirmation())
    store.upsert_job(job(id="second", source_url="https://jobs.invalid/second", status="docs_ready"))
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE jobs SET status = 'docs_ready' WHERE id = 'first'")
    second = store.get_job("second")
    revision = store.get_settings()
    with pytest.raises(StoreError):
        store.record_applied(
            "second",
            valid_confirmation(),
            expected_job_fingerprint=expected_job_fingerprint(second),
            expected_settings_version=revision["version"],
            readiness_check=lambda *_args: None,
        )
    assert store.get_job("second")["status"] == "docs_ready"


def test_compare_and_skip_updates_only_exact_non_applied_targets_and_preserves_other_rows(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    store.upsert_job(job(id="target", source_url="https://jobs.invalid/target", status="docs_ready"))
    store.upsert_job(job(id="concurrent", source_url="https://jobs.invalid/concurrent", status="new"))
    target = store.get_job("target")
    changed = store.skip_jobs_if_unchanged(
        [{"id": "target", "status": "docs_ready", "fingerprint": expected_job_fingerprint(target)}]
    )
    assert [row["id"] for row in changed] == ["target"]
    assert store.get_job("target")["status"] == "skipped"
    assert store.get_job("target")["score"] == 0
    assert store.get_job("concurrent")["status"] == "new"


def test_compare_and_skip_conflict_is_atomic_and_changes_nothing(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    for identifier in ("first", "second"):
        store.upsert_job(job(id=identifier, source_url=f"https://jobs.invalid/{identifier}", status="docs_ready"))
    expected = [
        {"id": identifier, "status": "docs_ready", "fingerprint": expected_job_fingerprint(store.get_job(identifier))}
        for identifier in ("first", "second")
    ]
    changed = store.get_job("second")
    changed["score"] = 79
    store.upsert_job(changed)
    before = store.load_state()
    with pytest.raises(StoreError, match="conflict"):
        store.skip_jobs_if_unchanged(expected)
    assert store.load_state() == before


def test_replace_state_preserves_applied_rows_while_replacing_other_rows(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    store.upsert_job(job(status="docs_ready"))
    guarded_store_apply(store, "synthetic-1", valid_confirmation())
    store.upsert_job(job(id="mutable", source_url="https://jobs.invalid/mutable"))
    state = store.load_state()
    mutable = next(row for row in state["jobs"] if row["id"] == "mutable")
    mutable["status"] = "shortlisted"
    store.replace_state(state)
    assert store.get_job("synthetic-1")["status"] == "applied"
    assert store.get_job("mutable")["status"] == "shortlisted"


def test_store_rejects_huge_deep_oversized_and_nonfinite_json_without_leaking_values(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    invalid_jobs = [
        job(score=10**10_000),
        job(score=float("inf")),
        job(job_description="x" * 300_000),
        job(search_metadata={"nested": [[[[[[[[[[[[[[[[[[["too deep"]]]]]]]]]]]]]]]]]]]}),
    ]
    for invalid in invalid_jobs:
        with pytest.raises(StoreError) as error:
            store.upsert_job(invalid)
        assert "too deep" not in str(error.value)
    assert store.list_jobs() == []


def test_cli_rejects_duplicate_keys_oversized_input_and_huge_integer(project_root: Path):
    initialize_project(project_root)
    import_path = project_root / "private" / "imports" / "job.json"
    command = [sys.executable, "Pipeline/pipeline_store.py", "--root", ".", "upsert-job", "--input", "private/imports/job.json"]
    for payload in (
        b'{"id":"one","id":"two"}',
        b'{"score":' + b"9" * 10_000 + b"}",
        b" " * (1024 * 1024 + 1),
    ):
        import_path.write_bytes(payload)
        result = subprocess.run(command, cwd=project_root, text=True, capture_output=True)
        assert result.returncode == 2
        assert "error:" in result.stderr
        assert len(result.stderr) < 200


def test_store_closes_connections_after_success_and_failure(tmp_path: Path):
    database = tmp_path / "pipeline.db"
    store = PipelineStore(database)
    store.initialize()
    store.upsert_job(job())
    with pytest.raises(StoreError):
        store.upsert_job(job(score=-1))
    store.get_settings()
    store.load_state()
    store.validate_integrity()
    moved = tmp_path / "moved.db"
    os.replace(database, moved)
    os.replace(moved, database)
    assert PipelineStore(database).get_job("synthetic-1")["id"] == "synthetic-1"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@jobs.invalid/role",
        "https://jobs.invalid:99999/role",
        "https://bad_host.invalid/role",
        " https://jobs.invalid/role",
        "https://jobs.invalid/role ",
        "https:// jobs.invalid/role",
        "https://jobs.invalid/role\nnext",
        "https://[not-ipv6]/role",
    ],
)
def test_store_rejects_ambiguous_or_malformed_http_urls(tmp_path: Path, url: str):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    with pytest.raises(StoreError, match="source_url"):
        store.upsert_job(job(source_url=url))


def test_cli_import_is_confined_exact_utf8_one_object_and_preserves_input(project_root: Path):
    initialize_project(project_root)
    import_path = project_root / "private" / "imports" / "job.json"
    payload = json.dumps(job(), ensure_ascii=False).encode("utf-8")
    import_path.write_bytes(payload)
    before_hash = hashlib.sha256(payload).hexdigest()
    command = [sys.executable, "Pipeline/pipeline_store.py", "--root", ".", "upsert-job", "--input", "private/imports/job.json"]
    result = subprocess.run(command, cwd=project_root, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["id"] == "synthetic-1"
    assert hashlib.sha256(import_path.read_bytes()).hexdigest() == before_hash
    assert import_path.exists()

    outside = project_root / "outside.json"
    outside.write_text(json.dumps(job(id="outside")), encoding="utf-8")
    rejected = subprocess.run(command[:-1] + ["outside.json"], cwd=project_root, text=True, capture_output=True)
    assert rejected.returncode == 2
    assert "error:" in rejected.stderr
    assert "Synthetic Org" not in rejected.stdout + rejected.stderr
    assert outside.exists()

    import_path.write_bytes(b"{}\n{}")
    malformed = subprocess.run(command, cwd=project_root, text=True, capture_output=True)
    assert malformed.returncode == 2
    assert "job" in malformed.stderr
    import_path.write_bytes(b"\xff")
    non_utf8 = subprocess.run(command, cwd=project_root, text=True, capture_output=True)
    assert non_utf8.returncode == 2
    assert "UTF-8" in non_utf8.stderr


def test_cli_rejects_import_reparse_ancestor_before_read(project_root: Path, tmp_path: Path):
    initialize_project(project_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "job.json").write_text(json.dumps(job()), encoding="utf-8")
    link = project_root / "private" / "imports" / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink unavailable: {error}")
    result = subprocess.run(
        [sys.executable, "Pipeline/pipeline_store.py", "--root", ".", "upsert-job", "--input", "private/imports/linked/job.json"],
        cwd=project_root,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2
    assert "confinement" in result.stderr


def test_initialize_project_creates_only_isolated_empty_database_and_export(project_root: Path):
    source_json = project_root / "Pipeline" / "legacy.json"
    source_json.write_text(json.dumps({"jobs": [job(id="must-not-import")]}), encoding="utf-8")
    initialize_project(project_root)
    store = PipelineStore(project_root / "Pipeline" / "pipeline.db")
    assert store.list_jobs() == []
    assert json.loads((project_root / "Pipeline" / "pipeline_state.json").read_text(encoding="utf-8"))["jobs"] == []
    assert (project_root / "private" / "imports").is_dir()
    initialize_project(project_root)
    assert store.list_jobs() == []


def test_onboarding_saves_first_nonblank_settings_once_without_enabling_submission(project_root: Path):
    initialize_project(project_root)
    private = project_root / "private"
    (private / "candidate.yml").write_text(yaml.safe_dump(onboarding_candidate()), encoding="utf-8")
    (private / "job-preferences.yml").write_text(yaml.safe_dump(onboarding_preferences()), encoding="utf-8")
    source = project_root / "source.pdf"
    make_pdf(source)
    register_master_cv(project_root, source)
    write_master_profile_draft(project_root, "# Synthetic profile\n")
    runtime = load_runtime(project_root)
    runtime["onboarding"]["creator_ready"] = True
    save_runtime(project_root, runtime)

    mark_onboarding_complete(project_root)
    mark_onboarding_complete(project_root)
    store = PipelineStore(project_root / "Pipeline" / "pipeline.db")
    current = store.get_settings()
    assert current["version"] == 1
    assert current["settings"]["daily_submission_cap"] == 0
    assert current["settings"]["role_families"][0]["keywords"] == ["synthetic", "Synthetic Analyst"]
    assert len(store.settings_history()) == 1
    assert load_runtime(project_root)["submission_mode"] == "review_only"


def test_onboarding_accepts_explicit_role_and_country_rows_used_by_settings(project_root: Path):
    initialize_project(project_root)
    private = project_root / "private"
    preferences = onboarding_preferences()
    preferences["role_families"] = [
        {"id": "synthetic", "label": "Synthetic", "keywords": ["analyst"], "priority": 1}
    ]
    preferences["countries"] = [
        {"code": "ZZ", "label": "Synthetic Country", "priority": 1}
    ]
    (private / "candidate.yml").write_text(yaml.safe_dump(onboarding_candidate()), encoding="utf-8")
    (private / "job-preferences.yml").write_text(yaml.safe_dump(preferences), encoding="utf-8")
    source = project_root / "source.pdf"
    make_pdf(source)
    register_master_cv(project_root, source)
    write_master_profile_draft(project_root, "# Synthetic profile\n")
    runtime = load_runtime(project_root)
    runtime["onboarding"]["creator_ready"] = True
    save_runtime(project_root, runtime)

    mark_onboarding_complete(project_root)
    settings = PipelineStore(project_root / "Pipeline" / "pipeline.db").get_settings()["settings"]
    assert settings["role_families"][0]["id"] == "synthetic"
    assert settings["countries"][0]["label"] == "Synthetic Country"


def test_onboarding_validation_failure_does_not_mutate_settings_or_runtime(project_root: Path):
    initialize_project(project_root)
    private = project_root / "private"
    bad = onboarding_candidate()
    bad["identity"]["full_name"] = ""
    (private / "candidate.yml").write_text(yaml.safe_dump(bad), encoding="utf-8")
    (private / "job-preferences.yml").write_text(yaml.safe_dump(onboarding_preferences()), encoding="utf-8")
    runtime_before = (private / "runtime.json").read_bytes()
    with pytest.raises(Exception):
        mark_onboarding_complete(project_root)
    store = PipelineStore(project_root / "Pipeline" / "pipeline.db")
    assert store.get_settings()["version"] == 0
    assert (private / "runtime.json").read_bytes() == runtime_before


def test_dashboard_read_models_are_bounded_projected_and_paginated(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    for index in range(3):
        private_fields = (
            {
                "blocker": {"category": "unknown_answer", "detail": "PRIVATE-BLOCKER"},
                "cv": "Documents/generated/private.pdf",
                "cover_letter": "Documents/generated/private.md",
            }
            if index == 0
            else {}
        )
        store.upsert_job(
            job(
                id=f"job-{index}",
                source_url=f"https://jobs.invalid/{index}",
                title=f"Synthetic {index}",
                status="needs_user" if index == 0 else "new",
                **private_fields,
            )
        )
    first = store.dashboard_jobs(page=1, page_size=2)
    assert first == {
        "items": [
            {"id": "job-0", "title": "Synthetic 0", "company": "Synthetic Org", "status": "needs_user", "source_url": "https://jobs.invalid/0"},
            {"id": "job-1", "title": "Synthetic 1", "company": "Synthetic Org", "status": "new", "source_url": "https://jobs.invalid/1"},
        ],
        "page": 1,
        "page_size": 2,
        "total": 3,
        "pages": 2,
    }
    assert store.dashboard_jobs(page=2, page_size=2)["items"][0]["id"] == "job-2"
    assert "PRIVATE-BLOCKER" not in json.dumps(first)
    summary = store.dashboard_job_summary()
    assert summary["summaries"] == {
        "total_jobs": 3,
        "by_status": {"needs_user": 1, "new": 2},
        "by_country": {"ZZ": 3},
        "applied_today": 0,
    }
    assert set(summary["meta"]) == {"run_count", "last_run", "created_at"}


def test_dashboard_history_projection_omits_settings_payload_and_is_bounded(tmp_path: Path):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    store.save_settings(configured_settings(), expected_version=0, reason="first")
    store.save_settings(configured_settings(score_threshold=80), expected_version=1, reason="second")
    page = store.dashboard_settings_history(page=1, page_size=1)
    assert page["items"][0]["version"] == 2
    assert set(page["items"][0]) == {"version", "reason", "created_at"}
    assert page["page"] == 1 and page["page_size"] == 1 and page["total"] == 2 and page["pages"] == 2


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("dashboard_jobs", {"page": 0, "page_size": 10}),
        ("dashboard_jobs", {"page": 10_001, "page_size": 10}),
        ("dashboard_jobs", {"page": 1, "page_size": 101}),
        ("dashboard_jobs", {"page": True, "page_size": 10}),
        ("dashboard_settings_history", {"page": 0, "page_size": 10}),
        ("dashboard_settings_history", {"page": 1, "page_size": 101}),
    ],
)
def test_dashboard_read_models_reject_unbounded_or_wrong_type_pages(tmp_path: Path, method: str, arguments: dict):
    store = PipelineStore(tmp_path / "pipeline.db")
    store.initialize()
    with pytest.raises(StoreError, match="dashboard.pagination"):
        getattr(store, method)(**arguments)
