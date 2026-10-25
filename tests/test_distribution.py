import importlib.util
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "job-pipeline"


def load_release():
    path = ROOT / "tools" / "release.py"
    assert path.is_file(), "release validator must exist"
    spec = importlib.util.spec_from_file_location("pipeline_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_both_hosts_use_same_plugin_identity_and_skills():
    portable = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert portable["name"] == claude["name"] == "job-pipeline"
    assert portable["version"] == claude["version"] == "0.1.0"
    assert {p.parent.name for p in (PLUGIN / "skills").glob("*/SKILL.md")} == {
        "setup", "search", "tailor", "apply", "vie", "status", "explain"
    }
    assert "mcpServers" not in claude and not (PLUGIN / "mcp.json").exists()


def test_marketplace_paths_resolve_to_same_plugin():
    claude = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    codex = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
    assert claude["plugins"][0]["source"] == "./plugins/job-pipeline"
    assert codex["plugins"][0]["source"] == {"source": "local", "path": "./plugins/job-pipeline"}


def test_runtime_starts_blank_and_disabled():
    runtime = json.loads((PLUGIN / "runtime-template" / "config" / "runtime.example.json").read_text())
    assert runtime["submission_mode"] == "review_only"
    assert runtime["onboarding"] == {"complete": False, "creator_ready": False}
    assert runtime["auto_apply_approved_at"] is None
    assert not list(PLUGIN.rglob("pipeline.db*"))
    assert not list(PLUGIN.rglob("pipeline_state.json"))


def test_release_validator_rejects_private_data_and_unlisted_files(tmp_path):
    release = load_release()
    (tmp_path / "README.md").write_text("Synthetic public code", encoding="utf-8")
    (tmp_path / "release-files.json").write_text(json.dumps(["README.md", "release-files.json"]), encoding="utf-8")
    assert release.validate_release(tmp_path) == []
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "candidate.yml").write_text("full_name: Synthetic User", encoding="utf-8")
    assert release.validate_release(tmp_path)


def test_packaged_markdown_links_and_shared_workspace_guide_resolve():
    for path in (PLUGIN / "skills").glob("*/SKILL.md"):
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\nname: " + path.parent.name + "\n")
        assert "description: " in text.split("---", 2)[1]
    for path in [ROOT / "README.md", *PLUGIN.rglob("*.md")]:
        if "Pipeline" in path.parts and "tests" in path.parts:
            continue
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if ":" in target or target.startswith("#"):
                continue
            assert (path.parent / target.split("#")[0]).exists(), (path, target)
    assert (PLUGIN / "references/workflows.md").read_bytes() == (PLUGIN / "runtime-template/WORKFLOWS.md").read_bytes()


def test_release_rejects_populated_preferences_example(tmp_path):
    release = load_release()
    path = tmp_path / "job-preferences.example.yml"
    path.write_text("schema_version: 1\ntarget_titles: [Synthetic Analyst]\n", encoding="utf-8")
    (tmp_path / "release-files.json").write_text(json.dumps([path.name, "release-files.json"]))
    assert any("populated" in item for item in release.validate_release(tmp_path))


def test_release_archives_have_verified_content_manifest(tmp_path):
    import hashlib
    from zipfile import ZipFile

    release = load_release()
    source = tmp_path / "source"
    plugin = source / "plugins/job-pipeline"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text('{"name":"job-pipeline","version":"0.1.0"}')
    (source / "release-files.json").write_text(json.dumps(["plugins/job-pipeline/plugin.json", "release-files.json"]))
    results = release.build_release(source, tmp_path / "release")
    assert len(results) == 3
    for path in results[:2]:
        with ZipFile(path) as archive:
            names = archive.namelist()
            assert len(names) == len(set(names))
            for line in archive.read("MANIFEST.sha256").decode().splitlines():
                digest, name = line.split("  ", 1)
                assert hashlib.sha256(archive.read(name)).hexdigest() == digest


def test_inventory_cannot_allowlist_ignored_private_runtime(tmp_path):
    release = load_release()
    (tmp_path / ".work").mkdir()
    (tmp_path / ".work/candidate.yml").write_text("Synthetic private facts")
    (tmp_path / "release-files.json").write_text(json.dumps([".work/candidate.yml", "release-files.json"]))
    assert release.validate_release(tmp_path)


def test_git_index_rejects_force_staged_private_paths_and_stale_public_bytes(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("git"):
        import pytest
        pytest.skip("Git unavailable")
    release = load_release()
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "README.md").write_text("Reviewed public text")
    (tmp_path / "release-files.json").write_text(json.dumps(["README.md", "release-files.json"]))
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md", "release-files.json"], check=True)
    assert release.validate_git_index(tmp_path) == []
    (tmp_path / "README.md").write_text("Changed public text")
    assert any("differs" in finding for finding in release.validate_git_index(tmp_path))
    (tmp_path / ".work").mkdir()
    (tmp_path / ".work/candidate.yml").write_text("Synthetic private facts")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--force", ".work/candidate.yml"], check=True)
    assert any("unexpected" in finding for finding in release.validate_git_index(tmp_path))
