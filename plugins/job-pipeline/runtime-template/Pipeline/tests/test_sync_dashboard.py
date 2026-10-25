import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from Pipeline.pipeline_store import PipelineStore
from Pipeline.policy_guard import PolicyError, record_applied
from Pipeline.sync_dashboard import synchronize
from Pipeline.io_utils import exclusive_project_lock
from Pipeline.tests.test_policy_guard import candidate, confirmation, job, ready_project


def test_synchronize_skips_non_applied_current_employer_only_through_canonical_store(project_root: Path):
    store = ready_project(project_root)
    store.upsert_job(job(project_root=project_root, id="excluded", company="Synthetic Current Org"))
    store.upsert_job(job(project_root=project_root, id="safe"))
    dashboard = project_root / "Pipeline" / "job-pipeline.html"
    dashboard.write_text("task-5-not-yet-built\n", encoding="utf-8")
    dashboard_before = dashboard.read_bytes()
    synchronize(project_root)
    assert store.get_job("excluded")["status"] == "skipped"
    assert store.get_job("excluded")["score"] == 0
    assert store.get_job("safe")["status"] == "docs_ready"
    exported = json.loads((project_root / "Pipeline" / "pipeline_state.json").read_text(encoding="utf-8"))
    assert exported == store.load_state()
    assert dashboard.read_bytes() == dashboard_before


def test_synchronize_hard_fails_applied_current_employer_without_mutation_or_export(project_root: Path):
    store = ready_project(project_root)
    store.upsert_job(job(project_root=project_root, id="excluded", company="Different Synthetic Org"))
    record_applied(project_root, "excluded", confirmation())
    with sqlite3.connect(store.path) as connection:
        payload = json.loads(connection.execute("SELECT payload_json FROM jobs WHERE id = 'excluded'").fetchone()[0])
        payload["company"] = "Synthetic Current Org"
        connection.execute("UPDATE jobs SET payload_json = ? WHERE id = 'excluded'", (json.dumps(payload),))
    before_db = store.path.read_bytes()
    export = project_root / "Pipeline" / "pipeline_state.json"
    before_export = export.read_bytes()
    with pytest.raises(PolicyError, match="current-employer applied job"):
        synchronize(project_root)
    assert store.path.read_bytes() == before_db
    assert export.read_bytes() == before_export


def test_synchronize_hard_fails_malformed_applied_confirmation_before_other_mutation(project_root: Path):
    store = ready_project(project_root)
    store.upsert_job(job(project_root=project_root, id="excluded", company="Synthetic Current Org"))
    store.upsert_job(job(project_root=project_root, id="applied"))
    record_applied(project_root, "applied", confirmation())
    with sqlite3.connect(store.path) as connection:
        payload = json.loads(connection.execute("SELECT payload_json FROM jobs WHERE id = 'applied'").fetchone()[0])
        payload["confirmation"]["source_url"] = "file:///private"
        connection.execute("UPDATE jobs SET payload_json = ? WHERE id = 'applied'", (json.dumps(payload),))
    before_db = store.path.read_bytes()
    before_export = (project_root / "Pipeline" / "pipeline_state.json").read_bytes()
    with pytest.raises(PolicyError, match="canonical state is invalid"):
        synchronize(project_root)
    assert store.path.read_bytes() == before_db
    assert (project_root / "Pipeline" / "pipeline_state.json").read_bytes() == before_export


def test_synchronize_rejects_malformed_private_candidate_without_leaking_value(project_root: Path):
    ready_project(project_root)
    private_value = "PRIVATE-CANDIDATE-VALUE"
    bad = candidate(current_employer=private_value, employment_status="not_employed")
    (project_root / "private" / "candidate.yml").write_text("current_employer: " + bad["current_employer"] + "\n", encoding="utf-8")
    with pytest.raises(PolicyError) as error:
        synchronize(project_root)
    assert private_value not in str(error.value)


def test_sync_cli_defaults_to_package_root_and_returns_json(project_root: Path):
    ready_project(project_root)
    result = subprocess.run(
        [sys.executable, "Pipeline/sync_dashboard.py"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"ok": True}


def test_synchronize_shares_exclusive_application_lock(project_root: Path):
    ready_project(project_root)
    export = project_root / "Pipeline" / "pipeline_state.json"
    before = export.read_bytes()
    with exclusive_project_lock(project_root, name=".application-policy.lock"):
        with pytest.raises(PolicyError, match="application policy lock"):
            synchronize(project_root)
    assert export.read_bytes() == before


def test_synchronize_preserves_job_inserted_after_preflight(project_root: Path, monkeypatch):
    store = ready_project(project_root)
    store.upsert_job(job(project_root=project_root, id="excluded", company="Synthetic Current Org"))
    reached_skip = threading.Event()
    continue_skip = threading.Event()
    original = PipelineStore.skip_jobs_if_unchanged

    def delayed_skip(self, expected):
        reached_skip.set()
        assert continue_skip.wait(timeout=10)
        return original(self, expected)

    monkeypatch.setattr(PipelineStore, "skip_jobs_if_unchanged", delayed_skip)
    failures = []

    def run_sync():
        try:
            synchronize(project_root)
        except Exception as error:
            failures.append(error)

    thread = threading.Thread(target=run_sync)
    thread.start()
    assert reached_skip.wait(timeout=10)
    store.upsert_job(job(project_root=project_root, id="concurrent", source_url="https://jobs.invalid/concurrent", status="new"))
    continue_skip.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert failures == []
    assert store.get_job("excluded")["status"] == "skipped"
    assert store.get_job("concurrent")["status"] == "new"
    exported = json.loads((project_root / "Pipeline" / "pipeline_state.json").read_text(encoding="utf-8"))
    assert {row["id"] for row in exported["jobs"]} == {"excluded", "concurrent"}


def test_synchronize_changed_target_conflicts_without_partial_mutation(project_root: Path, monkeypatch):
    store = ready_project(project_root)
    store.upsert_job(job(project_root=project_root, id="excluded", company="Synthetic Current Org"))
    reached_skip = threading.Event()
    continue_skip = threading.Event()
    original = PipelineStore.skip_jobs_if_unchanged

    def delayed_skip(self, expected):
        reached_skip.set()
        assert continue_skip.wait(timeout=10)
        return original(self, expected)

    monkeypatch.setattr(PipelineStore, "skip_jobs_if_unchanged", delayed_skip)
    failures = []
    thread = threading.Thread(target=lambda: _capture_sync_error(project_root, failures))
    thread.start()
    assert reached_skip.wait(timeout=10)
    changed = store.get_job("excluded")
    changed["score"] = 89
    store.upsert_job(changed)
    continue_skip.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], PolicyError)
    assert store.get_job("excluded")["status"] == "docs_ready"
    assert store.get_job("excluded")["score"] == 89


def _capture_sync_error(project_root: Path, failures: list[Exception]) -> None:
    try:
        synchronize(project_root)
    except Exception as error:
        failures.append(error)
