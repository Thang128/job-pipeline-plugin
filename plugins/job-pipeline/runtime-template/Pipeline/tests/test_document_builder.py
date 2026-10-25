from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz
import pytest
import yaml
from PIL import Image

import Pipeline.document_builder as builder
from Pipeline.cv_importer import LayoutBlock, LayoutDescriptor, PageGeometry
from Pipeline.document_builder import (
    CREATOR_KEYS,
    ComparisonReport,
    DocumentBuildError,
    approve_creator,
    bootstrap_creator,
    check_creator_drift,
    compare_baseline,
    compute_creator_content_sha256,
    render_cv,
)
from Pipeline.initialize import initialize_project
from Pipeline.io_utils import atomic_write_json, exclusive_project_lock
from Pipeline.onboarding import load_runtime
from Pipeline.policy_guard import compute_creator_content_sha256 as policy_creator_hash


CHECK_NAMES = {
    "page_count", "page_dimensions", "source_text", "unexpected_text", "section_order",
    "margins_columns", "font_substitutions", "image_count", "clipping", "overlap",
    "overflow", "density", "headline", "typography", "colors", "spacing", "bullets",
    "rules", "asset_placement",
}


def _make_pdf(path: Path, *, pages: int = 1, shifted_second: bool = False, image: bool = False) -> bytes:
    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=595, height=842)
        x = 98 if shifted_second and index == 1 else 48
        page.insert_text((x, 60), f"SYNTHETIC CV {index + 1}", fontsize=18, color=(0.1, 0.2, 0.3))
        page.insert_text((x, 100), "EXPERIENCE", fontsize=11)
        page.insert_text((x, 122), "Verified synthetic statement 12 percent", fontsize=9)
        page.draw_line((x, 132), (x + 180, 132), color=(0.2, 0.2, 0.2), width=1)
        if image:
            from io import BytesIO

            raster = Image.new("RGB", (10, 10), (30, 80, 120))
            buffer = BytesIO()
            raster.save(buffer, format="PNG")
            page.insert_image(fitz.Rect(x, 150, x + 24, 174), stream=buffer.getvalue())
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    document.close()
    return path.read_bytes()


def _descriptor_for(pdf_path: Path) -> LayoutDescriptor:
    payload = pdf_path.read_bytes()
    blocks: list[LayoutBlock] = []
    pages: list[PageGeometry] = []
    image_count = 0
    with fitz.open(pdf_path) as document:
        for page_number, page in enumerate(document, 1):
            pages.append(PageGeometry(page_number, page.rect.width, page.rect.height, 48, 48, 48, 48))
            raw = page.get_text("dict")
            for block in raw.get("blocks", []):
                if block.get("type") == 1:
                    blocks.append(LayoutBlock(page_number, "image", "", tuple(block["bbox"]), (), (), ()))
                    image_count += 1
                    continue
                if block.get("type") != 0:
                    continue
                spans = [span for line in block.get("lines", []) for span in line.get("spans", [])]
                text = "\n".join("".join(span.get("text", "") for span in line.get("spans", [])) for line in block.get("lines", []))
                blocks.append(LayoutBlock(
                    page_number, "text", text, tuple(block["bbox"]),
                    tuple(dict.fromkeys(str(span.get("font", "sans-serif")) for span in spans)),
                    tuple(dict.fromkeys(float(span.get("size", 10)) for span in spans)),
                    tuple(dict.fromkeys(int(span.get("color", 0)) for span in spans)),
                ))
    return LayoutDescriptor(
        hashlib.sha256(payload).hexdigest(), "pdf", len(pages), tuple(pages), tuple(blocks),
        ("experience",), image_count,
    )


def _write_layout_bundle(project_root: Path, *, pages: int = 1, image: bool = False) -> LayoutDescriptor:
    initialize_project(project_root)
    private = project_root / "private"
    source_path = private / "master-cv.pdf"
    payload = _make_pdf(source_path, pages=pages, image=image)
    descriptor = _descriptor_for(source_path)
    runtime = load_runtime(project_root)
    runtime.update({
        "master_cv_path": "private/master-cv.pdf",
        "master_cv_sha256": hashlib.sha256(payload).hexdigest(),
        "onboarding": {"complete": False, "creator_ready": False},
    })
    atomic_write_json(private / "runtime.json", runtime, project_root=project_root)
    creator_dir = private / "cv-creator"
    baseline_dir = creator_dir / "baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    (baseline_dir / "baseline.pdf").write_bytes(payload)
    with fitz.open(stream=payload, filetype="pdf") as document:
        for page_number, page in enumerate(document, 1):
            page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(baseline_dir / f"page-{page_number:03d}.png")
    atomic_write_json(
        creator_dir / "layout.json",
        builder._descriptor_payload_for_storage(descriptor, "private/master-cv.pdf"),
        project_root=project_root,
    )
    return descriptor


def _configure_creator_for_synthetic_render(project_root: Path, descriptor: LayoutDescriptor, *, summary_slot: bool = False) -> None:
    creator_path = project_root / "private/cv-creator/creator.json"
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    creator["font_substitutions"] = {
        name: "Arial" for block in descriptor.blocks for name in block.font_names if name
    }
    if summary_slot:
        mapping = creator["validation"]["block_mapping"]
        target_index = max(index for index, block in enumerate(descriptor.blocks) if block.block_type == "text" and block.text.strip())
        text_index = sum(1 for block in descriptor.blocks[: target_index + 1] if block.block_type == "text" and block.text.strip()) - 1
        mapping[text_index]["semantic_role"] = "summary"
        mapping[text_index]["zone"] = "summary"
        mapping[text_index]["immutable"] = False
    creator["creator_content_sha256"] = compute_creator_content_sha256(creator_path.parent, creator["font_substitutions"])
    atomic_write_json(creator_path, creator, project_root=project_root)


def _approve_without_render(project_root: Path, descriptor: LayoutDescriptor) -> Path:
    creator_path = bootstrap_creator(project_root, descriptor)
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    creator["font_substitutions"] = {
        name: "Arial" for block in descriptor.blocks for name in block.font_names if name
    }
    mapping = creator["validation"]["block_mapping"]
    if mapping:
        mapping[-1]["semantic_role"] = "summary"
        mapping[-1]["zone"] = "summary"
        mapping[-1]["immutable"] = False
    creator_dir = creator_path.parent
    content_hash = compute_creator_content_sha256(creator_dir, creator["font_substitutions"])
    layout_hash = hashlib.sha256((creator_dir / "layout.json").read_bytes()).hexdigest()
    baseline_hash = hashlib.sha256((creator_dir / "baseline/baseline.pdf").read_bytes()).hexdigest()
    recreated = creator_dir / "baseline/recreated-baseline.pdf"
    recreated.write_bytes((creator_dir / "baseline/baseline.pdf").read_bytes())
    recreated_hash = hashlib.sha256(recreated.read_bytes()).hexdigest()
    checks = builder._comparison_checks(
        descriptor,
        builder._pdf_metrics(creator_dir / "baseline/baseline.pdf"),
        builder._pdf_metrics(recreated),
        creator,
    )
    assert all(checks.values())
    artifacts = builder._comparison_artifacts((creator_dir / "baseline/baseline.pdf").read_bytes(), recreated.read_bytes())
    for name, payload in artifacts.items():
        (creator_dir / "baseline" / name).write_bytes(payload)
    mapping_hash = builder._mapping_sha256(mapping)
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
            "comparison": {
                "source_sha256": descriptor.source_sha256,
                "creator_content_sha256": content_hash,
                "layout_sha256": layout_hash,
                "source_baseline_sha256": baseline_hash,
                "recreated_baseline_sha256": recreated_hash,
                "checks_sha256": builder._checks_sha256(checks),
                "mapping_sha256": mapping_hash,
                "artifact_sha256": builder._artifact_manifest(artifacts),
                "completed_at": "2030-01-01T00:00:00Z",
            },
            "checks": checks,
        },
    })
    atomic_write_json(creator_path, creator, project_root=project_root)
    creator["approved_creator_sha256"] = builder._approval_envelope_sha256(project_root, creator)
    atomic_write_json(creator_path, creator, project_root=project_root)
    runtime = load_runtime(project_root)
    runtime["onboarding"]["complete"] = True
    runtime["onboarding"]["creator_ready"] = True
    atomic_write_json(project_root / "private/runtime.json", runtime, project_root=project_root)
    (project_root / "private/master-profile.md").write_text("# Synthetic profile\n", encoding="utf-8")
    return creator_path


def _refresh_creator_binding(project_root: Path) -> None:
    creator_path = project_root / "private/cv-creator/creator.json"
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    content_hash = compute_creator_content_sha256(creator_path.parent, creator["font_substitutions"])
    creator["creator_content_sha256"] = content_hash
    creator["validation"]["comparison"]["creator_content_sha256"] = content_hash
    atomic_write_json(creator_path, creator, project_root=project_root)
    creator["approved_creator_sha256"] = builder._approval_envelope_sha256(project_root, creator)
    atomic_write_json(creator_path, creator, project_root=project_root)


def _write_candidate_and_overlay(project_root: Path, overlay: object) -> Path:
    candidate_path = project_root / "private/candidate.yml"
    candidate = {
        "schema_version": 1,
        "identity": {"full_name": "Synthetic Candidate", "email": "candidate@example.invalid", "phone": "00000000", "residence_country": "ZZ"},
        "current_employer": "Synthetic Current Org",
        "employment_status": "employed",
        "work_authorization": [{"countries": ["ZZ"], "status": "authorized"}],
        "languages": ["Synthetic"],
        "notice_period": "Synthetic notice",
        "known_answers": {},
        "career_evidence": [
            {"id": "metric-1", "text": "Verified synthetic statement improved 12 percent during March 2030"},
            {"id": "skill-1", "text": "Used synthetic analysis safely"},
        ],
    }
    candidate_path.write_text(yaml.safe_dump(candidate, sort_keys=False), encoding="utf-8")
    overlay_path = project_root / "private/jobs/synthetic.yml"
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")
    return overlay_path


@pytest.fixture
def ready_project(project_root: Path) -> tuple[Path, Path]:
    descriptor = _write_layout_bundle(project_root)
    _approve_without_render(project_root, descriptor)
    overlay = _write_candidate_and_overlay(
        project_root,
        {"summary": {"text": "Improved 12 percent during March 2030", "evidence_ids": ["metric-1"]}},
    )
    return project_root, overlay


def test_bootstrap_writes_exact_private_creator_schema_and_keeps_shared_template_neutral(project_root: Path):
    descriptor = _write_layout_bundle(project_root)
    shared_template = project_root / "Documents/templates/cv.html.j2"
    before = shared_template.read_bytes()
    creator_path = bootstrap_creator(project_root, descriptor)
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    assert set(creator) == CREATOR_KEYS
    assert creator["approved_zones"] == ["headline", "summary", "experience_bullets", "skills"]
    assert creator["automated_validation"] is False
    assert creator["approved_at"] is None
    assert creator_path == project_root / "private/cv-creator/creator.json"
    assert shared_template.read_bytes() == before
    assert b"SYNTHETIC CV" not in before


def test_creator_hash_is_task4_implementation_not_weaker_duplicate(project_root: Path):
    descriptor = _write_layout_bundle(project_root)
    creator_path = bootstrap_creator(project_root, descriptor)
    substitutions = json.loads(creator_path.read_text(encoding="utf-8"))["font_substitutions"]
    assert compute_creator_content_sha256 is policy_creator_hash
    assert compute_creator_content_sha256(creator_path.parent, substitutions) == policy_creator_hash(creator_path.parent, substitutions)


@pytest.mark.parametrize("target", ["private", "private/cv-creator", "Documents/templates/cv.html.j2"])
def test_bootstrap_rejects_link_like_path_components(project_root: Path, tmp_path: Path, target: str):
    descriptor = _write_layout_bundle(project_root)
    path = project_root / target
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    if path.is_file():
        path.unlink()
        outside_file = outside / path.name
        outside_file.write_text("neutral", encoding="utf-8")
        link_target = outside_file
    else:
        if path.exists():
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_file(): child.unlink()
                elif child.is_dir(): child.rmdir()
            path.rmdir()
        link_target = outside
    try:
        path.symlink_to(link_target, target_is_directory=link_target.is_dir())
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(DocumentBuildError, match="unsafe"):
        bootstrap_creator(project_root, descriptor)


def test_bootstrap_rejects_hardlinked_layout(project_root: Path):
    descriptor = _write_layout_bundle(project_root)
    layout = project_root / "private/cv-creator/layout.json"
    os.link(layout, layout.with_name("layout-copy.json"))
    with pytest.raises(DocumentBuildError, match="unsafe"):
        bootstrap_creator(project_root, descriptor)


def test_bootstrap_rolls_back_owned_files_when_creator_commit_fails(project_root: Path, monkeypatch):
    descriptor = _write_layout_bundle(project_root, image=True)
    real_write = builder.atomic_write_json

    def fail_creator(path: Path, value: object, project_root: Path | None = None):
        if Path(path).name == "creator.json":
            raise OSError("synthetic commit failure")
        return real_write(path, value, project_root=project_root)

    monkeypatch.setattr(builder, "atomic_write_json", fail_creator)
    with pytest.raises(DocumentBuildError, match="bootstrap"):
        bootstrap_creator(project_root, descriptor)
    creator_dir = project_root / "private/cv-creator"
    assert not (creator_dir / "creator.json").exists()
    assert not (creator_dir / "template.html.j2").exists()
    assert not (creator_dir / "style.css").exists()
    assert not list((creator_dir / "assets").glob("*"))


@pytest.mark.parametrize("payload", [
    "{% include 'outside' %}", "{% extends 'outside' %}", "{% import 'outside' as x %}",
    "{{ cycler.__init__.__globals__ }}", "{{ range(3) }}",
])
def test_render_rejects_template_composition_calls_and_unsafe_attributes(ready_project, payload: str):
    project_root, overlay = ready_project
    creator_path = project_root / "private/cv-creator/creator.json"
    (creator_path.parent / "template.html.j2").write_text(payload, encoding="utf-8")
    _refresh_creator_binding(project_root)
    with pytest.raises(DocumentBuildError, match="template is unsafe"):
        render_cv(project_root, overlay, project_root / "Documents/generated/out.pdf")


@pytest.mark.parametrize("payload", [
    "@import 'x.css';", "body{background:url(https://example.invalid/x)}",
    "body{background:url(file:///x)}", "body{behavior:url(x.htc)}", "body{-moz-binding:url(x)}",
])
def test_render_rejects_css_resource_and_active_content(ready_project, payload: str):
    project_root, overlay = ready_project
    style = project_root / "private/cv-creator/style.css"
    style.write_text(payload, encoding="utf-8")
    _refresh_creator_binding(project_root)
    with pytest.raises(DocumentBuildError, match="stylesheet is unsafe"):
        render_cv(project_root, overlay, project_root / "Documents/generated/out.pdf")


@pytest.mark.parametrize(("raw", "message"), [
    ("summary: &x {text: safe, evidence_ids: [metric-1]}\nheadline: *x\n", "aliases"),
    ("summary: {text: first, evidence_ids: [metric-1]}\nsummary: {text: second, evidence_ids: [metric-1]}\n", "duplicate"),
    ("summary: {text: .nan, evidence_ids: [metric-1]}\n", "finite"),
    ("identity: {text: blocked, evidence_ids: [metric-1]}\n", "approved zones"),
    ("summary: {text: \"bad\\u0001text\", evidence_ids: [metric-1]}\n", "control"),
])
def test_overlay_yaml_fails_closed_for_unsafe_structures(ready_project, raw: str, message: str):
    project_root, overlay = ready_project
    overlay.write_text(raw, encoding="utf-8")
    with pytest.raises(DocumentBuildError, match=message):
        render_cv(project_root, overlay, project_root / "Documents/generated/out.pdf")


def test_overlay_rejects_oversize_and_deep_data(ready_project):
    project_root, overlay = ready_project
    overlay.write_text("summary:\n  text: \"" + "x" * (builder.MAX_OVERLAY_BYTES + 1) + "\"\n  evidence_ids: [metric-1]\n", encoding="utf-8")
    with pytest.raises(DocumentBuildError, match="too large"):
        render_cv(project_root, overlay, project_root / "Documents/generated/out.pdf")
    overlay.write_text("summary:\n  text: safe\n  evidence_ids:\n" + "    - " * 20 + "metric-1\n", encoding="utf-8")
    with pytest.raises(DocumentBuildError):
        render_cv(project_root, overlay, project_root / "Documents/generated/out.pdf")


@pytest.mark.parametrize(("claim", "message"), [
    ({"text": "Unverified claim", "evidence_ids": []}, "evidence_ids"),
    ({"text": "Unverified claim", "evidence_ids": ["missing"]}, "does not resolve"),
    ({"text": "Unverified claim", "evidence_ids": ["skill-1", "skill-1"]}, "unique"),
    ({"text": "Improved 999 percent", "evidence_ids": ["metric-1"]}, "figure '999'"),
    ({"text": "Improved during April 2030", "evidence_ids": ["metric-1"]}, "date token 'april'"),
])
def test_overlay_claims_require_unique_resolved_evidence_and_matching_numbers_dates(ready_project, claim, message):
    project_root, overlay = ready_project
    overlay.write_text(yaml.safe_dump({"summary": claim}), encoding="utf-8")
    with pytest.raises(DocumentBuildError, match=message):
        render_cv(project_root, overlay, project_root / "Documents/generated/out.pdf")


def _fake_backend(html: str, output: Path) -> str:
    import html as html_module
    import re

    text = html_module.unescape(re.sub(r"<[^>]+>", " ", html))
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    y = 50
    for line in [part.strip() for part in text.splitlines() if part.strip()][:40]:
        page.insert_text((48, y), line[:120], fontsize=8)
        y += 12
    document.save(output)
    document.close()
    return "test"


def _faithful_test_backend(html: str, output: Path) -> str:
    from weasyprint import HTML

    HTML(string=html, url_fetcher=builder._self_contained_url_fetcher).write_pdf(output)
    return "test-weasyprint"


def test_render_escapes_overlay_and_marks_qualitative_claim_for_visible_review(ready_project, monkeypatch):
    project_root, overlay = ready_project
    overlay.write_text(yaml.safe_dump({"summary": {"text": "Used <script>alert(x)</script> safely", "evidence_ids": ["skill-1"]}}), encoding="utf-8")
    captured: dict[str, str] = {}

    def fake(html: str, output: Path) -> str:
        captured["html"] = html
        return _faithful_test_backend(html, output)

    monkeypatch.setattr(builder, "_render_pdf_backend", fake)
    report = render_cv(project_root, overlay, project_root / "Documents/generated/out.pdf")
    assert report.ok is True
    assert "<script>alert(x)</script>" not in captured["html"]
    assert "&lt;script&gt;" in captured["html"]
    assert any(issue.severity == "review_required" and issue.code == "semantic_review" for issue in report.issues)


@pytest.mark.parametrize("output", [
    "outside.pdf", "Documents/out.pdf", "Documents/generated/../escape.pdf", "Documents/generated/out.txt",
])
def test_render_confines_pdf_output(ready_project, output: str):
    root, overlay = ready_project
    with pytest.raises(DocumentBuildError, match="output"):
        render_cv(root, overlay, root / output)


def test_renderer_failure_never_publishes_pdf_or_preview(ready_project, monkeypatch):
    project_root, overlay = ready_project
    monkeypatch.setattr(builder, "_render_pdf_backend", lambda *_: (_ for _ in ()).throw(DocumentBuildError("renderer unavailable")))
    output = project_root / "Documents/generated/out.pdf"
    with pytest.raises(DocumentBuildError, match="renderer unavailable"):
        render_cv(project_root, overlay, output)
    assert not output.exists()
    assert not list((project_root / "Documents/generated").rglob("*.png"))


def test_render_rejects_existing_and_hardlinked_output(ready_project, monkeypatch):
    project_root, overlay = ready_project
    monkeypatch.setattr(builder, "_render_pdf_backend", _fake_backend)
    generated = project_root / "Documents/generated"
    existing = generated / "existing.pdf"
    existing.write_bytes(b"preserve")
    with pytest.raises(DocumentBuildError, match="already exists"):
        render_cv(project_root, overlay, existing)
    hardlinked = generated / "hardlinked.pdf"
    os.link(existing, hardlinked)
    with pytest.raises(DocumentBuildError, match="already exists"):
        render_cv(project_root, overlay, hardlinked)


def test_real_weasyprint_recreates_two_page_source_and_side_by_side_images(project_root: Path):
    descriptor = _write_layout_bundle(project_root, pages=2)
    bootstrap_creator(project_root, descriptor)
    _configure_creator_for_synthetic_render(project_root, descriptor)
    report = compare_baseline(project_root)
    assert isinstance(report, ComparisonReport)
    assert report.ok is True
    assert set(report.checks) == CHECK_NAMES
    assert all(report.checks.values())
    assert len(report.source_pages) == len(report.baseline_pages) == len(report.side_by_side_pages) == 2
    assert all(path.parent == project_root / "private/cv-creator/baseline" for path in report.side_by_side_pages)
    for path in report.source_pages + report.baseline_pages + report.side_by_side_pages:
        with Image.open(path) as image:
            assert image.width <= builder.MAX_PREVIEW_WIDTH
            assert image.height <= builder.MAX_PREVIEW_HEIGHT


def test_compare_detects_later_page_geometry_drift(project_root: Path, monkeypatch):
    descriptor = _write_layout_bundle(project_root, pages=2)
    bootstrap_creator(project_root, descriptor)
    _configure_creator_for_synthetic_render(project_root, descriptor)

    def shifted(_root: Path, destination: Path) -> str:
        _make_pdf(destination, pages=2, shifted_second=True)
        return "test"

    monkeypatch.setattr(builder, "_render_private_baseline", shifted)
    report = compare_baseline(project_root)
    assert report.ok is False
    assert report.checks["margins_columns"] is False or report.checks["spacing"] is False


def test_compare_rejects_image_loss_and_clears_approval(project_root: Path, monkeypatch):
    descriptor = _write_layout_bundle(project_root, image=True)
    creator_path = bootstrap_creator(project_root, descriptor)
    _configure_creator_for_synthetic_render(project_root, descriptor)
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    creator["approved_at"] = "2030-01-01T00:00:00Z"
    creator["approved_creator_sha256"] = creator["creator_content_sha256"]
    atomic_write_json(creator_path, creator, project_root=project_root)
    runtime = load_runtime(project_root)
    runtime.update({"submission_mode": "auto_apply", "auto_apply_approved_at": "2030-01-01T00:00:00Z"})
    runtime["onboarding"]["creator_ready"] = True
    atomic_write_json(project_root / "private/runtime.json", runtime, project_root=project_root)

    def no_image(_root: Path, destination: Path) -> str:
        _make_pdf(destination)
        return "test"

    monkeypatch.setattr(builder, "_render_private_baseline", no_image)
    report = compare_baseline(project_root)
    updated = json.loads(creator_path.read_text(encoding="utf-8"))
    updated_runtime = load_runtime(project_root)
    assert report.checks["image_count"] is False
    assert updated["approved_at"] is None
    assert updated["approved_creator_sha256"] == ""
    assert updated_runtime["onboarding"]["creator_ready"] is False
    assert updated_runtime["submission_mode"] == "review_only"


def test_real_compare_reproduces_raster_asset_and_placement(project_root: Path):
    descriptor = _write_layout_bundle(project_root, image=True)
    bootstrap_creator(project_root, descriptor)
    _configure_creator_for_synthetic_render(project_root, descriptor)
    report = compare_baseline(project_root)
    assert report.ok is True
    assert report.checks["image_count"] is True
    assert report.checks["asset_placement"] is True


def test_approval_requires_current_comparison_exact_hash_and_explicit_flag(project_root: Path):
    descriptor = _write_layout_bundle(project_root)
    bootstrap_creator(project_root, descriptor)
    _configure_creator_for_synthetic_render(project_root, descriptor)
    compare_baseline(project_root)
    with pytest.raises(DocumentBuildError, match="explicit user approval"):
        approve_creator(project_root, descriptor.source_sha256, explicit_user_approval=False)
    with pytest.raises(DocumentBuildError, match="source hash"):
        approve_creator(project_root, "0" * 64, explicit_user_approval=True)
    approved = approve_creator(project_root, descriptor.source_sha256, explicit_user_approval=True)
    assert approved["approved_at"].endswith("Z")
    assert approved["approved_creator_sha256"] == builder._approval_envelope_sha256(project_root, approved)
    assert approved["approved_creator_sha256"] != approved["creator_content_sha256"]
    assert load_runtime(project_root)["onboarding"]["creator_ready"] is True


def test_approval_rejects_forged_or_stale_comparison(project_root: Path):
    descriptor = _write_layout_bundle(project_root)
    creator_path = _approve_without_render(project_root, descriptor)
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    creator["approved_at"] = None
    creator["approved_creator_sha256"] = ""
    creator["validation"]["comparison"]["checks_sha256"] = "0" * 64
    atomic_write_json(creator_path, creator, project_root=project_root)
    runtime = load_runtime(project_root)
    runtime["onboarding"]["creator_ready"] = False
    atomic_write_json(project_root / "private/runtime.json", runtime, project_root=project_root)
    with pytest.raises(DocumentBuildError, match="comparison"):
        approve_creator(project_root, descriptor.source_sha256, explicit_user_approval=True)


def test_approval_rechecks_binding_after_creator_write_before_readiness(project_root: Path, monkeypatch):
    descriptor = _write_layout_bundle(project_root)
    bootstrap_creator(project_root, descriptor)
    _configure_creator_for_synthetic_render(project_root, descriptor)
    compare_baseline(project_root)
    real_write = builder.atomic_write_json
    raced = False

    def racing_write(path: Path, value: object, project_root: Path | None = None):
        nonlocal raced
        result = real_write(path, value, project_root=project_root)
        if Path(path).name == "creator.json" and not raced:
            raced = True
            style = Path(project_root) / "private/cv-creator/style.css"
            style.write_text(style.read_text(encoding="utf-8") + "\nchanged", encoding="utf-8")
        return result

    monkeypatch.setattr(builder, "atomic_write_json", racing_write)
    with pytest.raises(DocumentBuildError, match="changed during approval"):
        approve_creator(project_root, descriptor.source_sha256, explicit_user_approval=True)
    assert load_runtime(project_root)["onboarding"]["creator_ready"] is False


@pytest.mark.parametrize("mutation", ["source", "template", "style", "asset", "layout", "baseline"])
def test_drift_revokes_creator_and_auto_apply(project_root: Path, mutation: str):
    descriptor = _write_layout_bundle(project_root)
    creator_path = _approve_without_render(project_root, descriptor)
    creator_dir = creator_path.parent
    runtime = load_runtime(project_root)
    runtime.update({"submission_mode": "auto_apply", "auto_apply_approved_at": "2030-01-01T00:00:00Z"})
    atomic_write_json(project_root / "private/runtime.json", runtime, project_root=project_root)
    if mutation == "source": (project_root / "private/master-cv.pdf").write_bytes(b"changed")
    elif mutation == "template": (creator_dir / "template.html.j2").write_text("changed", encoding="utf-8")
    elif mutation == "style": (creator_dir / "style.css").write_text("changed", encoding="utf-8")
    elif mutation == "asset":
        assets = creator_dir / "assets"; assets.mkdir(exist_ok=True); (assets / "new.png").write_bytes(b"changed")
    elif mutation == "layout": (creator_dir / "layout.json").write_bytes((creator_dir / "layout.json").read_bytes() + b" ")
    else: (creator_dir / "baseline/baseline.pdf").write_bytes(b"changed")
    assert check_creator_drift(project_root) is True
    updated = json.loads(creator_path.read_text(encoding="utf-8"))
    updated_runtime = load_runtime(project_root)
    assert updated["approved_at"] is None and updated["approved_creator_sha256"] == ""
    assert updated_runtime["onboarding"]["creator_ready"] is False
    assert updated_runtime["submission_mode"] == "review_only"
    assert updated_runtime["auto_apply_approved_at"] is None


def test_approval_and_build_share_application_policy_lock(ready_project):
    root, overlay = ready_project
    descriptor = _descriptor_for(root / "private/master-cv.pdf")
    creator_path = root / "private/cv-creator/creator.json"
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    creator["approved_at"] = None
    creator["approved_creator_sha256"] = ""
    atomic_write_json(creator_path, creator, project_root=root)
    with exclusive_project_lock(root, name=".application-policy.lock"):
        with pytest.raises(DocumentBuildError, match="lock"):
            approve_creator(root, descriptor.source_sha256, explicit_user_approval=True)
        with pytest.raises(DocumentBuildError, match="lock"):
            render_cv(root, overlay, root / "Documents/generated/out.pdf")


def test_concurrent_approvals_allow_one_lock_owner_only(ready_project, monkeypatch):
    project_root, _ = ready_project
    source_sha256 = _descriptor_for(project_root / "private/master-cv.pdf").source_sha256
    entered = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    first = True
    real_load = builder._load_creator

    def slow_load(root: Path):
        nonlocal first
        with guard:
            slow = first
            first = False
        if slow:
            entered.set()
            assert release.wait(10)
        return real_load(root)

    def attempt() -> str:
        try:
            approve_creator(project_root, source_sha256, explicit_user_approval=True)
            return "approved"
        except DocumentBuildError:
            return "locked"

    monkeypatch.setattr(builder, "_load_creator", slow_load)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(attempt)
        assert entered.wait(10)
        second_future = pool.submit(attempt)
        second_result = second_future.result(timeout=10)
        release.set()
        first_result = first_future.result(timeout=10)
    assert sorted([first_result, second_result]) == ["approved", "locked"]


def test_build_rechecks_creator_after_renderer_before_publication(ready_project, monkeypatch):
    project_root, overlay = ready_project

    def racing_backend(html_source: str, output: Path) -> str:
        result = _faithful_test_backend(html_source, output)
        style = project_root / "private/cv-creator/style.css"
        style.write_text(style.read_text(encoding="utf-8") + "\nchanged", encoding="utf-8")
        return result

    monkeypatch.setattr(builder, "_render_pdf_backend", racing_backend)
    output = project_root / "Documents/generated/race.pdf"
    with pytest.raises(DocumentBuildError, match="changed during build"):
        render_cv(project_root, overlay, output)
    assert not output.exists()
    assert load_runtime(project_root)["onboarding"]["creator_ready"] is False


def test_build_refuses_incomplete_onboarding_even_with_forged_creator_ready(ready_project, monkeypatch):
    project_root, overlay = ready_project
    runtime = load_runtime(project_root)
    runtime["onboarding"]["complete"] = False
    atomic_write_json(project_root / "private/runtime.json", runtime, project_root=project_root)
    monkeypatch.setattr(builder, "_render_pdf_backend", _fake_backend)
    with pytest.raises(DocumentBuildError, match="onboarding"):
        render_cv(project_root, overlay, project_root / "Documents/generated/out.pdf")


def test_build_rejects_render_that_moves_immutable_blocks_even_when_overlay_text_is_present(ready_project, monkeypatch):
    project_root, overlay = ready_project
    monkeypatch.setattr(builder, "_render_pdf_backend", _fake_backend)
    with pytest.raises(DocumentBuildError, match="immutable"):
        render_cv(project_root, overlay, project_root / "Documents/generated/immutable-drift.pdf")
    assert not (project_root / "Documents/generated/immutable-drift.pdf").exists()


def test_external_url_fetcher_denies_network_and_file_resources():
    for url in ("https://example.invalid/x", "file:///x", "ftp://example.invalid/x"):
        with pytest.raises(DocumentBuildError, match="resource access denied"):
            builder._self_contained_url_fetcher(url)


@pytest.mark.parametrize("payload", [
    "<style>@import 'https://example.invalid/x';</style>",
    '<img src="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=">',
])
def test_render_rejects_literal_template_resources(ready_project, payload: str):
    project_root, overlay = ready_project
    template = project_root / "private/cv-creator/template.html.j2"
    template.write_text(template.read_text(encoding="utf-8") + payload, encoding="utf-8")
    _refresh_creator_binding(project_root)
    with pytest.raises(DocumentBuildError, match="resource|stylesheet|active"):
        render_cv(project_root, overlay, project_root / "Documents/generated/out.pdf")


def test_corrupt_creator_still_revokes_runtime_auto_apply(ready_project):
    project_root, _ = ready_project
    runtime = load_runtime(project_root)
    runtime.update({"submission_mode": "auto_apply", "auto_apply_approved_at": "2030-01-01T00:00:00Z"})
    atomic_write_json(project_root / "private/runtime.json", runtime, project_root=project_root)
    (project_root / "private/cv-creator/creator.json").write_text("{bad", encoding="utf-8")
    assert check_creator_drift(project_root) is True
    updated = load_runtime(project_root)
    assert updated["submission_mode"] == "review_only"
    assert updated["auto_apply_approved_at"] is None
    assert updated["onboarding"]["creator_ready"] is False


def test_recreated_baseline_hash_drift_revokes_approval(ready_project):
    project_root, _ = ready_project
    recreated = project_root / "private/cv-creator/baseline/recreated-baseline.pdf"
    recreated.write_bytes(b"changed")
    assert check_creator_drift(project_root) is True
    assert load_runtime(project_root)["onboarding"]["creator_ready"] is False


def test_cli_build_defaults_output_from_overlay_and_prints_no_path(ready_project, monkeypatch, capsys):
    project_root, overlay = ready_project
    monkeypatch.setattr(builder, "_render_pdf_backend", _faithful_test_backend)
    monkeypatch.setattr(builder.sys, "argv", [
        "document_builder.py", "build", "--root", str(project_root),
        "--overlay", overlay.relative_to(project_root).as_posix(), "--preview",
    ])
    assert builder.main() == 0
    output = capsys.readouterr().out
    assert json.loads(output)["requires_review"] is True
    assert str(project_root) not in output
    assert (project_root / "Documents/generated/synthetic.pdf").is_file()


def test_cli_requires_exact_approval_flag_and_emits_bounded_generic_json(project_root: Path, monkeypatch, capsys):
    descriptor = _write_layout_bundle(project_root)
    bootstrap_creator(project_root, descriptor)
    _configure_creator_for_synthetic_render(project_root, descriptor)
    compare_baseline(project_root)
    monkeypatch.setattr(builder.sys, "argv", [
        "document_builder.py", "approve", "--root", str(project_root),
        "--source-sha256", descriptor.source_sha256,
    ])
    assert builder.main() == 1
    output = capsys.readouterr().out
    assert len(output) < 1024
    assert json.loads(output) == {"ok": False, "error": "approval confirmation is required"}
    assert str(project_root) not in output


@pytest.mark.parametrize(
    ("zone", "overlay", "protected_text"),
    [
        ("headline", {"headline": {"text": "Mapped replacement", "evidence_ids": ["e"]}}, "Synthetic Name"),
        ("summary", {"summary": {"text": "Mapped replacement", "evidence_ids": ["e"]}}, "contact@example.invalid"),
        (
            "experience_bullets",
            {"experience_bullets": {"role-1": [{"text": "Mapped replacement", "evidence_ids": ["e"]}]}},
            "Synthetic University Education",
        ),
        ("skills", {"skills": [{"text": "Mapped replacement", "evidence_ids": ["e"]}]}, "Synthetic University Education"),
    ],
)
def test_overlay_zones_never_replace_unmapped_identity_contact_employer_date_or_education(zone, overlay, protected_text):
    del zone
    blocks = [
        {"kind": "text", "text": "Synthetic Name", "font_size": 20.0, "page_number": 1, "x": 10.0, "y": 10.0, "zone": None},
        {"kind": "text", "text": "contact@example.invalid", "font_size": 6.0, "page_number": 1, "x": 10.0, "y": 20.0, "zone": None},
        {"kind": "text", "text": "Synthetic Employer — March 2030", "font_size": 8.0, "page_number": 1, "x": 10.0, "y": 30.0, "zone": None},
        {"kind": "text", "text": "Approved summary slot", "font_size": 9.0, "page_number": 1, "x": 10.0, "y": 40.0, "zone": "summary"},
        {"kind": "text", "text": "Synthetic University Education", "font_size": 7.0, "page_number": 1, "x": 10.0, "y": 50.0, "zone": None},
    ]
    try:
        builder._apply_overlay(blocks, overlay)
    except DocumentBuildError:
        pass
    assert protected_text in [block["text"] for block in blocks]


def test_overlay_edits_only_the_explicitly_mapped_summary_block():
    blocks = [
        {"kind": "text", "text": "Synthetic Name", "font_size": 20.0, "page_number": 1, "x": 10.0, "y": 10.0, "zone": None},
        {"kind": "text", "text": "contact@example.invalid", "font_size": 6.0, "page_number": 1, "x": 10.0, "y": 20.0, "zone": None},
        {"kind": "text", "text": "Approved summary slot", "font_size": 9.0, "page_number": 1, "x": 10.0, "y": 30.0, "zone": "summary"},
    ]
    builder._apply_overlay(blocks, {"summary": {"text": "Mapped replacement", "evidence_ids": ["e"]}})
    assert [block["text"] for block in blocks] == ["Synthetic Name", "contact@example.invalid", "Mapped replacement"]


def test_overlay_edits_ordinary_explicitly_mapped_experience_bullet():
    blocks = [
        {"kind": "text", "text": "Synthetic Employer", "font_size": 11.0, "page_number": 1, "x": 10.0, "y": 10.0, "zone": None},
        {"kind": "text", "text": "• Approved bullet slot", "font_size": 9.0, "page_number": 1, "x": 10.0, "y": 30.0, "zone": "experience_bullets"},
    ]
    builder._apply_overlay(
        blocks,
        {"experience_bullets": {"role-1": [{"text": "• Verified mapped bullet", "evidence_ids": ["e"]}]}},
    )
    assert [block["text"] for block in blocks] == ["Synthetic Employer", "• Verified mapped bullet"]


@pytest.mark.parametrize(
    ("protected_text", "semantic_role"),
    [
        ("Synthetic Name", "identity"),
        ("contact@example.invalid", "contact"),
        ("Synthetic Employer", "employer"),
        ("March 2030", "date"),
        ("Synthetic University Education", "education"),
    ],
)
def test_creator_mapping_rejects_explicit_mapping_of_protected_fact_blocks(project_root: Path, protected_text: str, semantic_role: str):
    initialize_project(project_root)
    source = project_root / "private/master-cv.pdf"
    document = fitz.open(); page = document.new_page(width=595, height=842)
    values = ["Synthetic Name", "contact@example.invalid", "Synthetic Employer", "March 2030", "Synthetic University Education", "• Safe editable bullet"]
    for index, value in enumerate(values):
        page.insert_text((48, 60 + index * 24), value, fontsize=10)
    document.save(source); document.close()
    descriptor = _descriptor_for(source)
    payload = source.read_bytes()
    runtime = load_runtime(project_root)
    runtime.update({"master_cv_path": "private/master-cv.pdf", "master_cv_sha256": hashlib.sha256(payload).hexdigest()})
    atomic_write_json(project_root / "private/runtime.json", runtime, project_root=project_root)
    creator_dir = project_root / "private/cv-creator"; baseline = creator_dir / "baseline"
    baseline.mkdir(parents=True, exist_ok=True); (baseline / "baseline.pdf").write_bytes(payload)
    atomic_write_json(creator_dir / "layout.json", builder._descriptor_payload_for_storage(descriptor, "private/master-cv.pdf"), project_root=project_root)
    creator_path = bootstrap_creator(project_root, descriptor)
    _write_candidate_and_overlay(project_root, {})
    candidate = yaml.safe_load((project_root / "private/candidate.yml").read_text(encoding="utf-8"))
    candidate["identity"]["full_name"] = "Synthetic Name"
    candidate["identity"]["email"] = "contact@example.invalid"
    candidate["current_employer"] = "Synthetic Employer"
    (project_root / "private/candidate.yml").write_text(yaml.safe_dump(candidate, sort_keys=False), encoding="utf-8")
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    text_blocks = [block for block in descriptor.blocks if block.block_type == "text" and block.text.strip()]
    index = next(index for index, block in enumerate(text_blocks) if protected_text in block.text)
    creator["validation"]["block_mapping"][index]["semantic_role"] = semantic_role
    creator["validation"]["block_mapping"][index]["zone"] = "summary"
    creator["validation"]["block_mapping"][index]["immutable"] = False
    with pytest.raises(DocumentBuildError, match="mapping"):
        builder._validated_block_mapping(project_root, descriptor, creator)


def test_bootstrap_persists_deterministic_immutable_block_mapping_without_silent_font_substitution(project_root: Path):
    descriptor = _write_layout_bundle(project_root)
    creator_path = bootstrap_creator(project_root, descriptor)
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    mapping = creator["validation"].get("block_mapping")
    assert isinstance(mapping, list) and mapping
    assert all(set(row) == {"block_id", "page_number", "source_text_sha256", "source_geometry_sha256", "semantic_role", "zone", "immutable"} for row in mapping)
    assert all(row["semantic_role"] == "protected_other" and row["zone"] is None and row["immutable"] is True for row in mapping)
    assert len({row["block_id"] for row in mapping}) == len(mapping)
    assert creator["font_substitutions"] == {}


def test_approval_rejects_fabricated_all_true_record_without_pdf_png_attestation(project_root: Path):
    descriptor = _write_layout_bundle(project_root)
    creator_path = _approve_without_render(project_root, descriptor)
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    creator["approved_at"] = None
    creator["approved_creator_sha256"] = ""
    atomic_write_json(creator_path, creator, project_root=project_root)
    for path in (creator_path.parent / "baseline").glob("*-page-*.png"):
        path.unlink()
    with pytest.raises(DocumentBuildError, match="comparison"):
        approve_creator(project_root, descriptor.source_sha256, explicit_user_approval=True)


@pytest.mark.parametrize("mutation", ["missing", "swapped"])
def test_approval_rejects_missing_or_swapped_comparison_png(project_root: Path, mutation: str):
    descriptor = _write_layout_bundle(project_root)
    bootstrap_creator(project_root, descriptor)
    _configure_creator_for_synthetic_render(project_root, descriptor)
    compare_baseline(project_root)
    baseline = project_root / "private/cv-creator/baseline"
    source = baseline / "source-page-001.png"
    comparison = baseline / "comparison-page-001.png"
    if mutation == "missing":
        comparison.unlink()
    else:
        source.write_bytes(comparison.read_bytes())
    with pytest.raises(DocumentBuildError, match="comparison"):
        approve_creator(project_root, descriptor.source_sha256, explicit_user_approval=True)


def test_compare_revokes_and_persists_review_only_before_render(project_root: Path, monkeypatch):
    descriptor = _write_layout_bundle(project_root)
    creator_path = _approve_without_render(project_root, descriptor)
    runtime = load_runtime(project_root)
    runtime.update({"submission_mode": "auto_apply", "auto_apply_approved_at": "2030-01-01T00:00:00Z"})
    atomic_write_json(project_root / "private/runtime.json", runtime, project_root=project_root)
    observed = {}

    def stop_after_observation(_root: Path, _destination: Path) -> str:
        current_creator = json.loads(creator_path.read_text(encoding="utf-8"))
        current_runtime = load_runtime(project_root)
        observed.update({
            "approved_at": current_creator["approved_at"],
            "creator_ready": current_runtime["onboarding"]["creator_ready"],
            "mode": current_runtime["submission_mode"],
            "auto": current_runtime["auto_apply_approved_at"],
        })
        raise DocumentBuildError("synthetic render stop")

    monkeypatch.setattr(builder, "_render_private_baseline", stop_after_observation)
    with pytest.raises(DocumentBuildError, match="synthetic render stop"):
        compare_baseline(project_root)
    assert observed == {"approved_at": None, "creator_ready": False, "mode": "review_only", "auto": None}


def test_compare_revokes_before_missing_layout_validation_failure(project_root: Path):
    descriptor = _write_layout_bundle(project_root)
    creator_path = _approve_without_render(project_root, descriptor)
    runtime = load_runtime(project_root)
    runtime.update({"submission_mode": "auto_apply", "auto_apply_approved_at": "2030-01-01T00:00:00Z"})
    atomic_write_json(project_root / "private/runtime.json", runtime, project_root=project_root)
    (project_root / "private/cv-creator/layout.json").unlink()

    with pytest.raises(DocumentBuildError, match="layout"):
        compare_baseline(project_root)

    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    runtime = load_runtime(project_root)
    assert creator["approved_at"] is None
    assert creator["automated_validation"] is False
    assert runtime["onboarding"]["creator_ready"] is False
    assert runtime["submission_mode"] == "review_only"
    assert runtime["auto_apply_approved_at"] is None


def test_compare_uses_import_lock_and_publication_failure_leaves_no_partial_new_set(project_root: Path, monkeypatch):
    descriptor = _write_layout_bundle(project_root)
    creator_path = _approve_without_render(project_root, descriptor)
    with exclusive_project_lock(project_root, name=".cv-import.lock"):
        with pytest.raises(DocumentBuildError, match="lock"):
            compare_baseline(project_root)

    existing = {path.name: path.read_bytes() for path in (creator_path.parent / "baseline").glob("*-page-*.png")}
    real_write = builder.atomic_write_bytes
    calls = 0

    def fail_mid_publish(path: Path, payload: bytes, project_root: Path | None = None):
        nonlocal calls
        if "source-page-" in Path(path).name or "recreated-page-" in Path(path).name or "comparison-page-" in Path(path).name:
            calls += 1
            if calls == 2:
                raise OSError("synthetic publication failure")
        return real_write(path, payload, project_root=project_root)

    monkeypatch.setattr(builder, "atomic_write_bytes", fail_mid_publish)
    with pytest.raises(DocumentBuildError):
        compare_baseline(project_root)
    baseline = creator_path.parent / "baseline"
    assert {path.name: path.read_bytes() for path in baseline.glob("*-page-*.png")} == existing
    runtime = load_runtime(project_root)
    assert runtime["submission_mode"] == "review_only" and runtime["auto_apply_approved_at"] is None
    assert runtime["onboarding"]["creator_ready"] is False


def _metric_page(*, text="A", text_bbox=(10.0, 10.0, 100.0, 30.0), text_size=10.0, image=None):
    return {
        "width": 200.0,
        "height": 200.0,
        "text": text,
        "blocks": [{"text": text, "bbox": text_bbox, "sizes": [text_size], "fonts": ["Helvetica"], "colors": [0]}],
        "images": [] if image is None else [image],
        "drawings": [],
        "colors": {0},
        "sizes": [text_size],
        "fonts": {"Helvetica"},
    }


@pytest.mark.parametrize(
    "recreated_page",
    [
        _metric_page(image=(190.0, 20.0, 230.0, 60.0)),
        _metric_page(image=(20.0, 12.0, 80.0, 28.0)),
    ],
)
def test_comparison_rejects_off_page_or_text_overlapping_images(recreated_page):
    source_page = _metric_page(image=(120.0, 120.0, 150.0, 150.0))
    descriptor = LayoutDescriptor(
        "0" * 64, "pdf", 1, (PageGeometry(1, 200, 200, 10, 10, 10, 10),),
        (LayoutBlock(1, "text", "A", (10, 10, 100, 30), ("Helvetica",), (10.0,), (0,)), LayoutBlock(1, "image", "", (120, 120, 150, 150), (), (), ())),
        (), 1,
    )
    checks = builder._comparison_checks(descriptor, {"pages": [source_page]}, {"pages": [recreated_page]}, {"font_substitutions": {"Helvetica": "Arial"}, "validation": {"block_mapping": []}})
    assert checks["clipping"] is False or checks["overlap"] is False


def test_comparison_detects_local_typography_drift_and_extra_text_independently():
    left = _metric_page(text="A")
    left["blocks"].append({"text": "B", "bbox": (10.0, 40.0, 100.0, 60.0), "sizes": [20.0], "fonts": ["Helvetica"], "colors": [0]})
    left["text"] = "A B"; left["sizes"] = [10.0, 20.0]
    right = _metric_page(text="A", text_size=20.0)
    right["blocks"].append({"text": "B EXTRA", "bbox": (10.0, 40.0, 100.0, 60.0), "sizes": [10.0], "fonts": ["Arial"], "colors": [0]})
    right["text"] = "A B EXTRA"; right["sizes"] = [20.0, 10.0]; right["fonts"] = {"Arial"}
    descriptor = LayoutDescriptor(
        "0" * 64, "pdf", 1, (PageGeometry(1, 200, 200, 10, 10, 10, 10),),
        (
            LayoutBlock(1, "text", "A", (10, 10, 100, 30), ("Helvetica",), (10.0,), (0,)),
            LayoutBlock(1, "text", "B", (10, 40, 100, 60), ("Helvetica",), (20.0,), (0,)),
        ), (), 0,
    )
    checks = builder._comparison_checks(descriptor, {"pages": [left]}, {"pages": [right]}, {"font_substitutions": {"Helvetica": "Arial"}, "validation": {"block_mapping": []}})
    assert checks["source_text"] is True
    assert checks["unexpected_text"] is False
    assert checks["typography"] is False


def test_comparison_rejects_undeclared_font_replacement_and_accepts_exact_declared_family():
    source_page = _metric_page(text="A")
    recreated_page = _metric_page(text="A")
    recreated_page["blocks"][0]["fonts"] = ["ArialMT"]
    recreated_page["fonts"] = {"ArialMT"}
    descriptor = LayoutDescriptor(
        "0" * 64,
        "pdf",
        1,
        (PageGeometry(1, 200, 200, 10, 10, 10, 10),),
        (LayoutBlock(1, "text", "A", (10, 10, 100, 30), ("Helvetica",), (10.0,), (0,)),),
        (),
        0,
    )
    unapproved = builder._comparison_checks(
        descriptor,
        {"pages": [source_page]},
        {"pages": [recreated_page]},
        {"font_substitutions": {}, "validation": {"block_mapping": []}},
    )
    declared = builder._comparison_checks(
        descriptor,
        {"pages": [source_page]},
        {"pages": [recreated_page]},
        {"font_substitutions": {"Helvetica": "Arial"}, "validation": {"block_mapping": []}},
    )
    assert unapproved["font_substitutions"] is False
    assert declared["font_substitutions"] is True


def test_comparison_rejects_wrong_rule_color_or_thickness(tmp_path: Path):
    def ruled(path: Path, color, width):
        document = fitz.open(); page = document.new_page(width=200, height=200)
        page.insert_text((10, 20), "A", fontsize=10)
        page.draw_line((10, 50), (150, 50), color=color, width=width)
        document.save(path); document.close()

    source = tmp_path / "source.pdf"; recreated = tmp_path / "recreated.pdf"
    ruled(source, (0.1, 0.2, 0.3), 1.0); ruled(recreated, (0.8, 0.1, 0.1), 4.0)
    descriptor = _descriptor_for(source)
    checks = builder._comparison_checks(descriptor, builder._pdf_metrics(source), builder._pdf_metrics(recreated), {"font_substitutions": {"Helvetica": "Arial"}, "validation": {"block_mapping": []}})
    assert checks["rules"] is False


@pytest.mark.parametrize(
    ("claim", "evidence"),
    [
        ("Processed 1e9 records", {"e": "Improved 12 percent"}),
        ("Used Python 3", {"e": "Worked for 3 years"}),
    ],
)
def test_quantified_claim_requires_nfkc_clause_level_unit_and_context_binding(claim, evidence):
    with pytest.raises(DocumentBuildError, match="evidence"):
        builder._claim({"text": claim, "evidence_ids": ["e"]}, evidence)
    assert builder._claim({"text": "Improved 12 percent", "evidence_ids": ["e"]}, {"e": "Synthetic work improved 12 percent safely"})["text"] == "Improved 12 percent"


def test_yaml_depth_500_fails_with_document_error_before_construction(tmp_path: Path):
    overlay = tmp_path / "deep.yml"
    overlay.write_text("summary: " + "[" * 500 + "x" + "]" * 500, encoding="utf-8")
    with pytest.raises(DocumentBuildError, match="overlay"):
        builder._load_overlay(overlay)


def test_single_renderer_unavailable_fails_with_install_remediation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(builder, "_weasy_smoke", lambda: False)
    with pytest.raises(DocumentBuildError, match="WeasyPrint 69"):
        builder._render_pdf_backend("<!doctype html><p>safe</p>", tmp_path / "out.pdf")


def test_cli_compare_returns_relative_png_paths_and_registered_source_hash(project_root: Path, monkeypatch, capsys):
    descriptor = _write_layout_bundle(project_root)
    bootstrap_creator(project_root, descriptor)
    baseline = project_root / "private/cv-creator/baseline"
    fake = ComparisonReport(
        True,
        (baseline / "source-page-001.png",),
        (baseline / "recreated-page-001.png",),
        (baseline / "comparison-page-001.png",),
        {name: True for name in CHECK_NAMES},
        (),
    )
    monkeypatch.setattr(builder, "compare_baseline", lambda _root: fake)
    monkeypatch.setattr(builder.sys, "argv", ["document_builder.py", "compare", "--root", str(project_root)])
    assert builder.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source_sha256"] == descriptor.source_sha256
    assert result["comparison_pages"] == ["private/cv-creator/baseline/comparison-page-001.png"]
    assert str(project_root) not in json.dumps(result)


def test_semantic_roles_are_explicit_and_acme_re_and_msc_cannot_be_remapped_as_skills(project_root: Path):
    source = project_root / "semantic.pdf"
    document = fitz.open()
    page = document.new_page(width=300, height=300)
    page.insert_text((30, 40), "Acme Re", fontsize=11)
    page.insert_text((30, 80), "MSc Actuarial Science", fontsize=11)
    document.save(source)
    document.close()
    descriptor = _descriptor_for(source)
    initialize_project(project_root)
    private = project_root / "private"
    registered = private / "master-cv.pdf"
    registered.write_bytes(source.read_bytes())
    runtime = load_runtime(project_root)
    runtime.update({"master_cv_path": "private/master-cv.pdf", "master_cv_sha256": descriptor.source_sha256})
    atomic_write_json(private / "runtime.json", runtime, project_root=project_root)
    baseline = private / "cv-creator/baseline"
    baseline.mkdir(parents=True, exist_ok=True)
    (baseline / "baseline.pdf").write_bytes(source.read_bytes())
    atomic_write_json(
        private / "cv-creator/layout.json",
        builder._descriptor_payload_for_storage(descriptor, "private/master-cv.pdf"),
        project_root=project_root,
    )
    creator_path = bootstrap_creator(project_root, descriptor)
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    rows = creator["validation"]["block_mapping"]
    assert [row["semantic_role"] for row in rows] == ["protected_other", "protected_other"]
    for row in rows:
        row.update({"zone": "skills", "immutable": False})
    with pytest.raises(DocumentBuildError, match="mapping"):
        builder._validated_block_mapping(project_root, descriptor, creator)


@pytest.mark.parametrize("mutation", ["missing", "unknown", "ambiguous"])
def test_semantic_mapping_rejects_missing_unknown_or_ambiguous_roles(project_root: Path, mutation: str):
    descriptor = _write_layout_bundle(project_root)
    creator_path = bootstrap_creator(project_root, descriptor)
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    row = creator["validation"]["block_mapping"][0]
    if mutation == "missing":
        row.pop("semantic_role", None)
    elif mutation == "unknown":
        row["semantic_role"] = "other_guess"
    else:
        row.update({"semantic_role": "summary", "zone": "skills", "immutable": False})
    with pytest.raises(DocumentBuildError, match="mapping"):
        builder._validated_block_mapping(project_root, descriptor, creator)


def test_approved_envelope_rejects_forged_semantic_mapping_hashes(project_root: Path):
    descriptor = _write_layout_bundle(project_root)
    bootstrap_creator(project_root, descriptor)
    _configure_creator_for_synthetic_render(project_root, descriptor, summary_slot=True)
    compare_baseline(project_root)
    approve_creator(project_root, descriptor.source_sha256, explicit_user_approval=True)
    creator_path = project_root / "private/cv-creator/creator.json"
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    row = creator["validation"]["block_mapping"][0]
    row.update({"semantic_role": "skills", "zone": "skills", "immutable": False})
    forged_mapping_hash = builder._mapping_sha256(creator["validation"]["block_mapping"])
    creator["validation"]["approved_mapping_sha256"] = forged_mapping_hash
    creator["validation"]["comparison"]["mapping_sha256"] = forged_mapping_hash
    atomic_write_json(creator_path, creator, project_root=project_root)
    with pytest.raises(DocumentBuildError, match="approved"):
        builder.verify_approved_creator_binding(project_root)


def test_approved_envelope_rejects_replaced_recreated_pdf_and_forged_manifest(project_root: Path):
    descriptor = _write_layout_bundle(project_root)
    bootstrap_creator(project_root, descriptor)
    _configure_creator_for_synthetic_render(project_root, descriptor)
    compare_baseline(project_root)
    approve_creator(project_root, descriptor.source_sha256, explicit_user_approval=True)
    creator_path = project_root / "private/cv-creator/creator.json"
    baseline = creator_path.parent / "baseline"
    recreated_path = baseline / "recreated-baseline.pdf"
    with fitz.open(recreated_path) as document:
        document.set_metadata({"producer": "synthetic replacement"})
        replacement = document.tobytes()
    recreated_path.write_bytes(replacement)
    creator = json.loads(creator_path.read_text(encoding="utf-8"))
    artifacts = builder._comparison_artifacts((baseline / "baseline.pdf").read_bytes(), replacement)
    for name, payload in artifacts.items():
        (baseline / name).write_bytes(payload)
    creator["validation"]["comparison"]["recreated_baseline_sha256"] = hashlib.sha256(replacement).hexdigest()
    creator["validation"]["comparison"]["artifact_sha256"] = builder._artifact_manifest(artifacts)
    atomic_write_json(creator_path, creator, project_root=project_root)
    with pytest.raises(DocumentBuildError, match="approved"):
        builder.verify_approved_creator_binding(project_root)


def test_routine_approved_binding_verification_never_reparses_pdfs_or_regenerates_pngs(project_root: Path, monkeypatch, record_property):
    descriptor = _write_layout_bundle(project_root)
    bootstrap_creator(project_root, descriptor)
    _configure_creator_for_synthetic_render(project_root, descriptor)
    compare_baseline(project_root)
    approve_creator(project_root, descriptor.source_sha256, explicit_user_approval=True)
    monkeypatch.setattr(builder, "_pdf_metrics", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("PDF reparse reached")))
    monkeypatch.setattr(builder, "_comparison_checks", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("geometry comparator reached")))
    monkeypatch.setattr(builder, "_comparison_artifacts", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("PNG regeneration reached")))
    started = time.perf_counter()
    creator = builder.verify_approved_creator_binding(project_root)
    record_property("routine_verify_seconds", time.perf_counter() - started)
    assert creator["approved_at"] is not None


def test_approve_performs_expensive_strict_comparison_exactly_once(project_root: Path, monkeypatch):
    descriptor = _write_layout_bundle(project_root)
    bootstrap_creator(project_root, descriptor)
    _configure_creator_for_synthetic_render(project_root, descriptor)
    compare_baseline(project_root)
    original = builder._recomputed_comparison
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(builder, "_recomputed_comparison", counted)
    approve_creator(project_root, descriptor.source_sha256, explicit_user_approval=True)
    assert calls == 1


def _colored_image_pdf(path: Path, color: tuple[int, int, int]) -> None:
    from io import BytesIO

    document = fitz.open()
    page = document.new_page(width=200, height=200)
    page.insert_text((10, 20), "A", fontsize=10)
    buffer = BytesIO()
    Image.new("RGB", (12, 12), color).save(buffer, format="PNG")
    page.insert_image(fitz.Rect(100, 100, 130, 130), stream=buffer.getvalue())
    document.save(path)
    document.close()


def test_comparison_rejects_same_bbox_image_with_different_decoded_pixels(tmp_path: Path):
    source = tmp_path / "red.pdf"
    recreated = tmp_path / "blue.pdf"
    _colored_image_pdf(source, (255, 0, 0))
    _colored_image_pdf(recreated, (0, 0, 255))
    descriptor = _descriptor_for(source)
    checks = builder._comparison_checks(
        descriptor,
        builder._pdf_metrics(source),
        builder._pdf_metrics(recreated),
        {"font_substitutions": {}, "validation": {"block_mapping": []}},
    )
    assert checks["asset_placement"] is False


def test_comparison_rejects_internal_text_block_full_bbox_drift():
    source = _metric_page(text="A")
    source["blocks"].append({"text": "B", "bbox": (10.0, 60.0, 180.0, 80.0), "sizes": [10.0], "fonts": ["Helvetica"], "colors": [0]})
    source["text"] = "A B"
    recreated = _metric_page(text="A", text_bbox=(10.0, 10.0, 70.0, 45.0))
    recreated["blocks"].append({"text": "B", "bbox": (10.0, 60.0, 180.0, 80.0), "sizes": [10.0], "fonts": ["Helvetica"], "colors": [0]})
    recreated["text"] = "A B"
    descriptor = LayoutDescriptor(
        "0" * 64,
        "pdf",
        1,
        (PageGeometry(1, 200, 200, 10, 10, 10, 10),),
        (
            LayoutBlock(1, "text", "A", (10, 10, 100, 30), ("Helvetica",), (10.0,), (0,)),
            LayoutBlock(1, "text", "B", (10, 60, 180, 80), ("Helvetica",), (10.0,), (0,)),
        ),
        (),
        0,
    )
    checks = builder._comparison_checks(
        descriptor,
        {"pages": [source]},
        {"pages": [recreated]},
        {"font_substitutions": {}, "validation": {"block_mapping": []}},
    )
    assert checks["spacing"] is False


@pytest.mark.parametrize(
    ("claim", "evidence", "accepted"),
    [
        ("Used Python. 3", {"a": "Used Python safely", "b": "Worked for 3 years"}, False),
        ("3", {"a": "Worked for 3 years"}, False),
        ("Improved 12.5 percent", {"a": "Improved 12.5 percent safely"}, True),
        ("Used Python 3.11", {"a": "Used Python 3.11 safely"}, True),
        ("Delivered during March 2030", {"a": "Delivered during March 2030 safely"}, True),
    ],
)
def test_evidence_binding_keeps_numeric_context_unit_and_decimal_version_in_one_row(claim, evidence, accepted):
    value = {"text": claim, "evidence_ids": list(evidence)}
    if accepted:
        assert builder._claim(value, evidence)["text"] == claim
    else:
        with pytest.raises(DocumentBuildError, match="evidence"):
            builder._claim(value, evidence)
