"""Integration checks for the public, read-only fixture HTTP surface."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ofc_engine import hosted_preview
from ofc_engine.hosted_preview import HostedFixtureServer
from ofc_engine.webdemo import SCENARIOS, demo_snapshot


@contextmanager
def running_server(*, build_sha="unknown"):
    server = HostedFixtureServer(("127.0.0.1", 0), build_sha=build_sha)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get(url: str):
    with urlopen(url, timeout=4) as response:
        return response.status, response.headers, response.read()


def error_code(url: str, *, method="GET", body=None, headers=None):
    with pytest.raises(HTTPError) as exc:
        urlopen(Request(url, data=body, method=method, headers=headers or {}), timeout=4)
    return exc.value.code


def test_server_preloads_exact_existing_synthetic_scenarios():
    with running_server() as (base, server):
        assert set(server.fixtures) == set(SCENARIOS)
        for scenario in SCENARIOS:
            code, hdrs, body = get(base + "/api/demo?scenario=" + scenario)
            assert code == 200
            assert json.loads(body) == demo_snapshot(scenario)
            assert hdrs["Cache-Control"] == "no-store"
            assert hdrs["X-Content-Type-Options"] == "nosniff"
            assert "default-src 'none'" in hdrs["Content-Security-Policy"]
            payload = json.loads(body)
            assert payload["demo_mode"] == payload["source_mode"] == "SYNTHETIC"
            assert not payload["release_authorized"]
            assert not payload["external_action_executed"]
            assert not payload["provider_authenticated"]
            assert payload["comparison"]["real_causal_proof"] is False


def test_preview_response_is_stable_without_storage():
    with running_server() as (base, server):
        before = dict(server.fixtures)
        a = get(base + "/api/demo?scenario=repair")[2]
        b = get(base + "/api/demo?scenario=repair")[2]
        assert a == b == before["repair"]
        assert not hasattr(server, "history")
        assert not hasattr(server, "database")


def test_health_and_readiness_contain_only_safe_build_metadata():
    with running_server(build_sha="a1b2c3d") as (base, _):
        for path in ("/healthz", "/readyz"):
            status, headers, body = get(base + path)
            assert status == 200
            assert json.loads(body) == {"status": "ok", "mode": "SYNTHETIC", "build_sha": "a1b2c3d"}
            assert headers["Cache-Control"] == "no-store"
        assert error_code(base + "/healthz?key=anything") == 400
        assert error_code(base + "/readyz?debug=1") == 400


def test_public_preview_never_exposes_mutable_or_saved_run_endpoints():
    with running_server() as (base, _):
        for endpoint in ("/api/runs", "/api/runs/" + "a" * 32, "/api/runs/" + "a" * 32 + "/events?after_sequence=-1", "/release", "/metrics"):
            assert error_code(base + endpoint) == 404
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            assert error_code(base + "/api/runs", method=method, body=b"{}") == 405
        assert error_code(base + "/api/demo?scenario=repair", method="POST", body=b"{}") == 405
        assert error_code(base + "/api/demo?scenario=repair", method="HEAD") == 405


@pytest.mark.parametrize("query", [
    "", "scenario=", "scenario=wrong", "scenario=repair&scenario=outage",
    "scenario=repair&extra=1", "scenarios=repair", "scenario=repair&scenario=repair",
])
def test_bad_scenario_query_rejected(query):
    with running_server() as (base, _):
        assert error_code(base + "/api/demo" + ("?" + query if query else "")) == 400


def test_long_request_target_and_asset_path_allowlist():
    with running_server() as (base, _):
        assert error_code(base + "/api/demo?scenario=" + "x" * 600) == 414
        for path in ("/.env", "/Dockerfile", "/src/ofc_engine/qloo.py", "/docs", "/..%2F..%2Fetc%2Fpasswd", "/api/debug"):
            assert error_code(base + path) == 404
        for path in ("/", "/styles.css", "/app.js"):
            status, _, body = get(base + path)
            assert status == 200 and body


def test_fixture_precomputed_once_at_startup(monkeypatch):
    called = []
    real = hosted_preview.demo_snapshot

    def count(scenario):
        called.append(scenario)
        return real(scenario)

    monkeypatch.setattr(hosted_preview, "demo_snapshot", count)
    with running_server() as (base, _):
        assert called == list(SCENARIOS)
        for _ in range(3):
            get(base + "/api/demo?scenario=repair")
        assert called == list(SCENARIOS)


def test_invalid_provenance_aborts_server_startup(monkeypatch):
    original = hosted_preview.demo_snapshot

    def invalid(scenario):
        data = original(scenario)
        data["release_authorized"] = True
        return data

    monkeypatch.setattr(hosted_preview, "demo_snapshot", invalid)
    with pytest.raises(ValueError, match="provenance boundary"):
        HostedFixtureServer(("127.0.0.1", 0))


@pytest.mark.parametrize("bind,sha", [
    ("example.com", "unknown"), ("::", "unknown"),
    ("127.0.0.1", "not-a-hash"), ("127.0.0.1", "KEY=secret"),
])
def test_invalid_network_bind_or_metadata_rejected(bind, sha):
    with pytest.raises(ValueError):
        HostedFixtureServer((bind, 0), build_sha=sha)


def test_bounded_connections_and_requests_fail_closed():
    with running_server() as (base, server):
        assert server._slots._value == 16
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: get(base + "/healthz")[0], range(16)))
        assert all(code == 200 for code in results)
        assert server._slots._value == 16


def test_docker_defaults_are_nonroot_read_only_app_and_no_private_files():
    project = Path(__file__).resolve().parents[1]
    dockerfile = (project / "Dockerfile").read_text(encoding="utf-8")
    ignore = (project / ".dockerignore").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
    assert '"ofc_engine.hosted_preview"' in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert 'os.environ.get("PORT", "8000")' in dockerfile
    assert "!src/**" in ignore and "**\n" in ignore
    assert "!pyproject.toml" in ignore
