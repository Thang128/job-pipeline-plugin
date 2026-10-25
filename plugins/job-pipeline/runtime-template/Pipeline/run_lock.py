from __future__ import annotations

import argparse
import ctypes
import json
import os
import secrets
import stat
import sys
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Pipeline.io_utils import ConfinementError, is_symlink_or_reparse, path_without_link_ancestors


class RunLockError(ValueError):
    """A run lock cannot be handled without weakening its safety contract."""


class RunLockedError(RunLockError):
    """Another run owns the fixed project run lock."""


LOCK_RELATIVE_PATH = Path("private") / "run.lock"
LOCK_SCHEMA_VERSION = 1
MAX_LOCK_BYTES = 4096
STALE_AFTER = timedelta(hours=3)
RUN_OWNER_PURPOSES = frozenset({"scheduled-run", "manual-apply", "vie-apply", "verification"})
LOCK_KEYS = {"schema_version", "token", "owner", "pid", "created_at"}
WINDOWS_EXACT_HANDLE_DELETE = os.name == "nt"


if WINDOWS_EXACT_HANDLE_DELETE:
    import msvcrt
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    DELETE_ACCESS = 0x00010000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    CREATE_NEW = 1
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_DISPOSITION_INFO_EX_CLASS = 21
    FILE_DISPOSITION_FLAG_DELETE = 0x00000001
    FILE_DISPOSITION_FLAG_POSIX_SEMANTICS = 0x00000002
    ERROR_FILE_NOT_FOUND = 2
    ERROR_PATH_NOT_FOUND = 3
    ERROR_FILE_EXISTS = 80
    ERROR_ALREADY_EXISTS = 183
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    MAX_FINAL_PATH_CHARS = 32768

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _FileDispositionInformationEx(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL
    _GetFileInformationByHandle = _kernel32.GetFileInformationByHandle
    _GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)]
    _GetFileInformationByHandle.restype = wintypes.BOOL
    _GetFinalPathNameByHandleW = _kernel32.GetFinalPathNameByHandleW
    _GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _SetFileInformationByHandle = _kernel32.SetFileInformationByHandle
    _SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _SetFileInformationByHandle.restype = wintypes.BOOL


class _NativeOpenError(OSError):
    def __init__(self, code: int):
        super().__init__(code, "native run lock open failed")
        self.code = code


@dataclass(frozen=True)
class _LockTarget:
    root: Path
    private: Path
    path: Path
    canonical_root: Path
    root_identity: tuple[int, int]
    private_identity: tuple[int, int]
    normalized_path: str


@dataclass(frozen=True)
class _HandleSnapshot:
    identity: tuple[int, int]
    size: int
    links: int
    attributes: int
    final_path: str


@dataclass
class _HeldLock:
    target: _LockTarget
    descriptor: int
    identity: tuple[int, int]
    record: dict[str, Any]


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _normalize_path(path: str | Path) -> str:
    value = os.fspath(path)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.normpath(os.path.abspath(value)))


def _extended_windows_path(path: Path) -> str:
    value = str(path)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _safe_stat_identity(path: Path, *, directory: bool) -> tuple[int, int]:
    metadata = path.lstat()
    expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not expected or is_symlink_or_reparse(path):
        raise RunLockError("unsafe run lock path")
    return _file_identity(metadata)


def _capture_target(project_root: Path) -> _LockTarget:
    try:
        root = path_without_link_ancestors(Path(os.path.abspath(os.fspath(project_root))))
        if not root.is_dir() or is_symlink_or_reparse(root):
            raise RunLockError("unsafe run lock path")
        canonical_root = root.resolve(strict=True)
        if root != canonical_root:
            raise RunLockError("unsafe run lock path")
        root_identity = _safe_stat_identity(root, directory=True)
        private = path_without_link_ancestors(root / "private")
        if private.parent != root or not private.is_dir():
            raise RunLockError("unsafe run lock path")
        private_identity = _safe_stat_identity(private, directory=True)
        path = Path(os.path.abspath(os.fspath(private / "run.lock")))
        if path.parent != private or path_without_link_ancestors(path) != path:
            raise RunLockError("unsafe run lock path")
        return _LockTarget(
            root=root,
            private=private,
            path=path,
            canonical_root=canonical_root,
            root_identity=root_identity,
            private_identity=private_identity,
            normalized_path=_normalize_path(path),
        )
    except RunLockError:
        raise
    except (ConfinementError, OSError, ValueError) as error:
        raise RunLockError("unsafe run lock path") from error


def _revalidate_target(target: _LockTarget) -> None:
    try:
        root = path_without_link_ancestors(target.root)
        private = path_without_link_ancestors(target.private)
        path = path_without_link_ancestors(target.path)
        if (
            root != target.root
            or private != target.private
            or path != target.path
            or root.resolve(strict=True) != target.canonical_root
            or _safe_stat_identity(root, directory=True) != target.root_identity
            or _safe_stat_identity(private, directory=True) != target.private_identity
        ):
            raise RunLockError("unsafe run lock path")
    except RunLockError:
        raise
    except (ConfinementError, OSError, ValueError) as error:
        raise RunLockError("unsafe run lock path") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RunLockError("unsafe run lock requires user review")
        result[key] = value
    return result


def _parse_created_at(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
        raise RunLockError("unsafe run lock requires user review")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise RunLockError("unsafe run lock requires user review") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise RunLockError("unsafe run lock requires user review")
    return parsed.astimezone(timezone.utc)


def _validate_record(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != LOCK_KEYS:
        raise RunLockError("unsafe run lock requires user review")
    if type(value.get("schema_version")) is not int or value["schema_version"] != LOCK_SCHEMA_VERSION:
        raise RunLockError("unsafe run lock requires user review")
    token = value.get("token")
    owner = value.get("owner")
    pid = value.get("pid")
    if not isinstance(token, str) or len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
        raise RunLockError("unsafe run lock requires user review")
    if not isinstance(owner, str) or owner not in RUN_OWNER_PURPOSES:
        raise RunLockError("unsafe run lock requires user review")
    if type(pid) is not int or not 1 <= pid <= 2**63 - 1:
        raise RunLockError("unsafe run lock requires user review")
    _parse_created_at(value.get("created_at"))
    return dict(value)


def _encoded_record(owner: str) -> tuple[dict[str, Any], bytes]:
    if not isinstance(owner, str) or owner not in RUN_OWNER_PURPOSES:
        raise RunLockError("owner must be an approved generic run purpose")
    record = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "token": secrets.token_hex(32),
        "owner": owner,
        "pid": os.getpid(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }
    _validate_record(record)
    payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > MAX_LOCK_BYTES:
        raise RunLockError("run lock record is too large")
    return record, payload


def _require_exact_backend() -> None:
    if not WINDOWS_EXACT_HANDLE_DELETE:
        raise RunLockError("exact-handle run lock is unavailable on this platform")


def _handle_value(handle: Any) -> int:
    if isinstance(handle, int):
        return handle
    value = ctypes.cast(handle, ctypes.c_void_p).value
    if value is None:
        raise OSError("native run lock handle is invalid")
    return int(value)


def _open_native_descriptor(target: _LockTarget, *, create: bool, share_existing_writer: bool = False) -> int:
    _require_exact_backend()
    desired = GENERIC_READ | DELETE_ACCESS | (GENERIC_WRITE if create else 0)
    share = FILE_SHARE_READ | FILE_SHARE_DELETE
    if not create and share_existing_writer:
        share |= FILE_SHARE_WRITE
    disposition = CREATE_NEW if create else OPEN_EXISTING
    handle = _CreateFileW(
        _extended_windows_path(target.path),
        desired,
        share,
        None,
        disposition,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = _handle_value(handle)
    if value == INVALID_HANDLE_VALUE:
        raise _NativeOpenError(ctypes.get_last_error())
    flags = (os.O_RDWR if create else os.O_RDONLY) | int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_NOINHERIT", 0))
    try:
        return msvcrt.open_osfhandle(value, flags)
    except OSError:
        _CloseHandle(handle)
        raise


def _native_handle(descriptor: int) -> int:
    _require_exact_backend()
    value = msvcrt.get_osfhandle(descriptor)
    if value == -1:
        raise OSError("native run lock handle is invalid")
    return int(value)


def _final_handle_path(handle: int) -> str:
    buffer = ctypes.create_unicode_buffer(MAX_FINAL_PATH_CHARS)
    length = _GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if length == 0 or length >= len(buffer):
        raise OSError(ctypes.get_last_error(), "native run lock path query failed")
    return _normalize_path(buffer.value)


def _handle_snapshot(descriptor: int) -> _HandleSnapshot:
    handle = _native_handle(descriptor)
    information = _ByHandleFileInformation()
    if not _GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise OSError(ctypes.get_last_error(), "native run lock identity query failed")
    size = (int(information.nFileSizeHigh) << 32) | int(information.nFileSizeLow)
    identity = (
        int(information.dwVolumeSerialNumber),
        (int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow),
    )
    return _HandleSnapshot(
        identity=identity,
        size=size,
        links=int(information.nNumberOfLinks),
        attributes=int(information.dwFileAttributes),
        final_path=_final_handle_path(handle),
    )


def _validate_handle_location(target: _LockTarget, descriptor: int, expected_identity: tuple[int, int] | None = None) -> _HandleSnapshot:
    _revalidate_target(target)
    snapshot = _handle_snapshot(descriptor)
    descriptor_stat = os.fstat(descriptor)
    path_stat = target.path.lstat()
    if (
        snapshot.final_path != target.normalized_path
        or snapshot.links != 1
        or snapshot.attributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)
        or expected_identity is not None and snapshot.identity != expected_identity
        or not stat.S_ISREG(descriptor_stat.st_mode)
        or int(descriptor_stat.st_nlink) != 1
        or not stat.S_ISREG(path_stat.st_mode)
        or int(path_stat.st_nlink) != 1
        or is_symlink_or_reparse(target.path)
        or _file_identity(descriptor_stat) != _file_identity(path_stat)
        or int(descriptor_stat.st_size) != snapshot.size
        or int(path_stat.st_size) != snapshot.size
    ):
        raise RunLockError("unsafe run lock requires user review")
    _revalidate_target(target)
    return snapshot


def _read_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024, MAX_LOCK_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_LOCK_BYTES:
            raise RunLockError("unsafe run lock requires user review")
    return b"".join(chunks)


def _parse_payload(payload: bytes) -> dict[str, Any]:
    if not 1 <= len(payload) <= MAX_LOCK_BYTES:
        raise RunLockError("unsafe run lock requires user review")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except RunLockError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunLockError("unsafe run lock requires user review") from error
    return _validate_record(value)


def _inspect_held(held: _HeldLock) -> dict[str, Any]:
    before = _validate_handle_location(held.target, held.descriptor, held.identity)
    payload = _read_descriptor(held.descriptor)
    after = _validate_handle_location(held.target, held.descriptor, held.identity)
    record = _parse_payload(payload)
    if before != after or before.size != len(payload) or record != held.record:
        raise RunLockError("unsafe run lock requires user review")
    return record


def _write_all(descriptor: int, payload: bytes) -> None:
    position = 0
    while position < len(payload):
        written = os.write(descriptor, payload[position:])
        if written <= 0:
            raise OSError("run lock write failed")
        position += written
    os.fsync(descriptor)


def _set_delete_disposition_exact(descriptor: int) -> None:
    _require_exact_backend()
    disposition = _FileDispositionInformationEx(
        FILE_DISPOSITION_FLAG_DELETE | FILE_DISPOSITION_FLAG_POSIX_SEMANTICS
    )
    if not _SetFileInformationByHandle(
        _native_handle(descriptor),
        FILE_DISPOSITION_INFO_EX_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise OSError(ctypes.get_last_error(), "native exact run lock cleanup failed")


def _close_descriptor(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError as error:
        raise RunLockError("exact run lock cleanup failed") from error


def _close_marked_descriptor(descriptor: int) -> None:
    """Close only after exact-handle disposition was set; no path operation follows."""
    _close_descriptor(descriptor)


def _open_existing(target: _LockTarget, *, share_existing_writer: bool = False) -> _HeldLock:
    _revalidate_target(target)
    try:
        descriptor = _open_native_descriptor(
            target,
            create=False,
            share_existing_writer=share_existing_writer,
        )
    except _NativeOpenError as error:
        if error.code in {ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND}:
            raise RunLockError("run lock changed during review") from error
        raise RunLockError("unsafe run lock requires user review") from error
    try:
        snapshot = _validate_handle_location(target, descriptor)
        payload = _read_descriptor(descriptor)
        repeated = _validate_handle_location(target, descriptor, snapshot.identity)
        record = _parse_payload(payload)
        if snapshot != repeated or snapshot.size != len(payload):
            raise RunLockError("unsafe run lock requires user review")
        return _HeldLock(target=target, descriptor=descriptor, identity=snapshot.identity, record=record)
    except Exception:
        try:
            _close_descriptor(descriptor)
        except RunLockError:
            pass
        raise


def _is_stale(record: dict[str, Any], *, now: datetime | None = None) -> bool:
    reference = now or datetime.now(timezone.utc)
    return reference - _parse_created_at(record["created_at"]) > STALE_AFTER


def _close_without_delete(held: _HeldLock) -> None:
    _close_descriptor(held.descriptor)


def _classify_contention(target: _LockTarget) -> None:
    try:
        held = _open_existing(target, share_existing_writer=True)
    except RunLockError as error:
        raise RunLockedError(str(error)) from error
    try:
        record = _inspect_held(held)
        stale = _is_stale(record)
    finally:
        _close_without_delete(held)
    if stale:
        raise RunLockedError("stale run lock requires user review")
    raise RunLockedError("another run is active")


def _delete_created_after_setup_failure(target: _LockTarget, descriptor: int) -> None:
    error: Exception | None = None
    marked = False
    try:
        _validate_handle_location(target, descriptor)
        _set_delete_disposition_exact(descriptor)
        marked = True
    except Exception as cleanup_error:
        error = cleanup_error
    try:
        if marked:
            _close_marked_descriptor(descriptor)
        else:
            _close_descriptor(descriptor)
    except RunLockError as close_error:
        error = error or close_error
    if error is not None:
        raise RunLockError("run lock acquisition failed and exact cleanup failed") from error


def _create_held(project_root: Path, owner: str) -> _HeldLock:
    _require_exact_backend()
    record, payload = _encoded_record(owner)
    target = _capture_target(project_root)
    _revalidate_target(target)
    try:
        descriptor = _open_native_descriptor(target, create=True)
    except _NativeOpenError as error:
        if error.code in {ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS}:
            _classify_contention(target)
        raise RunLockError("unsafe run lock path") from error
    try:
        _write_all(descriptor, payload)
        snapshot = _validate_handle_location(target, descriptor)
        held = _HeldLock(target=target, descriptor=descriptor, identity=snapshot.identity, record=record)
        _inspect_held(held)
        _revalidate_target(target)
        return held
    except Exception as error:
        try:
            _delete_created_after_setup_failure(target, descriptor)
        except RunLockError as cleanup_error:
            raise cleanup_error from error
        if isinstance(error, RunLockError):
            raise
        raise RunLockError("run lock acquisition failed") from error


def _cleanup_owned(held: _HeldLock) -> None:
    error: Exception | None = None
    marked = False
    try:
        _inspect_held(held)
        _set_delete_disposition_exact(held.descriptor)
        marked = True
    except Exception as cleanup_error:
        error = cleanup_error
    try:
        if marked:
            _close_marked_descriptor(held.descriptor)
        else:
            _close_descriptor(held.descriptor)
    except RunLockError as close_error:
        error = error or close_error
    if error is not None:
        raise RunLockError("exact run lock cleanup failed") from error


class _RunLockContext(AbstractContextManager[dict[str, Any]]):
    def __init__(self, project_root: Path, owner: str):
        self._project_root = Path(project_root)
        self._owner = owner
        self._held: _HeldLock | None = None

    def __enter__(self) -> dict[str, Any]:
        if self._held is not None:
            raise RunLockError("run lock context cannot be reused")
        self._held = _create_held(self._project_root, self._owner)
        return dict(self._held.record)

    def __exit__(self, exception_type: object, exception: BaseException | None, traceback: object) -> bool:
        del exception_type, traceback
        held, self._held = self._held, None
        if held is None:
            return False
        try:
            _cleanup_owned(held)
        except RunLockError:
            if exception is not None:
                exception.add_note("Run lock cleanup failed; lock retained for explicit user review.")
                return False
            raise
        return False


def acquire_run_lock(project_root: Path, owner: str) -> AbstractContextManager[dict[str, Any]]:
    """Acquire fixed private run lock; concurrent or stale locks fail closed."""
    return _RunLockContext(project_root, owner)


def clear_stale_run_lock(project_root: Path, *, confirm_user_approval: bool) -> bool:
    """Delete one exact unchanged stale handle after explicit current approval."""
    if confirm_user_approval is not True:
        raise RunLockError("explicit user approval is required to clear a stale run lock")
    _require_exact_backend()
    target = _capture_target(project_root)
    held = _open_existing(target)
    error: Exception | None = None
    marked = False
    try:
        first = _inspect_held(held)
        if not _is_stale(first):
            raise RunLockError("run lock is not stale")
        second = _inspect_held(held)
        if second != first or not _is_stale(second):
            raise RunLockError("run lock changed during stale review")
        _set_delete_disposition_exact(held.descriptor)
        marked = True
    except Exception as operation_error:
        error = operation_error
    try:
        if marked:
            _close_marked_descriptor(held.descriptor)
        else:
            _close_descriptor(held.descriptor)
    except RunLockError as close_error:
        error = error or close_error
    if error is not None:
        if marked:
            raise RunLockError("exact run lock cleanup failed") from error
        if isinstance(error, RunLockError):
            raise error
        raise RunLockError("exact run lock cleanup failed") from error
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the local single-run lock.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest="command", required=True)
    clear = commands.add_parser("clear-stale")
    clear.add_argument("--confirm-user-approval", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        cleared = clear_stale_run_lock(
            arguments.root,
            confirm_user_approval=arguments.confirm_user_approval,
        )
        print(json.dumps({"cleared": cleared, "ok": True}, sort_keys=True, separators=(",", ":")))
        return 0
    except (RunLockError, OSError, ValueError):
        print(json.dumps({"error": "run lock operation failed", "ok": False}, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
