"""Read-only smoke qualification for a deployed, synthetic OFC preview."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_MAX_BODY = 600_000
_SCENARIOS = ("repair", "outage", "dependent_missing", "budget_exhausted")


class SmokeFailure(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Response:
    status: int
    headers: Any
    content: bytes


def normalize_url(raw: str) -> str:
    if not isinstance(raw, str):
        raise ValueError("base URL must be a string")
    url = urlsplit(raw)
    host = (url.hostname or "").lower()
    if (
        url.username or url.password or url.query or url.fragment or
        url.path not in ("", "/") or
        not host or
        (url.scheme != "https" and not (url.scheme == "http" and host in ("127.0.0.1", "localhost"))) or
        (host not in ("127.0.0.1", "localhost") and not host.endswith(".onrender.com"))
    ):
        raise ValueError("base URL must be an HTTPS Render hostname or loopback HTTP origin")
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("invalid port")
    return raw.rstrip("/")


class PreviewProbe:
    def __init__(self, origin: str, timeout: float = 8):
        self.origin = normalize_url(origin)
        self.opener = build_opener(NoRedirect())
        self.timeout = timeout

    def get(self, path: str) -> Response:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("only relative request paths are allowed")
        return self._read(Request(self.origin + path, method="GET"))

    def options(self, path: str) -> Response:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("only relative request paths are allowed")
        return self._read(Request(self.origin + path, method="OPTIONS"))

    def _read(self, request: Request) -> Response:
        try:
            with self.opener.open(request, timeout=self.timeout) as reply:
                data = reply.read(_MAX_BODY + 1)
                status, headers = reply.status, reply.headers
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                exc.close()
                raise SmokeFailure("unexpected HTTP redirect") from None
            status, headers = exc.code, exc.headers
            data = exc.read(_MAX_BODY + 1)
            exc.close()
        except (URLError, OSError, TimeoutError):
            raise SmokeFailure("remote endpoint unreachable") from None
        if len(data) > _MAX_BODY:
            raise SmokeFailure("response body too large")
        return Response(status, headers, data)


def _json(response: Response, *, status: int = 200) -> dict:
    if response.status != status:
        raise SmokeFailure(f"unexpected HTTP status {response.status}; expected {status}")
    if response.headers.get_content_type() != "application/json":
        raise SmokeFailure("incorrect content type")
    try:
        parsed = json.loads(response.content)
    except (UnicodeDecodeError, ValueError):
        raise SmokeFailure("response is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise SmokeFailure("JSON response must be an object")
    return parsed


def _headers(reply: Response) -> None:
    if reply.headers.get("Cache-Control") != "no-store":
        raise SmokeFailure("cache boundary missing")
    if reply.headers.get("X-Content-Type-Options") != "nosniff":
        raise SmokeFailure("MIME hardening missing")
    if "default-src 'none'" not in reply.headers.get("Content-Security-Policy", ""):
        raise SmokeFailure("CSP missing")


def _wait_for_ready(client: PreviewProbe, *, seconds: int) -> dict:
    until = time.monotonic() + seconds
    while True:
        try:
            return _json(client.get("/readyz"))
        except SmokeFailure:
            if time.monotonic() >= until:
                raise
            time.sleep(min(3, max(0, until - time.monotonic())))


def qualify(origin: str, *, wait_seconds: int = 0, expected_sha: str | None = None) -> dict:
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 180:
        raise ValueError("wait_seconds must be between 0 and 180")
    if expected_sha is not None and not re.fullmatch(r"[0-9a-f]{7,40}", expected_sha):
        raise ValueError("expected SHA must be lowercase hexadecimal, length 7–40")
    client = PreviewProbe(origin)
    health = _wait_for_ready(client, seconds=wait_seconds)
    live = _json(client.get("/healthz"))
    if health != live or health.get("status") != "ok" or health.get("mode") != "SYNTHETIC":
        raise SmokeFailure("health/readiness provenance mismatch")
    if expected_sha is not None and health.get("build_sha") != expected_sha:
        raise SmokeFailure("unexpected deployed build identity")
    page = client.get("/")
    if page.status != 200 or page.headers.get_content_type() != "text/html":
        raise SmokeFailure("public plan workspace missing")
    _headers(page)
    if b'PROVIDER PENDING' not in page.content or b'id="objective"' not in page.content:
        raise SmokeFailure("plan editor or provider boundary missing")
    proof = client.get("/proof")
    if proof.status != 200 or proof.headers.get_content_type() != "text/html":
        raise SmokeFailure("synthetic proof lab missing")
    _headers(proof)
    if b'SANDBOX / SYNTHETIC' not in proof.content:
        raise SmokeFailure("fixture provenance missing")
    for asset, expected_type in (("/app.js", "javascript"), ("/experience.js", "javascript"), ("/styles.css", "text/css"), ("/studio.js", "javascript"), ("/studio.css", "text/css")):
        response = client.get(asset)
        if response.status != 200 or expected_type not in response.headers.get_content_type():
            raise SmokeFailure("web asset missing")
        _headers(response)
    baseline = None
    observed = {}
    for scenario in _SCENARIOS:
        response = client.get("/api/demo?scenario=" + scenario)
        _headers(response)
        item = _json(response)
        if (item.get("scenario") != scenario or item.get("demo_mode") != "SYNTHETIC" or
            item.get("source_mode") != "SYNTHETIC" or
            item.get("provider_authenticated") is not False or
            item.get("release_authorized") is not False or
            item.get("external_action_executed") is not False or
            item.get("comparison", {}).get("real_causal_proof") is not False):
            raise SmokeFailure("unsafe or misattributed demo response")
        comparison = item["comparison"]
        if comparison.get("identical_baseline") is not True or comparison.get("baseline_digest") != item.get("baseline_digest"):
            raise SmokeFailure("fixture arms do not share baseline")
        if not isinstance(item.get("timeline"), list) or not item["timeline"]:
            raise SmokeFailure("missing run event trace")
        if item["last_event_sequence"] != len(item["timeline"]) - 1:
            raise SmokeFailure("run event sequence discontinuity")
        if baseline is None:
            baseline = item["baseline_digest"]
        elif item["baseline_digest"] != baseline:
            raise SmokeFailure("scenario baseline drift")
        observed[scenario] = item.get("run_status")
    if (observed.get("repair") != "INTEGRITY_ONLY" or observed.get("outage") != "USER_REVIEW" or
        observed.get("dependent_missing") != "USER_REVIEW" or observed.get("budget_exhausted") != "REPAIR_EXHAUSTED"):
        raise SmokeFailure("scenario fail-closed outcomes incorrect")
    if client.get("/api/runs").status != 404:
        raise SmokeFailure("private run store publicly readable")
    if client.options("/api/runs").status != 405:
        raise SmokeFailure("mutation route unexpectedly accessible")
    if client.get("/.env").status != 404:
        raise SmokeFailure("private file path unexpectedly accessible")
    return {"result": "PASS", "mode": "SYNTHETIC", "build_sha": health.get("build_sha"),
            "scenarios": observed, "real_qloo_requests": 0, "release_authorized": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strict read-only remote fixture qualification")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--expected-sha")
    args = parser.parse_args(argv)
    try:
        report = qualify(args.base_url, wait_seconds=args.wait_seconds, expected_sha=args.expected_sha)
    except (ValueError, SmokeFailure) as exc:
        print(json.dumps({"result": "FAIL", "reason": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
