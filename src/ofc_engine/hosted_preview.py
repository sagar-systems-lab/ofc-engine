"""Read-only public fixture preview; no persistent runs or provider access."""
from __future__ import annotations

import argparse
import base64
import binascii
from importlib.resources import files
from http.server import ThreadingHTTPServer
import json
import os
import re
from threading import BoundedSemaphore
from urllib.parse import parse_qs, urlsplit

from .webdemo import DemoHandler, SCENARIOS, demo_snapshot
from .public_recorded import read_public_recorded_excerpt

_MAX_TARGET_LENGTH = 512
_MAX_CONNECTIONS = 16
_TIMEOUT_SECONDS = 5
_HEX_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def _source_revision(environment: dict[str, str]) -> str:
    render_sha = environment.get("RENDER_GIT_COMMIT", "")
    configured = environment.get("OFC_BUILD_SHA", "")
    if render_sha:
        if not re.fullmatch(r"[0-9a-f]{40}", render_sha):
            raise ValueError("invalid Render source revision")
        return render_sha
    return configured or "unknown"


def _encoded(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _verified_fixture(scenario: str) -> bytes:
    value = demo_snapshot(scenario)
    if (
        value.get("scenario") != scenario
        or value.get("demo_mode") != "SYNTHETIC"
        or value.get("source_mode") != "SYNTHETIC"
        or value.get("provider_authenticated") is not False
        or value.get("release_authorized") is not False
        or value.get("external_action_executed") is not False
        or value.get("comparison", {}).get("real_causal_proof") is not False
    ):
        raise ValueError("fixture provenance boundary violated")
    return _encoded(value)


class HostedFixtureHandler(DemoHandler):
    server_version = "OFCPreview"
    sys_version = ""

    def do_GET(self) -> None:
        if len(self.path) > _MAX_TARGET_LENGTH:
            self._send(414, b'{"error":"URI_TOO_LONG"}', "application/json")
            return
        parts = urlsplit(self.path)
        if parts.path in ("/healthz", "/readyz"):
            if parts.query:
                self._send(400, b'{"error":"INVALID_QUERY"}', "application/json")
                return
            self._send(200, self.server.health_body, "application/json")
            return
        if parts.path == "/api/demo":
            params = parse_qs(parts.query, keep_blank_values=True)
            if set(params) != {"scenario"} or len(params["scenario"]) != 1:
                self._send(400, b'{"error":"INVALID_SCENARIO"}', "application/json")
                return
            body = self.server.fixtures.get(params["scenario"][0])
            if body is None:
                self._send(400, b'{"error":"INVALID_SCENARIO"}', "application/json")
                return
            self._send(200, body, "application/json")
            return
        if self.server.public_excerpt_body is not None:
            if parts.path == "/api/local-run" and not parts.query:
                self._send(200, self.server.public_excerpt_body, "application/json")
                return
            if parts.path == "/local-run.js" and not parts.query:
                script = files("ofc_engine").joinpath("web", "local-run.js").read_bytes()
                self._send(200, script, "text/javascript; charset=utf-8")
                return
            if parts.path == "/" and not parts.query:
                page = files("ofc_engine").joinpath("web", "studio.html").read_bytes()
                marker = b"</head>"
                if page.count(marker) != 1:
                    self._send(503, b'{"error":"STUDIO_UNAVAILABLE"}', "application/json")
                    return
                page = page.replace(
                    marker, b'<script defer src="/local-run.js"></script>\n</head>', 1
                )
                self._send(200, page, "text/html; charset=utf-8")
                return
        # Keep the static allowlist inherited from the local viewer.
        super().do_GET()

    def do_HEAD(self) -> None:
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def do_POST(self) -> None:
        self._send(405, b'{"error":"READ_ONLY"}', "application/json")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST


class HostedFixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    def __init__(
        self, address: tuple[str, int], *, build_sha: str = "unknown",
        public_recorded_document: dict | None = None,
    ) -> None:
        if address[0] not in ("127.0.0.1", "0.0.0.0"):
            raise ValueError("bind address must be loopback or all IPv4 interfaces")
        if not isinstance(build_sha, str) or (
            build_sha != "unknown" and not _HEX_SHA.fullmatch(build_sha)
        ):
            raise ValueError("build SHA must be a lowercase hex revision or unknown")
        self.public_excerpt_body = None
        if public_recorded_document is not None:
            payload = read_public_recorded_excerpt(public_recorded_document)
            self.public_excerpt_body = _encoded(payload)
        self.fixtures = {scenario: _verified_fixture(scenario) for scenario in SCENARIOS}
        self.health_body = _encoded({"status": "ok", "mode": "SYNTHETIC", "build_sha": build_sha})
        self._slots = BoundedSemaphore(_MAX_CONNECTIONS)
        super().__init__(address, HostedFixtureHandler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(_TIMEOUT_SECONDS)
        return request, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Read-only public synthetic preview")
    parser.add_argument("--bind", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    sha = _source_revision(os.environ)
    approved = os.environ.get("OFC_PUBLISH_RECORDED_PROOF", "")
    packed = os.environ.get("OFC_PUBLIC_RECORDED_PACK_B64", "")
    document = None
    if packed or approved:
        if approved != "APPROVED" or not 0 < len(packed) <= 32000:
            parser.error("recorded proof publication requires explicit approval and a bounded pack")
        try:
            decoded = base64.b64decode(packed, validate=True)
            document = json.loads(decoded)
            read_public_recorded_excerpt(document)
        except (binascii.Error, UnicodeError, ValueError, TypeError):
            parser.error("invalid recorded proof pack")
    with HostedFixtureServer(
        (args.bind, args.port), build_sha=sha,
        public_recorded_document=document,
    ) as server:
        print(f"Fixture preview listening on {args.bind}:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
