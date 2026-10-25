from __future__ import annotations

import inspect
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event

import pytest

import Pipeline.run_lock as run_lock_module
from Pipeline.initialize import initialize_project
from Pipeline.run_lock import RunLockError, RunLockedError, acquire_run_lock, clear_stale_run_lock, main


def _stamp(age: timedelta) -> str:
    return (datetime.now(timezone.utc) - age).isoformat().replace("+00:00", "Z")


def _token(character: str) -> str:
    return character * 64


def _write_lock(
    path: Path,
    *,
    token: str | None = None,
    owner: str = "verification",
    age: timedelta = timedelta(hours=4),
) -> bytes:
    payload = {
        "schema_version": 1,
        "token": token or _token("a"),
        "owner": owner,
        "pid": 123,
        "created_at": _stamp(age),
    }
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    path.write_bytes(encoded)
    return encoded


def test_run_lock_blocks_parallel_owner_without_mutating_lock(project_root: Path) -> None:
    initialize_project(project_root)
    lock = project_root / "private" / "run.lock"
    with acquire_run_lock(project_root, owner="scheduled-run") as record:
        with pytest.raises(RunLockedError, match="another run is active"):
            with acquire_run_lock(project_root, owner="manual-apply"):
                pass
        assert record["owner"] == "scheduled-run"
        assert record["pid"] == os.getpid()
        assert set(record) == {"schema_version", "token", "owner", "pid", "created_at"}
    assert not lock.exists()


def test_simultaneous_acquisition_has_one_winner_repeatedly(project_root: Path) -> None:
    initialize_project(project_root)

    for _ in range(12):
        barrier = Barrier(2)
        release = Event()

        def contender(owner: str) -> str:
            barrier.wait(timeout=5)
            try:
                with acquire_run_lock(project_root, owner=owner):
                    release.wait(timeout=5)
                    return "acquired"
            except RunLockedError:
                release.set()
                return "blocked"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(contender, ("scheduled-run", "manual-apply")))
        assert sorted(results) == ["acquired", "blocked"]
        assert not (project_root / "private" / "run.lock").exists()


def test_stale_lock_requires_explicit_user_clear(project_root: Path) -> None:
    initialize_project(project_root)
    lock = project_root / "private" / "run.lock"
    before = _write_lock(lock)
    with pytest.raises(RunLockedError, match="stale run lock requires user review"):
        with acquire_run_lock(project_root, owner="verification"):
            pass
    assert lock.read_bytes() == before
    with pytest.raises(RunLockError, match="explicit user approval"):
        clear_stale_run_lock(project_root, confirm_user_approval=False)
    assert lock.read_bytes() == before
    assert clear_stale_run_lock(project_root, confirm_user_approval=True) is True
    assert not lock.exists()


def test_fresh_or_malformed_lock_never_clears(project_root: Path) -> None:
    initialize_project(project_root)
    lock = project_root / "private" / "run.lock"
    fresh = _write_lock(lock, age=timedelta(minutes=5))
    with pytest.raises(RunLockError, match="not stale"):
        clear_stale_run_lock(project_root, confirm_user_approval=True)
    assert lock.read_bytes() == fresh
    lock.write_bytes(b"not-json\n")
    with pytest.raises(RunLockError, match="unsafe run lock requires user review"):
        clear_stale_run_lock(project_root, confirm_user_approval=True)
    assert lock.read_bytes() == b"not-json\n"


def test_context_exit_exact_handle_delete_preserves_boundary_replacement(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(project_root)
    lock = project_root / "private" / "run.lock"
    replacement = b""
    original = run_lock_module._close_marked_descriptor

    def close_then_replace(descriptor: int) -> None:
        nonlocal replacement
        original(descriptor)
        assert not lock.exists()
        replacement = _write_lock(lock, token=_token("b"), owner="manual-apply", age=timedelta(minutes=1))

    monkeypatch.setattr(run_lock_module, "_close_marked_descriptor", close_then_replace)
    with acquire_run_lock(project_root, owner="verification"):
        pass
    assert lock.read_bytes() == replacement


def test_clear_stale_exact_handle_delete_preserves_boundary_replacement(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(project_root)
    lock = project_root / "private" / "run.lock"
    _write_lock(lock)
    replacement = b""
    original = run_lock_module._close_marked_descriptor

    def close_then_replace(descriptor: int) -> None:
        nonlocal replacement
        original(descriptor)
        assert not lock.exists()
        replacement = _write_lock(lock, token=_token("c"), owner="scheduled-run", age=timedelta(minutes=1))

    monkeypatch.setattr(run_lock_module, "_close_marked_descriptor", close_then_replace)
    assert clear_stale_run_lock(project_root, confirm_user_approval=True) is True
    assert lock.read_bytes() == replacement


def test_stale_clear_denies_writer_between_token_check_and_exact_delete(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(project_root)
    lock = project_root / "private" / "run.lock"
    _write_lock(lock)
    original = run_lock_module._set_delete_disposition_exact
    writer_was_denied = False

    def probe_writer_then_mark(descriptor: int) -> None:
        nonlocal writer_was_denied
        writer = run_lock_module._CreateFileW(
            run_lock_module._extended_windows_path(lock),
            run_lock_module.GENERIC_WRITE,
            run_lock_module.FILE_SHARE_READ
            | run_lock_module.FILE_SHARE_WRITE
            | run_lock_module.FILE_SHARE_DELETE,
            None,
            run_lock_module.OPEN_EXISTING,
            run_lock_module.FILE_ATTRIBUTE_NORMAL | run_lock_module.FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        writer_value = run_lock_module._handle_value(writer)
        if writer_value != run_lock_module.INVALID_HANDLE_VALUE:
            run_lock_module._CloseHandle(writer)
        writer_was_denied = writer_value == run_lock_module.INVALID_HANDLE_VALUE
        assert writer_was_denied, "stale review must hold a no-write-share exact handle"
        original(descriptor)

    monkeypatch.setattr(run_lock_module, "_set_delete_disposition_exact", probe_writer_then_mark)
    assert clear_stale_run_lock(project_root, confirm_user_approval=True) is True
    assert writer_was_denied
    assert not lock.exists()


def test_exact_delete_api_failure_retains_lock_and_is_visible(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(project_root)
    lock = project_root / "private" / "run.lock"

    def fail_exact_delete(descriptor: int) -> None:
        del descriptor
        raise OSError("synthetic exact-delete failure")

    monkeypatch.setattr(run_lock_module, "_set_delete_disposition_exact", fail_exact_delete)
    with pytest.raises(RunLockError, match="exact run lock cleanup failed"):
        with acquire_run_lock(project_root, owner="verification"):
            pass
    assert lock.exists()
    assert json.loads(lock.read_text(encoding="utf-8"))["owner"] == "verification"


def test_body_error_stays_primary_and_notes_cleanup_failure(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(project_root)
    lock = project_root / "private" / "run.lock"

    def fail_exact_delete(descriptor: int) -> None:
        del descriptor
        raise OSError("synthetic exact-delete failure")

    class BodyFailure(RuntimeError):
        pass

    monkeypatch.setattr(run_lock_module, "_set_delete_disposition_exact", fail_exact_delete)
    with pytest.raises(BodyFailure, match="body failed") as captured:
        with acquire_run_lock(project_root, owner="verification"):
            raise BodyFailure("body failed")
    assert getattr(captured.value, "__notes__", []) == [
        "Run lock cleanup failed; lock retained for explicit user review."
    ]
    assert lock.exists()


def test_owner_is_fixed_generic_purpose_and_candidate_label_is_never_written(project_root: Path) -> None:
    initialize_project(project_root)
    lock = project_root / "private" / "run.lock"
    assert {"scheduled-run", "manual-apply", "vie-apply", "verification"} <= run_lock_module.RUN_OWNER_PURPOSES
    for owner in ("", "candidate-name", "has spaces", "../escape", "x" * 65, "line\nbreak", "UPPER"):
        with pytest.raises(RunLockError, match="owner"):
            with acquire_run_lock(project_root, owner=owner):
                pass
        assert not lock.exists()
    with acquire_run_lock(project_root, owner="verification") as record:
        assert "candidate-name" not in json.dumps(record)


def test_lock_symlink_is_rejected_without_mutating_target(project_root: Path, tmp_path: Path) -> None:
    initialize_project(project_root)
    lock = project_root / "private" / "run.lock"
    target = tmp_path / "target.lock"
    original = _write_lock(target)
    try:
        lock.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    with pytest.raises(RunLockError, match="unsafe run lock path"):
        with acquire_run_lock(project_root, owner="verification"):
            pass
    assert target.read_bytes() == original


def test_hardlink_lock_is_rejected_without_mutating_target(project_root: Path, tmp_path: Path) -> None:
    initialize_project(project_root)
    lock = project_root / "private" / "run.lock"
    target = tmp_path / "target.lock"
    original = _write_lock(target)
    try:
        os.link(target, lock)
    except OSError as error:
        pytest.skip(f"hardlink creation unavailable: {error}")
    with pytest.raises(RunLockedError, match="unsafe run lock requires user review"):
        with acquire_run_lock(project_root, owner="verification"):
            pass
    with pytest.raises(RunLockError, match="unsafe run lock requires user review"):
        clear_stale_run_lock(project_root, confirm_user_approval=True)
    assert target.read_bytes() == original
    assert lock.exists()


def test_private_directory_link_is_rejected(project_root: Path, tmp_path: Path) -> None:
    private = project_root / "private"
    external = tmp_path / "external"
    external.mkdir()
    try:
        private.symlink_to(external, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink creation unavailable: {error}")
    with pytest.raises(RunLockError, match="unsafe run lock path"):
        with acquire_run_lock(project_root, owner="scheduled-run"):
            pass
    assert not (external / "run.lock").exists()


def test_project_path_through_link_ancestor_is_rejected_without_unrelated_delete(
    project_root: Path,
    tmp_path: Path,
) -> None:
    initialize_project(project_root)
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("preserve", encoding="utf-8")
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(project_root.parent, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"ancestor symlink creation unavailable: {error}")
    linked_root = linked_parent / project_root.name
    with pytest.raises(RunLockError, match="unsafe run lock path"):
        with acquire_run_lock(linked_root, owner="verification"):
            pass
    assert unrelated.read_text(encoding="utf-8") == "preserve"
    assert not (project_root / "private" / "run.lock").exists()


def test_cli_clear_failure_is_nonzero_generic_json_without_token_or_path(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    initialize_project(project_root)
    lock = project_root / "private" / "run.lock"
    _write_lock(lock, token=_token("d"))
    assert main(["--root", str(project_root), "clear-stale"]) == 2
    first = capsys.readouterr()
    assert json.loads(first.err) == {"error": "run lock operation failed", "ok": False}
    assert _token("d") not in first.err and str(project_root) not in first.err

    def fail_exact_delete(descriptor: int) -> None:
        del descriptor
        raise OSError("synthetic exact-delete failure")

    monkeypatch.setattr(run_lock_module, "_set_delete_disposition_exact", fail_exact_delete)
    assert main(["--root", str(project_root), "clear-stale", "--confirm-user-approval"]) == 2
    second = capsys.readouterr()
    assert json.loads(second.err) == {"error": "run lock operation failed", "ok": False}
    assert _token("d") not in second.err and str(project_root) not in second.err
    assert lock.exists()


def test_cli_clear_success_uses_generic_json(project_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    initialize_project(project_root)
    lock = project_root / "private" / "run.lock"
    _write_lock(lock)
    assert main(["--root", str(project_root), "clear-stale", "--confirm-user-approval"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"cleared": True, "ok": True}
    assert not lock.exists()


def test_no_path_unlink_fallback_exists() -> None:
    source = inspect.getsource(run_lock_module)
    assert ".unlink(" not in source
    assert "os.unlink" not in source
    assert "os.remove" not in source


def test_non_windows_backend_fails_before_creating_lock(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(project_root)
    monkeypatch.setattr(run_lock_module, "WINDOWS_EXACT_HANDLE_DELETE", False)
    with pytest.raises(RunLockError, match="exact-handle run lock is unavailable"):
        with acquire_run_lock(project_root, owner="verification"):
            pass
    assert not (project_root / "private" / "run.lock").exists()
