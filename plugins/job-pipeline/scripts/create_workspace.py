"""Copy verified, public runtime files into a new user-selected workspace."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _plain_ancestors(path: Path) -> None:
    for current in (path, *path.parents):
        if not current.exists() and not current.is_symlink():
            continue
        metadata = current.lstat()
        if current.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("workspace path contains a link or reparse point")


def create_workspace(destination: Path) -> Path:
    destination = Path(os.path.abspath(destination))
    _plain_ancestors(destination)
    if destination.exists():
        raise ValueError("workspace destination already exists; select a new directory")
    if destination.is_relative_to(PLUGIN_ROOT):
        raise ValueError("workspace must be outside installed plugin")
    for parent in destination.parents:
        if (parent / ".agents/plugins/marketplace.json").exists() or (parent / ".claude-plugin/marketplace.json").exists():
            raise ValueError("workspace must be outside marketplace checkout")
        if parent.name.casefold() in {".codex", ".claude"}:
            raise ValueError("workspace must be outside host configuration and cache")
    template = PLUGIN_ROOT / "runtime-template"
    manifest_path = PLUGIN_ROOT / "template-manifest.json"
    _plain_ancestors(template)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError("runtime manifest is invalid")
    verified = []
    for name, digest in sorted(manifest.items()):
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
            raise ValueError("runtime manifest path is unsafe")
        source = template.joinpath(*relative.parts)
        _plain_ancestors(source)
        if not source.is_file() or source.stat().st_nlink != 1:
            raise ValueError("runtime template contains an unsafe file")
        payload = source.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("runtime template checksum mismatch")
        if any(part.casefold() in {"private", "generated", ".venv"} for part in relative.parts) or source.name in {"pipeline_state.json", "accounts.json"} or source.suffix.casefold() in {".db", ".pdf", ".docx"}:
            raise ValueError("private artifact found in runtime manifest")
        verified.append((relative, payload))
    destination.parent.mkdir(parents=True, exist_ok=True)
    _plain_ancestors(destination.parent)
    destination.mkdir()
    for relative, payload in verified:
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        _plain_ancestors(target.parent)
        with target.open("xb") as handle:
            handle.write(payload)
    with (destination / "WORKSPACE_MANIFEST.sha256").open("x", encoding="utf-8") as handle:
        handle.write("\n".join(f"{digest}  {name}" for name, digest in sorted(manifest.items())) + "\n")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    try:
        destination = create_workspace(args.destination)
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error) if isinstance(error, ValueError) else "workspace copy failed"}))
        return 1
    print(json.dumps({"ok": True, "workspace": str(destination), "initialized": False, "next": "Read README.md and run Install.ps1 from the new workspace."}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
