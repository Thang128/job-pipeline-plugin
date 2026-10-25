import copy
import http.client
import json
import socket
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from Pipeline import dashboard_server
from Pipeline.dashboard_server import create_server, probe_health
from Pipeline.pipeline_settings import DEFAULT_SETTINGS
from Pipeline.pipeline_store import PipelineStore


def raw_request(server, method, path, *, body=None, headers=None):
    base_url, httpd = server
    host, port = httpd.server_address
    connection = http.client.HTTPConnection(host, port, timeout=5)
    request_headers = {"Host": f"{host}:{port}", "Origin": base_url}
    request_headers.update(headers or {})
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    payload = response.read()
    result = response.status, {key.casefold(): value for key, value in response.getheaders()}, payload
    connection.close()
    return result


def assert_security_headers(headers):
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in headers["content-security-policy"]


def test_server_binds_exact_loopback_and_bootstrap_is_zero_state_private_safe(server, request_json, project_root: Path):
    base_url, httpd = server
    assert httpd.server_address[0] == "127.0.0.1"
    assert len(httpd.csrf_token) >= 32
    private_marker = "PRIVATE-BOOTSTRAP-MARKER"
    (project_root / "private" / "candidate.yml").write_text(private_marker, encoding="utf-8")
    (project_root / "private" / "runtime.json").write_text(json.dumps({"marker": private_marker}), encoding="utf-8")
    status, payload, headers = request_json(server, "GET", "/api/bootstrap")
    assert status == 200
    assert set(payload) == {"settings", "settings_version", "summaries", "onboarding", "locked_rules", "meta", "csrf_token"}
    assert payload["summaries"] == {"total_jobs": 0, "by_status": {}, "by_country": {}, "applied_today": 0}
    assert payload["settings"] == DEFAULT_SETTINGS
    assert payload["settings_version"] == 0
    assert payload["onboarding"] == {
        "ready": False,
        "missing": ["master_cv", "master_profile", "candidate", "job_preferences", "creator_ready"],
    }
    assert payload["locked_rules"] == {
        "truthful_answers_only": True,
        "human_challenges_are_manual": True,
        "visible_submission_confirmation_required": True,
        "maximum_daily_submissions": 50,
    }
    assert payload["csrf_token"] == httpd.csrf_token
    assert private_marker not in json.dumps(payload)
    assert "profile" not in payload and "accounts" not in payload and "candidate" not in payload
    assert_security_headers(headers)
    assert headers["content-type"].startswith("application/json")
    assert probe_health(base_url) is True


def test_static_allowlist_serves_assets_and_rejects_plain_or_encoded_traversal(server):
    for path, content_type in [
        ("/", "text/html"), ("/job-pipeline.html", "text/html"),
        ("/dashboard.css", "text/css"), ("/dashboard_helpers.js", "text/javascript"),
        ("/dashboard.js", "text/javascript"),
    ]:
        status, headers, body = raw_request(server, "GET", path)
        assert status == 200 and body
        assert headers["content-type"].startswith(content_type)
        assert_security_headers(headers)
    for path in (
        "/../private/runtime.json", "/%2e%2e/private/runtime.json", "/%252e%252e/private/runtime.json",
        "/dashboard%2ejs", "/dashboard.js%2f..%2f..%2fprivate/runtime.json", "/private/runtime.json",
    ):
        status, headers, body = raw_request(server, "GET", path)
        assert status in {400, 404}
        assert headers["content-type"].startswith("application/json")
        assert b"runtime" not in body.lower()
        assert_security_headers(headers)


def test_host_origin_and_csrf_are_exact_and_fail_with_safe_json(server):
    base_url, httpd = server
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    payload = json.dumps({"settings": settings, "expected_version": 0, "reason": "test"}).encode()
    valid = {"Content-Type": "application/json", "X-CSRF-Token": httpd.csrf_token}
    cases = [
        ({**valid, "Host": "localhost:" + str(httpd.server_address[1])}, 403),
        ({**valid, "Origin": base_url + "/"}, 403),
        ({**valid, "Origin": "null"}, 403),
        ({"Content-Type": "application/json", "X-CSRF-Token": "wrong"}, 403),
        ({"Content-Type": "text/plain", "X-CSRF-Token": httpd.csrf_token}, 415),
    ]
    for headers, expected in cases:
        status, response_headers, body = raw_request(server, "PUT", "/api/settings", body=payload, headers=headers)
        assert status == expected
        error = json.loads(body)
        assert set(error) == {"error"}
        assert len(error["error"]) < 100
        assert_security_headers(response_headers)

    for headers in ({"Host": "localhost:" + str(httpd.server_address[1])}, {"Origin": base_url + "/"}):
        status, response_headers, body = raw_request(server, "GET", "/api/bootstrap", headers=headers)
        assert status == 403
        assert set(json.loads(body)) == {"error"}
        assert_security_headers(response_headers)


def test_unsupported_http_methods_fail_with_json_and_security_headers(server):
    for method in ("DELETE", "PATCH", "OPTIONS"):
        status, headers, body = raw_request(server, method, "/api/settings")
        assert status == 405
        assert json.loads(body) == {"error": "method not allowed"}
        assert_security_headers(headers)


def test_head_and_unknown_methods_share_guards_status_and_security_headers(server):
    base_url, httpd = server
    for path, expected in (("/api/bootstrap", 200), ("/dashboard.js", 200), ("/missing", 404)):
        status, headers, body = raw_request(server, "HEAD", path)
        assert status == expected
        assert body == b""
        assert_security_headers(headers)
    status, headers, body = raw_request(server, "BREW", "/api/settings")
    assert status == 405
    assert json.loads(body) == {"error": "method not allowed"}
    assert_security_headers(headers)
    for method, hostile in (
        ("OPTIONS", {"Origin": base_url + "/"}),
        ("DELETE", {"Host": "localhost:" + str(httpd.server_address[1])}),
        ("BREW", {"Origin": "null"}),
    ):
        status, headers, body = raw_request(server, method, "/api/settings", headers=hostile)
        assert status == 403
        assert set(json.loads(body)) == {"error"}
        assert_security_headers(headers)


def test_head_missing_allowlisted_static_asset_has_no_wire_body(server, project_root: Path):
    base_url, httpd = server
    (project_root / "Pipeline" / "dashboard.css").unlink()
    host, port = httpd.server_address
    request = (
        f"HEAD /dashboard.css HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Origin: {base_url}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    with socket.create_connection((host, port), timeout=5) as connection:
        connection.sendall(request)
        wire = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            wire += chunk
    header_block, body = wire.split(b"\r\n\r\n", 1)
    lines = header_block.decode("iso-8859-1").split("\r\n")
    headers = {key.casefold(): value.strip() for key, value in (line.split(":", 1) for line in lines[1:])}
    assert lines[0].split()[1] == "404"
    assert headers["content-type"].startswith("application/json")
    assert_security_headers(headers)
    assert body == b""


def test_json_body_is_duplicate_safe_and_never_echoes_private_input(server):
    _, httpd = server
    marker = "PRIVATE-REQUEST-MARKER"
    headers = {"Content-Type": "application/json", "X-CSRF-Token": httpd.csrf_token}
    bodies = [
        (b'{"settings":{},"settings":{}}', 400),
        (b'{"reason":"' + marker.encode() + b'",', 400),
    ]
    for body, expected in bodies:
        status, response_headers, response_body = raw_request(server, "POST", "/api/settings/preview", body=body, headers=headers)
        assert status == expected
        assert marker.encode() not in response_body
        assert len(response_body) < 500
        assert_security_headers(response_headers)


def test_oversized_json_body_returns_repeatable_413_without_parsing(server, monkeypatch):
    _, httpd = server
    marker = b"PRIVATE-OVERSIZED-MARKER"
    headers = {"Content-Type": "application/json", "X-CSRF-Token": httpd.csrf_token}
    body = b"{" + b'"x":"' + marker + b"a" * (512 * 1024) + b'"}'

    def forbidden_json_loads(*_args, **_kwargs):
        raise AssertionError("oversized request body must not be parsed")

    monkeypatch.setattr(dashboard_server.json, "loads", forbidden_json_loads)
    for _ in range(20):
        status, response_headers, response_body = raw_request(
            server,
            "POST",
            "/api/settings/preview",
            body=body,
            headers=headers,
        )
        assert status == 413
        assert marker not in response_body
        assert len(response_body) < 500
        assert_security_headers(response_headers)


def test_huge_stalled_body_rejection_is_time_bounded(server):
    base_url, httpd = server
    host, port = httpd.server_address
    request = (
        "POST /api/settings/preview HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Origin: {base_url}\r\n"
        f"X-CSRF-Token: {httpd.csrf_token}\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: 999999999\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")

    started = time.monotonic()
    with socket.create_connection((host, port), timeout=2) as connection:
        connection.settimeout(2)
        connection.sendall(request)
        wire = b""
        while b"\r\n\r\n" not in wire:
            wire += connection.recv(4096)
    elapsed = time.monotonic() - started

    assert wire.startswith(b"HTTP/1.0 413 ")
    assert elapsed < 1.5


def test_jobs_preview_save_history_and_rollback_use_canonical_store(server, request_json, project_root: Path):
    store = PipelineStore(project_root / "Pipeline" / "pipeline.db")
    store.upsert_job({
        "id": "synthetic-1", "title": "Synthetic Analyst", "company": "Synthetic Org", "country": "ZZ",
        "source_url": "https://jobs.invalid/synthetic-1", "status": "shortlisted", "score": 90,
        "detected_seniority": "entry",
    })
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["role_families"] = [{"id": "synthetic", "label": "Synthetic", "keywords": ["synthetic"], "enabled": True, "priority": 1}]
    settings["countries"] = [{"code": "ZZ", "label": "Synthetic", "enabled": True, "priority": 1}]
    status, jobs, _ = request_json(server, "GET", "/api/jobs")
    assert status == 200
    assert jobs == {
        "items": [{"id": "synthetic-1", "title": "Synthetic Analyst", "company": "Synthetic Org", "status": "shortlisted", "source_url": "https://jobs.invalid/synthetic-1"}],
        "page": 1, "page_size": 50, "total": 1, "pages": 1,
    }
    before = store.get_settings()
    status, preview, _ = request_json(server, "POST", "/api/settings/preview", {"settings": settings})
    assert status == 200
    assert preview == {"eligible_count": 1, "eligible_job_ids": ["synthetic-1"]}
    assert store.get_settings() == before
    status, saved, _ = request_json(server, "PUT", "/api/settings", {"settings": settings, "expected_version": 0, "reason": "dashboard edit"})
    assert status == 200 and saved["version"] == 1 and saved["export_status"] == "current"
    changed = copy.deepcopy(settings)
    changed["score_threshold"] = 80
    status, saved, _ = request_json(server, "PUT", "/api/settings", {"settings": changed, "expected_version": 1, "reason": "threshold edit"})
    assert status == 200 and saved["version"] == 2 and saved["export_status"] == "current"
    status, rolled, _ = request_json(server, "POST", "/api/settings/rollback", {"target_version": 1, "expected_version": 2, "reason": "dashboard rollback"})
    assert status == 200 and rolled["version"] == 3 and rolled["settings"] == settings and rolled["export_status"] == "current"
    exported = json.loads((project_root / "Pipeline" / "pipeline_state.json").read_text(encoding="utf-8"))
    assert exported == store.load_state()


def test_committed_save_and_rollback_report_pending_export_and_retry_current(server, request_json, project_root: Path, monkeypatch):
    _, httpd = server
    original_export = httpd.store.export_state

    def fail_export(*_args, **_kwargs):
        raise OSError("PRIVATE-EXPORT-PATH")

    first = copy.deepcopy(DEFAULT_SETTINGS)
    first["score_threshold"] = 70
    monkeypatch.setattr(httpd.store, "export_state", fail_export)
    status, saved, _ = request_json(server, "PUT", "/api/settings", {"settings": first, "expected_version": 0, "reason": "first"})
    assert status == 202
    assert saved == {"version": 1, "settings": first, "export_status": "pending"}
    assert "PRIVATE-EXPORT-PATH" not in json.dumps(saved)
    assert PipelineStore(project_root / "Pipeline" / "pipeline.db").get_settings()["version"] == 1

    monkeypatch.setattr(httpd.store, "export_state", original_export)
    status, retried, _ = request_json(server, "POST", "/api/export/retry", {})
    assert status == 200
    assert retried == {"version": 1, "settings": first, "export_status": "current"}
    assert json.loads((project_root / "Pipeline" / "pipeline_state.json").read_text(encoding="utf-8"))["settings_version"] == 1

    second = copy.deepcopy(first)
    second["score_threshold"] = 80
    status, saved, _ = request_json(server, "PUT", "/api/settings", {"settings": second, "expected_version": 1, "reason": "second"})
    assert status == 200 and saved["version"] == 2
    monkeypatch.setattr(httpd.store, "export_state", fail_export)
    status, rolled, _ = request_json(server, "POST", "/api/settings/rollback", {"target_version": 1, "expected_version": 2, "reason": "rollback"})
    assert status == 202
    assert rolled == {"version": 3, "settings": first, "export_status": "pending"}
    assert PipelineStore(project_root / "Pipeline" / "pipeline.db").get_settings()["version"] == 3


def test_bootstrap_jobs_and_history_use_bounded_private_safe_read_models(server, request_json, project_root: Path, monkeypatch):
    _, httpd = server
    store = PipelineStore(project_root / "Pipeline" / "pipeline.db")
    store.upsert_job({
        "id": "private-job", "title": "Synthetic Analyst", "company": "Synthetic Org", "country": "ZZ",
        "source_url": "https://jobs.invalid/private-job", "status": "needs_user", "score": 90,
        "detected_seniority": "entry", "job_description": "PRIVATE-DESCRIPTION",
        "blocker": {"category": "unknown_answer", "detail": "PRIVATE-BLOCKER"},
        "cv": "Documents/generated/private.pdf", "cover_letter": "Documents/generated/private.md",
    })
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["score_threshold"] = 70
    store.save_settings(settings, expected_version=0, reason="PRIVATE-REASON-ONE")
    settings["score_threshold"] = 80
    store.save_settings(settings, expected_version=1, reason="PRIVATE-REASON-TWO")

    monkeypatch.setattr(httpd.store, "load_state", lambda: (_ for _ in ()).throw(AssertionError("unbounded load")))
    status, bootstrap, _ = request_json(server, "GET", "/api/bootstrap")
    assert status == 200 and bootstrap["summaries"]["total_jobs"] == 1 and bootstrap["settings_version"] == 2

    status, jobs, _ = request_json(server, "GET", "/api/jobs?page=1&page_size=1")
    assert status == 200
    assert jobs == {
        "items": [{"id": "private-job", "title": "Synthetic Analyst", "company": "Synthetic Org", "status": "needs_user", "source_url": "https://jobs.invalid/private-job"}],
        "page": 1, "page_size": 1, "total": 1, "pages": 1,
    }
    assert not any(marker in json.dumps(jobs) for marker in ("PRIVATE-DESCRIPTION", "PRIVATE-BLOCKER", "Documents/generated"))

    status, history, _ = request_json(server, "GET", "/api/settings/history?page=1&page_size=1")
    assert status == 200 and history["total"] == 2 and len(history["items"]) == 1
    assert set(history["items"][0]) == {"version", "reason", "created_at"}
    assert history["items"][0]["reason"] == "PRIVATE-REASON-TWO"


@pytest.mark.parametrize(
    "path",
    [
        "/api/jobs?page=1&page=2", "/api/jobs?page=", "/api/jobs?page=0", "/api/jobs?page=10001",
        "/api/jobs?page=01", "/api/jobs?page_size=101", "/api/jobs?extra=1",
        "/api/settings/history?page=1&page=2", "/api/settings/history?page_size=0",
    ],
)
def test_paginated_routes_reject_duplicate_unknown_malformed_or_unbounded_query(server, path: str):
    status, headers, body = raw_request(server, "GET", path)
    assert status == 400
    assert set(json.loads(body)) == {"error"}
    assert_security_headers(headers)


def test_settings_conflict_is_atomic_under_concurrent_requests(server, project_root: Path):
    base_url, httpd = server
    first = copy.deepcopy(DEFAULT_SETTINGS)
    first["score_threshold"] = 70
    second = copy.deepcopy(DEFAULT_SETTINGS)
    second["score_threshold"] = 80
    barrier = threading.Barrier(3)
    results = []

    def save(settings):
        barrier.wait()
        body = json.dumps({"settings": settings, "expected_version": 0, "reason": "concurrent edit"}).encode()
        request = Request(base_url + "/api/settings", data=body, method="PUT", headers={
            "Content-Type": "application/json", "Origin": base_url, "X-CSRF-Token": httpd.csrf_token,
        })
        try:
            with urlopen(request, timeout=5) as response:
                results.append(response.status)
        except HTTPError as error:
            error.read()
            results.append(error.code)

    threads = [threading.Thread(target=save, args=(settings,)) for settings in (first, second)]
    for thread in threads: thread.start()
    barrier.wait()
    for thread in threads: thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(results) == [200, 409]
    store = PipelineStore(project_root / "Pipeline" / "pipeline.db")
    assert store.get_settings()["version"] == 1
    assert len(store.settings_history()) == 1


def test_concurrent_reads_remain_consistent_during_one_versioned_save(server, request_json, project_root: Path):
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["score_threshold"] = 70
    barrier = threading.Barrier(7)
    reads = []
    writes = []

    def read_bootstrap():
        barrier.wait()
        reads.append(request_json(server, "GET", "/api/bootstrap")[:2])

    def save_settings():
        barrier.wait()
        writes.append(request_json(server, "PUT", "/api/settings", {
            "settings": settings, "expected_version": 0, "reason": "concurrent read test",
        })[:2])

    threads = [threading.Thread(target=read_bootstrap) for _ in range(5)] + [threading.Thread(target=save_settings)]
    for thread in threads: thread.start()
    barrier.wait()
    for thread in threads: thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert writes[0][0] == 200
    assert all(status == 200 and payload["settings_version"] in {0, 1} for status, payload in reads)
    assert PipelineStore(project_root / "Pipeline" / "pipeline.db").validate_integrity()["settings_version"] == 1


def test_csrf_token_is_stable_for_server_lifetime_and_random_between_servers(server, project_root: Path):
    _, first = server
    second = create_server(project_root, port=0)
    try:
        assert first.csrf_token == first.csrf_token
        assert first.csrf_token != second.csrf_token
    finally:
        second.server_close()


def test_create_server_rejects_nonzero_unsafe_port_and_health_stops_cleanly(project_root: Path):
    with pytest.raises(ValueError):
        create_server(project_root, port=-1)
    httpd = create_server(project_root, port=0)
    host, port = httpd.server_address
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{host}:{port}"
    assert probe_health(base_url)
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert probe_health(base_url) is False


class _FakeForegroundServer:
    def __init__(self, port: int = 43210):
        self.server_address = ("127.0.0.1", port)
        self.served = False
        self.closed = False

    def serve_forever(self):
        self.served = True

    def server_close(self):
        self.closed = True


def test_main_does_not_open_browser_without_explicit_flag(project_root: Path, monkeypatch):
    server = _FakeForegroundServer()
    opened = []
    monkeypatch.setattr(dashboard_server, "create_server", lambda root, port: server)
    monkeypatch.setattr(dashboard_server.webbrowser, "open", lambda url: opened.append(url))

    assert dashboard_server.main(["--root", str(project_root), "--port", "0"]) == 0
    assert opened == []
    assert server.served is True
    assert server.closed is True


def test_main_open_uses_exact_bound_loopback_url(project_root: Path, monkeypatch):
    server = _FakeForegroundServer(port=54321)
    opened = []
    monkeypatch.setattr(dashboard_server, "create_server", lambda root, port: server)
    monkeypatch.setattr(dashboard_server.webbrowser, "open", lambda url: opened.append(url) or True)

    assert dashboard_server.main(["--root", str(project_root), "--port", "0", "--open"]) == 0
    assert opened == ["http://127.0.0.1:54321/"]
    assert server.served is True
    assert server.closed is True


def test_main_browser_failure_is_private_safe_and_server_stays_foreground(project_root: Path, monkeypatch, capsys):
    server = _FakeForegroundServer()

    def fail_open(_url):
        raise OSError("PRIVATE-BROWSER-DETAIL")

    monkeypatch.setattr(dashboard_server, "create_server", lambda root, port: server)
    monkeypatch.setattr(dashboard_server.webbrowser, "open", fail_open)

    assert dashboard_server.main(["--root", str(project_root), "--port", "0", "--open"]) == 0
    output = capsys.readouterr()
    assert "PRIVATE-BROWSER-DETAIL" not in output.out + output.err
    assert "open" in output.err.casefold()
    assert server.served is True
    assert server.closed is True
