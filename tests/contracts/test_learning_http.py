"""Loopback UI cannot be driven by another website or arbitrary request fields."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from llm_test_lab.learning.server import MAX_BODY, LessonHTTPServer


@pytest.fixture
def server(tmp_path: Path):
    server = LessonHTTPServer(tmp_path)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        server.service.close()
        thread.join(timeout=3)


def request(server, path="/api/session", *, method="GET", body=None, headers=None):
    supplied = {"X-Lab-Token": server.capability, "Origin": server.origin}
    if body is not None:
        supplied["Content-Type"] = "application/json"
    supplied.update(headers or {})
    supplied = {key: value for key, value in supplied.items() if value is not None}
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        client.request(method, path, body=body, headers=supplied)
        response = client.getresponse()
        return response.status, dict(response.headers), response.read()
    finally:
        client.close()


def test_page_is_local_static_and_capability_not_reflected_or_exported(server) -> None:
    assert server.server_address[0] == "127.0.0.1"
    status, headers, body = request(server, "/", headers={"X-Lab-Token": None})
    assert status == 200 and b"app.js" in body
    assert server.capability.encode() not in body
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
    status, _, body = request(server)
    assert status == 200 and json.loads(body)["stage"] == "prediction"
    assert server.capability.encode() not in body


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Lab-Token": None},
        {"X-Lab-Token": "not-authorized"},
        {"X-Lab-Token": "\u00ff"},
        {"Origin": "https://untrusted.example"},
        {"Host": "untrusted.example"},
    ],
)
def test_foreign_or_unauthorized_requests_cannot_read_progress(server, headers) -> None:
    status, _, _ = request(server, headers=headers)
    assert status == 403 and server.service.store.get().revision == 0


@pytest.mark.parametrize(
    "origin", [None, "null", "http://localhost:1", "https://untrusted.example"]
)
def test_mutations_require_exact_origin_even_with_token(server, origin) -> None:
    status, _, _ = request(
        server, "/api/reset", method="POST", body='{"revision":0}', headers={"Origin": origin}
    )
    assert status == 403 and server.service.store.get().revision == 0


@pytest.mark.parametrize(
    "path",
    [
        "/.env",
        "/../README.md",
        "/%2e%2e/README.md",
        "/api/session?token=secret",
        "/runs",
        "/api/run",
    ],
)
def test_no_filesystem_or_general_runner_routes(server, path) -> None:
    status, _, _ = request(server, path)
    assert status == 404


@pytest.mark.parametrize(
    "body",
    [
        '{"revision":0,"revision":0}',
        '{"revision":true}',
        '{"revision":0,"profile":"SYNTHETIC_SECRET_MARKER"}',
        '{"revision":0,"path":"SYNTHETIC_SECRET_MARKER"}',
        '{"revision":NaN}',
        "[]",
    ],
)
def test_invalid_bodies_cannot_mutate_or_echo_input(server, body) -> None:
    status, _, result = request(server, "/api/reset", method="POST", body=body)
    assert status == 400 and b"SYNTHETIC_SECRET_MARKER" not in result
    assert server.service.store.get().revision == 0


def test_body_limit_cors_and_stale_reset(server) -> None:
    assert request(server, "/api/reset", method="POST", body="x" * (MAX_BODY + 1))[0] == 413
    assert (
        request(
            server, "/api/reset", method="POST", body="{}", headers={"Content-Type": "text/plain"}
        )[0]
        == 415
    )
    status, headers, _ = request(server, method="OPTIONS")
    assert status == 403 and "Access-Control-Allow-Origin" not in headers
    assert request(server, "/api/reset", method="POST", body='{"revision":0}')[0] == 200
    assert request(server, "/api/reset", method="POST", body='{"revision":0}')[0] == 409
    assert server.service.store.get().revision == 1
