import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import fitz
import pytest
import yaml

import Pipeline.document_builder as document_builder
from Pipeline.cv_importer import LayoutBlock, LayoutDescriptor, PageGeometry
from Pipeline.initialize import initialize_project
from Pipeline.io_utils import atomic_write_json, exclusive_project_lock
from Pipeline.pipeline_settings import DEFAULT_SETTINGS
from Pipeline.pipeline_store import PipelineStore
from Pipeline.policy_guard import (
    PolicyError,
    application_blockers,
    assert_application_ready,
    compute_creator_content_sha256,
    disable_auto_apply,
    enable_auto_apply,
    enforce_current_employer_exclusion,
    is_current_employer_job,
    record_applied,
    work_authorization_blocker,
)


def configured_settings(**overrides):
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings.update(
        {
            "role_families": [
                {"id": "synthetic", "label": "Synthetic", "keywords": ["synthetic"], "enabled": True, "priority": 1}
            ],
            "countries": [
                {"code": "ZZ", "label": "Synthetic Country", "enabled": True, "priority": 1}
            ],
        }
    )
    settings.update(overrides)
    return settings


def candidate(**overrides):
    value = {
        "schema_version": 1,
        "identity": {
            "full_name": "Synthetic User",
            "email": "synthetic@example.test",
            "phone": "+10000000000",
            "residence_country": "ZZ",
        },
        "current_employer": "Synthetic Current Org",
        "employment_status": "employed",
        "work_authorization": [{"countries": ["ZZ"], "status": "authorized"}],
        "languages": ["English"],
        "notice_period": "30 days",
        "known_answers": {},
        "career_evidence": [{"id": "private-secret", "text": "Private experience must never identify employer."}],
    }
    value.update(overrides)
    return value


def preferences():
    return {
        "schema_version": 1,
        "target_titles": ["Synthetic Analyst"],
        "role_families": ["synthetic"],
        "industries": [],
        "seniority": {"minimum": "entry", "maximum": "mid"},
        "countries": [{"code": "ZZ", "priority": 1}],
        "work_modes": [],
        "salary": {"minimum": 0, "currency": "EUR"},
        "include_keywords": [],
        "exclude_keywords": [],
        "excluded_employers": [],
        "preferred_company_types": [],
        "vie": {"enabled": False, "eligible": False},
    }


def make_pdf(path: Path, text: str = "Synthetic CV") -> bytes:
    document = fitz.open()
    document.new_page().insert_text((72, 72), text)
    document.save(path)
    document.close()
    return path.read_bytes()


def creator_content_digest(creator_dir: Path, font_substitutions: dict[str, str]) -> str:
    entries = [
        ("template.html.j2", (creator_dir / "template.html.j2").read_bytes()),
        ("style.css", (creator_dir / "style.css").read_bytes()),
        (
            "font_substitutions.json",
            json.dumps(font_substitutions, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ),
    ]
    assets = creator_dir / "assets"
    if assets.is_dir():
        for path in sorted(
            (item for item in assets.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(assets).as_posix(),
        ):
            entries.append(("assets/" + path.relative_to(assets).as_posix(), path.read_bytes()))
    digest = hashlib.sha256()
    for name, payload in entries:
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(str(len(payload)).encode("ascii") + b"\0")
        digest.update(payload)
    return digest.hexdigest()


def job(*, project_root=None, **overrides):
    value = {
        "id": "synthetic-1",
        "title": "Synthetic Analyst",
        "company": "Different Synthetic Org",
        "country": "ZZ",
        "source_url": "https://jobs.invalid/synthetic-1",
        "status": "docs_ready",
        "score": 90,
        "detected_seniority": "entry",
        "sponsorship_status": "not_required",
        "cv": "Documents/generated/a.pdf",
        "cover_letter": "Documents/generated/a-letter.md",
        "tailoring_overlay": "private/jobs/a.yml",
    }
    value.update(overrides)
    if project_root is not None:
        from Pipeline.document_review import document_snapshot
        try:
            value["document_review"] = document_snapshot(project_root, value)
        except (OSError, ValueError, KeyError):
            pass
    return value


def confirmation(**overrides):
    value = {
        "kind": "portal_success",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "summary": "Portal displayed a visible submitted confirmation.",
        "source_url": "https://jobs.invalid/application/confirmation",
    }
    value.update(overrides)
    return value


def _install_strict_creator_binding(project_root: Path, payload: bytes, digest: str) -> None:
    creator_dir = project_root / "private/cv-creator"
    baseline_dir = creator_dir / "baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    (baseline_dir / "baseline.pdf").write_bytes(payload)
    (baseline_dir / "recreated-baseline.pdf").write_bytes(payload)
    pages = []
    blocks = []
    with fitz.open(stream=payload, filetype="pdf") as document:
        for page_number, page in enumerate(document, 1):
            pages.append(PageGeometry(page_number, page.rect.width, page.rect.height, 0, 0, 0, 0))
            for raw in page.get_text("dict").get("blocks", []):
                if raw.get("type") != 0:
                    continue
                spans = [span for line in raw.get("lines", []) for span in line.get("spans", [])]
                text = "\n".join("".join(str(span.get("text", "")) for span in line.get("spans", [])) for line in raw.get("lines", []))
                blocks.append(LayoutBlock(
                    page_number, "text", text, tuple(raw["bbox"]),
                    tuple(dict.fromkeys(str(span.get("font", "")) for span in spans)),
                    tuple(dict.fromkeys(float(span.get("size", 0)) for span in spans)),
                    tuple(dict.fromkeys(int(span.get("color", 0)) for span in spans)),
                ))
    descriptor = LayoutDescriptor(digest, "pdf", len(pages), tuple(pages), tuple(blocks), (), 0)
    atomic_write_json(
        creator_dir / "layout.json",
        document_builder._descriptor_payload_for_storage(descriptor, "private/master-cv.pdf"),
        project_root=project_root,
    )
    layout_hash = hashlib.sha256((creator_dir / "layout.json").read_bytes()).hexdigest()
    baseline_hash = hashlib.sha256(payload).hexdigest()
    mapping = document_builder._default_block_mapping(descriptor)
    mapping_hash = document_builder._mapping_sha256(mapping)
    creator_path = creator_dir / "creator.json"
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    content_hash = compute_creator_content_sha256(creator_dir, creator["font_substitutions"])
    source_metrics = document_builder._pdf_metrics(baseline_dir / "baseline.pdf")
    recreated_metrics = document_builder._pdf_metrics(baseline_dir / "recreated-baseline.pdf")
    checks = document_builder._comparison_checks(descriptor, source_metrics, recreated_metrics, {**creator, "validation": {"block_mapping": mapping}})
    assert all(checks.values())
    artifacts = document_builder._comparison_artifacts(payload, payload)
    for name, artifact in artifacts.items():
        (baseline_dir / name).write_bytes(artifact)
    creator.update({
        "automated_validation": True,
        "approved_at": "2030-01-01T00:00:00Z",
        "creator_content_sha256": content_hash,
        "approved_creator_sha256": "",
        "validation": {
            "layout_sha256": layout_hash,
            "source_baseline_sha256": baseline_hash,
            "block_mapping": mapping,
            "approved_mapping_sha256": mapping_hash,
            "checks": checks,
            "comparison": {
                "source_sha256": digest,
                "creator_content_sha256": content_hash,
                "layout_sha256": layout_hash,
                "source_baseline_sha256": baseline_hash,
                "recreated_baseline_sha256": baseline_hash,
                "checks_sha256": document_builder._checks_sha256(checks),
                "mapping_sha256": mapping_hash,
                "artifact_sha256": document_builder._artifact_manifest(artifacts),
                "completed_at": "2030-01-01T00:00:00Z",
            },
        },
    })
    atomic_write_json(creator_path, creator, project_root=project_root)
    creator["approved_creator_sha256"] = document_builder._approval_envelope_sha256(project_root, creator)
    atomic_write_json(creator_path, creator, project_root=project_root)


def ready_project(project_root: Path, *, submission_mode: str = "auto_apply", cap: int = 2, strict_creator: bool = True) -> PipelineStore:
    initialize_project(project_root)
    private = project_root / "private"
    (private / "candidate.yml").write_text(yaml.safe_dump(candidate()), encoding="utf-8")
    (private / "job-preferences.yml").write_text(yaml.safe_dump(preferences()), encoding="utf-8")
    payload = make_pdf(private / "master-cv.pdf")
    (private / "master-profile.md").write_text("# Synthetic profile\n", encoding="utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    runtime_path = private / "runtime.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime.update(
        {
            "submission_mode": submission_mode,
            "master_cv_path": "private/master-cv.pdf",
            "master_cv_sha256": digest,
            "onboarding": {"complete": True, "creator_ready": True},
            "auto_apply_approved_at": "2030-01-01T00:00:00Z" if submission_mode == "auto_apply" else None,
        }
    )
    atomic_write_json(runtime_path, runtime)
    creator_dir = private / "cv-creator"
    (creator_dir / "template.html.j2").write_text("{{ summary }}\n", encoding="utf-8")
    (creator_dir / "style.css").write_text("@page { size: A4; }\n", encoding="utf-8")
    assets = creator_dir / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "mark.png").write_bytes(b"synthetic-raster-asset")
    font_substitutions = {"Synthetic Sans": "Arial"}
    creator_hash = creator_content_digest(creator_dir, font_substitutions)
    atomic_write_json(
        creator_dir / "creator.json",
        {
            "schema_version": 1,
            "source_sha256": digest,
            "creator_version": 1,
            "template": "template.html.j2",
            "stylesheet": "style.css",
            "approved_zones": ["headline", "summary", "experience_bullets", "skills"],
            "automated_validation": True,
            "approved_at": "2030-01-01T00:00:00Z",
            "font_substitutions": font_substitutions,
            "validation": {"baseline": True},
            "creator_content_sha256": creator_hash,
            "approved_creator_sha256": creator_hash,
        },
    )
    if strict_creator:
        _install_strict_creator_binding(project_root, payload, digest)
    make_pdf(project_root / "Documents" / "generated" / "a.pdf", "Tailored synthetic CV")
    (project_root / "Documents" / "generated" / "a-letter.md").write_text("Synthetic letter\n", encoding="utf-8")
    (project_root / "private/jobs/a.yml").write_text("{}\n", encoding="utf-8")
    store = PipelineStore(project_root / "Pipeline" / "pipeline.db")
    current = store.get_settings()
    store.save_settings(configured_settings(daily_submission_cap=cap), expected_version=current["version"], reason="test setup")
    return store


@pytest.mark.parametrize(
    ("country", "rows", "sponsorship", "expected"),
    [
        ("ZZ", [{"countries": ["ZZ"], "status": "authorized"}], "unknown", None),
        ("zz", [{"countries": ["ZZ"], "status": "authorized"}], "unknown", None),
        ("AA", [{"countries": ["ZZ"], "status": "authorized"}], "unknown", "work authorization is not configured for the job country"),
        ("ZZ", [{"countries": ["ZZ"], "status": "sponsorship_required"}], "unknown", None),
        ("ZZ", [{"countries": ["ZZ"], "status": "sponsorship_required"}], "offered", None),
    ],
)
def test_work_authorization_requires_exact_country_coverage_and_honors_advert_silence(country, rows, sponsorship, expected):
    assert work_authorization_blocker(
        {"country": country, "sponsorship_status": sponsorship}, {"work_authorization": rows}
    ) == expected


@pytest.mark.parametrize(
    ("job_value", "candidate_value", "expected"),
    [
        ({}, {"work_authorization": []}, "job.country"),
        ({"country": []}, {"work_authorization": []}, "job.country"),
        ({"country": "ZZ"}, {"work_authorization": "private-value"}, "candidate.work_authorization"),
        ({"country": "ZZ"}, {"work_authorization": [{"countries": "ZZ", "status": "authorized"}]}, "candidate.work_authorization"),
        ({"country": "ZZ"}, {"work_authorization": [{"countries": ["ZZ"], "status": "private-value"}]}, "candidate.work_authorization"),
        (
            {"country": "ZZ"},
            {"work_authorization": [{"countries": ["ZZ"], "status": "authorized"}, {"countries": ["ZZ"], "status": "authorized"}]},
            "candidate.work_authorization",
        ),
    ],
)
def test_work_authorization_malformed_inputs_fail_closed_with_field_only_errors(job_value, candidate_value, expected):
    result = work_authorization_blocker(job_value, candidate_value)
    assert result == expected
    assert "private-value" not in result


def test_current_employer_uses_only_normalized_identity_fields_and_not_private_notes():
    private = candidate(current_employer="Synthetiqué Current Org")
    assert is_current_employer_job({"company": "  SYNTHETIQUE   CURRENT ORG "}, private)
    assert is_current_employer_job({"employer": "Synthétique Current Org"}, private)
    assert is_current_employer_job({"end_employer": "Synthetique Current Org"}, private)
    assert not is_current_employer_job({"company": "Other", "job_description": "Synthetique Current Org"}, private)
    assert not is_current_employer_job({"company": "Other", "career_evidence": "Synthetique Current Org"}, private)


def test_current_employer_does_not_match_substring_collisions():
    private = candidate(current_employer="Art")
    assert not is_current_employer_job({"company": "Cartography Holdings"}, private)
    assert not is_current_employer_job({"company": "Artisan Group"}, private)


def test_current_employer_exclusion_skips_non_applied_and_hard_fails_applied_without_leak():
    private = candidate(current_employer="Private Employer Secret")
    excluded = job(id="skip-me", company="private employer secret", blocker={"category": "captcha", "detail": "x"})
    safe = job(id="keep-me")
    state = {"meta": {}, "jobs": [excluded, safe]}
    assert enforce_current_employer_exclusion(state, private) == ["skip-me"]
    assert excluded["status"] == "skipped"
    assert excluded["score"] == 0
    assert "blocker" not in excluded
    applied = job(id="applied-secret", company="PRIVATE EMPLOYER SECRET", status="applied", confirmation=confirmation())
    with pytest.raises(PolicyError) as error:
        enforce_current_employer_exclusion({"meta": {}, "jobs": [applied]}, private)
    assert "Private Employer Secret" not in str(error.value)


def test_current_employer_exclusion_rejects_malformed_private_identity_field():
    with pytest.raises(PolicyError, match="candidate.current_employer"):
        enforce_current_employer_exclusion({"jobs": [job()]}, {"current_employer": ["private"]})


def test_application_blockers_require_validated_onboarding_creator_mode_cap_and_approval(project_root: Path):
    initialize_project(project_root)
    blockers = application_blockers(project_root, job(project_root=project_root), configured_settings(daily_submission_cap=0))
    assert {
        "onboarding is incomplete",
        "CV creator is not approved",
        "submission mode is review_only",
        "daily submission cap is 0",
        "candidate profile is invalid",
    }.issubset(blockers)

    ready_project(project_root)
    creator_path = project_root / "private" / "cv-creator" / "creator.json"
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    creator["automated_validation"] = False
    atomic_write_json(creator_path, creator)
    blockers = application_blockers(project_root, job(project_root=project_root), configured_settings(daily_submission_cap=2))
    assert "CV creator is not approved" in blockers


def test_creator_approval_rejects_duplicate_json_keys(project_root: Path):
    ready_project(project_root)
    creator_path = project_root / "private" / "cv-creator" / "creator.json"
    payload = creator_path.read_text(encoding="utf-8")
    creator_path.write_text(payload.replace('"schema_version": 1,', '"schema_version": 0, "schema_version": 1,', 1), encoding="utf-8")
    blockers = application_blockers(project_root, job(project_root=project_root), configured_settings(daily_submission_cap=2))
    assert "CV creator is not approved" in blockers


def test_creator_approval_rejects_minimal_legacy_record(project_root: Path):
    ready_project(project_root)
    creator_path = project_root / "private" / "cv-creator" / "creator.json"
    digest = json.loads(creator_path.read_text(encoding="utf-8"))["source_sha256"]
    atomic_write_json(
        creator_path,
        {
            "schema_version": 1,
            "source_sha256": digest,
            "automated_validation": True,
            "approved_at": "2030-01-01T00:00:00Z",
        },
    )
    blockers = application_blockers(project_root, job(project_root=project_root), configured_settings(daily_submission_cap=2))
    assert "CV creator is not approved" in blockers


def test_creator_hash_matches_task7_contract_for_non_ascii_font_names(project_root: Path):
    ready_project(project_root)
    creator_dir = project_root / "private" / "cv-creator"
    substitutions = {"Synthétique Sans": "Arial"}
    assert compute_creator_content_sha256(creator_dir, substitutions) == creator_content_digest(
        creator_dir, substitutions
    )


@pytest.mark.parametrize("mutation", ["zone", "asset_type"])
def test_creator_approval_rejects_undeclared_zone_and_unsupported_asset_type(project_root: Path, mutation: str):
    ready_project(project_root)
    creator_dir = project_root / "private" / "cv-creator"
    creator_path = creator_dir / "creator.json"
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    if mutation == "zone":
        creator["approved_zones"].append("identity")
    else:
        (creator_dir / "assets" / "mark.png").rename(creator_dir / "assets" / "mark.svg")
        digest = creator_content_digest(creator_dir, creator["font_substitutions"])
        creator["creator_content_sha256"] = digest
        creator["approved_creator_sha256"] = digest
    atomic_write_json(creator_path, creator)
    assert "CV creator is not approved" in application_blockers(
        project_root, job(project_root=project_root), configured_settings(daily_submission_cap=2)
    )


@pytest.mark.parametrize("relative_path", ["template.html.j2", "style.css", "assets/mark.png"])
def test_creator_approval_rejects_template_style_and_asset_drift(project_root: Path, relative_path: str):
    ready_project(project_root)
    path = project_root / "private" / "cv-creator" / relative_path
    path.write_bytes(path.read_bytes() + b"changed")
    assert "CV creator is not approved" in application_blockers(
        project_root, job(project_root=project_root), configured_settings(daily_submission_cap=2)
    )


def test_application_blockers_enforce_exact_status_score_and_all_unresolved_blockers(project_root: Path):
    ready_project(project_root)
    settings = configured_settings(daily_submission_cap=2, score_threshold=91)
    blockers = application_blockers(
        project_root,
        job(project_root=project_root, status="needs_user", score=90, blocker={"category": "unknown_answer", "detail": "private question"}),
        settings,
    )
    assert "job is not docs_ready" in blockers
    assert "score is below threshold" in blockers
    assert "job has unresolved unknown answer" in blockers
    assert "private question" not in "; ".join(blockers)

    captcha = application_blockers(
        project_root,
        job(project_root=project_root, blocker={"category": "captcha", "detail": "private challenge"}),
        configured_settings(daily_submission_cap=2),
    )
    assert "job has unresolved CAPTCHA" in captcha

    transient = application_blockers(
        project_root,
        job(project_root=project_root, blocker={"category": "transient_no_browser", "detail": "private browser state"}),
        configured_settings(daily_submission_cap=2),
    )
    assert "job has unresolved blocker" in transient


@pytest.mark.parametrize(
    ("field", "path", "expected"),
    [
        ("cv", "Documents/generated/missing.pdf", "generated CV is missing or invalid"),
        ("cover_letter", "Documents/generated/missing.md", "generated cover letter is missing or invalid"),
        ("cv", "private/master-cv.pdf", "generated CV path is outside Documents/generated"),
        ("cover_letter", "private/master-profile.md", "generated cover letter path is outside Documents/generated"),
        ("cv", "Documents/generated/a.txt", "generated CV has unsupported type"),
        ("cover_letter", "Documents/generated/a.exe", "generated cover letter has unsupported type"),
    ],
)
def test_application_blockers_require_safe_generated_documents(project_root: Path, field, path, expected):
    ready_project(project_root)
    blockers = application_blockers(project_root, job(project_root=project_root, **{field: path}), configured_settings(daily_submission_cap=2))
    assert expected in blockers


def test_application_blockers_reject_invalid_pdf_master_drift_and_link_ancestors(project_root: Path):
    ready_project(project_root)
    generated = project_root / "Documents" / "generated"
    (generated / "bad.pdf").write_bytes(b"%PDF-1.4\nsynthetic")
    blockers = application_blockers(project_root, job(project_root=project_root, cv="Documents/generated/bad.pdf"), configured_settings(daily_submission_cap=2))
    assert "generated CV is missing or invalid" in blockers

    (project_root / "private" / "master-cv.pdf").write_bytes(make_pdf(project_root / "changed.pdf", "Changed"))
    blockers = application_blockers(project_root, job(project_root=project_root), configured_settings(daily_submission_cap=2))
    assert "master CV hash has changed" in blockers

    if hasattr(os, "symlink"):
        ready_project(project_root)
        outside = project_root / "outside"
        outside.mkdir()
        make_pdf(outside / "linked.pdf")
        link = generated / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation unavailable")
        blockers = application_blockers(
            project_root, job(project_root=project_root, cv="Documents/generated/linked/linked.pdf"), configured_settings(daily_submission_cap=2)
        )
        assert "generated CV path is unsafe" in blockers


def test_application_blockers_reject_hardlinked_master_and_generated_documents(project_root: Path):
    ready_project(project_root)
    master = project_root / "private" / "master-cv.pdf"
    os.link(master, project_root / "private" / "master-copy.pdf")
    assert "master CV path is unsafe" in application_blockers(
        project_root, job(project_root=project_root), configured_settings(daily_submission_cap=2)
    )

    ready_project(project_root)
    generated = project_root / "Documents" / "generated" / "a.pdf"
    os.link(generated, project_root / "Documents" / "generated" / "a-copy.pdf")
    assert "generated CV path is unsafe" in application_blockers(
        project_root, job(project_root=project_root), configured_settings(daily_submission_cap=2)
    )


@pytest.mark.parametrize(
    "bad_confirmation",
    [
        None,
        {},
        confirmation(extra=True),
        confirmation(kind="unknown"),
        confirmation(observed_at="2030-01-01 00:00:00"),
        confirmation(summary=""),
        confirmation(source_url="file:///private"),
    ],
)
def test_proposed_applied_state_requires_exact_visible_confirmation(project_root: Path, bad_confirmation):
    ready_project(project_root)
    proposed = job(project_root=project_root, status="applied", confirmation=bad_confirmation)
    blockers = application_blockers(project_root, proposed, configured_settings(daily_submission_cap=2))
    assert "visible confirmation evidence is missing or invalid" in blockers
    with pytest.raises(PolicyError, match="visible confirmation"):
        assert_application_ready(project_root, proposed, configured_settings(daily_submission_cap=2))


def test_daily_count_must_remain_below_positive_canonical_cap(project_root: Path):
    store = ready_project(project_root, cap=1)
    store.upsert_job(job(project_root=project_root, id="already", source_url="https://jobs.invalid/already"))
    record_applied(project_root, "already", confirmation())
    blockers = application_blockers(project_root, job(project_root=project_root, id="next"), configured_settings(daily_submission_cap=1))
    assert "daily submission cap is reached" in blockers


def test_enable_requires_current_explicit_approval_complete_creator_and_positive_canonical_cap(project_root: Path):
    initialize_project(project_root)
    before = (project_root / "private" / "runtime.json").read_bytes()
    with pytest.raises(PolicyError, match="explicit user approval"):
        enable_auto_apply(project_root, explicit_user_approval=False)
    assert (project_root / "private" / "runtime.json").read_bytes() == before

    ready_project(project_root, submission_mode="review_only", cap=0)
    with pytest.raises(PolicyError, match="daily submission cap"):
        enable_auto_apply(project_root, explicit_user_approval=True)
    store = PipelineStore(project_root / "Pipeline" / "pipeline.db")
    current = store.get_settings()
    changed = copy.deepcopy(current["settings"])
    changed["daily_submission_cap"] = 1
    store.save_settings(changed, expected_version=current["version"], reason="explicit test choice")
    approved = enable_auto_apply(project_root, explicit_user_approval=True)
    assert approved["submission_mode"] == "auto_apply"
    assert approved["auto_apply_approved_at"].endswith("Z")


def test_disable_auto_apply_is_unconditional_idempotent(project_root: Path):
    ready_project(project_root)
    first = disable_auto_apply(project_root)
    second = disable_auto_apply(project_root)
    assert first["submission_mode"] == second["submission_mode"] == "review_only"
    assert first["auto_apply_approved_at"] is second["auto_apply_approved_at"] is None


@pytest.mark.parametrize("operation", ["enable", "disable"])
def test_runtime_policy_changes_share_exclusive_application_lock(project_root: Path, operation: str):
    ready_project(project_root, submission_mode="review_only" if operation == "enable" else "auto_apply", cap=1)
    runtime_path = project_root / "private" / "runtime.json"
    before = runtime_path.read_bytes()
    with exclusive_project_lock(project_root, name=".application-policy.lock"):
        with pytest.raises(PolicyError, match="application policy lock"):
            if operation == "enable":
                enable_auto_apply(project_root, explicit_user_approval=True)
            else:
                disable_auto_apply(project_root)
    assert runtime_path.read_bytes() == before


def test_record_applied_runs_all_gates_and_preserves_db_on_every_failure(project_root: Path):
    store = ready_project(project_root)
    store.upsert_job(job(project_root=project_root))
    before = store.load_state()
    for bad in (
        {},
        confirmation(summary=""),
        confirmation(source_url="file:///private"),
    ):
        with pytest.raises(PolicyError):
            record_applied(project_root, "synthetic-1", bad)
        assert store.load_state() == before

    (project_root / "Documents" / "generated" / "a-letter.md").unlink()
    with pytest.raises(PolicyError, match="cover letter"):
        record_applied(project_root, "synthetic-1", confirmation())
    assert store.load_state() == before


def test_record_applied_uses_atomic_store_transition_after_immediate_policy_recheck(project_root: Path, monkeypatch):
    store = ready_project(project_root)
    store.upsert_job(job(project_root=project_root))
    original = PipelineStore.record_applied
    observed = {"called": False}

    def guarded_transition(self, job_id, evidence, **kwargs):
        observed["called"] = True
        assert self.get_job(job_id)["status"] == "docs_ready"
        assert evidence["kind"] == "sent_message"
        assert callable(kwargs["readiness_check"])
        return original(self, job_id, evidence, **kwargs)

    monkeypatch.setattr(PipelineStore, "record_applied", guarded_transition)
    applied = record_applied(project_root, "synthetic-1", confirmation(kind="sent_message"))
    assert observed["called"] is True
    assert applied["status"] == "applied"
    assert applied["confirmation"]["kind"] == "sent_message"


def test_concurrent_cap_one_allows_exactly_one_application(project_root: Path):
    store = ready_project(project_root, cap=1)
    store.upsert_job(job(project_root=project_root, id="first", source_url="https://jobs.invalid/first"))
    store.upsert_job(job(project_root=project_root, id="second", source_url="https://jobs.invalid/second"))
    barrier = threading.Barrier(3)
    results: list[str] = []

    def apply(identifier: str) -> None:
        barrier.wait()
        try:
            record_applied(project_root, identifier, confirmation())
            results.append("applied")
        except PolicyError:
            results.append("blocked")

    threads = [threading.Thread(target=apply, args=(identifier,)) for identifier in ("first", "second")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(results) == ["applied", "blocked"]
    assert sum(row["status"] == "applied" for row in store.list_jobs()) == 1


@pytest.mark.parametrize("mutation", ["runtime", "cap", "job"])
def test_race_mutation_before_store_transition_blocks_without_partial_apply(project_root: Path, monkeypatch, mutation: str):
    store = ready_project(project_root, cap=1)
    store.upsert_job(job(project_root=project_root))
    original = PipelineStore.record_applied
    mutated = False

    def race(self, job_id, evidence, **kwargs):
        nonlocal mutated
        if not mutated:
            mutated = True
            if mutation == "runtime":
                runtime_path = project_root / "private" / "runtime.json"
                runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
                runtime["submission_mode"] = "review_only"
                runtime["auto_apply_approved_at"] = None
                atomic_write_json(runtime_path, runtime)
            elif mutation == "cap":
                current = store.get_settings()
                changed = copy.deepcopy(current["settings"])
                changed["daily_submission_cap"] = 0
                store.save_settings(changed, expected_version=current["version"], reason="concurrent cap change")
            else:
                changed_job = store.get_job(job_id)
                changed_job["score"] = 1
                store.upsert_job(changed_job)
        return original(self, job_id, evidence, **kwargs)

    monkeypatch.setattr(PipelineStore, "record_applied", race)
    with pytest.raises(PolicyError):
        record_applied(project_root, "synthetic-1", confirmation())
    assert store.get_job("synthetic-1")["status"] == "docs_ready"


def test_policy_cli_requires_explicit_flag_and_emits_private_safe_json(project_root: Path):
    ready_project(project_root, submission_mode="review_only", cap=1)
    missing = subprocess.run(
        [sys.executable, "Pipeline/policy_guard.py", "--root", ".", "enable-auto-apply"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing.returncode == 2
    error = json.loads(missing.stderr)
    assert error == {"error": "explicit user approval is required", "ok": False}
    enabled = subprocess.run(
        [sys.executable, "Pipeline/policy_guard.py", "--root", ".", "enable-auto-apply", "--confirm-user-approval"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert enabled.returncode == 0
    payload = json.loads(enabled.stdout)
    assert payload["ok"] is True
    assert set(payload) == {"ok", "submission_mode", "auto_apply_approved_at"}


def test_task4_rejects_legacy_creator_without_task7_layout_baseline_and_comparison_and_revokes_auto_apply(project_root: Path):
    ready_project(project_root, strict_creator=False)
    blockers = application_blockers(project_root, job(project_root=project_root), configured_settings(daily_submission_cap=2))
    runtime = json.loads((project_root / "private/runtime.json").read_text(encoding="utf-8"))
    assert "CV creator is not approved" in blockers
    assert runtime["submission_mode"] == "review_only"
    assert runtime["auto_apply_approved_at"] is None
    assert runtime["onboarding"]["creator_ready"] is False
    assert runtime["onboarding"]["creator_ready"] is False


def test_task4_enable_rejects_legacy_creator_even_when_content_hash_matches(project_root: Path):
    ready_project(project_root, submission_mode="review_only", cap=1, strict_creator=False)
    with pytest.raises(PolicyError, match="onboarding and CV creator approval"):
        enable_auto_apply(project_root, explicit_user_approval=True)
    runtime = json.loads((project_root / "private/runtime.json").read_text(encoding="utf-8"))
    assert runtime["submission_mode"] == "review_only"


def test_task4_apply_rejects_legacy_creator_without_partial_state_transition(project_root: Path):
    store = ready_project(project_root, cap=1, strict_creator=False)
    store.upsert_job(job(project_root=project_root))
    with pytest.raises(PolicyError, match="CV creator is not approved"):
        record_applied(project_root, "synthetic-1", confirmation())
    assert store.get_job("synthetic-1")["status"] == "docs_ready"


@pytest.mark.parametrize(
    "relative_path",
    [
        "private/cv-creator/layout.json",
        "private/cv-creator/baseline/baseline.pdf",
        "private/cv-creator/baseline/recreated-baseline.pdf",
        "private/cv-creator/baseline/comparison-page-001.png",
    ],
)
def test_task4_strict_binding_rejects_layout_baseline_or_comparison_drift_and_revokes(project_root: Path, relative_path: str):
    ready_project(project_root)
    path = project_root / relative_path
    path.write_bytes(path.read_bytes() + b"drift")
    blockers = application_blockers(project_root, job(project_root=project_root), configured_settings(daily_submission_cap=2))
    runtime = json.loads((project_root / "private/runtime.json").read_text(encoding="utf-8"))
    assert "CV creator is not approved" in blockers
    assert runtime["submission_mode"] == "review_only"
    assert runtime["auto_apply_approved_at"] is None


def test_task4_routine_creator_gate_uses_only_stable_envelope_hashes(project_root: Path, monkeypatch, record_property):
    store = ready_project(project_root)
    monkeypatch.setattr(document_builder, "_pdf_metrics", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("PDF reparse reached")))
    monkeypatch.setattr(document_builder, "_comparison_checks", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("geometry comparator reached")))
    monkeypatch.setattr(document_builder, "_comparison_artifacts", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("PNG regeneration reached")))
    started = time.perf_counter()
    blockers = application_blockers(project_root, store.get_job("synthetic-1") or job(project_root=project_root), store.get_settings()["settings"])
    record_property("task4_creator_gate_seconds", time.perf_counter() - started)
    assert "CV creator is not approved" not in blockers


@pytest.mark.parametrize("mutation", ["mapping", "recreated_artifacts"])
def test_task4_envelope_rejects_self_consistent_mutable_forgery_and_revokes(project_root: Path, mutation: str):
    ready_project(project_root)
    creator_path = project_root / "private/cv-creator/creator.json"
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    if mutation == "mapping":
        row = creator["validation"]["block_mapping"][0]
        row.update({"semantic_role": "summary", "zone": "summary", "immutable": False})
        mapping_hash = document_builder._mapping_sha256(creator["validation"]["block_mapping"])
        creator["validation"]["approved_mapping_sha256"] = mapping_hash
        creator["validation"]["comparison"]["mapping_sha256"] = mapping_hash
    else:
        baseline = creator_path.parent / "baseline"
        recreated_path = baseline / "recreated-baseline.pdf"
        with fitz.open(recreated_path) as document:
            document.set_metadata({"producer": "synthetic replacement"})
            replacement = document.tobytes()
        recreated_path.write_bytes(replacement)
        artifacts = document_builder._comparison_artifacts((baseline / "baseline.pdf").read_bytes(), replacement)
        for name, payload in artifacts.items():
            (baseline / name).write_bytes(payload)
        creator["validation"]["comparison"]["recreated_baseline_sha256"] = hashlib.sha256(replacement).hexdigest()
        creator["validation"]["comparison"]["artifact_sha256"] = document_builder._artifact_manifest(artifacts)
    atomic_write_json(creator_path, creator, project_root=project_root)
    blockers = application_blockers(project_root, job(project_root=project_root), configured_settings(daily_submission_cap=2))
    runtime = json.loads((project_root / "private/runtime.json").read_text(encoding="utf-8"))
    assert "CV creator is not approved" in blockers
    assert runtime["onboarding"]["creator_ready"] is False
    assert runtime["submission_mode"] == "review_only"
    assert runtime["auto_apply_approved_at"] is None
