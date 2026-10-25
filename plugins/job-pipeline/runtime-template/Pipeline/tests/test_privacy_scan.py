from __future__ import annotations

import io
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest
import yaml

from Pipeline import privacy_scan
from Pipeline.initialize import initialize_project
from Pipeline.pipeline_store import PipelineStore
from Pipeline.privacy_scan import Finding, assert_clean, scan_installed_project, scan_path


ROOT = Path(__file__).resolve().parents[2]


def pairs(findings: list[Finding]) -> set[tuple[str, str]]:
    return {(item.category, item.path) for item in findings}


def categories(findings: list[Finding]) -> set[str]:
    return {item.category for item in findings}


def write_zip(path: Path, members: dict[str, bytes | str], *, compression: int = ZIP_DEFLATED) -> None:
    with ZipFile(path, "w", compression) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)


def nested_zip(depth: int) -> bytes:
    payload = b"plain UTF-8 text"
    name = "safe.txt"
    for index in range(depth):
        stream = io.BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr(name, payload)
        payload = stream.getvalue()
        name = f"nested-{index}.zip"
    return payload


def patch_zip_flag(payload: bytes, bit: int) -> bytes:
    changed = bytearray(payload)
    local = changed.find(b"PK\x03\x04")
    central = changed.find(b"PK\x01\x02")
    assert local >= 0 and central >= 0
    struct.pack_into("<H", changed, local + 6, struct.unpack_from("<H", changed, local + 6)[0] | bit)
    struct.pack_into("<H", changed, central + 8, struct.unpack_from("<H", changed, central + 8)[0] | bit)
    return bytes(changed)


def patch_zip_compression(payload: bytes, method: int) -> bytes:
    changed = bytearray(payload)
    local = changed.find(b"PK\x03\x04")
    central = changed.find(b"PK\x01\x02")
    assert local >= 0 and central >= 0
    struct.pack_into("<H", changed, local + 8, method)
    struct.pack_into("<H", changed, central + 10, method)
    return bytes(changed)


def patch_declared_size(payload: bytes, size: int) -> bytes:
    changed = bytearray(payload)
    struct.pack_into("<I", changed, 22, size)
    central = changed.find(b"PK\x01\x02")
    assert central >= 0
    struct.pack_into("<I", changed, central + 24, size)
    return bytes(changed)


def patch_declared_size_and_crc(payload: bytes, size: int, crc: int) -> bytes:
    changed = bytearray(payload)
    struct.pack_into("<I", changed, 14, crc)
    struct.pack_into("<I", changed, 22, size)
    central = changed.find(b"PK\x01\x02")
    assert central >= 0
    struct.pack_into("<I", changed, central + 16, crc)
    struct.pack_into("<I", changed, central + 24, size)
    return bytes(changed)


def patch_all_declared_sizes(payload: bytes, size: int) -> bytes:
    changed = bytearray(payload)
    first_central = changed.find(b"PK\x01\x02")
    local_offset = 0
    while local_offset < first_central:
        struct.pack_into("<I", changed, local_offset + 22, size)
        local_offset += local_record_size(changed, local_offset)
    assert local_offset == first_central
    offset = 0
    patched = 0
    while True:
        central = changed.find(b"PK\x01\x02", offset)
        if central < 0:
            break
        struct.pack_into("<I", changed, central + 24, size)
        name_size, extra_size, comment_size = struct.unpack_from("<3H", changed, central + 28)
        offset = central + 46 + name_size + extra_size + comment_size
        patched += 1
    assert patched > 0
    return bytes(changed)


def patch_all_declared_sizes_and_crcs(payload: bytes, size: int, crc: int) -> bytes:
    changed = bytearray(payload)
    first_central = central_offset(changed)
    local_offset = 0
    while local_offset < first_central:
        struct.pack_into("<I", changed, local_offset + 14, crc)
        struct.pack_into("<I", changed, local_offset + 22, size)
        local_offset += local_record_size(changed, local_offset)
    assert local_offset == first_central
    offset = first_central
    patched = 0
    while offset < eocd_offset(changed):
        assert changed[offset : offset + 4] == b"PK\x01\x02"
        struct.pack_into("<I", changed, offset + 16, crc)
        struct.pack_into("<I", changed, offset + 24, size)
        offset += central_record_size(changed, offset)
        patched += 1
    assert patched > 0
    return bytes(changed)


def simple_zip_bytes(
    members: tuple[tuple[str, bytes | str], ...] = (("safe.txt", "safe"),),
) -> bytes:
    stream = io.BytesIO()
    with ZipFile(stream, "w", ZIP_STORED) as archive:
        for name, value in members:
            archive.writestr(name, value)
    return stream.getvalue()


def eocd_offset(payload: bytes | bytearray) -> int:
    offset = len(payload) - 22
    assert offset >= 0 and payload[offset : offset + 4] == b"PK\x05\x06"
    return offset


def central_offset(payload: bytes | bytearray) -> int:
    return struct.unpack_from("<I", payload, eocd_offset(payload) + 16)[0]


def local_record_size(payload: bytes | bytearray, offset: int) -> int:
    assert payload[offset : offset + 4] == b"PK\x03\x04"
    compressed_size = struct.unpack_from("<I", payload, offset + 18)[0]
    name_size, extra_size = struct.unpack_from("<2H", payload, offset + 26)
    return 30 + name_size + extra_size + compressed_size


def central_record_size(payload: bytes | bytearray, offset: int) -> int:
    assert payload[offset : offset + 4] == b"PK\x01\x02"
    name_size, extra_size, comment_size = struct.unpack_from("<3H", payload, offset + 28)
    return 46 + name_size + extra_size + comment_size


def add_archive_comment(payload: bytes, comment: bytes) -> bytes:
    changed = bytearray(payload)
    eocd = eocd_offset(changed)
    struct.pack_into("<H", changed, eocd + 20, len(comment))
    changed.extend(comment)
    return bytes(changed)


def add_member_comment(payload: bytes, comment: bytes) -> bytes:
    changed = bytearray(payload)
    central = central_offset(changed)
    insertion = central + central_record_size(changed, central)
    changed[insertion:insertion] = comment
    struct.pack_into("<H", changed, central + 32, len(comment))
    eocd = eocd_offset(changed)
    struct.pack_into("<I", changed, eocd + 12, struct.unpack_from("<I", changed, eocd + 12)[0] + len(comment))
    return bytes(changed)


def add_local_extra(payload: bytes, extra: bytes) -> bytes:
    changed = bytearray(payload)
    original_central = central_offset(changed)
    name_size = struct.unpack_from("<H", changed, 26)[0]
    insertion = 30 + name_size
    changed[insertion:insertion] = extra
    struct.pack_into("<H", changed, 28, len(extra))
    eocd = eocd_offset(changed)
    struct.pack_into("<I", changed, eocd + 16, original_central + len(extra))
    return bytes(changed)


def add_central_extra(payload: bytes, extra: bytes) -> bytes:
    changed = bytearray(payload)
    central = central_offset(changed)
    name_size = struct.unpack_from("<H", changed, central + 28)[0]
    insertion = central + 46 + name_size
    changed[insertion:insertion] = extra
    struct.pack_into("<H", changed, central + 30, len(extra))
    eocd = eocd_offset(changed)
    struct.pack_into("<I", changed, eocd + 12, struct.unpack_from("<I", changed, eocd + 12)[0] + len(extra))
    return bytes(changed)


def add_local_central_gap(payload: bytes, gap: bytes) -> bytes:
    changed = bytearray(payload)
    original_central = central_offset(changed)
    changed[original_central:original_central] = gap
    eocd = eocd_offset(changed)
    struct.pack_into("<I", changed, eocd + 16, original_central + len(gap))
    return bytes(changed)


def remove_second_central_record(payload: bytes) -> bytes:
    changed = bytearray(payload)
    first = central_offset(changed)
    second = first + central_record_size(changed, first)
    removed = central_record_size(changed, second)
    del changed[second : second + removed]
    eocd = eocd_offset(changed)
    struct.pack_into("<2H", changed, eocd + 8, 1, 1)
    struct.pack_into("<I", changed, eocd + 12, struct.unpack_from("<I", changed, eocd + 12)[0] - removed)
    return bytes(changed)


def remove_second_local_record(payload: bytes) -> bytes:
    changed = bytearray(payload)
    second = local_record_size(changed, 0)
    removed = local_record_size(changed, second)
    del changed[second : second + removed]
    eocd = eocd_offset(changed)
    struct.pack_into("<I", changed, eocd + 16, struct.unpack_from("<I", changed, eocd + 16)[0] - removed)
    return bytes(changed)


def run_scanner(*arguments: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    return subprocess.run(
        [sys.executable, "-B", "-E", "-s", "-m", "Pipeline.privacy_scan", *arguments],
        cwd=ROOT,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=180,
        check=False,
    )


def powershell_executable() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    return executable


def run_install(project: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["JOB_PIPELINE_TEST_PYTHON"] = sys.executable
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            powershell_executable(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(project / "Install.ps1"),
            "-SkipDependencyInstall",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=180,
        check=False,
    )


def test_scanner_reports_category_and_relative_path_without_secret_value(tmp_path: Path):
    secret = "sk-" + "x" * 40
    target = tmp_path / "bad.txt"
    target.write_text(secret, encoding="utf-8")

    findings = scan_path(tmp_path)

    assert pairs(findings) == {("secret", "bad.txt")}
    rendered = json.dumps([item.__dict__ for item in findings])
    assert secret not in rendered
    assert str(tmp_path) not in rendered


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("person" + "@" + "real-domain" + ".org", "email"),
        ("+" + "44" + "7700900123", "phone"),
        ("C:" + "\\Users\\" + "PrivateUser\\resume.pdf", "windows_user_path"),
        ("-----BEGIN " + "PRIVATE KEY-----", "private_key"),
        ("access_" + "token = value", "oauth_or_token"),
    ],
)
def test_content_patterns_are_fail_closed_and_redacted(tmp_path: Path, payload: str, expected: str):
    target = tmp_path / "content.txt"
    target.write_text(payload, encoding="utf-8")

    findings = scan_path(tmp_path)

    assert expected in categories(findings)
    assert payload not in json.dumps([item.__dict__ for item in findings])


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("Address: " + "123" + " Main " + "Street" + ", Sample City", "street_address"),
        ("123" + " avenue " + "du Prototype", "street_address"),
        ("Date of " + "Birth:" + " 14 " + "June " + "1990", "date_of_birth"),
        ("Date de " + "naissance:" + " 14 " + "juin " + "1990", "date_of_birth"),
        ("DOB" + ": 08/03/1990", "date_of_birth"),
        ("DOB" + ": 1990-06-14", "date_of_birth"),
    ],
)
def test_street_address_and_labeled_birth_date_are_private(
    tmp_path: Path,
    payload: str,
    expected: str,
):
    (tmp_path / "profile.txt").write_text(payload, encoding="utf-8")

    findings = scan_path(tmp_path)

    assert expected in categories(findings)
    assert payload not in json.dumps([item.__dict__ for item in findings])


@pytest.mark.parametrize(
    "payload",
    [
        "DOB" + ": 12/" + "31/1990",
        "Date of " + "Birth:" + " June " + "14, " + "1990",
        "Date de " + "naissance:" + " juin " + "14 " + "1990",
    ],
)
def test_labeled_birth_date_supports_us_numeric_and_month_first_names(tmp_path: Path, payload: str):
    (tmp_path / "profile.txt").write_text(payload, encoding="utf-8")

    assert "date_of_birth" in categories(scan_path(tmp_path))


@pytest.mark.parametrize(
    "payload",
    [
        "Employment: May 2024 - April 2025",
        "Version 3.11 released on 2026-08-23",
        "Date of " + "Birth:" + " 40/" + "19/2020",
        "DOB" + ": 19/" + "40/2020",
        "June " + "14, " + "1990",
        "juin " + "14 " + "1990",
        "Born from a research project in 2024",
        "The report contains 35000 records and 123 examples",
    ],
)
def test_address_and_birth_date_detection_avoids_generic_dates_and_numbers(tmp_path: Path, payload: str):
    (tmp_path / "history.txt").write_text(payload, encoding="utf-8")

    assert scan_path(tmp_path) == []


@pytest.mark.parametrize(
    "payload",
    [
        b"SQLite format 3" + bytes((0,)) + b"payload",
        b"%PDF-1.7 payload",
        bytes.fromhex("89504e470d0a1a0a") + b"payload",
        bytes.fromhex("ffd8ff") + b"payload",
        b"GIF89a" + b"payload",
        bytes.fromhex("d0cf11e0a1b11ae1") + b"payload",
        bytes.fromhex("7f454c46") + b"payload",
        b"MZ" + b"payload",
        bytes.fromhex("1f8b") + b"payload",
        bytes.fromhex("377abcaf271c") + b"payload",
        bytes.fromhex("526172211a0700") + b"payload",
        b"PK" + bytes((3, 4)) + b"payload",
        b"safe" + bytes((0,)) + b"payload",
        b"safe" + bytes((1,)) + b"payload",
    ],
)
def test_allowed_text_suffix_rejects_binary_signatures_and_controls(tmp_path: Path, payload: bytes):
    (tmp_path / "disguised.md").write_bytes(payload)

    assert "binary_content" in categories(scan_path(tmp_path))


def test_allowed_text_control_whitespace_remains_valid(tmp_path: Path):
    (tmp_path / "safe.txt").write_bytes(b"line one\tvalue\r\nline two\fform feed")

    assert scan_path(tmp_path) == []


@pytest.mark.parametrize("format_control", ["\u200b", "\u200c", "\u202e", "\ufeff"])
def test_unicode_format_controls_fail_and_cannot_obfuscate_email_patterns(
    tmp_path: Path,
    format_control: str,
):
    private_email = "person" + format_control + "@real-domain.org"
    (tmp_path / "notes.md").write_text(private_email, encoding="utf-8")

    findings = scan_path(tmp_path)
    rendered = json.dumps([item.__dict__ for item in findings])

    assert {"format_control", "email"} <= categories(findings)
    assert private_email not in rendered
    assert private_email.replace(format_control, "") not in rendered


def test_unicode_format_controls_cannot_obfuscate_deny_tokens(tmp_path: Path):
    private_value = "Sensitive" + "\u200c" + "   Candidate"
    (tmp_path / "notes.md").write_text(private_value, encoding="utf-8")

    findings = scan_path(tmp_path, deny_tokens=["sensitive candidate"])

    assert {"format_control", "private_identifier"} <= categories(findings)


@pytest.mark.parametrize("format_control", ["\u200b", "\u200c", "\u202e", "\ufeff"])
def test_unicode_format_controls_fail_in_directory_and_zip_member_names(
    tmp_path: Path,
    format_control: str,
):
    root = tmp_path / "source"
    root.mkdir()
    private_name = "person" + format_control + "@real-domain.org.md"
    (root / private_name).write_text("safe", encoding="utf-8")
    archive_path = tmp_path / "controlled-name.zip"
    write_zip(archive_path, {private_name: "safe"})

    directory_findings = scan_path(root)
    archive_findings = scan_path(archive_path)
    rendered = json.dumps([item.__dict__ for item in directory_findings + archive_findings])

    assert {"format_control", "email"} <= categories(directory_findings)
    assert {"format_control", "email"} <= categories(archive_findings)
    assert private_name not in rendered
    assert private_name.replace(format_control, "") not in rendered


def test_reserved_synthetic_contacts_pass(tmp_path: Path):
    (tmp_path / "safe.txt").write_text(
        "candidate@example.invalid contact@example.test user@sample.example user@example.com +10000000000",
        encoding="utf-8",
    )

    assert scan_path(tmp_path) == []


def test_deny_tokens_use_nfkc_casefold_and_whitespace_but_ignore_short_values(tmp_path: Path):
    target = tmp_path / "safe.txt"
    target.write_text("PRIVATE FULL WIDTH ABC and Alpha   Beta", encoding="utf-8")

    findings = scan_path(tmp_path, deny_tokens=("ｐｒｉｖａｔｅ full width abc", "ALPHA\nBETA", "short"))

    assert pairs(findings) == {("private_identifier", "safe.txt")}
    assert "private full width abc" not in json.dumps([item.__dict__ for item in findings]).casefold()


def test_sensitive_and_deny_token_filenames_are_findings_but_are_never_echoed(tmp_path: Path):
    private_name = "person" + "@" + "real-domain" + ".org.md"
    deny_name = "Private Identifier.txt"
    (tmp_path / private_name).write_text("safe", encoding="utf-8")
    (tmp_path / deny_name).write_text("safe", encoding="utf-8")

    findings = scan_path(tmp_path, deny_tokens=("private identifier",))
    rendered = json.dumps([item.__dict__ for item in findings])

    assert {"email", "private_identifier"} <= categories(findings)
    assert private_name not in rendered
    assert deny_name.casefold() not in rendered.casefold()


def test_invalid_utf8_and_unknown_binary_are_findings(tmp_path: Path):
    (tmp_path / "bad.md").write_bytes(b"\xff\xfe")
    (tmp_path / "payload.bin").write_bytes(b"binary")

    result = pairs(scan_path(tmp_path))

    assert ("invalid_utf8", "bad.md") in result
    assert ("unsupported_file", "payload.bin") in result


def test_nonempty_structured_example_identity_is_rejected(tmp_path: Path):
    config = tmp_path / "config"
    config.mkdir()
    value = yaml.safe_load((ROOT / "config/candidate.example.yml").read_text(encoding="utf-8"))
    value["identity"]["full_name"] = "Synthetic Person"
    (config / "candidate.example.yml").write_text(yaml.safe_dump(value), encoding="utf-8")

    assert ("structured_identity", "config/candidate.example.yml") in pairs(scan_path(tmp_path))


@pytest.mark.parametrize(
    ("relative", "category"),
    [
        ("private/value.txt", "forbidden_path"),
        ("backups/value.txt", "forbidden_path"),
        ("__pycache__/value.py", "cache_or_dependency"),
        ("node_modules/value.js", "cache_or_dependency"),
        ("Pipeline/pipeline.db", "forbidden_name"),
        ("config/accounts.json", "forbidden_name"),
        ("document.pdf", "forbidden_suffix"),
        ("script.exe", "unsupported_file"),
    ],
)
def test_source_path_policy_rejects_private_runtime_cache_and_binary_residue(
    tmp_path: Path, relative: str, category: str
):
    path = tmp_path / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"safe")

    assert category in categories(scan_path(tmp_path))


def test_source_scan_rejects_symlink_and_hardlink_without_following(tmp_path: Path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.txt")
    outside.write_text("safe", encoding="utf-8")
    linked = tmp_path / "linked.txt"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("Symbolic links unavailable on test volume")
    hard = tmp_path / "hard.txt"
    os.link(outside, hard)

    result = pairs(scan_path(tmp_path))

    assert ("link_or_reparse", "linked.txt") in result
    assert ("hardlink", "hard.txt") in result


def test_source_and_installed_scans_reject_lexical_parent_segments(tmp_path: Path):
    source = tmp_path / "source"
    (source / "Pipeline").mkdir(parents=True)
    (source / "safe.txt").write_text("safe", encoding="utf-8")

    source_findings = scan_path(source / "Pipeline" / "..")
    installed_findings = scan_installed_project(source / "Pipeline" / "..")

    assert ("unsafe_path", ".") in pairs(source_findings)
    assert ("unsafe_path", ".") in pairs(installed_findings)


def test_scan_rejects_link_ancestor_without_reading_or_writing_target(tmp_path: Path):
    outside = tmp_path / "outside"
    source = outside / "source"
    source.mkdir(parents=True)
    marker = source / "safe.txt"
    marker.write_text("unchanged", encoding="utf-8")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory links unavailable on test volume")

    findings = scan_path(alias / "source")

    assert ("link_or_reparse", ".") in pairs(findings)
    assert marker.read_text(encoding="utf-8") == "unchanged"


def test_scan_detects_directory_swap_after_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "root"
    child = root / "Pipeline"
    child.mkdir(parents=True)
    (child / "safe.py").write_text("value = 1\n", encoding="utf-8")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "safe.py").write_text("value = 2\n", encoding="utf-8")
    moved = tmp_path / "moved"
    original_entries = privacy_scan._directory_entries
    swapped = False

    def swap_after_root_listing(path: Path):
        nonlocal swapped
        entries = original_entries(path)
        if path == root and not swapped:
            child.rename(moved)
            replacement.rename(child)
            swapped = True
        return entries

    monkeypatch.setattr(privacy_scan, "_directory_entries", swap_after_root_listing)

    assert ("scan_error", ".") in pairs(scan_path(root))


def test_scanner_checks_nested_zip_and_path_traversal(tmp_path: Path):
    inner = io.BytesIO()
    with ZipFile(inner, "w", ZIP_DEFLATED) as archive:
        archive.writestr("../escape.txt", "bad")
    outer = tmp_path / "outer.zip"
    write_zip(outer, {"Desktop-Import/skill.zip": inner.getvalue()})

    assert "zip_path_traversal" in categories(scan_path(outer))


@pytest.mark.parametrize(
    "name",
    [
        "/absolute.txt",
        "//server/share.txt",
        "C:/drive.txt",
        "folder\\backslash.txt",
        "folder/../escape.txt",
        "file.txt:stream",
        "folder//empty.txt",
        "folder/./dot.txt",
    ],
)
def test_zip_rejects_absolute_unc_drive_ads_backslash_dot_and_empty_segments(tmp_path: Path, name: str):
    archive_path = tmp_path / "bad.zip"
    write_zip(archive_path, {name: "safe"})
    if "\\" in name:
        normalized = name.replace("\\", "/").encode("utf-8")
        raw = name.encode("utf-8")
        payload = archive_path.read_bytes()
        assert payload.count(normalized) == 2
        archive_path.write_bytes(payload.replace(normalized, raw))

    assert categories(scan_path(archive_path)) & {"zip_path_traversal", "zip_unsafe_name"}


@pytest.mark.parametrize("names", [("A.txt", "a.txt"), ("e\u0301.txt", "\u00e9.txt")])
def test_zip_rejects_duplicate_normalized_casefold_names(tmp_path: Path, names: tuple[str, str]):
    archive_path = tmp_path / "duplicate.zip"
    with ZipFile(archive_path, "w", ZIP_STORED) as archive:
        archive.writestr(names[0], "first")
        archive.writestr(names[1], "second")

    assert "zip_duplicate_name" in categories(scan_path(archive_path))


@pytest.mark.parametrize("name", ["CON.txt", "folder/NUL.json", "name. ", "LPT1.md"])
def test_zip_rejects_windows_reserved_and_trailing_alias_names(tmp_path: Path, name: str):
    archive_path = tmp_path / "alias.zip"
    write_zip(archive_path, {name: "safe"}, compression=ZIP_STORED)

    assert "zip_windows_alias" in categories(scan_path(archive_path))


def test_zip_rejects_file_directory_prefix_conflict(tmp_path: Path):
    archive_path = tmp_path / "prefix.zip"
    with ZipFile(archive_path, "w", ZIP_STORED) as archive:
        archive.writestr("folder", "file")
        archive.writestr("folder/child.txt", "child")

    assert "zip_prefix_conflict" in categories(scan_path(archive_path))


def test_zip_rejects_encryption_symlink_reparse_device_and_unsupported_compression(tmp_path: Path):
    plain = io.BytesIO()
    with ZipFile(plain, "w", ZIP_STORED) as archive:
        archive.writestr("safe.txt", "safe")

    encrypted = tmp_path / "encrypted.zip"
    encrypted.write_bytes(patch_zip_flag(plain.getvalue(), 0x1))

    linked = tmp_path / "linked.zip"
    with ZipFile(linked, "w", ZIP_STORED) as archive:
        info = ZipInfo("linked.txt")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target")

    device = tmp_path / "device.zip"
    with ZipFile(device, "w", ZIP_STORED) as archive:
        info = ZipInfo("device.txt")
        info.create_system = 0
        info.external_attr = 0x440
        archive.writestr(info, "device")

    unsupported = tmp_path / "unsupported.zip"
    unsupported.write_bytes(patch_zip_compression(plain.getvalue(), 99))

    assert "zip_encrypted" in categories(scan_path(encrypted))
    assert "zip_link_or_device" in categories(scan_path(linked))
    assert "zip_link_or_device" in categories(scan_path(device))
    assert "zip_unsupported_compression" in categories(scan_path(unsupported))


@pytest.mark.parametrize("dos_flag", [0x400, 0x40, 0x10])
def test_zip_checks_security_dos_attributes_for_unix_created_members(tmp_path: Path, dos_flag: int):
    archive_path = tmp_path / f"unix-dos-attribute-{dos_flag}.zip"
    with ZipFile(archive_path, "w", ZIP_STORED) as archive:
        info = ZipInfo("plain.txt")
        info.create_system = 3
        info.external_attr = ((stat.S_IFREG | 0o600) << 16) | dos_flag
        archive.writestr(info, "safe")

    expected = "zip_invalid" if dos_flag == 0x10 else "zip_link_or_device"
    assert expected in categories(scan_path(archive_path))


def test_zip_rejects_unix_type_bits_for_dos_origin_and_unsupported_origin(tmp_path: Path):
    typed = tmp_path / "dos-with-unix-link.zip"
    with ZipFile(typed, "w", ZIP_STORED) as archive:
        info = ZipInfo("plain.txt")
        info.create_system = 0
        info.external_attr = (stat.S_IFLNK | 0o600) << 16
        archive.writestr(info, "safe")

    unsupported = tmp_path / "unsupported-origin.zip"
    with ZipFile(unsupported, "w", ZIP_STORED) as archive:
        info = ZipInfo("plain.txt")
        info.create_system = 7
        info.external_attr = (stat.S_IFREG | 0o600) << 16
        archive.writestr(info, "safe")

    assert "zip_link_or_device" in categories(scan_path(typed))
    assert "zip_invalid" in categories(scan_path(unsupported))


def test_zip_rejects_directory_payload_and_file_directory_attribute_mismatch(tmp_path: Path):
    hidden = tmp_path / "hidden-directory-data.zip"
    with ZipFile(hidden, "w", ZIP_STORED) as archive:
        archive.writestr("folder/", "hidden")

    mismatch = tmp_path / "mismatch.zip"
    with ZipFile(mismatch, "w", ZIP_STORED) as archive:
        info = ZipInfo("looks-like-file.txt")
        info.create_system = 3
        info.external_attr = (stat.S_IFDIR | 0o755) << 16
        archive.writestr(info, "safe")

    assert "zip_invalid" in categories(scan_path(hidden))
    assert "zip_invalid" in categories(scan_path(mismatch))


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("preamble", lambda payload, value: value + payload),
        ("archive-comment", add_archive_comment),
        ("member-comment", add_member_comment),
        ("local-extra", add_local_extra),
        ("central-extra", add_central_extra),
        ("local-central-gap", add_local_central_gap),
        ("trailing", lambda payload, value: payload + value),
    ],
)
def test_zip_rejects_every_unaccounted_raw_byte_and_comment(
    tmp_path: Path,
    label: str,
    mutate,
):
    hidden = ("sk-" + "r" * 40).encode("ascii")
    archive_path = tmp_path / f"{label}.zip"
    archive_path.write_bytes(mutate(simple_zip_bytes(), hidden))

    findings = scan_path(archive_path)
    rendered = json.dumps([item.__dict__ for item in findings])

    assert "zip_invalid" in categories(findings)
    assert hidden.decode("ascii") not in rendered


@pytest.mark.parametrize("field", ["name", "flags", "method", "crc", "compressed-size", "size"])
def test_zip_rejects_local_and_central_header_mismatch(tmp_path: Path, field: str):
    changed = bytearray(simple_zip_bytes())
    if field == "name":
        changed[30] ^= 1
    elif field == "flags":
        struct.pack_into("<H", changed, 6, struct.unpack_from("<H", changed, 6)[0] ^ 0x800)
    elif field == "method":
        struct.pack_into("<H", changed, 8, ZIP_DEFLATED)
    elif field == "crc":
        struct.pack_into("<I", changed, 14, struct.unpack_from("<I", changed, 14)[0] ^ 1)
    elif field == "compressed-size":
        struct.pack_into("<I", changed, 18, struct.unpack_from("<I", changed, 18)[0] + 1)
    else:
        struct.pack_into("<I", changed, 22, struct.unpack_from("<I", changed, 22)[0] + 1)
    archive_path = tmp_path / f"mismatch-{field}.zip"
    archive_path.write_bytes(changed)

    assert "zip_invalid" in categories(scan_path(archive_path))


def test_zip_rejects_data_descriptor_and_zip64_metadata(tmp_path: Path):
    descriptor = tmp_path / "descriptor.zip"
    descriptor.write_bytes(patch_zip_flag(simple_zip_bytes(), 0x8))

    zip64_payload = bytearray(simple_zip_bytes())
    eocd = eocd_offset(zip64_payload)
    struct.pack_into("<2H", zip64_payload, eocd + 8, 0xFFFF, 0xFFFF)
    struct.pack_into("<2I", zip64_payload, eocd + 12, 0xFFFFFFFF, 0xFFFFFFFF)
    zip64 = tmp_path / "zip64.zip"
    zip64.write_bytes(zip64_payload)

    assert "zip_invalid" in categories(scan_path(descriptor))
    assert "zip_invalid" in categories(scan_path(zip64))


def test_zip_rejects_central_only_and_local_only_members(tmp_path: Path):
    hidden = "sk-" + "l" * 40
    payload = simple_zip_bytes((("safe.txt", "safe"), ("hidden.txt", hidden)))
    local_only = tmp_path / "local-only.zip"
    local_only.write_bytes(remove_second_central_record(payload))
    central_only = tmp_path / "central-only.zip"
    central_only.write_bytes(remove_second_local_record(payload))

    local_findings = scan_path(local_only)
    central_findings = scan_path(central_only)
    rendered = json.dumps([item.__dict__ for item in local_findings + central_findings])

    assert "zip_invalid" in categories(local_findings)
    assert "zip_invalid" in categories(central_findings)
    assert hidden not in rendered


def test_zip_rejects_hidden_directory_payload_even_when_central_sizes_are_zero(tmp_path: Path):
    changed = bytearray(simple_zip_bytes((("folder/", b"hidden"),)))
    central = central_offset(changed)
    struct.pack_into("<3I", changed, central + 16, 0, 0, 0)
    archive_path = tmp_path / "hidden-directory-data.zip"
    archive_path.write_bytes(changed)

    assert "zip_invalid" in categories(scan_path(archive_path))


def test_zip_invalid_utf8_filename_is_a_finding_not_an_api_exception(tmp_path: Path):
    stream = io.BytesIO()
    with ZipFile(stream, "w", ZIP_STORED) as archive:
        archive.writestr("a.txt", "safe")
    payload = bytearray(patch_zip_flag(stream.getvalue(), 0x800))
    assert payload.count(b"a.txt") == 2
    payload = payload.replace(b"a.txt", b"\xff.txt")
    archive_path = tmp_path / "invalid-name.zip"
    archive_path.write_bytes(payload)

    assert "zip_invalid" in categories(scan_path(archive_path))


def test_zip_rejects_depth_member_count_declared_size_and_crc_errors(tmp_path: Path):
    too_deep = tmp_path / "deep.zip"
    too_deep.write_bytes(nested_zip(4))

    too_many = tmp_path / "many.zip"
    with ZipFile(too_many, "w", ZIP_DEFLATED) as archive:
        for index in range(2001):
            archive.writestr(f"item-{index:04d}.txt", "")

    plain = io.BytesIO()
    with ZipFile(plain, "w", ZIP_STORED) as archive:
        archive.writestr("safe.txt", "payload")
    oversized = tmp_path / "oversized.zip"
    oversized.write_bytes(patch_declared_size(plain.getvalue(), 25 * 1024 * 1024 + 1))

    corrupt = bytearray(plain.getvalue())
    marker = corrupt.find(b"payload")
    assert marker >= 0
    corrupt[marker] ^= 0xFF
    bad_crc = tmp_path / "crc.zip"
    bad_crc.write_bytes(corrupt)

    assert "zip_depth" in categories(scan_path(too_deep))
    assert "zip_member_limit" in categories(scan_path(too_many))
    assert "zip_member_size" in categories(scan_path(oversized))
    assert "zip_crc" in categories(scan_path(bad_crc))


def test_zip_rejects_aggregate_declared_uncompressed_size(tmp_path: Path):
    stream = io.BytesIO()
    with ZipFile(stream, "w", ZIP_STORED) as archive:
        for index in range(5):
            archive.writestr(f"item-{index}.txt", "")
    archive_path = tmp_path / "aggregate-size.zip"
    archive_path.write_bytes(patch_all_declared_sizes(stream.getvalue(), 21 * 1024 * 1024))

    assert "zip_total_size" in categories(scan_path(archive_path))


def test_zip_bounds_actual_deflate_output_even_when_declared_size_and_crc_are_forged(tmp_path: Path):
    stream = io.BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("expanded.txt", b"A" * (25 * 1024 * 1024 + 1))
    declared = b"AAAA"
    archive_path = tmp_path / "actual-size.zip"
    archive_path.write_bytes(patch_declared_size_and_crc(stream.getvalue(), len(declared), zlib.crc32(declared)))

    assert "zip_member_size" in categories(scan_path(archive_path))


def test_zip_failed_members_incrementally_consume_one_shared_actual_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    stream = io.BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for index in range(3):
            archive.writestr(f"part-{index}.txt", b"A" * 100)
    forged = patch_all_declared_sizes_and_crcs(stream.getvalue(), 4, zlib.crc32(b"A" * 4))
    archive_path = tmp_path / "shared-actual-budget.zip"
    archive_path.write_bytes(forged)
    monkeypatch.setattr(privacy_scan, "MAX_ZIP_MEMBER_BYTES", 128)
    monkeypatch.setattr(privacy_scan, "MAX_ZIP_TOTAL_BYTES", 200)

    findings = scan_path(archive_path)

    assert "zip_total_size" in categories(findings)


def test_zip_stops_before_decoding_another_member_when_actual_budget_is_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    stream = io.BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for index in range(3):
            archive.writestr(f"part-{index}.txt", b"A" * 100)
    changed = bytearray(
        patch_all_declared_sizes_and_crcs(stream.getvalue(), 4, zlib.crc32(b"A" * 4))
    )
    offset = 0
    for index in range(3):
        name_size, extra_size = struct.unpack_from("<2H", changed, offset + 26)
        compressed_size = struct.unpack_from("<I", changed, offset + 18)[0]
        if index == 2:
            data_offset = offset + 30 + name_size + extra_size
            changed[data_offset : data_offset + compressed_size] = b"\xff" * compressed_size
        offset += local_record_size(changed, offset)
    archive_path = tmp_path / "stop-at-shared-budget.zip"
    archive_path.write_bytes(changed)
    monkeypatch.setattr(privacy_scan, "MAX_ZIP_MEMBER_BYTES", 128)
    monkeypatch.setattr(privacy_scan, "MAX_ZIP_TOTAL_BYTES", 200)

    findings = scan_path(archive_path)

    assert "zip_total_size" in categories(findings)
    assert "zip_crc" not in categories(findings)


def test_zip_rejects_private_cache_runtime_and_invalid_utf8_members(tmp_path: Path):
    archive_path = tmp_path / "bad.zip"
    write_zip(
        archive_path,
        {
            "private/value.txt": "safe",
            "node_modules/tool.js": "safe",
            "Pipeline/pipeline_state.json": "{}",
            "bad.md": b"\xff",
        },
    )

    found = categories(scan_path(archive_path))

    assert {"forbidden_path", "cache_or_dependency", "forbidden_name", "invalid_utf8"} <= found


def test_assert_clean_raises_only_safe_category_and_path(tmp_path: Path):
    secret = "sk-" + "z" * 40
    (tmp_path / "bad.txt").write_text(secret, encoding="utf-8")

    with pytest.raises(Exception) as captured:
        assert_clean(tmp_path)

    assert secret not in str(captured.value)
    assert str(tmp_path) not in str(captured.value)
    assert "bad.txt" in str(captured.value)


@pytest.mark.source_only
def test_blank_package_source_is_clean():
    assert scan_path(ROOT) == []


def test_installed_scan_allows_expected_mutable_shapes_without_reading_private_values(project_root: Path):
    initialize_project(project_root)
    secret = "sk-" + "q" * 40
    (project_root / "private/candidate.yml").write_text(secret, encoding="utf-8")
    (project_root / "private/master-profile.md").write_text(secret, encoding="utf-8")
    (project_root / "private/master-cv.pdf").write_bytes(secret.encode("utf-8"))
    (project_root / "private/run.lock").write_text(secret, encoding="utf-8")
    (project_root / "private/imports/job.json").write_text(secret, encoding="utf-8")
    (project_root / "private/jobs/job.yml").write_text(secret, encoding="utf-8")
    creator = project_root / "private/cv-creator"
    (creator / "assets").mkdir()
    (creator / "baseline").mkdir()
    for relative in ("layout.json", "creator.json", "template.html.j2", "style.css"):
        (creator / relative).write_text(secret, encoding="utf-8")
    (creator / "assets/asset-001.webp").write_bytes(secret.encode("utf-8"))
    (creator / "baseline/baseline.pdf").write_bytes(secret.encode("utf-8"))
    (creator / "baseline/page-001.png").write_bytes(secret.encode("utf-8"))
    generated = project_root / "Documents/generated"
    (generated / "cv.pdf").write_bytes(secret.encode("utf-8"))
    (generated / "letter.md").write_text(secret, encoding="utf-8")
    (generated / "previews").mkdir()
    (generated / "previews/cv-page-001.png").write_bytes(secret.encode("utf-8"))

    assert scan_installed_project(project_root, deny_tokens=(secret,)) == []


def test_installed_scan_still_scans_static_files_and_rejects_mutable_scripts_archives_and_unknowns(project_root: Path):
    initialize_project(project_root)
    secret = "sk-" + "m" * 40
    (project_root / "CLAUDE.md").write_text(secret, encoding="utf-8")
    (project_root / "private/notes.txt").write_text("safe", encoding="utf-8")
    (project_root / "private/jobs/run.py").write_text("safe", encoding="utf-8")
    (project_root / "Documents/generated/archive.zip").write_bytes(b"not a zip")

    found = pairs(scan_installed_project(project_root, deny_tokens=(secret,)))

    assert ("secret", "CLAUDE.md") in found
    assert ("private_identifier", "CLAUDE.md") in found
    assert ("unexpected_mutable_path", "private/notes.txt") in found
    assert ("unsafe_mutable_type", "private/jobs/run.py") in found
    assert ("unsafe_mutable_type", "Documents/generated/archive.zip") in found


def test_installed_scan_validates_venv_root_then_skips_contents(project_root: Path):
    initialize_project(project_root)
    secret = "sk-" + "v" * 40
    venv = project_root / ".venv"
    venv.mkdir()
    (venv / "Scripts").mkdir()
    (venv / "Scripts/python.exe").write_bytes(secret.encode("utf-8"))

    assert scan_installed_project(project_root, deny_tokens=(secret,)) == []

    shutil.rmtree(venv)
    outside = project_root.parent / "external-venv"
    outside.mkdir()
    try:
        venv.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory links unavailable on test volume")
    assert ("link_or_reparse", ".venv") in pairs(scan_installed_project(project_root))


def test_installed_scan_rejects_mutable_symlink_and_hardlink(project_root: Path):
    initialize_project(project_root)
    outside = project_root.parent / "outside-profile.md"
    outside.write_text("private", encoding="utf-8")
    profile = project_root / "private/master-profile.md"
    os.link(outside, profile)
    assert ("hardlink", "private/master-profile.md") in pairs(scan_installed_project(project_root))

    profile.unlink()
    try:
        profile.symlink_to(outside)
    except OSError:
        pytest.skip("Symbolic links unavailable on test volume")
    assert ("link_or_reparse", "private/master-profile.md") in pairs(scan_installed_project(project_root))


def test_installed_scan_allows_blank_runtime_and_can_require_blank(project_root: Path):
    initialize_project(project_root)

    assert scan_installed_project(project_root, expect_blank=True) == []

    (project_root / "private/master-profile.md").write_text("Synthetic private profile\n", encoding="utf-8")
    assert ("nonblank_private_runtime", "private/master-profile.md") in pairs(
        scan_installed_project(project_root, expect_blank=True)
    )


@pytest.mark.parametrize(
    ("relative", "mutator", "expected"),
    [
        ("private/candidate.yml", lambda value: value["identity"].update(full_name="Synthetic"), "nonblank_candidate"),
        ("private/job-preferences.yml", lambda value: value["target_titles"].append("Synthetic"), "nonblank_preferences"),
        ("private/accounts.json", lambda value: value["employer_tenants"].append({}), "nonblank_accounts"),
        ("private/runtime.json", lambda value: value.update(browser_profile="Profile"), "nonblank_runtime"),
    ],
)
def test_expect_blank_parses_exact_private_schemas(
    project_root: Path, relative: str, mutator, expected: str
):
    initialize_project(project_root)
    path = project_root / relative
    if path.suffix == ".yml":
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutator(value)
        path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
        mutator(value)
        path.write_text(json.dumps(value), encoding="utf-8")

    assert expected in categories(scan_installed_project(project_root, expect_blank=True))


def test_expect_blank_detects_database_settings_and_exported_state_mismatch(project_root: Path):
    initialize_project(project_root)
    store = PipelineStore(project_root / "Pipeline/pipeline.db")
    current = store.get_settings()
    changed = dict(current["settings"])
    changed["daily_submission_cap"] = 1
    store.save_settings(changed, expected_version=current["version"], reason="synthetic-test")
    store.export_state()

    assert "nonblank_database" in categories(scan_installed_project(project_root, expect_blank=True))

    initialize_project(project_root)
    state_path = project_root / "Pipeline/pipeline_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["jobs"] = [{"synthetic": True}]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    assert "database_state_mismatch" in categories(scan_installed_project(project_root, expect_blank=True))


def test_expect_blank_opens_canonical_database_once_with_read_only_uri(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    initialize_project(project_root)
    observed: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
    real_connect = sqlite3.connect

    def tracking_connect(database, *args, **kwargs):
        observed.append((database, args, dict(kwargs)))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)

    assert scan_installed_project(project_root, expect_blank=True) == []
    assert len(observed) == 1
    database, _args, kwargs = observed[0]
    assert isinstance(database, str)
    assert "mode=ro" in database
    assert "immutable=" not in database
    assert kwargs.get("uri") is True
    canonical = project_root / "Pipeline/pipeline.db"
    renamed = canonical.with_name("renamed.db")
    canonical.rename(renamed)
    renamed.rename(canonical)


def test_expect_blank_rejects_unexpected_database_schema(project_root: Path):
    initialize_project(project_root)
    database = project_root / "Pipeline/pipeline.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unexpected(value TEXT)")

    assert "invalid_blank_schema" in categories(scan_installed_project(project_root, expect_blank=True))


def test_expect_blank_reads_wal_without_mutating_database_sidecars(project_root: Path):
    initialize_project(project_root)
    database = project_root / "Pipeline/pipeline.db"
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].casefold() == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO jobs(id, payload_json, status, country, score, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("synthetic-job", "{}", "new", "XX", 0, "2000-01-01T00:00:00Z"),
        )
        writer.commit()
        sidecars = [database, database.with_name(database.name + "-wal"), database.with_name(database.name + "-shm")]
        assert all(path.is_file() for path in sidecars)

        before = {
            path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size, path.stat().st_mtime_ns)
            for path in sidecars
        }
        findings = scan_installed_project(project_root, expect_blank=True)
        after = {
            path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size, path.stat().st_mtime_ns)
            for path in sidecars
        }

        assert "invalid_blank_schema" in categories(findings)
        assert after == before
    finally:
        writer.close()


def test_expect_blank_does_not_mutate_rollback_journal_and_closes_before_return(project_root: Path):
    initialize_project(project_root)
    database = project_root / "Pipeline/pipeline.db"
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE settings SET version = version + 1 WHERE singleton = 1")
        journal = database.with_name(database.name + "-journal")
        assert journal.is_file()
        before = {
            path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size, path.stat().st_mtime_ns)
            for path in (database, journal)
        }

        findings = scan_installed_project(project_root, expect_blank=True)
        after = {
            path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size, path.stat().st_mtime_ns)
            for path in (database, journal)
        }

        assert "invalid_blank_schema" in categories(findings)
        assert after == before
    finally:
        writer.rollback()
        writer.close()



def test_expect_blank_reads_read_only_database_without_mutating_it(project_root: Path):
    initialize_project(project_root)
    database = project_root / "Pipeline/pipeline.db"
    os.chmod(database, stat.S_IREAD)
    try:
        before = (hashlib.sha256(database.read_bytes()).hexdigest(), database.stat().st_size, database.stat().st_mtime_ns)

        assert scan_installed_project(project_root, expect_blank=True) == []

        after = (hashlib.sha256(database.read_bytes()).hexdigest(), database.stat().st_size, database.stat().st_mtime_ns)
        assert after == before
    finally:
        os.chmod(database, stat.S_IWRITE | stat.S_IREAD)


def test_expect_blank_rejects_duplicate_key_yaml_without_disclosing_value(project_root: Path):
    initialize_project(project_root)
    secret = "PRIVATE-" + "Q" * 40
    candidate = project_root / "private/candidate.yml"
    candidate.write_text("schema_version: 1\nschema_version: 1\nsecret: " + secret, encoding="utf-8")

    findings = scan_installed_project(project_root, expect_blank=True)

    assert "invalid_blank_schema" in categories(findings)
    assert secret not in json.dumps([item.__dict__ for item in findings])


def test_installed_generated_allows_root_documents_but_preview_pngs_only_one_level_down(project_root: Path):
    initialize_project(project_root)
    generated = project_root / "Documents/generated"
    (generated / "unexpected.png").write_bytes(b"synthetic")
    (generated / "unknown").mkdir()
    (generated / "unknown/document.pdf").write_bytes(b"synthetic")
    (generated / "previews").mkdir()
    (generated / "previews/nested").mkdir()
    (generated / "previews/nested/page.png").write_bytes(b"synthetic")

    found = pairs(scan_installed_project(project_root))

    assert ("unsafe_mutable_type", "Documents/generated/unexpected.png") in found
    assert ("unexpected_mutable_path", "Documents/generated/unknown") in found
    assert ("unexpected_mutable_path", "Documents/generated/previews/nested") in found


def test_cli_rejects_expect_blank_without_installed():
    completed = run_scanner(str(ROOT), "--expect-blank")

    assert completed.returncode == 2
    assert completed.stderr == "privacy-scan: invalid arguments.\n"
    assert str(ROOT) not in completed.stdout + completed.stderr


def test_cli_argument_errors_are_fixed_and_never_echo_argv():
    private_path = "C:" + "\\Users\\" + "SyntheticSecret"
    for arguments in (
        (),
        (str(ROOT), "--unknown-option", private_path),
        (str(ROOT), private_path),
    ):
        completed = run_scanner(*arguments)

        assert completed.returncode == 2
        assert completed.stdout == ""
        assert completed.stderr == "privacy-scan: invalid arguments.\n"
        assert private_path not in completed.stdout + completed.stderr


def test_cli_bounded_stdin_rejects_one_mebibyte_unbroken_value_without_echo(tmp_path: Path):
    value = "S" * (1024 * 1024)

    completed = run_scanner(str(tmp_path), "--deny-stdin", stdin=value)

    assert completed.returncode == 2
    assert len(completed.stdout + completed.stderr) < 200
    assert value[:100] not in completed.stdout + completed.stderr
    assert str(tmp_path) not in completed.stdout + completed.stderr


def test_cli_deny_stdin_outputs_deterministic_redacted_json(tmp_path: Path):
    token = "Private" + " Identifier"
    (tmp_path / "safe.txt").write_text("prefix private   identifier suffix", encoding="utf-8")

    first = run_scanner(str(tmp_path), "--deny-stdin", stdin=token + "\n")
    second = run_scanner(str(tmp_path), "--deny-stdin", stdin=token + "\n")

    assert first.returncode == 1
    assert first.stdout == second.stdout
    assert json.loads(first.stdout) == {
        "findings": [{"category": "private_identifier", "path": "safe.txt"}]
    }
    assert token.casefold() not in (first.stdout + first.stderr).casefold()
    assert str(tmp_path) not in first.stdout + first.stderr


def test_installer_runs_installed_scanner_before_success_and_preserves_populated_private_state(project_root: Path):
    secret = "sk-" + "i" * 40
    (project_root / "CLAUDE.md").write_text(secret, encoding="utf-8")

    failed = run_install(project_root)

    assert failed.returncode != 0
    assert "installation complete" not in failed.stdout.casefold()
    assert secret not in failed.stdout + failed.stderr

    shutil.copy2(ROOT / "CLAUDE.md", project_root / "CLAUDE.md")
    initialize_project(project_root)
    profile = project_root / "private/master-profile.md"
    profile.write_text("Synthetic friend profile\n", encoding="utf-8")
    first = run_install(project_root)
    second = run_install(project_root)
    assert first.returncode == second.returncode == 0
    assert profile.read_text(encoding="utf-8") == "Synthetic friend profile\n"
