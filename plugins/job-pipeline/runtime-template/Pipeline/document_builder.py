from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import math
import os
import re
import stat
import sys
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

import fitz
import yaml
from PIL import Image
from jinja2 import StrictUndefined, nodes
from jinja2.sandbox import SandboxedEnvironment

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Pipeline.cv_importer import (
    LayoutBlock,
    LayoutDescriptor,
    PageGeometry,
    _run_isolated_worker,
    _validate_descriptor_shape,
)
from Pipeline.io_utils import (
    ConfinementError,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    confined_mkdir,
    confined_path,
    exclusive_project_lock,
    is_symlink_or_reparse,
    path_without_link_ancestors,
)
from Pipeline.onboarding import OnboardingError, load_runtime, save_runtime, validate_candidate
from Pipeline.policy_guard import (
    PolicyError,
    _stable_single_link_bytes,
    application_policy_lock,
    compute_creator_content_sha256,
)


class DocumentBuildError(ValueError):
    """Fail-closed document error whose message never contains private content."""


@dataclass(frozen=True)
class BuildIssue:
    severity: str
    code: str
    message: str


@dataclass(frozen=True)
class BuildReport:
    ok: bool
    pdf_path: Path | None
    preview_paths: tuple[Path, ...]
    page_count: int
    issues: tuple[BuildIssue, ...]


@dataclass(frozen=True)
class ComparisonReport:
    ok: bool
    source_pages: tuple[Path, ...]
    baseline_pages: tuple[Path, ...]
    side_by_side_pages: tuple[Path, ...]
    checks: dict[str, bool]
    differences: tuple[str, ...]


CREATOR_KEYS = {
    "schema_version",
    "source_sha256",
    "creator_version",
    "template",
    "stylesheet",
    "approved_zones",
    "automated_validation",
    "approved_at",
    "font_substitutions",
    "validation",
    "creator_content_sha256",
    "approved_creator_sha256",
}
APPROVED_ZONES = ("headline", "summary", "experience_bullets", "skills")
PROTECTED_SEMANTIC_ROLES = (
    "identity",
    "contact",
    "employer",
    "date",
    "education",
    "layout",
    "protected_other",
)
SEMANTIC_ROLES = frozenset((*APPROVED_ZONES, *PROTECTED_SEMANTIC_ROLES))
CHECK_NAMES = (
    "page_count",
    "page_dimensions",
    "source_text",
    "unexpected_text",
    "section_order",
    "margins_columns",
    "font_substitutions",
    "image_count",
    "clipping",
    "overlap",
    "overflow",
    "density",
    "headline",
    "typography",
    "colors",
    "spacing",
    "bullets",
    "rules",
    "asset_placement",
)
SHA256 = re.compile(r"[0-9a-f]{64}")
UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
NUMBER_TOKEN = re.compile(r"(?i)(?<![\w])[-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:e[-+]?\d+)?(?:\s*(?:%|‰|percent|per\s+cent))?(?![\w])")
DATE_TOKEN = re.compile(
    r"(?i)\b(?:january|february|march|april|may|june|july|august|september|october|november|december|"
    r"janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)\b"
)
MAX_CREATOR_JSON_BYTES = 256 * 1024
MAX_LAYOUT_BYTES = 8 * 1024 * 1024
MAX_TEMPLATE_BYTES = 512 * 1024
MAX_STYLESHEET_BYTES = 512 * 1024
MAX_OVERLAY_BYTES = 256 * 1024
MAX_CANDIDATE_BYTES = 2 * 1024 * 1024
MAX_PDF_BYTES = 32 * 1024 * 1024
MAX_RASTER_BYTES = 8 * 1024 * 1024
MAX_RASTER_PIXELS = 16_000_000
MAX_PREVIEW_WIDTH = 3000
MAX_PREVIEW_HEIGHT = 4000
MAX_VALUE_DEPTH = 12
MAX_VALUE_ITEMS = 500
MAX_YAML_EVENTS = 2_000
MAX_YAML_NODES = 1_000
RENDER_TIMEOUT_SECONDS = 45
RENDER_MEMORY_BYTES = 512 * 1024 * 1024
ALLOWED_RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
ALLOWED_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp"}
ACTIVE_TAGS = {"script", "iframe", "object", "embed", "link", "base", "form", "input", "button", "video", "audio", "source", "svg", "math"}
FORBIDDEN_TEMPLATE_NODES = (nodes.Include, nodes.Extends, nodes.Import, nodes.FromImport, nodes.Call, nodes.Getattr)
_SMOKE_LOCK = threading.Lock()
_SMOKE_OK = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _root(project_root: Path) -> Path:
    try:
        return confined_path(project_root, project_root)
    except (ConfinementError, OSError, ValueError) as error:
        raise DocumentBuildError("project path is unsafe") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DocumentBuildError("JSON contains duplicate keys")
        value[key] = item
    return value


def _read_single(path: Path, maximum: int, label: str) -> bytes:
    try:
        return _stable_single_link_bytes(path, maximum)
    except (PolicyError, OSError, ValueError) as error:
        raise DocumentBuildError(f"{label} is unsafe") from error


def _json_file(path: Path, maximum: int, label: str) -> dict[str, Any]:
    payload = _read_single(path, maximum, label)
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except DocumentBuildError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise DocumentBuildError(f"{label} is invalid") from error
    if not isinstance(value, dict):
        raise DocumentBuildError(f"{label} is invalid")
    _validate_value(value, label=label)
    return value


def _validate_value(value: object, *, label: str, depth: int = 0) -> None:
    if depth > MAX_VALUE_DEPTH:
        raise DocumentBuildError(f"{label} is too deep")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > 10**18:
            raise DocumentBuildError(f"{label} is invalid")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise DocumentBuildError(f"{label} values must be finite")
        return
    if isinstance(value, str):
        if len(value) > MAX_OVERLAY_BYTES or CONTROL.search(value):
            raise DocumentBuildError(f"{label} contains control characters")
        return
    if isinstance(value, list):
        if len(value) > MAX_VALUE_ITEMS:
            raise DocumentBuildError(f"{label} is too large")
        for item in value:
            _validate_value(item, label=label, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_VALUE_ITEMS or any(not isinstance(key, str) or not key or CONTROL.search(key) for key in value):
            raise DocumentBuildError(f"{label} is invalid")
        for item in value.values():
            _validate_value(item, label=label, depth=depth + 1)
        return
    raise DocumentBuildError(f"{label} is invalid")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha(value: object) -> str:
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _normalized_claim_text(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold()
    folded = re.sub(r"[\u2010-\u2015]", "-", folded)
    folded = re.sub(r"\s+", " ", folded)
    return folded.strip(" \t\r\n.,;:!?-")


def _block_identifier(block: LayoutBlock, ordinal: int) -> str:
    identity = _canonical_sha({
        "page_number": block.page_number,
        "ordinal": ordinal,
        "text_sha256": _sha(unicodedata.normalize("NFKC", block.text).encode("utf-8")),
        "bbox": [round(float(value), 4) for value in block.bbox],
    })
    return f"p{block.page_number:03d}-t{ordinal:04d}-{identity[:12]}"


def _default_block_mapping(descriptor: LayoutDescriptor) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    ordinal = 0
    for block in descriptor.blocks:
        if block.block_type != "text" or not block.text.strip():
            continue
        ordinal += 1
        result.append({
            "block_id": _block_identifier(block, ordinal),
            "page_number": block.page_number,
            "source_text_sha256": _sha(unicodedata.normalize("NFKC", block.text).encode("utf-8")),
            "source_geometry_sha256": _canonical_sha([round(float(value), 4) for value in block.bbox]),
            "semantic_role": "protected_other",
            "zone": None,
            "immutable": True,
        })
    return result


def _mapping_sha256(mapping: list[dict[str, Any]]) -> str:
    return _canonical_sha(mapping)


def _validated_block_mapping(root: Path, descriptor: LayoutDescriptor, creator: dict[str, Any]) -> dict[str, dict[str, Any]]:
    validation = creator.get("validation")
    mapping = validation.get("block_mapping") if isinstance(validation, dict) else None
    expected = _default_block_mapping(descriptor)
    if not isinstance(mapping, list) or len(mapping) != len(expected):
        raise DocumentBuildError("creator block mapping is invalid")
    result: dict[str, dict[str, Any]] = {}
    for row, baseline in zip(mapping, expected):
        if not isinstance(row, dict) or set(row) != set(baseline):
            raise DocumentBuildError("creator block mapping is invalid")
        if any(row[key] != baseline[key] for key in ("block_id", "page_number", "source_text_sha256", "source_geometry_sha256")):
            raise DocumentBuildError("creator block mapping is invalid")
        zone = row.get("zone")
        immutable = row.get("immutable")
        semantic_role = row.get("semantic_role")
        if semantic_role not in SEMANTIC_ROLES:
            raise DocumentBuildError("creator block mapping is invalid")
        if semantic_role in APPROVED_ZONES:
            if zone is None:
                if immutable is not True:
                    raise DocumentBuildError("creator block mapping is invalid")
            elif zone != semantic_role or immutable is not False:
                raise DocumentBuildError("creator block mapping is invalid")
        elif zone is not None or immutable is not True:
            raise DocumentBuildError("creator block mapping is invalid")
        result[row["block_id"]] = row
    return result


def _checks_sha256(checks: dict[str, bool]) -> str:
    return _canonical_sha({name: bool(checks.get(name)) for name in CHECK_NAMES})


def _descriptor_payload_for_storage(descriptor: LayoutDescriptor, source_path: str) -> dict[str, Any]:
    payload = asdict(descriptor)
    payload.pop("_attestation", None)
    payload["source_path"] = source_path
    return payload


def _descriptor_from_layout(value: dict[str, Any]) -> LayoutDescriptor:
    expected = {"source_sha256", "source_kind", "page_count", "pages", "blocks", "section_order", "image_count", "source_path"}
    if set(value) != expected or value.get("source_path") not in {"private/master-cv.pdf", "private/master-cv.docx"}:
        raise DocumentBuildError("layout descriptor is invalid")
    try:
        pages = tuple(PageGeometry(**page) for page in value["pages"])
        blocks = tuple(
            LayoutBlock(
                page_number=block["page_number"],
                block_type=block["block_type"],
                text=block["text"],
                bbox=tuple(block["bbox"]),
                font_names=tuple(block["font_names"]),
                font_sizes=tuple(block["font_sizes"]),
                colors=tuple(block["colors"]),
                style_names=tuple(block.get("style_names", [])),
            )
            for block in value["blocks"]
        )
        descriptor = LayoutDescriptor(
            source_sha256=value["source_sha256"],
            source_kind=value["source_kind"],
            page_count=value["page_count"],
            pages=pages,
            blocks=blocks,
            section_order=tuple(value["section_order"]),
            image_count=value["image_count"],
        )
        _validate_descriptor_shape(descriptor)
        return descriptor
    except DocumentBuildError:
        raise
    except Exception as error:
        raise DocumentBuildError("layout descriptor is invalid") from error


def _load_layout(root: Path) -> tuple[LayoutDescriptor, bytes]:
    path = confined_path(root, root / "private/cv-creator/layout.json")
    payload = _read_single(path, MAX_LAYOUT_BYTES, "layout descriptor")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except DocumentBuildError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DocumentBuildError("layout descriptor is invalid") from error
    if not isinstance(value, dict):
        raise DocumentBuildError("layout descriptor is invalid")
    return _descriptor_from_layout(value), payload


def _registered_source(root: Path) -> tuple[Path, bytes, dict[str, Any]]:
    try:
        runtime = load_runtime(root)
    except OnboardingError as error:
        raise DocumentBuildError("registered master CV is invalid") from error
    raw_path = runtime.get("master_cv_path")
    digest = runtime.get("master_cv_sha256")
    if raw_path not in {"private/master-cv.pdf", "private/master-cv.docx"} or not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise DocumentBuildError("registered master CV is invalid")
    path = confined_path(root, root.joinpath(*PurePosixPath(raw_path).parts))
    payload = _read_single(path, 15 * 1024 * 1024, "registered master CV")
    if _sha(payload) != digest:
        raise DocumentBuildError("registered master CV hash has changed")
    return path, payload, runtime


def _load_creator(root: Path) -> dict[str, Any]:
    value = _json_file(confined_path(root, root / "private/cv-creator/creator.json"), MAX_CREATOR_JSON_BYTES, "creator configuration")
    if set(value) != CREATOR_KEYS:
        raise DocumentBuildError("creator configuration is invalid")
    zones = value.get("approved_zones")
    substitutions = value.get("font_substitutions")
    if (
        value.get("schema_version") != 1
        or not isinstance(value.get("source_sha256"), str)
        or SHA256.fullmatch(value["source_sha256"]) is None
        or type(value.get("creator_version")) is not int
        or not 1 <= value["creator_version"] <= 1_000_000
        or value.get("template") != "template.html.j2"
        or value.get("stylesheet") != "style.css"
        or zones != list(APPROVED_ZONES)
        or type(value.get("automated_validation")) is not bool
        or (value.get("approved_at") is not None and (not isinstance(value["approved_at"], str) or UTC_TIMESTAMP.fullmatch(value["approved_at"]) is None))
        or not isinstance(substitutions, dict)
        or len(substitutions) > 100
        or any(not isinstance(key, str) or not key.strip() or not isinstance(item, str) or not item.strip() for key, item in substitutions.items())
        or not isinstance(value.get("validation"), dict)
        or not isinstance(value.get("creator_content_sha256"), str)
        or (value["creator_content_sha256"] and SHA256.fullmatch(value["creator_content_sha256"]) is None)
        or not isinstance(value.get("approved_creator_sha256"), str)
        or (value["approved_creator_sha256"] and SHA256.fullmatch(value["approved_creator_sha256"]) is None)
    ):
        raise DocumentBuildError("creator configuration is invalid")
    return value


def _color_css(value: int) -> str:
    return f"#{value:06x}"


def _build_css(descriptor: LayoutDescriptor) -> str:
    rules = [
        "html,body{margin:0;padding:0;background:#fff}",
        "body{font-family:Arial,sans-serif}",
        ".cv-page{position:relative;overflow:hidden;margin:0;padding:0;break-after:page}",
        ".cv-page:last-child{break-after:auto}",
        ".cv-block{position:absolute;box-sizing:border-box;margin:0;padding:0;white-space:pre-wrap;line-height:1.15}",
        ".cv-image{position:absolute;object-fit:contain}",
        ".cv-rule{position:absolute;box-sizing:border-box}",
    ]
    for page in descriptor.pages:
        name = f"cv{page.page_number}"
        rules.append(f"@page {name}{{size:{page.width_points:.3f}pt {page.height_points:.3f}pt;margin:0}}")
        rules.append(
            f".page-{page.page_number}{{page:{name};width:{page.width_points:.3f}pt;height:{page.height_points:.3f}pt}}"
        )
    return "\n".join(rules) + "\n"


def _validate_raster(payload: bytes, suffix: str) -> tuple[str, str]:
    if not payload or len(payload) > MAX_RASTER_BYTES or suffix.casefold() not in ALLOWED_RASTER_SUFFIXES:
        raise DocumentBuildError("creator raster asset is unsafe")
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            width, height = image.size
            format_name = str(image.format or "").upper()
    except Exception as error:
        raise DocumentBuildError("creator raster asset is unsafe") from error
    if width < 1 or height < 1 or width * height > MAX_RASTER_PIXELS or format_name not in {"PNG", "JPEG", "WEBP"}:
        raise DocumentBuildError("creator raster asset is unsafe")
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[format_name]
    extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[format_name]
    return mime, extension


def _extract_rasters(baseline_payload: bytes) -> list[bytes]:
    rasters: list[bytes] = []
    try:
        with fitz.open(stream=baseline_payload, filetype="pdf") as document:
            for page in document:
                for item in page.get_images(full=True):
                    extracted = document.extract_image(item[0])
                    payload = extracted.get("image")
                    extension = "." + str(extracted.get("ext", ""))
                    if not isinstance(payload, bytes):
                        raise DocumentBuildError("creator raster asset is unsafe")
                    _validate_raster(payload, extension)
                    rasters.append(payload)
    except DocumentBuildError:
        raise
    except Exception as error:
        raise DocumentBuildError("source baseline is invalid") from error
    return rasters


def _revoke_values(creator: dict[str, Any], runtime: dict[str, Any], *, clear_validation: bool) -> None:
    creator["approved_at"] = None
    creator["approved_creator_sha256"] = ""
    if clear_validation:
        creator["automated_validation"] = False
    runtime["onboarding"]["creator_ready"] = False
    runtime["submission_mode"] = "review_only"
    runtime["auto_apply_approved_at"] = None


@contextmanager
def _creator_locks(root: Path) -> Iterator[None]:
    try:
        with application_policy_lock(root):
            with exclusive_project_lock(root, name=".cv-import.lock"):
                with exclusive_project_lock(root, name=".document-builder.lock"):
                    yield
    except DocumentBuildError:
        raise
    except (PolicyError, ConfinementError, OSError, ValueError) as error:
        raise DocumentBuildError("creator lock is already held") from error


def bootstrap_creator(project_root: Path, descriptor: LayoutDescriptor) -> Path:
    root = _root(project_root)
    try:
        _validate_descriptor_shape(descriptor)
    except Exception as error:
        raise DocumentBuildError("layout descriptor is invalid") from error
    try:
        for path in (
            root / "private",
            root / "private/cv-creator",
            root / "Documents/templates/cv.html.j2",
            root / "Documents/templates/cv.css",
        ):
            confined_path(root, path)
    except (ConfinementError, OSError, ValueError) as error:
        raise DocumentBuildError("bootstrap path is unsafe") from error
    with _creator_locks(root):
        stored, layout_payload = _load_layout(root)
        expected_payload = _descriptor_payload_for_storage(descriptor, "private/master-cv." + descriptor.source_kind)
        if _canonical_sha(expected_payload) != _canonical_sha(_descriptor_payload_for_storage(stored, "private/master-cv." + stored.source_kind)):
            raise DocumentBuildError("layout descriptor does not match attested layout")
        _, source_payload, runtime = _registered_source(root)
        if descriptor.source_sha256 != _sha(source_payload):
            raise DocumentBuildError("layout descriptor source hash does not match")
        creator_dir = confined_mkdir(root, root / "private/cv-creator")
        baseline_path = confined_path(root, creator_dir / "baseline/baseline.pdf")
        baseline_payload = _read_single(baseline_path, MAX_PDF_BYTES, "source baseline")
        try:
            with fitz.open(stream=baseline_payload, filetype="pdf") as document:
                if document.page_count != descriptor.page_count:
                    raise DocumentBuildError("source baseline is invalid")
        except DocumentBuildError:
            raise
        except Exception as error:
            raise DocumentBuildError("source baseline is invalid") from error
        templates = confined_path(root, root / "Documents/templates")
        shared_template = _read_single(confined_path(root, templates / "cv.html.j2"), MAX_TEMPLATE_BYTES, "shared template")
        shared_css = _read_single(confined_path(root, templates / "cv.css"), MAX_STYLESHEET_BYTES, "shared stylesheet")
        try:
            template_text = shared_template.decode("utf-8")
            css_text = shared_css.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DocumentBuildError("shared creator template is invalid") from error
        _validate_template(template_text)
        _validate_css(css_text)
        assets_dir = confined_mkdir(root, creator_dir / "assets")
        if any(assets_dir.iterdir()):
            raise DocumentBuildError("private creator assets already exist")
        template_destination = confined_path(root, creator_dir / "template.html.j2")
        style_destination = confined_path(root, creator_dir / "style.css")
        destination = confined_path(root, creator_dir / "creator.json")
        if any(path.exists() or is_symlink_or_reparse(path) for path in (template_destination, style_destination, destination)):
            raise DocumentBuildError("private creator already exists")
        written: list[Path] = []
        completed = False
        try:
            for index, payload in enumerate(_extract_rasters(baseline_payload), 1):
                mime, extension = _validate_raster(payload, ".png" if payload.startswith(b"\x89PNG") else ".jpg")
                del mime
                asset = assets_dir / f"asset-{index:03d}{extension}"
                atomic_write_bytes(asset, payload, project_root=root)
                written.append(asset)
            atomic_write_text(template_destination, template_text, project_root=root)
            written.append(template_destination)
            atomic_write_text(style_destination, _build_css(descriptor), project_root=root)
            written.append(style_destination)
            substitutions: dict[str, str] = {}
            content_hash = compute_creator_content_sha256(creator_dir, substitutions)
            creator = {
                "schema_version": 1,
                "source_sha256": descriptor.source_sha256,
                "creator_version": 1,
                "template": "template.html.j2",
                "stylesheet": "style.css",
                "approved_zones": list(APPROVED_ZONES),
                "automated_validation": False,
                "approved_at": None,
                "font_substitutions": substitutions,
                "validation": {
                    "layout_sha256": _sha(layout_payload),
                    "source_baseline_sha256": _sha(baseline_payload),
                    "block_mapping": _default_block_mapping(descriptor),
                    "approved_mapping_sha256": "",
                    "checks": {},
                    "comparison": {},
                },
                "creator_content_sha256": content_hash,
                "approved_creator_sha256": "",
            }
            _revoke_values(creator, runtime, clear_validation=True)
            atomic_write_json(destination, creator, project_root=root)
            written.append(destination)
            save_runtime(root, runtime)
            completed = True
            return destination
        except DocumentBuildError:
            raise
        except Exception as error:
            raise DocumentBuildError("creator bootstrap failed") from error
        finally:
            if not completed:
                for path in reversed(written):
                    try:
                        confined_path(root, path).unlink()
                    except OSError:
                        pass


class _StrictYAMLLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise DocumentBuildError("overlay aliases are forbidden")
        return super().compose_node(parent, index)


def _strict_yaml_mapping(loader: _StrictYAMLLoader, node: Any, deep: bool = False) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise DocumentBuildError("overlay contains duplicate keys")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictYAMLLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_yaml_mapping)


def _preflight_yaml(text: str, label: str) -> None:
    depth = 0
    events = 0
    nodes_seen = 0
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            events += 1
            if events > MAX_YAML_EVENTS:
                raise DocumentBuildError(f"{label} is too large")
            if isinstance(event, yaml.events.AliasEvent):
                raise DocumentBuildError(f"{label} aliases are forbidden")
            if getattr(event, "tag", None) is not None:
                raise DocumentBuildError(f"{label} tags are forbidden")
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
                nodes_seen += 1
                if depth > MAX_VALUE_DEPTH:
                    raise DocumentBuildError(f"{label} is too deep")
            elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
                if depth < 0:
                    raise DocumentBuildError(f"{label} is invalid")
            elif isinstance(event, yaml.events.ScalarEvent):
                nodes_seen += 1
                if len(event.value) > MAX_OVERLAY_BYTES:
                    raise DocumentBuildError(f"{label} is too large")
            if nodes_seen > MAX_YAML_NODES:
                raise DocumentBuildError(f"{label} is too large")
        if depth != 0:
            raise DocumentBuildError(f"{label} is invalid")
    except DocumentBuildError:
        raise
    except (RecursionError, MemoryError, yaml.YAMLError, ValueError, TypeError) as error:
        raise DocumentBuildError(f"{label} is invalid") from error


def _construct_yaml(text: str, label: str) -> object:
    _preflight_yaml(text, label)
    try:
        return yaml.load(text, Loader=_StrictYAMLLoader)
    except DocumentBuildError:
        raise
    except (RecursionError, MemoryError, yaml.YAMLError, ValueError, TypeError) as error:
        raise DocumentBuildError(f"{label} is invalid") from error


def _load_overlay(path: Path) -> dict[str, Any]:
    try:
        checked = path_without_link_ancestors(path)
        metadata = checked.lstat()
        if is_symlink_or_reparse(checked) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise DocumentBuildError("overlay is unsafe")
        if metadata.st_size > MAX_OVERLAY_BYTES:
            raise DocumentBuildError("overlay is too large")
    except DocumentBuildError:
        raise
    except (OSError, ValueError) as error:
        raise DocumentBuildError("overlay is unsafe") from error
    payload = _read_single(path, MAX_OVERLAY_BYTES, "overlay")
    if len(payload) > MAX_OVERLAY_BYTES:
        raise DocumentBuildError("overlay is too large")
    try:
        text = payload.decode("utf-8")
        value = _construct_yaml(text, "overlay")
    except DocumentBuildError:
        raise
    except (UnicodeDecodeError, RecursionError, MemoryError, yaml.YAMLError, ValueError) as error:
        raise DocumentBuildError("overlay is invalid") from error
    if not isinstance(value, dict):
        raise DocumentBuildError("overlay is invalid")
    _validate_value(value, label="overlay")
    if not set(value).issubset(APPROVED_ZONES):
        raise DocumentBuildError("overlay may contain approved zones only")
    return value


def _load_evidence(root: Path) -> dict[str, str]:
    try:
        value = validate_candidate(_json_or_yaml_candidate(confined_path(root, root / "private/candidate.yml")))
    except OnboardingError as error:
        raise DocumentBuildError("candidate career evidence is invalid") from error
    rows = value.get("career_evidence") if isinstance(value, dict) else None
    if not isinstance(rows, list) or not rows:
        raise DocumentBuildError("candidate career evidence is invalid")
    evidence: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "text"}:
            raise DocumentBuildError("candidate career evidence is invalid")
        identifier, text = row.get("id"), row.get("text")
        if not isinstance(identifier, str) or not identifier or identifier in evidence or not isinstance(text, str) or not text.strip() or CONTROL.search(text):
            raise DocumentBuildError("candidate career evidence is invalid")
        evidence[identifier] = text
    return evidence


def _json_or_yaml_candidate(path: Path) -> dict[str, Any]:
    payload = _read_single(path, MAX_CANDIDATE_BYTES, "candidate profile")
    try:
        value = _construct_yaml(payload.decode("utf-8"), "candidate profile")
    except DocumentBuildError:
        raise
    except (UnicodeDecodeError, RecursionError, MemoryError, yaml.YAMLError) as error:
        raise DocumentBuildError("candidate profile is invalid") from error
    if not isinstance(value, dict):
        raise DocumentBuildError("candidate profile is invalid")
    _validate_value(value, label="candidate profile")
    return value


def _claim(value: object, evidence: dict[str, str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"text", "evidence_ids"}:
        raise DocumentBuildError("overlay claim must contain text and evidence_ids")
    text, identifiers = value["text"], value["evidence_ids"]
    if not isinstance(text, str) or not text.strip() or CONTROL.search(text):
        raise DocumentBuildError("overlay claim text is invalid")
    if not isinstance(identifiers, list) or not identifiers or any(not isinstance(item, str) or not item for item in identifiers):
        raise DocumentBuildError("overlay claim requires nonempty evidence_ids")
    if len(set(identifiers)) != len(identifiers):
        raise DocumentBuildError("overlay evidence_ids must be unique")
    unresolved = [item for item in identifiers if item not in evidence]
    if unresolved:
        raise DocumentBuildError("overlay evidence_id does not resolve")
    normalized_source = unicodedata.normalize("NFKC", text)
    cited_rows = [_normalized_claim_text(unicodedata.normalize("NFKC", evidence[item])) for item in identifiers]
    clauses = [
        item
        for item in re.split(r"(?:[!?;]+|(?<!\d)\.(?!\d)|[\r\n•]+)", normalized_source)
        if item.strip()
    ]
    for clause in clauses:
        numbers = NUMBER_TOKEN.findall(clause)
        dates = DATE_TOKEN.findall(clause)
        version = re.search(r"(?i)\b(?:v(?:ersion)?\s*\d|[a-z][a-z0-9+#.-]*\s+\d+(?:\.\d+)*)\b", clause)
        if not numbers and not dates and version is None:
            continue
        normalized_clause = _normalized_claim_text(clause)
        context = NUMBER_TOKEN.sub(" ", normalized_clause)
        context = DATE_TOKEN.sub(" ", context)
        context = re.sub(r"(?i)\b(?:version|v|percent|per\s+cent)\b", " ", context)
        if not re.search(r"[^\W\d_]", context, flags=re.UNICODE):
            raise DocumentBuildError("quantified claim lacks semantic context in cited evidence")
        if not normalized_clause or not any(normalized_clause in row for row in cited_rows):
            if dates:
                raise DocumentBuildError(f"date token '{dates[0].casefold()}' is not present in cited evidence")
            matched_number = re.match(r"(?i)[-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:e[-+]?\d+)?", numbers[0].strip()) if numbers else None
            token = matched_number.group(0) if matched_number else "version"
            raise DocumentBuildError(f"figure '{html.escape(token)}' is not present in cited evidence")
    return {"text": text, "evidence_ids": list(identifiers)}


def _validated_overlay(value: dict[str, Any], evidence: dict[str, str]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for zone, raw in value.items():
        if zone in {"headline", "summary"}:
            normalized[zone] = _claim(raw, evidence)
        elif zone == "skills":
            if not isinstance(raw, list) or not raw:
                raise DocumentBuildError("skills must be a nonempty list of claims")
            normalized[zone] = [_claim(item, evidence) for item in raw]
        else:
            if not isinstance(raw, dict) or not raw:
                raise DocumentBuildError("experience_bullets must map experience IDs to claim lists")
            bullets: dict[str, list[dict[str, Any]]] = {}
            for identifier, items in raw.items():
                if not isinstance(identifier, str) or not identifier or not isinstance(items, list) or not items:
                    raise DocumentBuildError("experience_bullets is invalid")
                bullets[identifier] = [_claim(item, evidence) for item in items]
            normalized[zone] = bullets
    return normalized


def _validate_template(source: str) -> None:
    if not source or len(source.encode("utf-8")) > MAX_TEMPLATE_BYTES or CONTROL.search(source):
        raise DocumentBuildError("template is unsafe")
    environment = _environment()
    try:
        tree = environment.parse(source)
    except Exception as error:
        raise DocumentBuildError("template is unsafe") from error
    if any(isinstance(item, FORBIDDEN_TEMPLATE_NODES) for item in tree.find_all(nodes.Node)):
        raise DocumentBuildError("template is unsafe")


def _validate_css(source: str) -> None:
    folded = re.sub(r"/\*.*?\*/", "", source.casefold(), flags=re.DOTALL)
    compact = re.sub(r"\s+", "", folded)
    if (
        not source
        or len(source.encode("utf-8")) > MAX_STYLESHEET_BYTES
        or CONTROL.search(source)
        or "\\" in source
        or "@import" in compact
        or "@font-face" in compact
        or "url(" in compact
        or "expression(" in compact
        or "behavior:" in compact
        or "-moz-binding" in compact
        or "javascript:" in compact
        or "file:" in compact
        or "http:" in compact
        or "https:" in compact
        or "</style" in compact
    ):
        raise DocumentBuildError("stylesheet is unsafe")


def _environment() -> SandboxedEnvironment:
    environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=True, enable_async=False)
    environment.globals.clear()
    environment.tests.clear()
    environment.filters.clear()
    environment.filters.update({
        "upper": lambda value: str(value).upper(),
        "lower": lambda value: str(value).lower(),
        "join": lambda value, separator=", ": separator.join(str(item) for item in value),
        "date": lambda value: str(value),
        "markdown_text": lambda value: str(value),
    })
    return environment


class _HTMLGate(HTMLParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._style_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        folded = tag.casefold()
        if folded in ACTIVE_TAGS:
            raise DocumentBuildError("rendered HTML contains active content")
        for name, value in attrs:
            attribute = name.casefold()
            content = value or ""
            if attribute.startswith("on") or attribute in {"href", "srcset", "action", "formaction"}:
                raise DocumentBuildError("rendered HTML contains active content")
            if folded == "meta" and attribute == "http-equiv":
                raise DocumentBuildError("rendered HTML contains active content")
            if attribute == "src":
                if folded != "img" or not content.startswith("data:image/"):
                    raise DocumentBuildError("rendered HTML contains resource access")
                _self_contained_url_fetcher(content)
            if attribute == "style":
                _validate_inline_style(content)
        if folded == "style":
            self._style_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "style" and self._style_depth:
            self._style_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._style_depth:
            _validate_css(data)


def _validate_inline_style(source: str) -> None:
    folded = source.casefold()
    if CONTROL.search(source) or "url(" in folded or "expression(" in folded or "javascript:" in folded or "file:" in folded or "http:" in folded or "https:" in folded:
        raise DocumentBuildError("rendered HTML contains resource access")


def _validate_html(source: str) -> None:
    if len(source.encode("utf-8")) > 4 * 1024 * 1024 or CONTROL.search(source):
        raise DocumentBuildError("rendered HTML is unsafe")
    parser = _HTMLGate(convert_charrefs=False)
    try:
        parser.feed(source)
        parser.close()
    except DocumentBuildError:
        raise
    except Exception as error:
        raise DocumentBuildError("rendered HTML is unsafe") from error


def _asset_data_uris(creator_dir: Path) -> list[str]:
    assets = creator_dir / "assets"
    if not assets.exists():
        return []
    if is_symlink_or_reparse(assets) or not assets.is_dir():
        raise DocumentBuildError("creator assets are unsafe")
    uris: list[str] = []
    paths = sorted((path for path in assets.rglob("*") if path.is_file()), key=lambda path: path.relative_to(assets).as_posix())
    for path in paths:
        payload = _read_single(path, MAX_RASTER_BYTES, "creator raster asset")
        mime, _ = _validate_raster(payload, path.suffix)
        uris.append(f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}")
    return uris


def _block_style(block: LayoutBlock, substitutions: dict[str, str]) -> str:
    x0, y0, x1, y1 = block.bbox
    font = substitutions.get(block.font_names[0], block.font_names[0]) if block.font_names else "sans-serif"
    size = max(block.font_sizes) if block.font_sizes else 10.0
    color = block.colors[0] if block.colors else 0
    return (
        f"left:{x0:.3f}pt;top:{y0:.3f}pt;width:{max(0.5, x1-x0):.3f}pt;"
        f"min-height:{max(0.5, y1-y0):.3f}pt;font-family:{font},sans-serif;"
        f"font-size:{size:.3f}pt;color:{_color_css(color)}"
    )


def _source_rules(baseline: Path) -> dict[int, list[dict[str, str]]]:
    rules: dict[int, list[dict[str, str]]] = {}
    payload = _read_single(baseline, MAX_PDF_BYTES, "source baseline")
    try:
        with fitz.open(stream=payload, filetype="pdf") as document:
            for page_number, page in enumerate(document, 1):
                page_rules: list[dict[str, str]] = []
                for drawing in page.get_drawings():
                    rect = drawing.get("rect")
                    if not isinstance(rect, fitz.Rect) or (rect.width <= 0 and rect.height <= 0):
                        continue
                    color = drawing.get("color") or drawing.get("fill") or (0.0, 0.0, 0.0)
                    rgb = tuple(max(0, min(255, round(float(item) * 255))) for item in color[:3])
                    width = max(float(drawing.get("width") or 1.0), rect.height, 0.5)
                    page_rules.append({
                        "style": (
                            f"left:{rect.x0:.3f}pt;top:{rect.y0:.3f}pt;width:{max(rect.width,0.5):.3f}pt;"
                            f"height:{width:.3f}pt;background:#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
                        )
                    })
                rules[page_number] = page_rules
    except DocumentBuildError:
        raise
    except Exception as error:
        raise DocumentBuildError("source baseline is invalid") from error
    return rules


def _overlay_texts(overlay: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for zone, value in overlay.items():
        if zone in {"headline", "summary"}:
            texts.append(value["text"])
        elif zone == "skills":
            texts.extend(item["text"] for item in value)
        else:
            for items in value.values():
                texts.extend(item["text"] for item in items)
    return texts


def _apply_overlay(blocks: list[dict[str, Any]], overlay: dict[str, Any]) -> None:
    for zone, value in overlay.items():
        slots = sorted(
            (item for item in blocks if item.get("kind") == "text" and item.get("zone") == zone),
            key=lambda item: (item["page_number"], item["y"], item["x"], item.get("block_id", "")),
        )
        if zone in {"headline", "summary"}:
            replacements = [value["text"]]
        elif zone == "skills":
            replacements = [item["text"] for item in value]
        else:
            replacements = [item["text"] for values in value.values() for item in values]
        if not slots or len(replacements) > len(slots):
            raise DocumentBuildError(f"creator has insufficient {zone} slots")
        for slot, replacement in zip(slots, replacements):
            slot["text"] = replacement


def _render_context(root: Path, descriptor: LayoutDescriptor, creator: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    creator_dir = root / "private/cv-creator"
    assets = _asset_data_uris(creator_dir)
    image_index = 0
    page_map: dict[int, dict[str, Any]] = {
        page.page_number: {"page_number": page.page_number, "blocks": [], "rules": []} for page in descriptor.pages
    }
    mapping = _validated_block_mapping(root, descriptor, creator)
    text_ordinal = 0
    for block in descriptor.blocks:
        x0, y0, _, _ = block.bbox
        if block.block_type == "image":
            if image_index >= len(assets):
                continue
            x0, y0, x1, y1 = block.bbox
            page_map[block.page_number]["blocks"].append({
                "kind": "image",
                "src": assets[image_index],
                "style": f"left:{x0:.3f}pt;top:{y0:.3f}pt;width:{max(x1-x0,.5):.3f}pt;height:{max(y1-y0,.5):.3f}pt",
                "page_number": block.page_number,
                "x": x0,
                "y": y0,
                "font_size": 0.0,
            })
            image_index += 1
        elif block.text.strip():
            text_ordinal += 1
            block_id = _block_identifier(block, text_ordinal)
            page_map[block.page_number]["blocks"].append({
                "kind": "text",
                "text": block.text,
                "style": _block_style(block, creator["font_substitutions"]),
                "page_number": block.page_number,
                "x": x0,
                "y": y0,
                "font_size": max(block.font_sizes) if block.font_sizes else 10.0,
                "block_id": block_id,
                "zone": mapping[block_id]["zone"],
            })
    all_blocks = [block for page in page_map.values() for block in page["blocks"]]
    _apply_overlay(all_blocks, overlay)
    rules = _source_rules(creator_dir / "baseline/baseline.pdf")
    for page_number, items in rules.items():
        page_map[page_number]["rules"] = items
    stylesheet = _read_single(creator_dir / "style.css", MAX_STYLESHEET_BYTES, "creator stylesheet").decode("utf-8")
    _validate_css(stylesheet)
    return {"stylesheet": stylesheet, "pages": list(page_map.values())}


def _render_html(root: Path, descriptor: LayoutDescriptor, creator: dict[str, Any], overlay: dict[str, Any]) -> str:
    template_path = root / "private/cv-creator/template.html.j2"
    source = _read_single(template_path, MAX_TEMPLATE_BYTES, "creator template").decode("utf-8")
    _validate_template(source)
    try:
        rendered = _environment().from_string(source).render(_render_context(root, descriptor, creator, overlay))
    except DocumentBuildError:
        raise
    except Exception as error:
        raise DocumentBuildError("template rendering failed") from error
    _validate_html(rendered)
    return rendered


def _self_contained_url_fetcher(url: str, timeout: int = 10, ssl_context: Any = None) -> dict[str, Any]:
    del timeout, ssl_context
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)", url)
    if match is None:
        raise DocumentBuildError("resource access denied")
    try:
        payload = base64.b64decode(match.group(2), validate=True)
        _validate_raster(payload, {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[match.group(1)])
    except Exception as error:
        raise DocumentBuildError("resource access denied") from error
    return {"string": payload, "mime_type": match.group(1), "redirected_url": url}


def _weasy_worker(html_source: str, output_path: str, smoke: bool, gate: Any) -> None:
    try:
        if not gate.wait(timeout=10):
            raise RuntimeError
        environment = _minimal_environment(Path(output_path).parent)
        os.environ.clear()
        os.environ.update(environment)
        import weasyprint

        if weasyprint.__version__ != "69.0":
            raise RuntimeError
        from weasyprint import HTML

        source = "<!doctype html><meta charset=utf-8><p>smoke</p>" if smoke else html_source
        HTML(string=source, url_fetcher=_self_contained_url_fetcher).write_pdf(output_path)
    except BaseException:
        raise SystemExit(70) from None


def _weasy_smoke() -> bool:
    global _SMOKE_OK
    with _SMOKE_LOCK:
        if _SMOKE_OK:
            return True
        with tempfile.TemporaryDirectory(prefix="job-pipeline-render-smoke-") as directory:
            output = Path(directory) / "smoke.pdf"
            ok = _run_isolated_worker(
                _weasy_worker,
                ("", str(output), True),
                timeout_seconds=RENDER_TIMEOUT_SECONDS,
                memory_bytes=RENDER_MEMORY_BYTES,
            )
            if ok and output.is_file() and output.stat().st_size > 0:
                try:
                    with fitz.open(output) as document:
                        ok = document.page_count == 1
                except Exception:
                    ok = False
            _SMOKE_OK = bool(ok)
            return _SMOKE_OK


def _minimal_environment(temp_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {"TEMP": str(temp_dir), "TMP": str(temp_dir), "HOME": str(temp_dir)}
    for key in ("SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT"):
        value = os.environ.get(key)
        if value:
            result[key] = value
    result.update({"NO_PROXY": "*", "no_proxy": "*", "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": ""})
    return result


def _render_pdf_backend(html_source: str, output: Path) -> str:
    if _weasy_smoke():
        if _run_isolated_worker(
            _weasy_worker,
            (html_source, str(output), False),
            timeout_seconds=RENDER_TIMEOUT_SECONDS,
            memory_bytes=RENDER_MEMORY_BYTES,
        ) and output.is_file():
            return "weasyprint-69"
    raise DocumentBuildError("WeasyPrint 69.0 is required; install the pinned requirements and retry")


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _pdf_metrics(path: Path) -> dict[str, Any]:
    payload = _read_single(path, MAX_PDF_BYTES, "rendered PDF")
    pages: list[dict[str, Any]] = []
    try:
        with fitz.open(stream=payload, filetype="pdf") as document:
            if document.is_encrypted or not 1 <= document.page_count <= 4:
                raise DocumentBuildError("rendered PDF is invalid")
            for page in document:
                blocks: list[dict[str, Any]] = []
                colors: set[int] = set()
                sizes: list[float] = []
                fonts: set[str] = set()
                raw = page.get_text("dict")
                for block in raw.get("blocks", []):
                    if block.get("type") != 0:
                        continue
                    spans = [span for line in block.get("lines", []) for span in line.get("spans", [])]
                    text = "\n".join("".join(str(span.get("text", "")) for span in line.get("spans", [])) for line in block.get("lines", []))
                    if not text.strip():
                        continue
                    bbox = tuple(float(value) for value in block.get("bbox", (0, 0, 0, 0)))
                    blocks.append({
                        "text": text,
                        "bbox": bbox,
                        "sizes": [float(span.get("size", 0)) for span in spans],
                        "fonts": [str(span.get("font", "")) for span in spans],
                        "colors": [int(span.get("color", 0)) for span in spans],
                    })
                    sizes.extend(float(span.get("size", 0)) for span in spans)
                    colors.update(int(span.get("color", 0)) for span in spans)
                    fonts.update(str(span.get("font", "")) for span in spans)
                images: list[dict[str, Any]] = []
                for item in page.get_image_info(hashes=True, xrefs=True):
                    bbox = tuple(float(value) for value in item.get("bbox", (0, 0, 0, 0)))
                    width = int(item.get("width", 0))
                    height = int(item.get("height", 0))
                    try:
                        xref = int(item.get("xref", 0))
                        pixmap = fitz.Pixmap(document, xref) if xref > 0 else page.get_pixmap(clip=fitz.Rect(bbox), alpha=True)
                        decoded = (
                            f"{pixmap.width}:{pixmap.height}:{pixmap.n}:{int(pixmap.alpha)}:".encode("ascii")
                            + bytes(pixmap.samples)
                        )
                    except Exception as error:
                        raise DocumentBuildError("rendered PDF image is invalid") from error
                    images.append({
                        "page_number": page.number + 1,
                        "bbox": bbox,
                        "width": width,
                        "height": height,
                        "pixel_sha256": _sha(decoded),
                    })
                drawings: list[dict[str, Any]] = []
                for drawing in page.get_drawings():
                    rect = drawing.get("rect")
                    if not isinstance(rect, fitz.Rect) or (rect.width <= 0 and rect.height <= 0):
                        continue
                    if rect.width >= page.rect.width - 1 and rect.height >= page.rect.height - 1:
                        continue
                    raw_color = drawing.get("color") or drawing.get("fill") or (0.0, 0.0, 0.0)
                    rgb = tuple(max(0, min(255, round(float(item) * 255))) for item in tuple(raw_color)[:3])
                    while len(rgb) < 3:
                        rgb += (0,)
                    drawings.append({
                        "bbox": tuple(float(value) for value in rect),
                        "color": (rgb[0] << 16) | (rgb[1] << 8) | rgb[2],
                        "width": float(drawing.get("width") or max(rect.height, 0.5)),
                    })
                pages.append({
                    "width": float(page.rect.width), "height": float(page.rect.height), "text": page.get_text(),
                    "blocks": blocks, "images": images, "drawings": drawings, "colors": colors,
                    "sizes": sizes, "fonts": fonts,
                })
    except DocumentBuildError:
        raise
    except Exception as error:
        raise DocumentBuildError("rendered PDF is invalid") from error
    return {"payload": payload, "pages": pages}


def _image_bbox(image: object) -> tuple[float, float, float, float]:
    raw = image.get("bbox") if isinstance(image, dict) else image
    if not isinstance(raw, (tuple, list)) or len(raw) != 4:
        raise DocumentBuildError("rendered PDF image geometry is invalid")
    return tuple(float(value) for value in raw)


def _bounds(page: dict[str, Any]) -> tuple[float, float, float, float] | None:
    boxes = [block["bbox"] for block in page["blocks"]] + [_image_bbox(image) for image in page["images"]]
    if not boxes:
        return None
    return min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes)


def _columns(page: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for block in sorted(page["blocks"], key=lambda item: item["bbox"][0]):
        x = block["bbox"][0]
        if not values or abs(x - values[-1]) > 18:
            values.append(x)
    return values


def _inside(page: dict[str, Any]) -> bool:
    boxes = [block["bbox"] for block in page["blocks"]] + [_image_bbox(image) for image in page["images"]]
    boxes.extend(drawing["bbox"] for drawing in page["drawings"])
    for x0, y0, x1, y1 in boxes:
        if x0 < -1 or y0 < -1 or x1 > page["width"] + 1 or y1 > page["height"] + 1:
            return False
    return True


def _no_material_overlap(page: dict[str, Any]) -> bool:
    typed = [("text", fitz.Rect(block["bbox"])) for block in page["blocks"]]
    typed.extend(("image", fitz.Rect(_image_bbox(image))) for image in page["images"])
    for index, (left_kind, left) in enumerate(typed):
        for right_kind, right in typed[index + 1 :]:
            intersection = left & right
            if (
                not intersection.is_empty
                and intersection.get_area() > 4
                and intersection.get_area() > 0.2 * min(left.get_area(), right.get_area())
                and (left_kind == "image" or right_kind == "image" or left_kind == right_kind == "text")
            ):
                return False
    return True


def _validate_immutable_blocks(
    descriptor: LayoutDescriptor, pages: list[dict[str, Any]], mapping: list[dict[str, Any]]
) -> None:
    expected_rows = _default_block_mapping(descriptor)
    if not isinstance(mapping, list) or len(mapping) != len(expected_rows):
        raise DocumentBuildError("rendered PDF immutable mapping failed")
    text_blocks = [block for block in descriptor.blocks if block.block_type == "text" and block.text.strip()]
    for block, expected, row in zip(text_blocks, expected_rows, mapping):
        if row != expected and any(row.get(key) != expected[key] for key in ("block_id", "page_number", "source_text_sha256", "source_geometry_sha256")):
            raise DocumentBuildError("rendered PDF immutable mapping failed")
        if row.get("immutable") is not True:
            continue
        page = pages[block.page_number - 1] if 1 <= block.page_number <= len(pages) else None
        if page is None:
            raise DocumentBuildError("rendered PDF immutable block failed")
        normalized = _normalize_text(block.text)
        candidates = [item for item in page["blocks"] if _normalize_text(item["text"]) == normalized]
        retained = False
        for item in candidates:
            if not all(abs(a - b) <= 8 for a, b in zip(block.bbox, item["bbox"])):
                continue
            expected_sizes = [value for value in block.font_sizes if value > 0]
            actual_sizes = [value for value in item.get("sizes", []) if value > 0]
            if expected_sizes and (not actual_sizes or any(min(abs(value - actual) for actual in actual_sizes) > 2 for value in expected_sizes)):
                continue
            if block.colors and not set(block.colors).issubset(set(item.get("colors", []))):
                continue
            retained = True
            break
        if not retained:
            raise DocumentBuildError("rendered PDF immutable block failed")


def _validate_build_pdf(
    path: Path,
    descriptor: LayoutDescriptor,
    overlay_text: list[str],
    block_mapping: list[dict[str, Any]] | None = None,
) -> tuple[int, list[bytes]]:
    metrics = _pdf_metrics(path)
    pages = metrics["pages"]
    if len(pages) != descriptor.page_count:
        raise DocumentBuildError("rendered PDF page count failed")
    for expected, actual in zip(descriptor.pages, pages):
        if abs(expected.width_points - actual["width"]) > 1 or abs(expected.height_points - actual["height"]) > 1:
            raise DocumentBuildError("rendered PDF page dimensions failed")
        if not _inside(actual):
            raise DocumentBuildError("rendered PDF clipping or overflow failed")
        if not _no_material_overlap(actual):
            raise DocumentBuildError("rendered PDF overlap failed")
        if sum(len(block["text"]) for block in actual["blocks"]) / max(1.0, actual["width"] * actual["height"]) > 0.25:
            raise DocumentBuildError("rendered PDF density failed")
    combined = _normalize_text(" ".join(page["text"] for page in pages))
    for text in overlay_text:
        if _normalize_text(text) not in combined:
            raise DocumentBuildError("rendered PDF lost approved overlay text")
    if block_mapping is not None:
        _validate_immutable_blocks(descriptor, pages, block_mapping)
    previews: list[bytes] = []
    try:
        with fitz.open(stream=metrics["payload"], filetype="pdf") as document:
            for page in document:
                payload = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")
                with Image.open(io.BytesIO(payload)) as image:
                    if image.width > MAX_PREVIEW_WIDTH or image.height > MAX_PREVIEW_HEIGHT or image.width * image.height > MAX_RASTER_PIXELS:
                        raise DocumentBuildError("preview image bounds failed")
                if len(payload) > MAX_RASTER_BYTES:
                    raise DocumentBuildError("preview image bounds failed")
                previews.append(payload)
    except DocumentBuildError:
        raise
    except Exception as error:
        raise DocumentBuildError("preview rendering failed") from error
    return len(pages), previews


def _render_private_baseline(root: Path, destination: Path) -> str:
    descriptor, _ = _load_layout(root)
    creator = _load_creator(root)
    return _render_pdf_backend(_render_html(root, descriptor, creator, {}), destination)


def _comparison_checks(descriptor: LayoutDescriptor, source: dict[str, Any], recreated: dict[str, Any], creator: dict[str, Any]) -> dict[str, bool]:
    source_pages, recreated_pages = source["pages"], recreated["pages"]
    paired = list(zip(source_pages, recreated_pages))
    source_text = _normalize_text(" ".join(page["text"] for page in source_pages))
    recreated_text = _normalize_text(" ".join(page["text"] for page in recreated_pages))
    source_block_texts = [_normalize_text(block["text"]) for page in source_pages for block in page["blocks"] if _normalize_text(block["text"])]
    recreated_block_texts = [_normalize_text(block["text"]) for page in recreated_pages for block in page["blocks"] if _normalize_text(block["text"])]
    source_present = all(text in recreated_text for text in source_block_texts)
    unexpected = all(any(text == source_text_item for source_text_item in source_block_texts) for text in recreated_block_texts)
    dimensions = len(source_pages) == len(recreated_pages) and all(
        abs(left["width"] - right["width"]) <= 1 and abs(left["height"] - right["height"]) <= 1
        for left, right in paired
    )
    geometry = True
    spacing = True
    typography = len(source_pages) == len(recreated_pages)
    colors = typography
    bullets = typography
    font_gate = typography
    substitutions = creator["font_substitutions"]

    def normalized_font(value: str) -> str:
        value = re.sub(r"^[A-Z]{6}\+", "", value)
        return re.sub(r"[^a-z0-9]", "", value.casefold())

    aliases = {
        "arial": {"arial", "arialmt", "liberationsans"},
        "timesnewroman": {"timesnewroman", "timesnewromanpsmt", "liberationserif"},
        "couriernew": {"couriernew", "couriernewpsmt", "liberationmono"},
    }

    def family_matches(expected: str, actual: str) -> bool:
        wanted = normalized_font(expected)
        observed = normalized_font(actual)
        return observed == wanted or observed in aliases.get(wanted, {wanted})

    for left, right in paired:
        left_bounds, right_bounds = _bounds(left), _bounds(right)
        if left_bounds is None or right_bounds is None:
            geometry = geometry and left_bounds == right_bounds
        else:
            geometry = geometry and all(abs(a - b) <= 8 for a, b in zip(left_bounds, right_bounds))
        left_columns, right_columns = _columns(left), _columns(right)
        geometry = geometry and len(left_columns) == len(right_columns) and all(abs(a - b) <= 8 for a, b in zip(left_columns, right_columns))
        left_positions = [block["bbox"] for block in left["blocks"]]
        right_positions = [block["bbox"] for block in right["blocks"]]
        spacing = spacing and len(left_positions) == len(right_positions) and all(
            all(abs(a - b) <= 8 for a, b in zip(left_box, right_box))
            for left_box, right_box in zip(left_positions, right_positions)
        )
        typography = typography and len(left["blocks"]) == len(right["blocks"])
        colors = colors and len(left["blocks"]) == len(right["blocks"])
        bullets = bullets and len(left["blocks"]) == len(right["blocks"])
        font_gate = font_gate and len(left["blocks"]) == len(right["blocks"])
        for left_block, right_block in zip(left["blocks"], right["blocks"]):
            left_sizes = [value for value in left_block.get("sizes", []) if value > 0]
            right_sizes = [value for value in right_block.get("sizes", []) if value > 0]
            typography = typography and len(left_sizes) == len(right_sizes) and all(abs(a - b) <= 1 for a, b in zip(left_sizes, right_sizes))
            colors = colors and left_block.get("colors", []) == right_block.get("colors", [])
            left_bullets = sum(left_block["text"].count(char) for char in "•▪◦‣-")
            right_bullets = sum(right_block["text"].count(char) for char in "•▪◦‣-")
            bullets = bullets and left_bullets == right_bullets
            source_fonts = [font for font in left_block.get("fonts", []) if font]
            actual_fonts = [font for font in right_block.get("fonts", []) if font]
            if len(source_fonts) != len(actual_fonts):
                font_gate = False
            for source_font, actual_font in zip(source_fonts, actual_fonts):
                if family_matches(source_font, actual_font):
                    continue
                replacement = substitutions.get(source_font)
                if replacement is None or not family_matches(replacement, actual_font):
                    font_gate = False

    source_images = [box for page in source_pages for box in page["images"]]
    recreated_images = [box for page in recreated_pages for box in page["images"]]
    asset_placement = len(source_pages) == len(recreated_pages)
    for left_page, right_page in paired:
        asset_placement = asset_placement and len(left_page["images"]) == len(right_page["images"])
        for left_image, right_image in zip(left_page["images"], right_page["images"]):
            asset_placement = asset_placement and all(
                abs(a - b) <= 8
                for a, b in zip(_image_bbox(left_image), _image_bbox(right_image))
            )
            if isinstance(left_image, dict) or isinstance(right_image, dict):
                asset_placement = asset_placement and isinstance(left_image, dict) and isinstance(right_image, dict)
                if isinstance(left_image, dict) and isinstance(right_image, dict):
                    asset_placement = asset_placement and (
                        left_image.get("page_number") == right_image.get("page_number")
                        and left_image.get("width") == right_image.get("width")
                        and left_image.get("height") == right_image.get("height")
                        and left_image.get("pixel_sha256") == right_image.get("pixel_sha256")
                    )
    source_drawings = [drawing for page in source_pages for drawing in page["drawings"]]
    recreated_drawings = [drawing for page in recreated_pages for drawing in page["drawings"]]
    rule_gate = len(source_pages) == len(recreated_pages)
    for left_page, right_page in paired:
        rule_gate = rule_gate and len(left_page["drawings"]) == len(right_page["drawings"])
        for left, right in zip(left_page["drawings"], right_page["drawings"]):
            rule_gate = rule_gate and all(abs(a - b) <= 2 for a, b in zip(left["bbox"], right["bbox"]))
            rule_gate = rule_gate and left["color"] == right["color"] and abs(left["width"] - right["width"]) <= 0.5
    largest_source = max((block for page in source_pages for block in page["blocks"]), key=lambda block: max(block["sizes"] or [0]), default=None)
    headline = largest_source is None or _normalize_text(largest_source["text"]) in recreated_text
    section_positions = [source_text.find(_normalize_text(item)) for item in descriptor.section_order]
    recreated_section_positions = [recreated_text.find(_normalize_text(item)) for item in descriptor.section_order]
    section_order = all(position >= 0 for position in section_positions + recreated_section_positions) and section_positions == sorted(section_positions) and recreated_section_positions == sorted(recreated_section_positions)
    density = len(source_pages) == len(recreated_pages) and all(
        abs(
            len(_normalize_text(left["text"])) / max(1.0, left["width"] * left["height"])
            - len(_normalize_text(right["text"])) / max(1.0, right["width"] * right["height"])
        )
        <= max(0.0001, (len(_normalize_text(left["text"])) / max(1.0, left["width"] * left["height"])) * 0.05)
        for left, right in paired
    )
    return {
        "page_count": len(source_pages) == len(recreated_pages) == descriptor.page_count,
        "page_dimensions": dimensions,
        "source_text": source_present,
        "unexpected_text": unexpected,
        "section_order": section_order,
        "margins_columns": geometry,
        "font_substitutions": font_gate,
        "image_count": len(source_images) == len(recreated_images) == descriptor.image_count,
        "clipping": all(_inside(page) for page in recreated_pages),
        "overlap": all(_no_material_overlap(page) for page in recreated_pages),
        "overflow": dimensions and all(_inside(page) for page in recreated_pages),
        "density": density,
        "headline": headline,
        "typography": typography,
        "colors": colors,
        "spacing": spacing,
        "bullets": bullets,
        "rules": rule_gate,
        "asset_placement": asset_placement,
    }


def _render_pngs(pdf_payload: bytes) -> list[bytes]:
    output: list[bytes] = []
    try:
        with fitz.open(stream=pdf_payload, filetype="pdf") as document:
            for page in document:
                payload = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")
                with Image.open(io.BytesIO(payload)) as image:
                    if image.width > MAX_PREVIEW_WIDTH or image.height > MAX_PREVIEW_HEIGHT or image.width * image.height > MAX_RASTER_PIXELS:
                        raise DocumentBuildError("comparison image bounds failed")
                if len(payload) > MAX_RASTER_BYTES:
                    raise DocumentBuildError("comparison image bounds failed")
                output.append(payload)
    except DocumentBuildError:
        raise
    except Exception as error:
        raise DocumentBuildError("comparison image rendering failed") from error
    return output


def _side_by_side(left: bytes, right: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(left)) as left_image, Image.open(io.BytesIO(right)) as right_image:
            width, height = left_image.width + right_image.width, max(left_image.height, right_image.height)
            if width > MAX_PREVIEW_WIDTH * 2 or height > MAX_PREVIEW_HEIGHT or width * height > MAX_RASTER_PIXELS * 2:
                raise DocumentBuildError("comparison image bounds failed")
            combined = Image.new("RGB", (width, height), "white")
            combined.paste(left_image.convert("RGB"), (0, 0))
            combined.paste(right_image.convert("RGB"), (left_image.width, 0))
            buffer = io.BytesIO()
            combined.save(buffer, format="PNG", optimize=True)
            payload = buffer.getvalue()
            if len(payload) > MAX_RASTER_BYTES * 2:
                raise DocumentBuildError("comparison image bounds failed")
            return payload
    except DocumentBuildError:
        raise
    except Exception as error:
        raise DocumentBuildError("comparison image creation failed") from error


def _comparison_artifacts(source_pdf: bytes, recreated_pdf: bytes) -> dict[str, bytes]:
    source_pngs = _render_pngs(source_pdf)
    recreated_pngs = _render_pngs(recreated_pdf)
    if len(source_pngs) != len(recreated_pngs):
        raise DocumentBuildError("comparison page set is incomplete")
    artifacts: dict[str, bytes] = {"recreated-baseline.pdf": recreated_pdf}
    for page_number, payload in enumerate(source_pngs, 1):
        artifacts[f"source-page-{page_number:03d}.png"] = payload
    for page_number, payload in enumerate(recreated_pngs, 1):
        artifacts[f"recreated-page-{page_number:03d}.png"] = payload
    for page_number, pair in enumerate(zip(source_pngs, recreated_pngs), 1):
        artifacts[f"comparison-page-{page_number:03d}.png"] = _side_by_side(*pair)
    return artifacts


def _artifact_manifest(artifacts: dict[str, bytes]) -> dict[str, str]:
    return {name: _sha(payload) for name, payload in sorted(artifacts.items())}


def _expected_artifact_names(page_count: int) -> set[str]:
    names = {"recreated-baseline.pdf"}
    for prefix in ("source-page", "recreated-page", "comparison-page"):
        names.update(f"{prefix}-{page_number:03d}.png" for page_number in range(1, page_count + 1))
    return names


def _read_comparison_artifacts(root: Path, descriptor: LayoutDescriptor) -> dict[str, bytes]:
    baseline_dir = confined_path(root, root / "private/cv-creator/baseline")
    expected = _expected_artifact_names(descriptor.page_count)
    try:
        present = {
            path.name
            for path in baseline_dir.iterdir()
            if path.name == "recreated-baseline.pdf"
            or re.fullmatch(r"(?:source|recreated|comparison)-page-\d{3}\.png", path.name)
        }
    except OSError as error:
        raise DocumentBuildError("creator comparison is incomplete") from error
    if present != expected:
        raise DocumentBuildError("creator comparison is incomplete")
    result: dict[str, bytes] = {}
    for name in sorted(expected):
        maximum = MAX_PDF_BYTES if name.endswith(".pdf") else MAX_RASTER_BYTES * 2
        result[name] = _read_single(baseline_dir / name, maximum, "creator comparison artifact")
    recreated_pdf = result["recreated-baseline.pdf"]
    source_pdf = _read_single(baseline_dir / "baseline.pdf", MAX_PDF_BYTES, "source baseline")
    regenerated = _comparison_artifacts(source_pdf, recreated_pdf)
    if set(regenerated) != set(result) or any(regenerated[name] != result[name] for name in result):
        raise DocumentBuildError("creator comparison artifacts do not match PDFs")
    return result


def _stable_artifact_manifest(root: Path, descriptor: LayoutDescriptor) -> dict[str, str]:
    baseline_dir = confined_path(root, root / "private/cv-creator/baseline")
    expected = _expected_artifact_names(descriptor.page_count)
    try:
        present = {
            path.name
            for path in baseline_dir.iterdir()
            if path.name == "recreated-baseline.pdf"
            or re.fullmatch(r"(?:source|recreated|comparison)-page-\d{3}\.png", path.name)
        }
    except OSError as error:
        raise DocumentBuildError("creator comparison is incomplete") from error
    if present != expected:
        raise DocumentBuildError("creator comparison is incomplete")
    manifest: dict[str, str] = {}
    for name in sorted(expected):
        maximum = MAX_PDF_BYTES if name.endswith(".pdf") else MAX_RASTER_BYTES * 2
        manifest[name] = _sha(_read_single(baseline_dir / name, maximum, "creator comparison artifact"))
    return manifest


def _restore_comparison_artifacts(root: Path, paths: set[Path], prior: dict[Path, bytes]) -> None:
    for path in sorted(paths, key=lambda item: item.name):
        try:
            if path in prior:
                atomic_write_bytes(path, prior[path], project_root=root)
            else:
                path.unlink(missing_ok=True)
        except Exception:
            pass


def _publish_comparison_artifacts(
    root: Path, artifacts: dict[str, bytes], page_count: int
) -> tuple[dict[Path, bytes], set[Path]]:
    baseline_dir = confined_path(root, root / "private/cv-creator/baseline")
    expected = _expected_artifact_names(page_count)
    if set(artifacts) != expected:
        raise DocumentBuildError("comparison artifact set is invalid")
    managed = {
        path
        for path in baseline_dir.iterdir()
        if path.name == "recreated-baseline.pdf"
        or re.fullmatch(r"(?:source|recreated|comparison)-page-\d{3}\.png", path.name)
    }
    targets = {baseline_dir / name for name in expected}
    prior: dict[Path, bytes] = {}
    for path in managed | targets:
        if path.exists() or is_symlink_or_reparse(path):
            maximum = MAX_PDF_BYTES if path.suffix.casefold() == ".pdf" else MAX_RASTER_BYTES * 2
            prior[path] = _read_single(path, maximum, "existing comparison artifact")
    try:
        for path in sorted(targets, key=lambda item: item.name):
            atomic_write_bytes(path, artifacts[path.name], project_root=root)
        for path in sorted(managed - targets, key=lambda item: item.name):
            confined_path(root, path).unlink()
        return prior, managed | targets
    except Exception as error:
        _restore_comparison_artifacts(root, managed | targets, prior)
        raise DocumentBuildError("comparison publication failed") from error


def compare_baseline(project_root: Path) -> ComparisonReport:
    root = _root(project_root)
    with _creator_locks(root):
        try:
            runtime = load_runtime(root)
        except (OnboardingError, OSError, ValueError) as error:
            raise DocumentBuildError("registered master CV is invalid") from error
        runtime["onboarding"]["creator_ready"] = False
        runtime["submission_mode"] = "review_only"
        runtime["auto_apply_approved_at"] = None
        save_runtime(root, runtime)
        creator = _load_creator(root)
        creator["automated_validation"] = False
        _revoke_values(creator, runtime, clear_validation=True)
        atomic_write_json(root / "private/cv-creator/creator.json", creator, project_root=root)
        descriptor, layout_payload = _load_layout(root)
        _, source_payload, runtime = _registered_source(root)
        if creator["source_sha256"] != descriptor.source_sha256 or descriptor.source_sha256 != _sha(source_payload):
            raise DocumentBuildError("registered source hash does not match creator")
        creator_dir = root / "private/cv-creator"
        baseline_dir = confined_mkdir(root, creator_dir / "baseline")
        source_pdf = baseline_dir / "baseline.pdf"
        baseline_payload = _read_single(source_pdf, MAX_PDF_BYTES, "source baseline")
        mapping = list(_validated_block_mapping(root, descriptor, creator).values())
        mapping_hash = _mapping_sha256(mapping)
        content_hash = compute_creator_content_sha256(creator_dir, creator["font_substitutions"])
        creator["creator_content_sha256"] = content_hash
        creator["validation"] = {
            "layout_sha256": _sha(layout_payload),
            "source_baseline_sha256": _sha(baseline_payload),
            "block_mapping": mapping,
            "approved_mapping_sha256": "",
            "checks": {},
            "comparison": {},
        }
        atomic_write_json(creator_dir / "creator.json", creator, project_root=root)
        source_metrics = _pdf_metrics(source_pdf)
        with tempfile.TemporaryDirectory(prefix=".comparison-stage-", dir=creator_dir) as directory:
            stage = Path(directory)
            recreated_temp = stage / "recreated-baseline.pdf"
            _render_private_baseline(root, recreated_temp)
            recreated_metrics = _pdf_metrics(recreated_temp)
            checks = _comparison_checks(descriptor, source_metrics, recreated_metrics, creator)
            artifacts = _comparison_artifacts(source_metrics["payload"], recreated_metrics["payload"])
            for name, payload in artifacts.items():
                atomic_write_bytes(stage / name, payload, project_root=creator_dir)
            for name, payload in artifacts.items():
                if _read_single(stage / name, MAX_PDF_BYTES if name.endswith(".pdf") else MAX_RASTER_BYTES * 2, "staged comparison artifact") != payload:
                    raise DocumentBuildError("comparison staging failed")

            current_descriptor, current_layout = _load_layout(root)
            _, current_source, _ = _registered_source(root)
            current_creator = _load_creator(root)
            current_baseline = _read_single(source_pdf, MAX_PDF_BYTES, "source baseline")
            current_mapping = list(_validated_block_mapping(root, current_descriptor, current_creator).values())
            if (
                current_descriptor.source_sha256 != descriptor.source_sha256
                or _sha(current_layout) != _sha(layout_payload)
                or _sha(current_source) != _sha(source_payload)
                or _sha(current_baseline) != _sha(baseline_payload)
                or _mapping_sha256(current_mapping) != mapping_hash
                or compute_creator_content_sha256(creator_dir, current_creator["font_substitutions"]) != content_hash
            ):
                raise DocumentBuildError("creator changed during comparison")
            prior, managed = _publish_comparison_artifacts(root, artifacts, descriptor.page_count)
            creator = current_creator
            creator["creator_content_sha256"] = content_hash
            creator["automated_validation"] = all(checks.values())
            creator["validation"] = {
                "layout_sha256": _sha(layout_payload),
                "source_baseline_sha256": _sha(baseline_payload),
                "block_mapping": current_mapping,
                "approved_mapping_sha256": "",
                "checks": checks,
                "comparison": {
                    "source_sha256": descriptor.source_sha256,
                    "creator_content_sha256": content_hash,
                    "layout_sha256": _sha(layout_payload),
                    "source_baseline_sha256": _sha(baseline_payload),
                    "recreated_baseline_sha256": _sha(recreated_metrics["payload"]),
                    "checks_sha256": _checks_sha256(checks),
                    "mapping_sha256": mapping_hash,
                    "artifact_sha256": _artifact_manifest(artifacts),
                    "completed_at": _now(),
                },
            }
            try:
                atomic_write_json(creator_dir / "creator.json", creator, project_root=root)
            except Exception as error:
                _restore_comparison_artifacts(root, managed, prior)
                raise DocumentBuildError("comparison publication failed") from error
        source_paths = [baseline_dir / f"source-page-{page_number:03d}.png" for page_number in range(1, descriptor.page_count + 1)]
        recreated_paths = [baseline_dir / f"recreated-page-{page_number:03d}.png" for page_number in range(1, descriptor.page_count + 1)]
        comparison_paths = [baseline_dir / f"comparison-page-{page_number:03d}.png" for page_number in range(1, descriptor.page_count + 1)]
        differences = tuple(name for name in CHECK_NAMES if not checks.get(name, False))
        return ComparisonReport(not differences, tuple(source_paths), tuple(recreated_paths), tuple(comparison_paths), checks, differences)


def _current_bindings(root: Path, creator: dict[str, Any]) -> dict[str, str]:
    descriptor, layout_payload = _load_layout(root)
    _, source_payload, _ = _registered_source(root)
    baseline = _read_single(root / "private/cv-creator/baseline/baseline.pdf", MAX_PDF_BYTES, "source baseline")
    recreated = _read_single(root / "private/cv-creator/baseline/recreated-baseline.pdf", MAX_PDF_BYTES, "recreated baseline")
    content_hash = compute_creator_content_sha256(root / "private/cv-creator", creator["font_substitutions"])
    return {
        "source_sha256": _sha(source_payload),
        "descriptor_source_sha256": descriptor.source_sha256,
        "layout_sha256": _sha(layout_payload),
        "source_baseline_sha256": _sha(baseline),
        "recreated_baseline_sha256": _sha(recreated),
        "creator_content_sha256": content_hash,
    }


def _recomputed_comparison(root: Path, creator: dict[str, Any], *, require_approved: bool) -> tuple[dict[str, str], dict[str, bool], str]:
    bindings = _current_bindings(root, creator)
    descriptor, _ = _load_layout(root)
    mapping = list(_validated_block_mapping(root, descriptor, creator).values())
    mapping_hash = _mapping_sha256(mapping)
    validation = creator.get("validation")
    if not isinstance(validation, dict) or set(validation) != {
        "layout_sha256", "source_baseline_sha256", "block_mapping", "approved_mapping_sha256", "checks", "comparison",
    }:
        raise DocumentBuildError("creator comparison is stale or forged")
    checks, comparison = validation.get("checks"), validation.get("comparison")
    if not isinstance(checks, dict) or set(checks) != set(CHECK_NAMES) or any(type(value) is not bool for value in checks.values()) or not all(checks.values()):
        raise DocumentBuildError("creator comparison is stale or forged")
    if not isinstance(comparison, dict) or set(comparison) != {
        "source_sha256", "creator_content_sha256", "layout_sha256", "source_baseline_sha256",
        "recreated_baseline_sha256", "checks_sha256", "mapping_sha256", "artifact_sha256", "completed_at",
    }:
        raise DocumentBuildError("creator comparison is stale or forged")
    artifacts = _read_comparison_artifacts(root, descriptor)
    artifact_hashes = _artifact_manifest(artifacts)
    if comparison.get("artifact_sha256") != artifact_hashes:
        raise DocumentBuildError("creator comparison is stale or forged")
    source_metrics = _pdf_metrics(root / "private/cv-creator/baseline/baseline.pdf")
    recreated_metrics = _pdf_metrics(root / "private/cv-creator/baseline/recreated-baseline.pdf")
    recomputed = _comparison_checks(descriptor, source_metrics, recreated_metrics, creator)
    valid = (
        checks == recomputed
        and all(recomputed.values())
        and validation["block_mapping"] == mapping
        and validation["layout_sha256"] == bindings["layout_sha256"]
        and validation["source_baseline_sha256"] == bindings["source_baseline_sha256"]
        and comparison["source_sha256"] == bindings["source_sha256"] == bindings["descriptor_source_sha256"] == creator["source_sha256"]
        and comparison["creator_content_sha256"] == bindings["creator_content_sha256"] == creator["creator_content_sha256"]
        and comparison["layout_sha256"] == bindings["layout_sha256"]
        and comparison["source_baseline_sha256"] == bindings["source_baseline_sha256"]
        and comparison["recreated_baseline_sha256"] == bindings["recreated_baseline_sha256"]
        and comparison["checks_sha256"] == _checks_sha256(recomputed)
        and comparison["mapping_sha256"] == mapping_hash
        and isinstance(comparison["completed_at"], str)
        and UTC_TIMESTAMP.fullmatch(comparison["completed_at"]) is not None
    )
    if require_approved:
        valid = valid and validation["approved_mapping_sha256"] == mapping_hash
    elif validation["approved_mapping_sha256"] not in {"", mapping_hash}:
        valid = False
    if not valid:
        raise DocumentBuildError("creator comparison is stale or forged")
    return bindings, recomputed, mapping_hash


def _approval_envelope_sha256(root: Path, creator: dict[str, Any]) -> str:
    descriptor, layout_payload = _load_layout(root)
    _, source_payload, _ = _registered_source(root)
    baseline_payload = _read_single(root / "private/cv-creator/baseline/baseline.pdf", MAX_PDF_BYTES, "source baseline")
    recreated_payload = _read_single(
        root / "private/cv-creator/baseline/recreated-baseline.pdf", MAX_PDF_BYTES, "recreated baseline"
    )
    content_hash = compute_creator_content_sha256(root / "private/cv-creator", creator["font_substitutions"])
    mapping = list(_validated_block_mapping(root, descriptor, creator).values())
    mapping_hash = _mapping_sha256(mapping)
    validation = creator.get("validation")
    if not isinstance(validation, dict) or set(validation) != {
        "layout_sha256", "source_baseline_sha256", "block_mapping", "approved_mapping_sha256", "checks", "comparison",
    }:
        raise DocumentBuildError("CV creator is not approved")
    checks = validation.get("checks")
    comparison = validation.get("comparison")
    if (
        not isinstance(checks, dict)
        or set(checks) != set(CHECK_NAMES)
        or any(type(value) is not bool for value in checks.values())
        or not all(checks.values())
        or not isinstance(comparison, dict)
        or set(comparison) != {
            "source_sha256", "creator_content_sha256", "layout_sha256", "source_baseline_sha256",
            "recreated_baseline_sha256", "checks_sha256", "mapping_sha256", "artifact_sha256", "completed_at",
        }
    ):
        raise DocumentBuildError("CV creator is not approved")
    artifact_manifest = _stable_artifact_manifest(root, descriptor)
    source_hash = _sha(source_payload)
    layout_hash = _sha(layout_payload)
    baseline_hash = _sha(baseline_payload)
    recreated_hash = _sha(recreated_payload)
    checks_hash = _checks_sha256(checks)
    valid = (
        creator.get("automated_validation") is True
        and creator.get("creator_content_sha256") == content_hash
        and creator.get("source_sha256") == source_hash == descriptor.source_sha256
        and validation.get("layout_sha256") == layout_hash
        and validation.get("source_baseline_sha256") == baseline_hash
        and validation.get("block_mapping") == mapping
        and validation.get("approved_mapping_sha256") == mapping_hash
        and comparison.get("source_sha256") == source_hash
        and comparison.get("creator_content_sha256") == content_hash
        and comparison.get("layout_sha256") == layout_hash
        and comparison.get("source_baseline_sha256") == baseline_hash
        and comparison.get("recreated_baseline_sha256") == recreated_hash
        and comparison.get("checks_sha256") == checks_hash
        and comparison.get("mapping_sha256") == mapping_hash
        and comparison.get("artifact_sha256") == artifact_manifest
        and isinstance(comparison.get("completed_at"), str)
        and UTC_TIMESTAMP.fullmatch(comparison["completed_at"]) is not None
    )
    if not valid:
        raise DocumentBuildError("CV creator is not approved")
    protected_ids = sorted(
        row["block_id"] for row in mapping if row["semantic_role"] in PROTECTED_SEMANTIC_ROLES
    )
    envelope = {
        "creator_config": {
            "schema_version": creator["schema_version"],
            "source_sha256": creator["source_sha256"],
            "creator_version": creator["creator_version"],
            "template": creator["template"],
            "stylesheet": creator["stylesheet"],
            "approved_zones": creator["approved_zones"],
            "automated_validation": creator["automated_validation"],
            "font_substitutions": creator["font_substitutions"],
        },
        "creator_content_sha256": content_hash,
        "semantic_mapping": mapping,
        "protected_block_ids": protected_ids,
        "source_sha256": source_hash,
        "layout_sha256": layout_hash,
        "source_baseline_sha256": baseline_hash,
        "recreated_baseline_sha256": recreated_hash,
        "checks": checks,
        "checks_sha256": checks_hash,
        "comparison_completed_at": comparison["completed_at"],
        "artifacts": [
            {"name": name, "sha256": digest} for name, digest in sorted(artifact_manifest.items())
        ],
    }
    return _canonical_sha(envelope)


def _comparison_current(root: Path, creator: dict[str, Any], *, require_approved: bool = False) -> bool:
    try:
        _recomputed_comparison(root, creator, require_approved=require_approved)
        return True
    except (DocumentBuildError, PolicyError, OSError, ValueError):
        return False


def verify_approved_creator_binding(project_root: Path) -> dict[str, Any]:
    root = _root(project_root)
    creator = _load_creator(root)
    envelope_hash = _approval_envelope_sha256(root, creator)
    if (
        creator.get("automated_validation") is not True
        or creator.get("approved_at") is None
        or not isinstance(creator["approved_at"], str)
        or UTC_TIMESTAMP.fullmatch(creator["approved_at"]) is None
        or creator.get("approved_creator_sha256") != envelope_hash
    ):
        raise DocumentBuildError("CV creator is not approved")
    return creator


def approve_creator(project_root: Path, source_sha256: str, *, explicit_user_approval: bool) -> dict:
    if explicit_user_approval is not True:
        raise DocumentBuildError("explicit user approval is required")
    if not isinstance(source_sha256, str) or SHA256.fullmatch(source_sha256) is None:
        raise DocumentBuildError("source hash is invalid")
    root = _root(project_root)
    with _creator_locks(root):
        creator_path = root / "private/cv-creator/creator.json"
        prior = _read_single(creator_path, MAX_CREATOR_JSON_BYTES, "creator configuration")
        creator = _load_creator(root)
        _, source_payload, runtime = _registered_source(root)
        if source_sha256 != _sha(source_payload) or creator["source_sha256"] != source_sha256:
            raise DocumentBuildError("source hash does not match registered source")
        try:
            bindings, checks, mapping_hash = _recomputed_comparison(root, creator, require_approved=False)
        except DocumentBuildError:
            raise DocumentBuildError("creator comparison is stale") from None
        if creator.get("automated_validation") is not True or not all(checks.values()):
            raise DocumentBuildError("baseline validation has not passed")
        creator["creator_content_sha256"] = bindings["creator_content_sha256"]
        creator["approved_at"] = _now()
        creator["validation"]["approved_mapping_sha256"] = mapping_hash
        creator["approved_creator_sha256"] = _approval_envelope_sha256(root, creator)
        runtime["onboarding"]["creator_ready"] = True
        try:
            atomic_write_json(creator_path, creator, project_root=root)
            try:
                committed = verify_approved_creator_binding(root)
            except DocumentBuildError as error:
                raise DocumentBuildError("creator changed during approval") from error
            if committed.get("approved_creator_sha256") != creator["approved_creator_sha256"]:
                raise DocumentBuildError("creator changed during approval")
            save_runtime(root, runtime)
        except Exception as error:
            try:
                atomic_write_bytes(creator_path, prior, project_root=root)
            except OSError:
                pass
            if isinstance(error, DocumentBuildError):
                raise
            raise DocumentBuildError("creator approval could not be recorded") from error
        return creator


def _drift(root: Path, creator: dict[str, Any]) -> bool:
    try:
        verify_approved_creator_binding(root)
    except (DocumentBuildError, PolicyError, OSError, ValueError):
        return True
    return False


def _revoke_locked(root: Path, creator: dict[str, Any], runtime: dict[str, Any]) -> None:
    _revoke_values(creator, runtime, clear_validation=True)
    atomic_write_json(root / "private/cv-creator/creator.json", creator, project_root=root)
    save_runtime(root, runtime)


def check_creator_drift(project_root: Path) -> bool:
    root = _root(project_root)
    with _creator_locks(root):
        try:
            runtime = load_runtime(root)
        except OnboardingError:
            return True
        try:
            creator = _load_creator(root)
        except DocumentBuildError:
            runtime["onboarding"]["creator_ready"] = False
            runtime["submission_mode"] = "review_only"
            runtime["auto_apply_approved_at"] = None
            save_runtime(root, runtime)
            return True
        changed = _drift(root, creator)
        if changed:
            _revoke_locked(root, creator, runtime)
        return changed


def _safe_overlay_path(root: Path, overlay_path: Path) -> Path:
    try:
        if not overlay_path.is_absolute():
            overlay_path = root / overlay_path
        path = confined_path(root, overlay_path)
        relative = path.relative_to(root)
    except (ConfinementError, OSError, ValueError) as error:
        raise DocumentBuildError("overlay path is unsafe") from error
    if relative.parts[:2] != ("private", "jobs") or path.suffix.casefold() not in {".yml", ".yaml"}:
        raise DocumentBuildError("overlay path is unsafe")
    return path


def _safe_output_path(root: Path, output_path: Path) -> Path:
    try:
        if not output_path.is_absolute():
            output_path = root / output_path
        path = confined_path(root, output_path)
        relative = path.relative_to(root)
    except (ConfinementError, OSError, ValueError) as error:
        raise DocumentBuildError("output path is unsafe") from error
    if relative.parts[:2] != ("Documents", "generated") or len(relative.parts) != 3 or path.suffix.casefold() != ".pdf":
        raise DocumentBuildError("output must be a PDF directly under Documents/generated")
    if path.exists() or is_symlink_or_reparse(path):
        raise DocumentBuildError("output already exists")
    return path


def _publish_build(root: Path, output: Path, pdf: bytes, previews: list[bytes]) -> tuple[Path, ...]:
    preview_dir = confined_mkdir(root, output.parent / "previews")
    targets = [output] + [preview_dir / f"{output.stem}-page-{index:03d}.png" for index in range(1, len(previews) + 1)]
    if any(path.exists() or is_symlink_or_reparse(path) for path in targets):
        raise DocumentBuildError("output already exists")
    published: list[Path] = []
    try:
        for path, payload in zip(targets, [pdf] + previews):
            temporary = path.with_name(path.name + ".publish-" + os.urandom(8).hex())
            atomic_write_bytes(temporary, payload, project_root=root)
            try:
                os.link(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            published.append(path)
    except Exception as error:
        for path in reversed(published):
            try:
                path.unlink()
            except OSError:
                pass
        raise DocumentBuildError("document publication failed") from error
    return tuple(targets[1:])


def render_cv(project_root: Path, overlay_path: Path, output_path: Path) -> BuildReport:
    root = _root(project_root)
    output = _safe_output_path(root, output_path)
    overlay_file = _safe_overlay_path(root, overlay_path)
    if check_creator_drift(root):
        raise DocumentBuildError("CV creator approval has drifted; compare and approve again")
    with _creator_locks(root):
        creator = _load_creator(root)
        runtime = load_runtime(root)
        if runtime["onboarding"]["complete"] is not True or runtime["onboarding"]["creator_ready"] is not True:
            raise DocumentBuildError("onboarding is incomplete")
        profile_payload = _read_single(root / "private/master-profile.md", 2 * 1024 * 1024, "master profile")
        try:
            profile_text = profile_payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DocumentBuildError("master profile is invalid") from error
        if not profile_text.strip() or CONTROL.search(profile_text):
            raise DocumentBuildError("master profile is invalid")
        if _drift(root, creator):
            _revoke_locked(root, creator, runtime)
            raise DocumentBuildError("CV creator approval has drifted; compare and approve again")
        descriptor, _ = _load_layout(root)
        overlay = _validated_overlay(_load_overlay(overlay_file), _load_evidence(root))
        html_source = _render_html(root, descriptor, creator, overlay)
        with tempfile.TemporaryDirectory(prefix="job-pipeline-build-") as directory:
            temporary_pdf = Path(directory) / "document.pdf"
            _render_pdf_backend(html_source, temporary_pdf)
            page_count, previews = _validate_build_pdf(
                temporary_pdf, descriptor, _overlay_texts(overlay), creator["validation"]["block_mapping"]
            )
            if _drift(root, creator):
                _revoke_locked(root, creator, runtime)
                raise DocumentBuildError("CV creator changed during build")
            pdf_payload = _read_single(temporary_pdf, MAX_PDF_BYTES, "rendered PDF")
            preview_paths = _publish_build(root, output, pdf_payload, previews)
        issue = BuildIssue(
            "review_required",
            "semantic_review",
            "Qualitative claims require visible human review; automated checks verify evidence references, not semantic truth.",
        )
        return BuildReport(True, output, preview_paths, page_count, (issue,))


def _bounded_result(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if len(payload) > 1024:
        payload = '{"ok":false,"error":"result unavailable"}'
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Build private CV creator documents.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("bootstrap", "compare"):
        command = subparsers.add_parser(name)
        command.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    approve = subparsers.add_parser("approve")
    approve.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    approve.add_argument("--source-sha256", required=True)
    approve.add_argument("--confirm-user-approval", action="store_true")
    build = subparsers.add_parser("build")
    build.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    build.add_argument("--overlay", type=Path, required=True)
    build.add_argument("--output", type=Path)
    build.add_argument("--preview", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.command == "bootstrap":
            descriptor, _ = _load_layout(_root(arguments.root))
            bootstrap_creator(arguments.root, descriptor)
            result = {"ok": True, "creator_ready": False}
        elif arguments.command == "compare":
            report = compare_baseline(arguments.root)
            root = _root(arguments.root)
            _, source_payload, _ = _registered_source(root)
            comparison_pages = []
            for path in report.side_by_side_pages:
                checked = confined_path(root, path)
                relative = checked.relative_to(root).as_posix()
                if not relative.startswith("private/cv-creator/baseline/comparison-page-"):
                    raise DocumentBuildError("comparison result is invalid")
                comparison_pages.append(relative)
            result = {
                "ok": report.ok,
                "checks_passed": sum(report.checks.values()),
                "checks_total": len(report.checks),
                "creator_ready": False,
                "comparison_pages": comparison_pages,
                "source_sha256": _sha(source_payload),
            }
        elif arguments.command == "approve":
            if not arguments.confirm_user_approval:
                print(_bounded_result({"ok": False, "error": "approval confirmation is required"}))
                return 1
            creator = approve_creator(arguments.root, arguments.source_sha256, explicit_user_approval=True)
            result = {"ok": True, "creator_ready": True, "approved_at": creator["approved_at"]}
        else:
            output = arguments.output or Path("Documents/generated") / f"{arguments.overlay.stem}.pdf"
            report = render_cv(arguments.root, arguments.overlay, output)
            result = {"ok": report.ok, "page_count": report.page_count, "preview_count": len(report.preview_paths), "requires_review": True}
        print(_bounded_result(result))
        return 0 if result.get("ok") else 1
    except DocumentBuildError:
        print(_bounded_result({"ok": False, "error": "document operation failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
