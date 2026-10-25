import importlib
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "job-pipeline"


def test_bootstrap_copies_only_clean_template_to_new_workspace(tmp_path):
    bootstrap = importlib.import_module("create_workspace")
    destination = tmp_path / "candidate-workspace"
    result = bootstrap.create_workspace(destination)
    assert result == destination
    assert (destination / "Pipeline" / "pipeline_store.py").is_file()
    assert not (destination / "private").exists()
    assert not (destination / "Pipeline" / "pipeline.db").exists()
    runtime = json.loads((destination / "config" / "runtime.example.json").read_text())
    assert runtime["submission_mode"] == "review_only"
    assert (destination / "WORKSPACE_MANIFEST.sha256").is_file()


def test_bootstrap_refuses_existing_and_plugin_destinations(tmp_path):
    bootstrap = importlib.import_module("create_workspace")
    with pytest.raises(ValueError):
        bootstrap.create_workspace(tmp_path)
    with pytest.raises(ValueError):
        bootstrap.create_workspace(PLUGIN / "new-workspace")
    assert not (PLUGIN / "new-workspace").exists()


def test_bootstrap_refuses_symlink_ancestor(tmp_path):
    bootstrap = importlib.import_module("create_workspace")
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires OS permission")
    with pytest.raises(ValueError):
        bootstrap.create_workspace(alias / "workspace")


def test_fresh_workspace_initialization_has_no_history_or_submission_authority(tmp_path):
    from Pipeline.initialize import initialize_project
    from Pipeline.pipeline_store import PipelineStore

    destination = importlib.import_module("create_workspace").create_workspace(tmp_path / "fresh")
    initialize_project(destination)
    store = PipelineStore(destination / "Pipeline/pipeline.db")
    assert store.list_jobs() == []
    assert store.get_settings()["settings"]["daily_submission_cap"] == 0
    runtime = json.loads((destination / "private/runtime.json").read_text())
    assert runtime["submission_mode"] == "review_only"
    assert runtime["auto_apply_approved_at"] is None
