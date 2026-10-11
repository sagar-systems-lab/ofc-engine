"""Bounded loopback measurement for the stateless synthetic preview."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import math
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

_SCENARIOS = ("repair", "outage", "dependent_missing", "budget_exhausted")
_STATUSES = ("INTEGRITY_ONLY", "USER_REVIEW", "USER_REVIEW", "REPAIR_EXHAUSTED")
_MAX_BODY = 200_000


def _origin(raw: str) -> str:
    url = urlsplit(raw)
    if (
        url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost")
        or url.username or url.password or url.path not in ("", "/")
        or url.query or url.fragment or url.port is None or not 1 <= url.port <= 65535
    ):
        raise ValueError("benchmark requires a loopback HTTP origin with an explicit port")
    return raw.rstrip("/")


def _get(origin: str, scenario: str) -> tuple[float, str]:
    request = Request(origin + "/api/demo?scenario=" + scenario, method="GET")
    start = time.perf_counter_ns()
    with urlopen(request, timeout=5) as reply:
        body = reply.read(_MAX_BODY + 1)
        if reply.status != 200 or len(body) > _MAX_BODY:
            raise RuntimeError("preview response unavailable or oversized")
        if reply.headers.get("Cache-Control") != "no-store":
            raise RuntimeError("preview response was unexpectedly cacheable")
        if reply.headers.get_content_type() != "application/json":
            raise RuntimeError("preview response is not JSON")
    latency_ms = (time.perf_counter_ns() - start) / 1_000_000
    result = json.loads(body)
    expected = _STATUSES[_SCENARIOS.index(scenario)]
    if (
        result.get("scenario") != scenario
        or result.get("run_status") != expected
        or result.get("source_mode") != "SYNTHETIC"
        or result.get("release_authorized") is not False
        or result.get("external_action_executed") is not False
        or result.get("provider_authenticated") is not False
    ):
        raise RuntimeError("unsafe or inconsistent fixture result")
    return latency_ms, sha256(body).hexdigest()


def _percentile(sorted_values: list[float], p: float) -> float:
    return round(sorted_values[max(0, math.ceil(p * len(sorted_values)) - 1)], 3)


def measure(origin: str, *, samples: int = 64, workers: int = 4, warmup: int = 8) -> dict:
    origin = _origin(origin)
    if type(samples) is not int or not 8 <= samples <= 200:
        raise ValueError("samples must be 8–200")
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("workers must be 1–8")
    if type(warmup) is not int or not 0 <= warmup <= 20:
        raise ValueError("warmup must be 0–20")

    for i in range(warmup):
        _get(origin, _SCENARIOS[i % len(_SCENARIOS)])
    observed: dict[str, str] = {}
    for scenario in _SCENARIOS:
        _, digest = _get(origin, scenario)
        observed[scenario] = digest

    def execute(i: int) -> float:
        scenario = _SCENARIOS[i % len(_SCENARIOS)]
        elapsed, digest = _get(origin, scenario)
        if digest != observed[scenario]:
            raise RuntimeError("fixture response changed during measurement")
        return elapsed

    start = time.perf_counter_ns()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        latencies = list(pool.map(execute, range(samples)))
    elapsed_s = (time.perf_counter_ns() - start) / 1_000_000_000
    latencies.sort()
    return {
        "status": "MEASURED_LOCAL_ONLY",
        "source_mode": "SYNTHETIC",
        "samples": len(latencies),
        "warmup": warmup,
        "workers": workers,
        "p50_ms": _percentile(latencies, 0.50),
        "p95_ms": _percentile(latencies, 0.95),
        "p99_ms": _percentile(latencies, 0.99),
        "observed_requests_per_second": round(samples / elapsed_s, 2),
        "release_authorized": False,
        "real_qloo_requests": 0,
        "production_sla_qualified": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure only the local synthetic HTTP preview")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--samples", type=int, default=64)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=8)
    args = parser.parse_args(argv)
    try:
        report = measure(
            args.base_url, samples=args.samples, workers=args.workers, warmup=args.warmup,
        )
    except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
        parser.exit(1, "Preview benchmark failed: " + str(exc) + "\n")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
