"""Concrete operator commands for a bounded, shared-host job workflow."""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

from Pipeline.document_review import document_snapshot
from Pipeline.io_utils import atomic_write_json, confined_path
from Pipeline.onboarding import load_runtime, onboarding_status
from Pipeline.pipeline_settings import rank_jobs
from Pipeline.pipeline_store import PipelineStore
from Pipeline.policy_guard import _document_blocker, _stable_single_link_bytes, _verify_strict_creator_binding, application_blockers, selection_blockers
from Pipeline.run_lock import acquire_run_lock


def _store(root: Path) -> PipelineStore:
    return PipelineStore(root / "Pipeline/pipeline.db")


def _job(root: Path, identifier: str) -> dict:
    value = _store(root).get_job(identifier)
    if value is None:
        raise ValueError("tracked job does not exist")
    return value


def queue(root: Path) -> dict:
    store = _store(root)
    revision = store.get_settings()
    ranked = rank_jobs(store.list_jobs(), revision["settings"])
    ranked = [row for row in ranked if not selection_blockers(root, row, revision["settings"])]
    retry = lambda row: row.get("blocker", {}).get("category") in {"transient_no_browser", "transient_portal_error"}
    ranked = [row for row in ranked if retry(row)] + [row for row in ranked if not retry(row)]
    return {"settings_version": revision["version"], "jobs": ranked}


def preflight(root: Path, identifier: str) -> dict:
    store = _store(root)
    revision = store.get_settings()
    value = _job(root, identifier)
    return {"id": identifier, "settings_version": revision["version"], "job_fingerprint": store.job_fingerprint(value), "blockers": application_blockers(root, value, revision["settings"])}


def review_documents(root: Path, identifier: str, *, visual_review_confirmed: bool) -> dict:
    if visual_review_confirmed is not True:
        raise ValueError("visible document review must be confirmed")
    _verify_strict_creator_binding(root)
    value = _job(root, identifier)
    if value["status"] not in {"shortlisted", "docs_ready"}:
        raise ValueError("job is not ready for document review")
    if selection_blockers(root, value, _store(root).get_settings()["settings"]):
        raise ValueError("job is ineligible under current policy")
    for field in ("cv", "cover_letter"):
        blocker = _document_blocker(root, value.get(field), cv=field == "cv")
        if blocker:
            raise ValueError(blocker)
    value["document_review"] = document_snapshot(root, value)
    value["status"] = "docs_ready"
    value.pop("blocker", None)
    store = _store(root)
    result = store.upsert_job(value)
    store.export_state()
    return result


def record_failure(root: Path, identifier: str, category: str, detail: str, *, run_id: str) -> dict:
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= 128:
        raise ValueError("run identity is required")
    value = _job(root, identifier)
    if value["status"] == "applied":
        raise ValueError("applied job is immutable")
    if category == "transient_portal_error":
        if value["status"] not in {"docs_ready", "needs_user"}:
            raise ValueError("transient retry requires prepared documents")
        count = value.get("portal_error_count", 0)
        if value.get("last_portal_error_run") != run_id:
            count = min(count + 1, 3)
        value["portal_error_count"] = count
        value["last_portal_error_run"] = run_id
        value["status"] = "docs_ready" if count < 3 else "needs_user"
        category = category if count < 3 else "portal_broken"
    elif category == "transient_no_browser":
        if value["status"] != "docs_ready":
            raise ValueError("transient retry requires prepared documents")
        value["status"] = "docs_ready"
        value["portal_error_count"] = 0
        value.pop("last_portal_error_run", None)
    else:
        value["status"] = "needs_user"
        value["portal_error_count"] = 0
        value.pop("last_portal_error_run", None)
    value["blocker"] = {"category": category, "detail": detail}
    store = _store(root)
    result = store.upsert_job(value)
    store.export_state()
    return result


def _control(root: Path, name: str) -> dict | None:
    path = confined_path(root, root / "private" / name)
    if not path.exists():
        return None
    value = json.loads(_stable_single_link_bytes(path, 4096).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("run control record is invalid")
    return value


def _remove_control(root: Path, name: str, expected: dict) -> None:
    if _control(root, name) == expected:
        confined_path(root, root / "private" / name).unlink(missing_ok=True)


def assert_run_owner(root: Path, token: str) -> dict:
    path = confined_path(root, root / "private/run.lock")
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 4096:
        raise ValueError("active run lock is missing")
    if not isinstance(token, str) or not 1 <= len(token) <= 128:
        raise ValueError("active run ownership does not match")
    # The Windows holder has an exclusive DELETE-capable handle. Ordinary
    # readers cannot open it. A fresh nonce proves the holder is responding
    # without relaxing its sharing or trusting a stale sidecar receipt.
    request = {"token": token, "nonce": secrets.token_hex(16)}
    atomic_write_json(root / "private/.run-check.json", request, project_root=root)
    try:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if _control(root, ".run-ack.json") == request and path.exists():
                return request
            time.sleep(0.05)
        raise ValueError("active run ownership did not respond")
    finally:
        _remove_control(root, ".run-check.json", request)
        _remove_control(root, ".run-ack.json", request)


def hold_run(root: Path, *, owner: str, seconds: int) -> int:
    if not 1 <= seconds <= 7200:
        raise ValueError("run duration must be from 1 through 7200 seconds")
    release = confined_path(root, root / "private/.run-release.json")
    with acquire_run_lock(root, owner) as record:
        print(json.dumps({"locked": True, "token": record["token"], "seconds": seconds}), flush=True)
        deadline = time.monotonic() + seconds
        last_nonce = None
        try:
            while time.monotonic() < deadline:
                request = _control(root, ".run-check.json")
                if request and set(request) == {"token", "nonce"} and request["token"] == record["token"] and isinstance(request["nonce"], str) and len(request["nonce"]) == 32 and request["nonce"] != last_nonce:
                    atomic_write_json(root / "private/.run-ack.json", request, project_root=root)
                    last_nonce = request["nonce"]
                if _control(root, ".run-release.json") == {"token": record["token"]}:
                    return 0
                time.sleep(0.1)
            print(json.dumps({"locked": False, "error": "run lease ended; stop external actions and start a new run"}), flush=True)
            return 1
        finally:
            for name in (".run-check.json", ".run-ack.json", ".run-release.json"):
                value = _control(root, name)
                if value and value.get("token") == record["token"]:
                    _remove_control(root, name, value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("queue")
    hold = commands.add_parser("hold-run")
    hold.add_argument("--owner", choices=["manual-apply", "vie-apply", "scheduled-run", "verification"], required=True)
    hold.add_argument("--seconds", type=int, default=1800)
    release = commands.add_parser("release-run")
    release.add_argument("--run-token", required=True)
    for name in ("preflight", "review-documents", "failure"):
        command = commands.add_parser(name)
        command.add_argument("--job", required=True)
        command.add_argument("--run-token", required=True)
        if name == "review-documents":
            command.add_argument("--confirm-visual-review", action="store_true")
        if name == "failure":
            command.add_argument("--category", required=True)
            command.add_argument("--detail", required=True)
    args = parser.parse_args()
    root = args.root.absolute()
    try:
        if args.command == "hold-run":
            return hold_run(root, owner=args.owner, seconds=args.seconds)
        if args.command in {"release-run", "preflight", "review-documents", "failure"}:
            assert_run_owner(root, args.run_token)
        if args.command == "release-run":
            atomic_write_json(root / "private/.run-release.json", {"token": args.run_token}, project_root=root)
            result = {"release_requested": True}
        elif args.command == "status":
            store = _store(root)
            result = {"onboarding": onboarding_status(root), "submission_mode": load_runtime(root)["submission_mode"], "settings": store.get_settings(), "summary": store.dashboard_job_summary()}
        elif args.command == "queue":
            result = queue(root)
        elif args.command == "preflight":
            result = preflight(root, args.job)
        elif args.command == "review-documents":
            result = review_documents(root, args.job, visual_review_confirmed=args.confirm_visual_review)
        else:
            result = record_failure(root, args.job, args.category, args.detail, run_id=args.run_token)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, KeyError):
        print(json.dumps({"ok": False, "error": "operation refused; check readiness, ownership, files and current policy"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
