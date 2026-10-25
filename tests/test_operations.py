import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from Pipeline.tests.conftest import project_root
from Pipeline.tests.test_policy_guard import configured_settings, job, ready_project


def test_three_consecutive_portal_errors_escalate_and_browser_missing_does_not(project_root):
    operations = importlib.import_module("Pipeline.operations")
    store = ready_project(project_root)
    store.upsert_job(job())
    for count in (1, 2):
        value = operations.record_failure(project_root, "synthetic-1", "transient_portal_error", "Portal timed out", run_id=f"run-{count}")
        assert value["status"] == "docs_ready"
        assert value["portal_error_count"] == count
    value = operations.record_failure(project_root, "synthetic-1", "transient_portal_error", "Portal timed out", run_id="run-3")
    assert value["status"] == "needs_user"
    assert value["blocker"]["category"] == "portal_broken"
    store.upsert_job(job(id="browser"))
    value = operations.record_failure(project_root, "browser", "transient_no_browser", "Controller unavailable", run_id="run-4")
    assert value["status"] == "docs_ready"


def test_document_visual_review_binds_exact_files_and_tamper_blocks(project_root):
    operations = importlib.import_module("Pipeline.operations")
    store = ready_project(project_root)
    store.upsert_job(job(status="shortlisted"))
    with pytest.raises(ValueError):
        operations.review_documents(project_root, "synthetic-1", visual_review_confirmed=False)
    value = operations.review_documents(project_root, "synthetic-1", visual_review_confirmed=True)
    assert value["status"] == "docs_ready"
    assert operations.preflight(project_root, "synthetic-1")["blockers"] == []
    (project_root / "Documents/generated/a-letter.md").write_text("Changed letter", encoding="utf-8")
    assert "document review no longer matches files or creator" in operations.preflight(project_root, "synthetic-1")["blockers"]


def test_retry_queue_precedes_new_document_work(project_root):
    operations = importlib.import_module("Pipeline.operations")
    store = ready_project(project_root)
    store.upsert_job(job(id="new-docs", status="shortlisted", score=100))
    store.upsert_job(job(id="retry", score=76, blocker={"category": "transient_no_browser", "detail": "No controller"}))
    assert operations.queue(project_root)["jobs"][0]["id"] == "retry"


def test_repeated_error_in_same_run_counts_once(project_root):
    operations = importlib.import_module("Pipeline.operations")
    store = ready_project(project_root)
    store.upsert_job(job())
    for _ in range(3):
        result = operations.record_failure(project_root, "synthetic-1", "transient_portal_error", "Portal timed out", run_id="same-run")
    assert result["portal_error_count"] == 1
    assert result["status"] == "docs_ready"


def test_preflight_requires_document_review_even_with_valid_files(project_root):
    operations = importlib.import_module("Pipeline.operations")
    store = ready_project(project_root)
    store.upsert_job(job())
    assert "document visual review is missing" in operations.preflight(project_root, "synthetic-1")["blockers"]


def test_queue_filters_current_employer_and_unknown_candidate_authorization(project_root):
    operations = importlib.import_module("Pipeline.operations")
    store = ready_project(project_root)
    store.upsert_job(job(id="excluded", company="Synthetic Current Org"))
    store.upsert_job(job(id="unknown-country", country="YY", is_vie=True))
    store.upsert_job(job(id="eligible"))
    assert [row["id"] for row in operations.queue(project_root)["jobs"]] == ["eligible"]


@pytest.mark.parametrize("source", ["overlay", "candidate", "profile"])
def test_corrected_document_inputs_invalidate_visual_review(project_root, source):
    import yaml
    operations = importlib.import_module("Pipeline.operations")
    store = ready_project(project_root)
    store.upsert_job(job(status="shortlisted", tailoring_overlay="private/jobs/a.yml"))
    overlay = project_root / "private/jobs/a.yml"
    overlay.write_text("{}\n", encoding="utf-8")
    operations.review_documents(project_root, "synthetic-1", visual_review_confirmed=True)
    assert operations.preflight(project_root, "synthetic-1")["blockers"] == []
    if source == "overlay":
        overlay.write_text("# Revised overlay\n{}\n", encoding="utf-8")
    elif source == "candidate":
        path = project_root / "private/candidate.yml"
        value = yaml.safe_load(path.read_text())
        value["career_evidence"][0]["text"] = "Corrected private experience evidence."
        path.write_text(yaml.safe_dump(value), encoding="utf-8")
    else:
        (project_root / "private/master-profile.md").write_text("# Corrected profile\n", encoding="utf-8")
    assert "document review no longer matches files or creator" in operations.preflight(project_root, "synthetic-1")["blockers"]


def test_bounded_holder_supports_cross_process_owner_check_and_release(project_root):
    from Pipeline.initialize import initialize_project
    from Pipeline.operations import assert_run_owner
    from Pipeline.run_lock import RunLockedError, acquire_run_lock

    initialize_project(project_root)
    process = subprocess.Popen([sys.executable, "-B", "-m", "Pipeline.operations", "hold-run", "--owner", "verification", "--seconds", "20"], cwd=project_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        record = json.loads(process.stdout.readline())
        assert_run_owner(project_root, record["token"])
        with pytest.raises(RunLockedError):
            with acquire_run_lock(project_root, "verification"):
                pass
        result = subprocess.run([sys.executable, "-B", "-m", "Pipeline.operations", "release-run", "--run-token", record["token"]], cwd=project_root, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout
        assert process.wait(timeout=10) == 0
        assert not (project_root / "private/run.lock").exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
