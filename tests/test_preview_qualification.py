"""Local preview benchmark and public-route security regression tests."""
from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ofc_engine.hosted_preview import HostedFixtureServer


ROOT = Path(__file__).resolve().parents[1]


def load_benchmark():
    spec = spec_from_file_location("bench_preview", ROOT / "scripts" / "bench_preview.py")
    assert spec is not None and spec.loader is not None
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@contextmanager
def preview():
    server = HostedFixtureServer(("127.0.0.1", 0), build_sha="abc123d")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_local_measurement_is_bounded_and_never_claims_sla():
    bench = load_benchmark()
    with preview() as url:
        result = bench.measure(url, samples=32, workers=4, warmup=4)
    assert result["status"] == "MEASURED_LOCAL_ONLY"
    assert result["source_mode"] == "SYNTHETIC"
    assert result["samples"] == 32 and result["workers"] == 4
    assert result["warmup"] == 4
    assert 0 <= result["p50_ms"] <= result["p95_ms"] <= result["p99_ms"]
    assert result["observed_requests_per_second"] > 0
    assert result["real_qloo_requests"] == 0
    assert result["release_authorized"] is False
    assert result["production_sla_qualified"] is False


@pytest.mark.parametrize("target", [
    "https://ofc-fixture-preview.onrender.com",
    "http://evil.invalid:8080",
    "http://127.0.0.1",
    "http://localhost:8080/path",
    "http://localhost:8080?inject=1",
    "http://user:password@localhost:8080",
    "file:///etc/passwd",
    "http://127.0.0.1:99999",
])
def test_benchmark_never_targets_public_or_unexpected_origins(target):
    with pytest.raises(ValueError):
        load_benchmark()._origin(target)


@pytest.mark.parametrize("kwargs", [
    {"samples": 7}, {"samples": 201}, {"samples": True},
    {"workers": 0}, {"workers": 9}, {"workers": True},
    {"warmup": -1}, {"warmup": 21}, {"warmup": True},
])
def test_benchmark_load_is_explicitly_bounded(kwargs):
    with pytest.raises(ValueError):
        load_benchmark().measure("http://127.0.0.1:1234", **kwargs)


def test_read_only_preview_rejects_forged_actions_and_private_paths():
    with preview() as url:
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            request = Request(
                url + "/api/runs", data=b'{"release_authorized":true}',
                headers={"Content-Type": "application/json"}, method=method,
            )
            with pytest.raises(HTTPError) as result:
                urlopen(request, timeout=5)
            assert result.value.code == 405
        for path in (
            "/api/runs/00000000000000000000000000000000",
            "/api/runs/00000000000000000000000000000000/events",
            "/api/release",
            "/api/admin",
            "/.env",
            "/%2eenv",
            "/src/ofc_engine/qloo.py",
            "/README.md",
        ):
            with pytest.raises(HTTPError) as result:
                urlopen(url + path, timeout=5)
            assert result.value.code == 404


def test_all_fixture_scenarios_have_consistent_body_under_parallel_load():
    bench = load_benchmark()
    with preview() as url:
        a = bench.measure(url, samples=48, workers=8, warmup=2)
    assert a["samples"] == 48
    assert a["production_sla_qualified"] is False
