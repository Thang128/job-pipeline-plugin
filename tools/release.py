"""Validate an explicit public inventory and build data-free release archives."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

import yaml

INVENTORY = "release-files.json"
IGNORED = {".git", "__pycache__", ".pytest_cache", ".venv", ".work", ".superpowers"}
PRIVATE_PARTS = {"private", "generated", "backups", "browser-data", "node_modules"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pdf", ".docx", ".key", ".pem", ".log", ".zip", ".plugin"}
SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
MACHINE_PATH = re.compile(r"(?i)[A-Z]:[\\/]+Users[\\/]+[^\\/\s]+")
EMAIL = re.compile(r"[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})")
BLANK_CANDIDATE = {"schema_version": 1, "identity": {"full_name": "", "email": "", "phone": "", "residence_country": ""}, "current_employer": "", "employment_status": "", "work_authorization": [], "languages": [], "notice_period": "", "known_answers": {}, "career_evidence": []}
BLANK_PREFERENCES = {"schema_version": 1, "target_titles": [], "role_families": [], "industries": [], "seniority": {"minimum": "", "maximum": ""}, "countries": [], "work_modes": [], "salary": {"minimum": None, "currency": ""}, "include_keywords": [], "exclude_keywords": [], "excluded_employers": [], "preferred_company_types": [], "vie": {"enabled": False, "eligible": False}}
BLANK_RUNTIME = {"schema_version": 1, "submission_mode": "review_only", "browser_profile": "", "master_cv_path": "", "master_cv_sha256": "", "onboarding": {"complete": False, "creator_ready": False}, "auto_apply_approved_at": None}
BLANK_ACCOUNTS = {"schema_version": 1, "identity_providers": [], "employer_tenants": [], "no_account_platforms": []}


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate object key")
        value[key] = item
    return value


class _UniqueYaml(yaml.SafeLoader):
    pass


def _yaml_mapping(loader, node):
    return _unique_pairs([(loader.construct_object(key), loader.construct_object(value)) for key, value in node.value])


_UniqueYaml.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_mapping)


def _safe_file(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if not relative or pure.is_absolute() or ".." in pure.parts or "\\" in relative or ":" in relative:
        raise ValueError("unsafe inventory path")
    current = root
    for part in pure.parts:
        current = current / part
        metadata = current.lstat()
        if current.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked release path")
    if not current.is_file() or current.stat().st_nlink != 1:
        raise ValueError("release entry is not a plain single-link file")
    return current


def release_files(root: Path) -> list[Path]:
    root = root.absolute()
    for ancestor in (root, *root.parents):
        if ancestor.is_symlink() or getattr(ancestor.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("linked release root")
    inventory = json.loads(_safe_file(root, INVENTORY).read_text(encoding="utf-8"))
    if not isinstance(inventory, list) or not inventory or inventory != sorted(set(inventory)):
        raise ValueError("inventory must be a sorted unique file list")
    for name in inventory:
        if set(part.casefold() for part in PurePosixPath(name).parts) & (IGNORED | PRIVATE_PARTS):
            raise ValueError("private or runtime path in inventory")
    return [_safe_file(root, name) for name in inventory]


def validate_git_index(root: Path) -> list[str]:
    root = root.absolute()
    try:
        files = release_files(root)
        expected = {p.relative_to(root).as_posix(): p for p in files}
        result = subprocess.run(["git", "-C", str(root), "ls-files", "--stage", "-z"], check=True, capture_output=True)
        findings = []
        seen = set()
        for entry in result.stdout.split(b"\0"):
            if not entry:
                continue
            metadata, raw_name = entry.split(b"\t", 1)
            mode, object_id, stage = metadata.split()
            name = raw_name.decode("utf-8")
            seen.add(name)
            if name not in expected:
                findings.append("unexpected staged path: " + name)
                continue
            if stage != b"0" or mode not in {b"100644", b"100755"}:
                findings.append("unsafe staged mode: " + name)
                continue
            content = subprocess.run(["git", "-C", str(root), "cat-file", "blob", object_id.decode("ascii")], check=True, capture_output=True).stdout
            if content != expected[name].read_bytes():
                findings.append("staged content differs from reviewed source: " + name)
        for missing in expected.keys() - seen:
            findings.append("public file is not staged or tracked: " + missing)
        return sorted(set(findings))
    except (OSError, ValueError, subprocess.CalledProcessError):
        return ["Git index validation failed"]


def validate_release(root: Path) -> list[str]:
    root = root.absolute()
    try:
        files = release_files(root)
    except (OSError, ValueError, TypeError):
        return ["invalid release inventory"]
    expected = {p.relative_to(root).as_posix() for p in files}
    findings = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if set(relative.parts) & IGNORED:
            continue
        metadata = path.lstat()
        if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            findings.append("linked path: " + relative.as_posix())
        elif path.is_file() and relative.as_posix() not in expected:
            findings.append("unlisted file: " + relative.as_posix())
    for path in files:
        relative = path.relative_to(root)
        if set(p.casefold() for p in relative.parts) & PRIVATE_PARTS or path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            findings.append("private artifact: " + relative.as_posix())
            continue
        if path.name.casefold() in {"accounts.json", "pipeline_state.json", ".env"}:
            findings.append("private runtime file: " + relative.as_posix())
        if path.stat().st_size > 4 * 1024 * 1024:
            findings.append("oversized public file: " + relative.as_posix())
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            findings.append("unexpected binary: " + relative.as_posix())
            continue
        if SECRET.search(content) or MACHINE_PATH.search(content):
            findings.append("sensitive content: " + relative.as_posix())
        for domain in EMAIL.findall(content):
            if not (domain.endswith((".test", ".invalid", ".example", ".github.com")) or domain in {"example.com", "example.org", "example.net"}):
                findings.append("non-example contact: " + relative.as_posix())
                break
        expected = {"candidate.example.yml": BLANK_CANDIDATE, "job-preferences.example.yml": BLANK_PREFERENCES, "runtime.example.json": BLANK_RUNTIME, "accounts.example.json": BLANK_ACCOUNTS}.get(path.name)
        if expected is not None:
            try:
                value = yaml.load(content, Loader=_UniqueYaml) if path.suffix == ".yml" else json.loads(content, object_pairs_hook=_unique_pairs)
                if value != expected:
                    findings.append("populated or unsafe example: " + relative.as_posix())
            except (ValueError, TypeError, yaml.YAMLError):
                findings.append("invalid example: " + relative.as_posix())
        if path.name == "master-profile.example.md":
            headings = ["# Identity", "# Experience", "# Education", "# Skills", "# Projects", "# Languages", "# Verified metrics", "# Inconsistencies requiring review"]
            if [line for line in content.splitlines() if line.strip()] != headings:
                findings.append("populated profile example: " + relative.as_posix())
    return sorted(set(findings))


def build_release(root: Path, output: Path) -> list[Path]:
    root = root.resolve()
    output = output.absolute()
    if output.resolve().is_relative_to(root):
        raise ValueError("release output must be outside source tree")
    findings = validate_release(root)
    if (root / ".git").exists():
        findings += validate_git_index(root)
    if findings:
        raise ValueError("release validation failed: " + "; ".join(findings))
    output.mkdir(parents=True, exist_ok=True)
    files = release_files(root)
    plugin = root / "plugins" / "job-pipeline"
    version = json.loads((plugin / "plugin.json").read_text())["version"]
    outputs = []
    for name, base, members in (
        (f"job-pipeline-plugin-{version}.zip", root, files),
        ("job-pipeline.plugin", plugin, [p for p in files if p.is_relative_to(plugin)]),
    ):
        target = output / name
        if target.exists():
            raise FileExistsError("release artifact already exists")
        hashes = []
        with ZipFile(target, "x", compression=ZIP_DEFLATED) as archive:
            for path in members:
                payload = path.read_bytes()
                relative = path.relative_to(base).as_posix()
                archive.writestr(relative, payload)
                hashes.append(hashlib.sha256(payload).hexdigest() + "  " + relative)
            archive.writestr("MANIFEST.sha256", "\n".join(hashes) + "\n")
        outputs.append(target)
    sums = output / "SHA256SUMS"
    if sums.exists():
        raise FileExistsError("checksum artifact already exists")
    sums.write_text("\n".join(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name for p in outputs) + "\n", encoding="utf-8")
    return outputs + [sums]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-git", action="store_true")
    args = parser.parse_args()
    findings = validate_release(args.root)
    if args.check_git or (args.root / ".git").exists():
        findings += validate_git_index(args.root)
    if findings:
        print("\n".join(findings))
        return 1
    if args.output:
        for path in build_release(args.root, args.output):
            print(path.name)
    else:
        print("Release inventory and privacy checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
