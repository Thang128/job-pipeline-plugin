"""Private-safe loopback server for the local starter dashboard."""

from __future__ import annotations

import argparse
import json
import math
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from Pipeline.io_utils import confined_path, is_symlink_or_reparse
from Pipeline.onboarding import onboarding_status
from Pipeline.pipeline_settings import SettingsError, rank_jobs, validate_settings
from Pipeline.pipeline_store import PipelineStore, StoreError


MAX_BODY_BYTES = 256 * 1024
MAX_REJECT_DRAIN_BYTES = 3 * MAX_BODY_BYTES
MAX_REJECT_DRAIN_SECONDS = 0.5
REJECT_DRAIN_CHUNK_BYTES = 64 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4_096
STATIC_ASSETS = {
    "/": ("job-pipeline.html", "text/html; charset=utf-8"),
    "/job-pipeline.html": ("job-pipeline.html", "text/html; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard_helpers.js": ("dashboard_helpers.js", "text/javascript; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
}
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"


class RequestError(ValueError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _bounded_json(value: object) -> None:
    nodes = 0
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError("JSON bounds")
        if item is None or type(item) is bool:
            continue
        if type(item) is int:
            if abs(item) > 10**18:
                raise ValueError("JSON bounds")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("JSON bounds")
        elif isinstance(item, str):
            if len(item) > 10_000:
                raise ValueError("JSON bounds")
        elif isinstance(item, list):
            if len(item) > 200:
                raise ValueError("JSON bounds")
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, dict):
            if len(item) > 200 or any(not isinstance(key, str) or not key or len(key) > 10_000 for key in item):
                raise ValueError("JSON bounds")
            stack.extend((child, depth + 1) for child in item.values())
        else:
            raise ValueError("JSON type")


def bootstrap_payload(server: "DashboardHTTPServer") -> dict:
    dashboard = server.store.dashboard_job_summary()
    current = server.store.get_settings()
    status = onboarding_status(server.project_root)
    return {
        "settings": current["settings"],
        "settings_version": current["version"],
        "summaries": dashboard["summaries"],
        "onboarding": {
            "ready": status["ready"],
            "missing": [name for name, present in status["requirements"].items() if not present],
        },
        "locked_rules": {
            "truthful_answers_only": True,
            "human_challenges_are_manual": True,
            "visible_submission_confirmation_required": True,
            "maximum_daily_submissions": 50,
        },
        "meta": dashboard["meta"],
        "csrf_token": server.csrf_token,
    }


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, project_root: Path, port: int):
        self.project_root = project_root
        self.asset_dir = project_root / "Pipeline"
        self.store = PipelineStore(self.asset_dir / "pipeline.db")
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), DashboardHandler)

    @property
    def allowed_origin(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    @property
    def allowed_host(self) -> str:
        return f"127.0.0.1:{self.server_address[1]}"


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_json(self, status: int, payload: object, *, head: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _send_static(self, filename: str, content_type: str, *, head: bool = False) -> None:
        try:
            path = confined_path(self.server.project_root, self.server.asset_dir / filename)
            if path.parent != self.server.asset_dir or not path.is_file() or is_symlink_or_reparse(path):
                raise OSError
            body = path.read_bytes()
        except (OSError, ValueError):
            self._send_json(404, {"error": "not found"}, head=head)
            return
        self.send_response(200)
        self._headers(content_type, len(body))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _single_header(self, name: str, *, required: bool = True) -> str | None:
        values = self.headers.get_all(name, [])
        if len(values) != 1:
            if not values and not required:
                return None
            raise RequestError(403, "request headers are not allowed")
        return values[0]

    def _guard(self, *, write: bool) -> None:
        if self._single_header("Host") != self.server.allowed_host:
            raise RequestError(403, "request host is not allowed")
        origin = self._single_header("Origin", required=write)
        if origin is not None and origin != self.server.allowed_origin:
            raise RequestError(403, "request origin is not allowed")
        if write:
            token = self._single_header("X-CSRF-Token") or ""
            if not secrets.compare_digest(token, self.server.csrf_token):
                raise RequestError(403, "CSRF token is invalid")

    def _path(self) -> str:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment or "%" in parsed.path or "\\" in parsed.path or any(part == ".." for part in parsed.path.split("/")):
            raise RequestError(400, "invalid path")
        return parsed.path

    def _query(self, allowed: set[str]) -> dict[str, str]:
        try:
            pairs = urllib.parse.parse_qsl(
                urllib.parse.urlsplit(self.path).query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=8,
            )
        except ValueError:
            raise RequestError(400, "invalid query") from None
        values: dict[str, str] = {}
        for key, value in pairs:
            if key not in allowed or key in values:
                raise RequestError(400, "invalid query")
            values[key] = value
        return values

    def _pagination(self) -> tuple[int, int]:
        query = self._query({"page", "page_size"})

        def number(name: str, default: int) -> int:
            raw = query.get(name)
            if raw is None:
                return default
            if not raw.isascii() or not raw.isdecimal() or raw.startswith("0"):
                raise RequestError(400, "invalid pagination")
            value = int(raw)
            if not 1 <= value <= (10_000 if name == "page" else 100):
                raise RequestError(400, "invalid pagination")
            return value

        return number("page", 1), number("page_size", 50)

    def _discard_oversized_body(self, length: int) -> None:
        remaining = min(length, MAX_REJECT_DRAIN_BYTES)
        deadline = time.monotonic() + MAX_REJECT_DRAIN_SECONDS
        previous_timeout = self.connection.gettimeout()
        try:
            while remaining > 0:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                self.connection.settimeout(timeout)
                try:
                    chunk = self.rfile.read1(min(remaining, REJECT_DRAIN_CHUNK_BYTES))
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                remaining -= len(chunk)
        finally:
            self.connection.settimeout(previous_timeout)
            self.close_connection = True

    def _read_json(self) -> dict:
        if self.headers.get("Transfer-Encoding") is not None:
            raise RequestError(400, "invalid request body")
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().casefold() != "application/json":
            raise RequestError(415, "Content-Type must be application/json")
        lengths = self.headers.get_all("Content-Length", [])
        try:
            length = int(lengths[0]) if len(lengths) == 1 else 0
        except ValueError:
            length = 0
        if length <= 0:
            raise RequestError(400, "request body is required")
        if length > MAX_BODY_BYTES:
            self._discard_oversized_body(length)
            raise RequestError(413, "request body is too large")
        try:
            payload = self.rfile.read(length).decode("utf-8")
            value = json.loads(
                payload,
                object_pairs_hook=_unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
            )
            _bounded_json(value)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            raise RequestError(400, "request body must contain valid bounded JSON") from None
        if not isinstance(value, dict):
            raise RequestError(400, "request body must be an object")
        return value

    def _handle_error(self, error: Exception, *, head: bool = False) -> None:
        if isinstance(error, RequestError):
            self._send_json(error.status, {"error": str(error)}, head=head)
        elif isinstance(error, StoreError) and "conflict" in str(error).casefold():
            self._send_json(409, {"error": "settings version conflict"}, head=head)
        elif isinstance(error, (SettingsError, StoreError, TypeError, ValueError)):
            self._send_json(400, {"error": "invalid request"}, head=head)
        elif isinstance(error, (OSError, sqlite3.Error)):
            self._send_json(500, {"error": "local data operation failed"}, head=head)
        else:
            self._send_json(500, {"error": "dashboard request failed"}, head=head)

    def _get_request(self, *, head: bool) -> None:
        try:
            self._guard(write=False)
            path = self._path()
            if path == "/api/health":
                self._query(set())
                integrity = self.server.store.validate_integrity()
                self._send_json(200, {"ok": True, "job_count": integrity["job_count"], "settings_version": integrity["settings_version"]}, head=head)
            elif path == "/api/bootstrap":
                self._query(set())
                self._send_json(200, bootstrap_payload(self.server), head=head)
            elif path == "/api/jobs":
                page, page_size = self._pagination()
                self._send_json(200, self.server.store.dashboard_jobs(page=page, page_size=page_size), head=head)
            elif path == "/api/settings/history":
                page, page_size = self._pagination()
                self._send_json(200, self.server.store.dashboard_settings_history(page=page, page_size=page_size), head=head)
            elif path in STATIC_ASSETS:
                self._query(set())
                self._send_static(*STATIC_ASSETS[path], head=head)
            else:
                self._query(set())
                self._send_json(404, {"error": "not found"}, head=head)
        except Exception as error:
            self._handle_error(error, head=head)

    def do_GET(self) -> None:
        self._get_request(head=False)

    def do_HEAD(self) -> None:
        self._get_request(head=True)

    def do_POST(self) -> None:
        self._write_request("POST")

    def do_PUT(self) -> None:
        self._write_request("PUT")

    def _method_not_allowed(self) -> None:
        try:
            self._guard(write=False)
            self._path()
            self._query(set())
            self._send_json(405, {"error": "method not allowed"})
        except Exception as error:
            self._handle_error(error)

    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_CONNECT = _method_not_allowed
    do_TRACE = _method_not_allowed

    def send_error(self, _code: int, _message: str | None = None, _explain: str | None = None) -> None:
        self._method_not_allowed()

    def _write_request(self, method: str) -> None:
        try:
            self._guard(write=True)
            path = self._path()
            payload = self._read_json()
            if method == "POST" and path == "/api/settings/preview":
                self._query(set())
                if set(payload) != {"settings"}:
                    raise RequestError(400, "invalid preview request")
                settings = validate_settings(payload["settings"])
                ranked = rank_jobs(self.server.store.list_jobs(), settings)
                self._send_json(200, {"eligible_count": len(ranked), "eligible_job_ids": [job["id"] for job in ranked]})
                return
            if method == "PUT" and path == "/api/settings":
                self._query(set())
                if set(payload) != {"settings", "expected_version", "reason"}:
                    raise RequestError(400, "invalid save request")
                result = self.server.store.save_settings(payload["settings"], expected_version=payload["expected_version"], reason=payload["reason"])
                self._send_committed(result)
                return
            if method == "POST" and path == "/api/settings/rollback":
                self._query(set())
                if set(payload) != {"target_version", "expected_version", "reason"}:
                    raise RequestError(400, "invalid rollback request")
                result = self.server.store.rollback_settings(
                    target_version=payload["target_version"],
                    expected_version=payload["expected_version"],
                    reason=payload["reason"],
                )
                self._send_committed(result)
                return
            if method == "POST" and path == "/api/export/retry":
                self._query(set())
                if payload:
                    raise RequestError(400, "invalid export retry request")
                current = self.server.store.get_settings()
                self.server.store.export_state()
                self._send_json(200, {**current, "export_status": "current"})
                return
            self._send_json(404, {"error": "not found"})
        except Exception as error:
            self._handle_error(error)

    def _send_committed(self, result: dict) -> None:
        try:
            self.server.store.export_state()
            self._send_json(200, {**result, "export_status": "current"})
        except (OSError, StoreError):
            self._send_json(202, {**result, "export_status": "pending"})


def create_server(project_root: Path, port: int = 0) -> ThreadingHTTPServer:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port")
    try:
        root = confined_path(project_root, project_root)
        asset_dir = confined_path(root, root / "Pipeline")
    except (OSError, ValueError) as error:
        raise ValueError("project root") from error
    store = PipelineStore(asset_dir / "pipeline.db")
    store.initialize()
    store.validate_integrity()
    return DashboardHTTPServer(root, port)


def probe_health(base_url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return False
        port = parsed.port
        if port is None or not 1 <= port <= 65535 or parsed.path not in {"", "/"}:
            return False
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/health")
        with urllib.request.urlopen(request, timeout=0.7) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return response.status == 200 and payload.get("ok") is True
    except (OSError, ValueError, urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local Job Pipeline Starter dashboard.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the exact loopback dashboard URL in the default browser.")
    arguments = parser.parse_args(argv)
    server = create_server(arguments.root, arguments.port)
    dashboard_url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Job Pipeline Starter: {dashboard_url}")
    if arguments.open:
        try:
            if not webbrowser.open(dashboard_url):
                print("Dashboard browser could not be opened; use the loopback URL shown above.", file=sys.stderr)
        except Exception:
            print("Dashboard browser could not be opened; use the loopback URL shown above.", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
