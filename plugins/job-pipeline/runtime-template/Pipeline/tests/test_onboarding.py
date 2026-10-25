import hashlib
import json
import os
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import fitz
import pytest
import yaml
from docx import Document

from Pipeline.initialize import initialize_project
from Pipeline.io_utils import atomic_write_text, is_symlink_or_reparse
from Pipeline.onboarding import (
    MAX_CAREER_ROWS,
    MAX_LIST_ITEMS,
    MAX_PDF_PAGES,
    MAX_PROFILE_BYTES,
    OnboardingError,
    load_runtime,
    mark_onboarding_complete,
    onboarding_status,
    register_master_cv,
    register_master_profile,
    save_runtime,
    validate_candidate,
    validate_preferences,
    write_master_profile_draft,
)


def make_pdf(path: Path) -> bytes:
    document = fitz.open()
    document.new_page().insert_text((72, 72), "Synthetic CV")
    document.save(path)
    document.close()
    return path.read_bytes()


def make_docx(path: Path) -> bytes:
    document = Document()
    document.add_paragraph("Synthetic CV")
    document.save(path)
    return path.read_bytes()


def replace_zip_member(path: Path, member: str, payload: bytes) -> None:
    replacement = path.with_name(path.stem + "-replacement" + path.suffix)
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(replacement, "w", compression=zipfile.ZIP_DEFLATED) as destination:
        for info in source.infolist():
            if info.filename != member:
                destination.writestr(info, source.read(info.filename))
        destination.writestr(member, payload)
    os.replace(replacement, path)


def candidate() -> dict:
    return {
        "schema_version": 1,
        "identity": {
            "full_name": "Synthetic User",
            "email": "synthetic@example.test",
            "phone": "+10000000000",
            "residence_country": "FR",
        },
        "current_employer": "Example Corp",
        "employment_status": "employed",
        "work_authorization": [
            {"countries": ["FR"], "status": "authorized"},
            {"countries": ["CA"], "status": "sponsorship_required"},
        ],
        "languages": ["English"],
        "notice_period": "30 days",
        "known_answers": {},
        "career_evidence": [{"id": "project-1", "text": "Built a synthetic project."}],
    }


def preferences() -> dict:
    return {
        "schema_version": 1,
        "target_titles": ["Analyst"],
        "role_families": ["analytics"],
        "industries": [],
        "seniority": {"minimum": "junior", "maximum": "senior"},
        "countries": [{"code": "FR", "priority": 1}, {"code": "CA", "priority": 2}],
        "work_modes": [],
        "salary": {"minimum": 0, "currency": "EUR"},
        "include_keywords": [],
        "exclude_keywords": [],
        "excluded_employers": [],
        "preferred_company_types": [],
        "vie": {"enabled": False, "eligible": False},
    }


def write_onboarding_inputs(project_root: Path) -> None:
    private = project_root / "private"
    (private / "candidate.yml").write_text(yaml.safe_dump(candidate()), encoding="utf-8")
    (private / "job-preferences.yml").write_text(yaml.safe_dump(preferences()), encoding="utf-8")
    source_cv = project_root / "source.pdf"
    make_pdf(source_cv)
    register_master_cv(project_root, source_cv)
    write_master_profile_draft(project_root, "# Synthetic profile\n")


def test_initialize_is_idempotent_and_creates_no_fake_master_files(project_root: Path):
    initialize_project(project_root)
    before = {
        path.name: path.read_bytes()
        for path in (project_root / "private").iterdir()
        if path.is_file()
    }
    initialize_project(project_root)
    assert before == {
        path.name: path.read_bytes()
        for path in (project_root / "private").iterdir()
        if path.is_file()
    }
    assert set(before) == {"candidate.yml", "job-preferences.yml", "accounts.json", "runtime.json"}
    assert not list((project_root / "private").glob("master-cv.*"))
    assert not (project_root / "private" / "master-profile.md").exists()
    assert onboarding_status(project_root)["ready"] is False


def test_atomic_write_replaces_file_and_cleans_temporary_file_after_replace_failure(tmp_path: Path):
    destination = tmp_path / "state.txt"
    destination.write_text("old", encoding="utf-8")
    atomic_write_text(destination, "new\n")
    assert destination.read_text(encoding="utf-8") == "new\n"

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    with pytest.raises(OSError):
        atomic_write_text(blocked, "cannot replace directory")
    assert not (tmp_path / "blocked.tmp").exists()


def test_symlink_detection_and_cv_registration_reject_symlink_source(project_root: Path):
    initialize_project(project_root)
    target = project_root / "actual.pdf"
    target.write_bytes(b"%PDF")
    link = project_root / "linked.pdf"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlink unavailable: {error}")
    assert is_symlink_or_reparse(link) is True
    with pytest.raises(OnboardingError, match="symlink or reparse"):
        register_master_cv(project_root, link)
    assert not list((project_root / "private").glob("master-cv.*"))


def test_register_master_cv_copies_supported_file_records_relative_hash_and_refuses_overwrite(project_root: Path):
    initialize_project(project_root)
    source = project_root / "outside.PDF"
    payload = make_pdf(source)
    result = register_master_cv(project_root, source)
    assert result == {
        "path": "private/master-cv.pdf",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    assert (project_root / result["path"]).read_bytes() == payload
    assert load_runtime(project_root)["master_cv_path"] == "private/master-cv.pdf"

    replacement = project_root / "replacement.pdf"
    replacement.write_bytes(b"replacement")
    with pytest.raises(OnboardingError, match="already exists"):
        register_master_cv(project_root, replacement)
    assert (project_root / result["path"]).read_bytes() == payload


@pytest.mark.parametrize(
    ("name", "payload", "message"),
    [
        ("cv.docm", b"bad", "PDF or DOCX"),
        ("empty.pdf", b"", "size"),
    ],
)
def test_register_master_cv_rejects_invalid_extension_and_size(project_root: Path, name: str, payload: bytes, message: str):
    initialize_project(project_root)
    source = project_root / name
    source.write_bytes(payload)
    with pytest.raises(OnboardingError, match=message):
        register_master_cv(project_root, source)
    assert not list((project_root / "private").glob("master-cv.*"))


def test_register_master_cv_rejects_directory_and_file_over_15_megabytes(project_root: Path):
    initialize_project(project_root)
    with pytest.raises(OnboardingError, match="regular file"):
        register_master_cv(project_root, project_root)
    oversized = project_root / "large.pdf"
    with oversized.open("wb") as handle:
        handle.truncate(15 * 1024 * 1024 + 1)
    with pytest.raises(OnboardingError, match="size"):
        register_master_cv(project_root, oversized)


def test_master_profile_registration_and_guided_draft_are_safe_utf8_and_non_overwriting(project_root: Path):
    initialize_project(project_root)
    source = project_root / "profile.txt"
    source.write_text("# Synthetic profile\n", encoding="utf-8")
    destination = register_master_profile(project_root, source)
    assert destination == project_root / "private" / "master-profile.md"
    assert destination.read_text(encoding="utf-8") == "# Synthetic profile\n"
    with pytest.raises(OnboardingError, match="already exists"):
        write_master_profile_draft(project_root, "# Different")


@pytest.mark.parametrize(
    ("name", "payload", "message"),
    [
        ("profile.pdf", b"%PDF", "Markdown or text"),
        ("profile.md", b"\xff", "UTF-8"),
        ("profile.md", b"normal\x00hidden", "control"),
    ],
)
def test_master_profile_rejects_extension_non_utf8_and_controls(project_root: Path, name: str, payload: bytes, message: str):
    initialize_project(project_root)
    source = project_root / name
    source.write_bytes(payload)
    with pytest.raises(OnboardingError, match=message):
        register_master_profile(project_root, source)
    assert not (project_root / "private" / "master-profile.md").exists()


def test_master_profile_rejects_file_over_two_megabytes(project_root: Path):
    initialize_project(project_root)
    source = project_root / "large.md"
    with source.open("wb") as handle:
        handle.truncate(2 * 1024 * 1024 + 1)
    with pytest.raises(OnboardingError, match="size"):
        register_master_profile(project_root, source)


def test_runtime_requires_exact_schema_relative_private_path_and_valid_auto_apply_approval(project_root: Path):
    initialize_project(project_root)
    runtime = load_runtime(project_root)
    assert runtime == {
        "schema_version": 1,
        "submission_mode": "review_only",
        "browser_profile": "",
        "master_cv_path": "",
        "master_cv_sha256": "",
        "onboarding": {"complete": False, "creator_ready": False},
        "auto_apply_approved_at": None,
    }
    runtime.update(
        {
            "submission_mode": "auto_apply",
            "master_cv_path": "private/master-cv.pdf",
            "master_cv_sha256": "a" * 64,
            "auto_apply_approved_at": "2026-08-23T12:34:56Z",
        }
    )
    save_runtime(project_root, runtime)
    assert load_runtime(project_root) == runtime

    for key, value, message in [
        ("extra", True, "runtime"),
        ("submission_mode", "unsafe", "submission_mode"),
        ("master_cv_path", "C:/outside.pdf", "master_cv_path"),
        ("master_cv_path", "private/../outside.pdf", "master_cv_path"),
        ("master_cv_sha256", "A" * 64, "master_cv_sha256"),
        ("auto_apply_approved_at", "2026-08-23T12:34:56+00:00", "approval"),
    ]:
        invalid = dict(runtime)
        invalid[key] = value
        with pytest.raises(OnboardingError, match=message):
            save_runtime(project_root, invalid)


def test_runtime_rejects_boolean_errors_and_review_only_approval(project_root: Path):
    initialize_project(project_root)
    runtime = load_runtime(project_root)
    runtime["onboarding"] = {"complete": 1, "creator_ready": False}
    with pytest.raises(OnboardingError, match="onboarding"):
        save_runtime(project_root, runtime)
    runtime = load_runtime(project_root)
    runtime["auto_apply_approved_at"] = "2026-08-23T12:34:56Z"
    with pytest.raises(OnboardingError, match="approval"):
        save_runtime(project_root, runtime)


def test_candidate_validation_rejects_unknown_fields_employment_mismatch_and_bad_career_evidence():
    valid = candidate()
    assert validate_candidate(valid) == valid
    for mutate, message in [
        (lambda item: item.update({"unknown": "x"}), "candidate"),
        (lambda item: item.update({"employment_status": "contractor"}), "employment_status"),
        (lambda item: item.update({"current_employer": ""}), "current_employer"),
        (lambda item: item.update({"employment_status": "not_employed"}), "current_employer"),
        (lambda item: item.update({"career_evidence": [{"id": "Bad slug", "text": "x"}]}), "career_evidence"),
        (lambda item: item.update({"career_evidence": [{"id": "same", "text": "x"}, {"id": "same", "text": "y"}]}), "career_evidence"),
    ]:
        invalid = candidate()
        mutate(invalid)
        with pytest.raises(OnboardingError, match=message):
            validate_candidate(invalid)


def test_candidate_validation_requires_identity_contact_and_strict_work_authorization_rows():
    for mutate, message in [
        (lambda item: item["identity"].update({"email": ""}), "identity.email"),
        (lambda item: item.update({"work_authorization": []}), "work_authorization"),
        (lambda item: item.update({"work_authorization": [{"countries": ["fr"], "status": "authorized"}]}), "work_authorization"),
        (lambda item: item.update({"work_authorization": [{"countries": ["FR"], "status": "maybe"}]}), "work_authorization"),
    ]:
        invalid = candidate()
        mutate(invalid)
        with pytest.raises(OnboardingError, match=message):
            validate_candidate(invalid)


def test_preference_validation_rejects_unknown_fields_and_required_target_data():
    valid = preferences()
    assert validate_preferences(valid) == valid
    for mutate, message in [
        (lambda item: item.update({"unknown": "x"}), "preferences"),
        (lambda item: item.update({"target_titles": []}), "target_titles"),
        (lambda item: item.update({"role_families": []}), "role_families"),
        (lambda item: item.update({"countries": []}), "countries"),
        (lambda item: item.update({"countries": [{"code": "FR", "priority": 2}]}), "countries"),
    ]:
        invalid = preferences()
        mutate(invalid)
        with pytest.raises(OnboardingError, match=message):
            validate_preferences(invalid)


def test_preference_validation_enforces_seniority_salary_and_vie_cross_field_rules():
    for mutate, message in [
        (lambda item: item["seniority"].update({"minimum": "senior", "maximum": "junior"}), "seniority"),
        (lambda item: item["salary"].update({"minimum": -1}), "salary.minimum"),
        (lambda item: item["salary"].update({"currency": "eur"}), "salary.currency"),
        (lambda item: item["vie"].update({"enabled": True, "eligible": False}), "vie"),
    ]:
        invalid = preferences()
        mutate(invalid)
        with pytest.raises(OnboardingError, match=message):
            validate_preferences(invalid)


def test_mark_complete_requires_creator_and_exact_target_country_coverage(project_root: Path):
    initialize_project(project_root)
    write_onboarding_inputs(project_root)
    with pytest.raises(OnboardingError, match="creator"):
        mark_onboarding_complete(project_root)
    runtime = load_runtime(project_root)
    runtime["onboarding"]["creator_ready"] = True
    save_runtime(project_root, runtime)
    candidate_data = candidate()
    candidate_data["work_authorization"].append({"countries": ["FR"], "status": "authorized"})
    (project_root / "private" / "candidate.yml").write_text(yaml.safe_dump(candidate_data), encoding="utf-8")
    with pytest.raises(OnboardingError, match="work_authorization"):
        mark_onboarding_complete(project_root)


def test_mark_complete_validates_inputs_writes_completion_and_status(project_root: Path):
    initialize_project(project_root)
    write_onboarding_inputs(project_root)
    runtime = load_runtime(project_root)
    runtime["onboarding"]["creator_ready"] = True
    save_runtime(project_root, runtime)
    completed = mark_onboarding_complete(project_root)
    assert completed["onboarding"] == {"complete": True, "creator_ready": True}
    assert onboarding_status(project_root) == {
        "ready": True,
        "requirements": {
            "master_cv": True,
            "master_profile": True,
            "candidate": True,
            "job_preferences": True,
            "creator_ready": True,
        },
    }


def test_status_becomes_not_ready_when_completed_candidate_or_preferences_are_tampered(project_root: Path):
    initialize_project(project_root)
    write_onboarding_inputs(project_root)
    runtime = load_runtime(project_root)
    runtime["onboarding"]["creator_ready"] = True
    save_runtime(project_root, runtime)
    mark_onboarding_complete(project_root)
    (project_root / "private" / "candidate.yml").write_text("schema_version: 1\n", encoding="utf-8")
    assert onboarding_status(project_root)["ready"] is False


def test_cli_status_and_validation_never_echo_private_values(project_root: Path):
    initialize_project(project_root)
    secret = "SENSITIVE_PRIVATE_VALUE"
    candidate_data = candidate()
    candidate_data["identity"]["full_name"] = secret
    (project_root / "private" / "candidate.yml").write_text(yaml.safe_dump(candidate_data), encoding="utf-8")
    command = [sys.executable, "Pipeline/onboarding.py", "--root", ".", "status"]
    status = subprocess.run(command, cwd=project_root, text=True, capture_output=True, check=True)
    assert secret not in status.stdout + status.stderr
    assert json.loads(status.stdout)["ready"] is False

    command[-1] = "validate"
    validation = subprocess.run(command, cwd=project_root, text=True, capture_output=True)
    assert validation.returncode == 2
    assert secret not in validation.stdout + validation.stderr
    assert "error:" in validation.stderr


def test_initialize_rejects_reparse_ancestor_before_writing_outside(project_root: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    documents = project_root / "Documents"
    original_documents = project_root / "Documents-original"
    assert documents.resolve().parent == project_root.resolve()
    assert original_documents.parent.resolve() == project_root.resolve()
    documents.rename(original_documents)
    try:
        documents.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if documents.exists() and not documents.is_symlink():
            raise AssertionError("Fixture must free the Documents path before linking") from error
        pytest.skip(f"symlink unavailable: {error}")
    with pytest.raises(OSError, match="confinement"):
        initialize_project(project_root)
    assert not (outside / "generated").exists()


def test_atomic_write_rejects_nested_reparse_before_outside_write(project_root: Path, tmp_path: Path):
    initialize_project(project_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = project_root / "private" / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink unavailable: {error}")
    with pytest.raises(OSError, match="confinement"):
        atomic_write_text(link / "escape.txt", "unsafe", project_root=project_root)
    assert not (outside / "escape.txt").exists()


def test_cv_registration_is_exclusive_and_cleans_lock_and_temps(project_root: Path):
    initialize_project(project_root)
    source = project_root / "source.pdf"
    payload = make_pdf(source)
    start = threading.Barrier(3)
    outcomes: list[object] = []

    def register() -> None:
        start.wait()
        try:
            outcomes.append(register_master_cv(project_root, source))
        except Exception as error:  # Test the public failure class from concurrent callers.
            outcomes.append(error)

    threads = [threading.Thread(target=register) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, OnboardingError) for outcome in outcomes) == 1
    assert (project_root / "private" / "master-cv.pdf").read_bytes() == payload
    assert not list((project_root / "private").glob("*.tmp-*"))
    assert not (project_root / "private" / ".onboarding.lock").exists()


def test_cv_registration_rolls_back_published_cv_when_runtime_write_fails(project_root: Path, monkeypatch: pytest.MonkeyPatch):
    initialize_project(project_root)
    source = project_root / "source.pdf"
    make_pdf(source)

    def fail_runtime_write(*_args: object, **_kwargs: object) -> None:
        raise OnboardingError("runtime")

    monkeypatch.setattr("Pipeline.onboarding.save_runtime", fail_runtime_write)
    with pytest.raises(OnboardingError, match="runtime"):
        register_master_cv(project_root, source)
    assert not list((project_root / "private").glob("master-cv.*"))
    assert not list((project_root / "private").glob("*.tmp-*"))
    assert not (project_root / "private" / ".onboarding.lock").exists()


def test_cv_registration_accepts_real_passive_pdf_and_docx(project_root: Path):
    initialize_project(project_root)
    pdf = project_root / "real.pdf"
    pdf_payload = make_pdf(pdf)
    assert register_master_cv(project_root, pdf)["sha256"] == hashlib.sha256(pdf_payload).hexdigest()

    second = project_root.parent / "second-project"
    second.mkdir()
    for name in ("config",):
        (second / name).mkdir()
    for source in (project_root / "config").iterdir():
        (second / "config" / source.name).write_bytes(source.read_bytes())
    initialize_project(second)
    docx = second / "real.docx"
    docx_payload = make_docx(docx)
    assert register_master_cv(second, docx)["sha256"] == hashlib.sha256(docx_payload).hexdigest()


@pytest.mark.parametrize("marker", [b"/JavaScript", b"/EmbeddedFile", b"/Launch"])
def test_cv_registration_rejects_active_pdf_markers(project_root: Path, marker: bytes):
    initialize_project(project_root)
    source = project_root / "active.pdf"
    source.write_bytes(make_pdf(project_root / "base.pdf") + marker)
    with pytest.raises(OnboardingError, match="master CV"):
        register_master_cv(project_root, source)
    assert not list((project_root / "private").glob("master-cv.*"))


def test_cv_registration_rejects_encrypted_pdf(project_root: Path):
    initialize_project(project_root)
    source = project_root / "encrypted.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "Synthetic CV")
    document.save(source, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    document.close()
    with pytest.raises(OnboardingError, match="master CV"):
        register_master_cv(project_root, source)


def test_cv_registration_rejects_compressed_page_javascript_action(project_root: Path):
    initialize_project(project_root)
    source = project_root / "compressed-action.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Synthetic CV")
    document.xref_set_key(page.xref, "AA", "<</O<</S/JavaScript/JS(app.alert('x'))>>>>")
    document.save(source, deflate=True, garbage=4, use_objstms=1)
    document.close()
    payload = source.read_bytes().lower()
    assert all(marker not in payload for marker in (b"/javascript", b"/js", b"/aa"))
    with pytest.raises(OnboardingError, match="master CV.pdf"):
        register_master_cv(project_root, source)


def test_cv_registration_accepts_passive_uri_and_mail_links(project_root: Path):
    initialize_project(project_root)
    source = project_root / "passive-links.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Synthetic CV")
    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 80, 220, 100), "uri": "mailto:synthetic@example.test"})
    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 110, 220, 130), "uri": "https://www.linkedin.com/in/synthetic"})
    document.save(source, deflate=True, use_objstms=1)
    document.close()
    assert register_master_cv(project_root, source)["path"] == "private/master-cv.pdf"


def test_cv_registration_accepts_passive_uri_with_action_like_path_segments(project_root: Path):
    initialize_project(project_root)
    source = project_root / "passive-action-like-uri.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Synthetic CV")
    page.insert_link(
        {
            "kind": fitz.LINK_URI,
            "from": fitz.Rect(72, 80, 220, 100),
            "uri": "https://example.test/js/aa/profile",
        }
    )
    document.save(source, deflate=True, use_objstms=1)
    document.close()
    assert register_master_cv(project_root, source)["path"] == "private/master-cv.pdf"


@pytest.mark.parametrize("member", ["../escape", "/absolute", "..\\escape", "word/vbaProject.bin", "word/activeX/activeX1.bin", "word/embeddings/object1.bin"])
def test_cv_registration_rejects_unsafe_docx_container_members(project_root: Path, member: str):
    initialize_project(project_root)
    source = project_root / "unsafe.docx"
    make_docx(source)
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(member, b"unsafe")
    with pytest.raises(OnboardingError, match="master CV"):
        register_master_cv(project_root, source)
    assert not list((project_root / "private").glob("master-cv.*"))


def test_cv_registration_rejects_docx_symlink_and_active_template_relationship(project_root: Path):
    initialize_project(project_root)
    symlink_container = project_root / "symlink.docx"
    make_docx(symlink_container)
    link_info = zipfile.ZipInfo("word/link")
    link_info.create_system = 3
    link_info.external_attr = (0o120777 << 16)
    with zipfile.ZipFile(symlink_container, "a") as archive:
        archive.writestr(link_info, "target")
    with pytest.raises(OnboardingError, match="master CV"):
        register_master_cv(project_root, symlink_container)

    active_template = project_root / "template.docx"
    make_docx(active_template)
    relation = b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" Target="unsafe.dotm"/></Relationships>'
    with zipfile.ZipFile(active_template, "a") as archive:
        archive.writestr("word/_rels/active.rels", relation)
    with pytest.raises(OnboardingError, match="master CV"):
        register_master_cv(project_root, active_template)


def test_cv_registration_rejects_docx_zip_bombs_and_malformed_containers(project_root: Path):
    initialize_project(project_root)
    bomb = project_root / "bomb.docx"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "x")
        archive.writestr("_rels/.rels", "x")
        archive.writestr("word/document.xml", "x")
        archive.writestr("word/huge.bin", b"0" * (MAX_PROFILE_BYTES + 1))
    with pytest.raises(OnboardingError, match="master CV"):
        register_master_cv(project_root, bomb)
    malformed = project_root / "malformed.docx"
    with zipfile.ZipFile(malformed, "w") as archive:
        archive.writestr("[Content_Types].xml", "x")
    with pytest.raises(OnboardingError, match="master CV"):
        register_master_cv(project_root, malformed)


def test_guided_profile_draft_enforces_same_byte_limit(project_root: Path):
    initialize_project(project_root)
    with pytest.raises(OnboardingError, match="master_profile"):
        write_master_profile_draft(project_root, "x" * (MAX_PROFILE_BYTES + 1))
    assert not (project_root / "private" / "master-profile.md").exists()


@pytest.mark.parametrize(
    ("factory", "path"),
    [
        (candidate, ("employment_status",)),
        (candidate, ("work_authorization", 0, "status")),
        (candidate, ("career_evidence", 0, "id")),
        (preferences, ("seniority", "minimum")),
        (preferences, ("countries", 0, "code")),
        (preferences, ("salary", "currency")),
        (preferences, ("vie", "enabled")),
    ],
)
def test_validators_convert_wrong_types_to_safe_field_errors(factory, path: tuple[object, ...]):
    value = factory()
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = []
    validator = validate_candidate if factory is candidate else validate_preferences
    with pytest.raises(OnboardingError) as error:
        validator(value)
    assert "TypeError" not in str(error.value)
    assert "KeyError" not in str(error.value)
    assert "AttributeError" not in str(error.value)


@pytest.mark.parametrize("key", ["submission_mode", "master_cv_path", "master_cv_sha256", "onboarding"])
def test_runtime_validator_converts_wrong_types_to_safe_field_errors(project_root: Path, key: str):
    initialize_project(project_root)
    runtime = load_runtime(project_root)
    runtime[key] = []
    with pytest.raises(OnboardingError) as error:
        save_runtime(project_root, runtime)
    assert str(error.value).startswith("runtime")
    assert not any(name in str(error.value) for name in ("TypeError", "KeyError", "AttributeError"))


def test_validators_reject_bounded_collections_and_text_without_leaking_values():
    invalid_candidate = candidate()
    invalid_candidate["career_evidence"] = [{"id": f"row-{index}", "text": "x"} for index in range(MAX_CAREER_ROWS + 1)]
    with pytest.raises(OnboardingError, match="career_evidence"):
        validate_candidate(invalid_candidate)
    invalid_preferences = preferences()
    invalid_preferences["target_titles"] = ["x"] * (MAX_LIST_ITEMS + 1)
    with pytest.raises(OnboardingError, match="target_titles"):
        validate_preferences(invalid_preferences)


def test_status_and_cli_fail_closed_for_malformed_runtime_without_echoing_values(project_root: Path):
    initialize_project(project_root)
    secret = "RUNTIME_PRIVATE_VALUE"
    (project_root / "private" / "runtime.json").write_text(json.dumps({"submission_mode": [secret]}), encoding="utf-8")
    assert onboarding_status(project_root)["ready"] is False
    result = subprocess.run(
        [sys.executable, "Pipeline/onboarding.py", "--root", ".", "status"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=True,
    )
    assert secret not in result.stdout + result.stderr


def test_cv_source_rejects_symlink_ancestor_before_opening_leaf(project_root: Path, tmp_path: Path):
    initialize_project(project_root)
    outside = tmp_path / "outside-source"
    outside.mkdir()
    make_pdf(outside / "source.pdf")
    linked_parent = project_root / "linked-source"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink unavailable: {error}")
    with pytest.raises(OnboardingError, match="symlink or reparse"):
        register_master_cv(project_root, linked_parent / "source.pdf")
    assert not list((project_root / "private").glob("master-cv.*"))


def test_rootless_atomic_write_rejects_symlink_ancestor_before_outside_write(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink unavailable: {error}")
    with pytest.raises(OSError, match="confinement"):
        atomic_write_text(linked_parent / "nested" / "escape.txt", "unsafe")
    assert not (outside / "nested" / "escape.txt").exists()


def test_initialize_preserves_friend_content_when_publication_race_is_lost(project_root: Path, monkeypatch: pytest.MonkeyPatch):
    from Pipeline import initialize as initialize_module

    original_publish = getattr(initialize_module, "publish_bytes_no_overwrite", None)

    def rival_publish(root: Path, destination: Path, payload: bytes) -> None:
        if destination.name == "candidate.yml":
            destination.write_bytes(b"friend-owned")
            raise FileExistsError(destination)
        assert original_publish is not None
        original_publish(root, destination, payload)

    monkeypatch.setattr(initialize_module, "publish_bytes_no_overwrite", rival_publish, raising=False)
    initialize_project(project_root)
    assert (project_root / "private" / "candidate.yml").read_bytes() == b"friend-owned"


def test_cv_registration_rejects_pdf_above_page_limit(project_root: Path):
    initialize_project(project_root)
    source = project_root / "many-pages.pdf"
    document = fitz.open()
    for _ in range(MAX_PDF_PAGES + 1):
        document.new_page().insert_text((72, 72), "Synthetic CV")
    document.save(source)
    document.close()
    with pytest.raises(OnboardingError, match="master CV.pdf"):
        register_master_cv(project_root, source)


@pytest.mark.parametrize("content_types", [b"not xml", b'<Types xmlns="urn:wrong"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>', b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>'])
def test_cv_registration_rejects_invalid_docx_content_types(project_root: Path, content_types: bytes):
    initialize_project(project_root)
    source = project_root / "bad-content-types.docx"
    make_docx(source)
    replace_zip_member(source, "[Content_Types].xml", content_types)
    with pytest.raises(OnboardingError, match="master CV.docx"):
        register_master_cv(project_root, source)


def test_guided_profile_accepts_large_valid_document_below_byte_limit(project_root: Path):
    initialize_project(project_root)
    content = "a" * 20_000
    result = write_master_profile_draft(project_root, content)
    assert result.read_text(encoding="utf-8") == content
