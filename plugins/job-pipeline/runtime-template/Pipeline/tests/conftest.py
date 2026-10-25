import json
import shutil
from threading import Thread
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from Pipeline.dashboard_server import create_server
from Pipeline.initialize import initialize_project

SOURCE_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    destination = tmp_path / "project"
    shutil.copytree(
        SOURCE_ROOT,
        destination,
        ignore=shutil.ignore_patterns(
            ".venv",
            "node_modules",
            ".pytest_cache",
            "private",
            "pipeline.db*",
            "pipeline_state.json",
            "generated",
            "__pycache__",
            "*.pyc",
        ),
    )
    return destination


@pytest.fixture
def extracted_copy(project_root: Path) -> Path:
    return project_root


@pytest.fixture
def server(project_root: Path):
    initialize_project(project_root)
    httpd = create_server(project_root, port=0)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    base_url = f"http://{host}:{port}"
    try:
        yield base_url, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.fixture
def request_json():
    def send(
        server,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict, dict[str, str]]:
        base_url, httpd = server
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = dict(headers or {})
        request_headers.setdefault("Origin", base_url)
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
            request_headers.setdefault("X-CSRF-Token", httpd.csrf_token)
        request = Request(base_url + path, data=body, method=method, headers=request_headers)
        try:
            response = urlopen(request, timeout=5)
        except Exception as error:
            if not hasattr(error, "read"):
                raise
            response = error
        with response:
            return (
                response.status,
                json.loads(response.read().decode("utf-8")),
                {key.casefold(): value for key, value in response.headers.items()},
            )

    return send
