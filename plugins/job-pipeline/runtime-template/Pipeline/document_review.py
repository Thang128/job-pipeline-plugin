"""Bind a visible review to exact generated documents and approved creator."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from Pipeline.io_utils import confined_path
from Pipeline.onboarding import load_runtime

REVIEW_KEYS = {"cv_sha256", "cover_letter_sha256", "master_sha256", "creator_sha256", "overlay_sha256", "candidate_sha256", "profile_sha256"}


def document_snapshot(root: Path, job: dict) -> dict[str, str]:
    from Pipeline.policy_guard import _stable_single_link_bytes
    runtime = load_runtime(root)
    creator_path = confined_path(root, root / "private/cv-creator/creator.json")
    if creator_path.stat().st_nlink != 1:
        raise ValueError("creator path is unsafe")
    creator = json.loads(_stable_single_link_bytes(creator_path, 256 * 1024).decode("utf-8"))
    result = {"master_sha256": runtime["master_cv_sha256"], "creator_sha256": creator["approved_creator_sha256"]}
    overlay = job.get("tailoring_overlay")
    if not isinstance(overlay, str) or "\\" in overlay:
        raise ValueError("tailoring overlay path is invalid")
    overlay_relative = Path(overlay)
    if overlay_relative.is_absolute() or ".." in overlay_relative.parts or overlay_relative.parts[:2] != ("private", "jobs") or len(overlay_relative.parts) != 3 or overlay_relative.suffix not in {".yml", ".yaml"}:
        raise ValueError("tailoring overlay path is invalid")
    sources = {"overlay_sha256": overlay_relative, "candidate_sha256": Path("private/candidate.yml"), "profile_sha256": Path("private/master-profile.md")}
    for field, relative in sources.items():
        source = confined_path(root, root / relative)
        result[field] = hashlib.sha256(_stable_single_link_bytes(source, 2 * 1024 * 1024)).hexdigest()
    for field in ("cv", "cover_letter"):
        raw = job.get(field)
        if not isinstance(raw, str) or "\\" in raw:
            raise ValueError("document path is invalid")
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:2] != ("Documents", "generated"):
            raise ValueError("document path is invalid")
        path = confined_path(root, root / relative)
        if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 15 * 1024 * 1024:
            raise ValueError("document file is invalid")
        result[field + "_sha256"] = hashlib.sha256(_stable_single_link_bytes(path, 15 * 1024 * 1024)).hexdigest()
    return result


def review_blocker(root: Path, job: dict) -> str | None:
    review = job.get("document_review")
    if not isinstance(review, dict) or set(review) != REVIEW_KEYS:
        return "document visual review is missing"
    try:
        if review != document_snapshot(root, job):
            return "document review no longer matches files or creator"
    except (OSError, ValueError, KeyError, TypeError):
        return "document review no longer matches files or creator"
    return None
