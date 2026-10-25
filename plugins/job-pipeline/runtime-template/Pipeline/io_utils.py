from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class ConfinementError(OSError):
    """A project path would traverse a symlink, reparse point, or escape root."""


def is_symlink_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def path_without_link_ancestors(path: Path) -> Path:
    """Return lexical absolute path after checking each existing component."""
    candidate = Path(os.path.abspath(os.fspath(path)))
    cursor = Path(candidate.anchor)
    if is_symlink_or_reparse(cursor):
        raise ConfinementError("confinement: link-like path component")
    for part in candidate.parts[1:]:
        cursor = cursor / part
        if is_symlink_or_reparse(cursor):
            raise ConfinementError("confinement: link-like path component")
        if not cursor.exists():
            break
    return candidate


def confined_path(project_root: Path, path: Path) -> Path:
    """Return a lexical child of root after checking each existing component."""
    root = Path(os.path.abspath(os.fspath(project_root)))
    candidate = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise ConfinementError("confinement: path escapes project root") from error
    if is_symlink_or_reparse(root):
        raise ConfinementError("confinement: project root is link-like")
    canonical_root = root.resolve(strict=True)
    cursor = root
    for part in relative.parts:
        if is_symlink_or_reparse(cursor):
            raise ConfinementError("confinement: link-like path component")
        if cursor.exists() and not cursor.resolve(strict=True).is_relative_to(canonical_root):
            raise ConfinementError("confinement: component escapes project root")
        cursor = cursor / part
    if is_symlink_or_reparse(cursor):
        raise ConfinementError("confinement: link-like path component")
    if cursor.exists() and not cursor.resolve(strict=True).is_relative_to(canonical_root):
        raise ConfinementError("confinement: component escapes project root")
    return candidate


def confined_mkdir(project_root: Path, path: Path) -> Path:
    target = confined_path(project_root, path)
    target.mkdir(parents=True, exist_ok=True)
    return confined_path(project_root, target)


def _temporary_path(path: Path, project_root: Path | None) -> Path:
    if project_root is not None:
        confined_mkdir(project_root, path.parent)
        confined_path(project_root, path.parent)
    else:
        path = path_without_link_ancestors(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path_without_link_ancestors(path.parent)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    if project_root is not None:
        confined_path(project_root, temporary)
    return temporary


def atomic_write_bytes(path: Path, payload: bytes, project_root: Path | None = None) -> None:
    if project_root is not None:
        path = confined_path(project_root, path)
    else:
        path = path_without_link_ancestors(path)
    temporary = _temporary_path(path, project_root)
    try:
        temporary.write_bytes(payload)
        if project_root is not None:
            confined_path(project_root, path)
            confined_path(project_root, temporary)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, text: str, project_root: Path | None = None) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), project_root)


def atomic_write_json(path: Path, value: Any, project_root: Path | None = None) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", project_root)


def publish_bytes_no_overwrite(project_root: Path, path: Path, payload: bytes) -> None:
    target = confined_path(project_root, path)
    if target.exists() or is_symlink_or_reparse(target):
        raise FileExistsError("destination already exists")
    temporary = _temporary_path(target, project_root)
    try:
        temporary.write_bytes(payload)
        confined_path(project_root, target)
        confined_path(project_root, temporary)
        os.link(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def exclusive_project_lock(project_root: Path, name: str = ".onboarding.lock") -> Iterator[None]:
    root = confined_path(project_root, project_root)
    lock = confined_path(root, root / "private" / name)
    confined_mkdir(root, lock.parent)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise ConfinementError("confinement: onboarding lock is already held") from error
    try:
        os.close(descriptor)
        yield
    finally:
        try:
            confined_path(root, lock)
            lock.unlink()
        except FileNotFoundError:
            pass
