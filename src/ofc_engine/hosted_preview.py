"""Public studio with opt-in durable runs and operator-gated provider review."""
from __future__ import annotations

import argparse
import base64
import binascii
from importlib.resources import files
from http.server import ThreadingHTTPServer
import json
import os
import re
import sqlite3
import psycopg
from threading import BoundedSemaphore
from urllib.parse import parse_qs, urlsplit

from .webdemo import DemoHandler, SCENARIOS, demo_snapshot
from .public_recorded import read_public_recorded_excerpt
from .hosted_live import HostedLiveReview, configured_live_review
from .hosted_preflight import preflight
from .hosted_runs import HostedRunRegistry, RunCapacityReached
from .hosted_tour import configured_tour
from .intake import AgentProposal
from .postgres_store import PostgresRevisionStore

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

    def _studio_page(self, extra_script: str | None = None) -> None:
        page = files("ofc_engine").joinpath("web", "studio.html").read_bytes()
        marker = b"</head>"
        if page.count(marker) != 1:
            self._send(503, b'{"error":"STUDIO_UNAVAILABLE"}', "application/json")
            return
        names = ["/hosted-preflight.js"]
        if self.server.tour_proposal is not None and self.server.run_registry is not None:
            names.append("/hosted-tour.js")
        if extra_script:
            names.append(extra_script)
        scripts = "".join(
            f'<script defer src="{name}"></script>\n' for name in names
        ).encode("utf-8")
        self._send(200, page.replace(marker, scripts + marker, 1),
                   "text/html; charset=utf-8")

    def _durable_ready(self) -> bool:
        registry = self.server.run_registry
        if registry is None:
            return True
        try:
            return registry.ready()
        except (psycopg.Error, OSError, ValueError):
            return False

    def do_GET(self) -> None:
        if len(self.path) > _MAX_TARGET_LENGTH:
            self._send(414, b'{"error":"URI_TOO_LONG"}', "application/json")
            return
        parts = urlsplit(self.path)
        if parts.path == "/hosted-preflight.js" and not parts.query:
            script = files("ofc_engine").joinpath("web", "hosted-preflight.js").read_bytes()
            self._send(200, script, "text/javascript; charset=utf-8")
            return
        if parts.path == "/hosted-tour.js" and not parts.query:
            if self.server.tour_proposal is None or self.server.run_registry is None:
                self._send(404, b'{"error":"TOUR_UNAVAILABLE"}', "application/json")
                return
            script = files("ofc_engine").joinpath("web", "hosted-tour.js").read_bytes()
            self._send(200, script, "text/javascript; charset=utf-8")
            return
        if parts.path == "/api/runs/status" and not parts.query:
            if not self._durable_ready():
                self._send(503, b'{"error":"RUN_STORAGE_UNAVAILABLE","release_authorized":false}',
                           "application/json")
                return
            self._send(200, _encoded({
                "enabled": self.server.run_registry is not None,
                "tour_available": self.server.tour_proposal is not None and self.server.run_registry is not None,
                "release_authorized": False,
                "real_qloo_requests": 0,
            }), "application/json")
            return
        if parts.path.startswith("/api/runs/"):
            match = re.fullmatch(r"/api/runs/(web-[0-9a-f]{32})/review", parts.path)
            if (
                match is None or parts.query or self.server.run_registry is None
                or not self.headers.get("Authorization", "").startswith("Bearer ")
            ):
                self._send(404, b'{"error":"RUN_UNAVAILABLE"}', "application/json")
                return
            try:
                result = self.server.run_registry.review(
                    match.group(1), self.headers["Authorization"][7:]
                )
                live = self.server.hosted_live
                if live is not None and live.bound_run_id == result["run_id"]:
                    status, record = live.public_result()
                    if status == 200 and record.get("agent_run"):
                        run = record["agent_run"]
                        proof = run.get("provenance", {})
                        if (
                            run.get("verification_status") != "REVIEW_REQUIRED"
                            or run.get("release_authorized") is not False
                            or run.get("external_action_executed") is not False
                            or run.get("provider_authenticated") is not False
                            or run.get("independent_recheck_completed") is not False
                            or run.get("source_mode") != "LIVE_HTTP_UNVERIFIED"
                            or run.get("real_qloo_requests") != 1
                            or run.get("revision") != result["revision"]
                            or proof.get("baseline_digest") != result["baseline_digest"]
                            or proof.get("current_plan_digest") != result["plan_digest"]
                            or proof.get("journal_head") != result["journal_head"]
                            or not isinstance(run.get("decisions"), list)
                            or len(run["decisions"]) != len(result["decisions"])
                            or [d.get("decision_id") for d in run["decisions"]]
                            != [d["decision_id"] for d in result["decisions"]]
                        ):
                            raise ValueError("stored provider review does not match this run")
                        result.update({
                            "source_mode": "LIVE_HTTP_UNVERIFIED",
                            "real_qloo_requests": 1,
                            "decisions": run["decisions"],
                            "pending_rechecks": run["pending_rechecks"],
                            "agent_run": run,
                        })
            except KeyError:
                self._send(404, b'{"error":"RUN_UNAVAILABLE"}', "application/json")
                return
            except (ValueError, psycopg.Error, sqlite3.Error):
                self._send(503, b'{"error":"RUN_REVIEW_UNAVAILABLE"}', "application/json")
                return
            self._send(200, _encoded(result), "application/json")
            return
        if parts.path in ("/healthz", "/readyz"):
            if parts.query:
                self._send(400, b'{"error":"INVALID_QUERY"}', "application/json")
                return
            if parts.path == "/readyz" and not self._durable_ready():
                self._send(503, b'{"error":"RUN_STORAGE_UNAVAILABLE"}', "application/json")
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
        if self.server.hosted_live is not None:
            if parts.path == "/api/qloo/status" and not parts.query:
                try:
                    state = self.server.hosted_live.state()
                except (OSError, ValueError, sqlite3.Error, psycopg.Error):
                    self._send(503, b'{"error":"LEDGER_UNAVAILABLE"}', "application/json")
                    return
                self._send(200, _encoded(state), "application/json")
                return
            if parts.path == "/api/qloo/result" and not parts.query:
                if self.server.hosted_live.bound_run_id is not None:
                    self._send(404, b'{"error":"RUN_UNAVAILABLE"}', "application/json")
                    return
                try:
                    code, report = self.server.hosted_live.public_result()
                except (OSError, ValueError, sqlite3.Error, psycopg.Error):
                    self._send(503, b'{"error":"LEDGER_UNAVAILABLE"}', "application/json")
                    return
                self._send(code, _encoded(report), "application/json")
                return
            if parts.path == "/hosted-live.js" and not parts.query:
                if self.server.hosted_live.bound_run_id is not None:
                    self._send(404, b'{"error":"RUN_UNAVAILABLE"}', "application/json")
                    return
                script = files("ofc_engine").joinpath("web", "hosted-live.js").read_bytes()
                self._send(200, script, "text/javascript; charset=utf-8")
                return
            if parts.path == "/" and not parts.query:
                self._studio_page(
                    None if self.server.hosted_live.bound_run_id is not None
                    else "/hosted-live.js"
                )
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
                self._studio_page("/local-run.js")
                return
        if parts.path == "/" and not parts.query:
            self._studio_page()
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
        if self.command != "POST":
            self._send(405, b'{"error":"METHOD_NOT_ALLOWED"}', "application/json")
            return
        if self.path in ("/api/runs", "/api/runs/tour"):
            if self.server.run_registry is None:
                self._send(405, b'{"error":"READ_ONLY"}', "application/json")
                return
            if self.path == "/api/runs/tour" and self.server.tour_proposal is None:
                self._send(404, b'{"error":"TOUR_UNAVAILABLE"}', "application/json")
                return
            host = self.headers.get("Host", "")
            expected = (
                "http://" + host if host.startswith(("127.0.0.1:", "localhost:"))
                else "https://" + host
            )
            length = self.headers.get("Content-Length", "")
            if (
                not host or self.headers.get("Origin", "") != expected
                or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")
                or self.headers.get("Content-Type") != "application/json"
                or not length.isascii() or not length.isdecimal()
                or not 1 <= int(length) <= 96 * 1024
            ):
                self._send(403, b'{"error":"INVALID_REQUEST"}', "application/json")
                return
            try:
                payload = json.loads(self.rfile.read(int(length)))
                if self.path == "/api/runs/tour":
                    if payload != {"confirm": "SAVE_SIX_DECISION_TOUR"}:
                        raise ValueError("invalid tour confirmation")
                    payload = self.server.tour_proposal.model_dump(mode="json")
                result = self.server.run_registry.create(payload)
            except RunCapacityReached:
                self._send(429, b'{"error":"REVIEW_CAPACITY_REACHED"}', "application/json")
                return
            except (ValueError, TypeError, UnicodeError, OverflowError):
                self._send(422, b'{"error":"INVALID_PROPOSAL"}', "application/json")
                return
            except (psycopg.Error, sqlite3.Error):
                self._send(503, b'{"error":"RUN_STORAGE_UNAVAILABLE"}', "application/json")
                return
            self._send(201, _encoded(result), "application/json")
            return
        if self.path == "/api/preflight":
            host = self.headers.get("Host", "")
            expected_origin = (
                "http://" + host if host.startswith(("127.0.0.1:", "localhost:"))
                else "https://" + host
            )
            if (
                not host or self.headers.get("Origin", "") != expected_origin
                or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")
                or self.headers.get("Content-Type") != "application/json"
            ):
                self._send(403, b'{"error":"ORIGIN_REJECTED"}', "application/json")
                return
            length = self.headers.get("Content-Length", "")
            if (
                not length.isascii() or not length.isdecimal()
                or not 1 <= int(length) <= 96 * 1024
            ):
                self._send(400, b'{"error":"INVALID_REQUEST"}', "application/json")
                return
            try:
                content = json.loads(self.rfile.read(int(length)))
                outcome = preflight(content)
            except (ValueError, TypeError, UnicodeError, OverflowError):
                self._send(422, b'{"error":"INVALID_PROPOSAL","release_authorized":false}',
                           "application/json")
                return
            self._send(200, _encoded(outcome), "application/json")
            return
        if self.path != "/api/qloo/live-rank" or self.server.hosted_live is None:
            self._send(405, b'{"error":"READ_ONLY"}', "application/json")
            return
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        expected_origin = (
            "http://" + host if host.startswith(("127.0.0.1:", "localhost:"))
            else "https://" + host
        )
        if (
            not host or
            origin != expected_origin
            or self.headers.get("Sec-Fetch-Site") not in (
                None, "same-origin"
            )
            or self.headers.get("Content-Type") != "application/json"
        ):
            self._send(403, b'{"error":"ORIGIN_REJECTED"}', "application/json")
            return
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdecimal() or not 1 <= int(length) <= 256:
            self._send(400, b'{"error":"INVALID_REQUEST"}', "application/json")
            return
        try:
            body = json.loads(self.rfile.read(int(length)))
        except (ValueError, UnicodeDecodeError):
            self._send(400, b'{"error":"INVALID_REQUEST"}', "application/json")
            return
        if body != {"confirm": "SPEND_ONE_HOSTED_QLOO_REQUEST"}:
            self._send(400, b'{"error":"INVALID_REQUEST"}', "application/json")
            return
        if not self.server.hosted_live.authorize(
            self.headers.get("X-OFC-Operator-Token", "")
        ):
            self._send(403, b'{"error":"OPERATOR_AUTH_REQUIRED","release_authorized":false}', "application/json")
            return
        try:
            status, result = self.server.hosted_live.observe()
        except (OSError, ValueError, sqlite3.Error, psycopg.Error):
            self._send(503, b'{"error":"LEDGER_UNAVAILABLE","release_authorized":false}', "application/json")
            return
        self._send(status, _encoded(result), "application/json")

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
        hosted_live: HostedLiveReview | None = None,
        run_registry: HostedRunRegistry | None = None,
        tour_proposal: AgentProposal | None = None,
    ) -> None:
        if address[0] not in ("127.0.0.1", "0.0.0.0"):
            raise ValueError("bind address must be loopback or all IPv4 interfaces")
        if not isinstance(build_sha, str) or (
            build_sha != "unknown" and not _HEX_SHA.fullmatch(build_sha)
        ):
            raise ValueError("build SHA must be a lowercase hex revision or unknown")
        if hosted_live is not None and public_recorded_document is not None:
            raise ValueError("only one Qloo presentation mode may be enabled")
        self.run_registry = run_registry
        self.tour_proposal = tour_proposal
        self.hosted_live = hosted_live
        if tour_proposal is not None and run_registry is None:
            raise ValueError("hosted tour requires durable run registry")
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
    try:
        live = configured_live_review(os.environ)
    except (ValueError, psycopg.Error):
        parser.error("invalid or incomplete hosted Qloo approval configuration")
    durable_mode = os.environ.get("OFC_DURABLE_RUNS_MODE", "")
    try:
        tour = configured_tour(os.environ)
    except ValueError:
        parser.error("invalid hosted tour scope")
    registry = None
    if durable_mode:
        if durable_mode != "ENABLED" or not os.environ.get("OFC_DATABASE_URL"):
            parser.error("durable runs require an explicitly configured PostgreSQL URL")
        try:
            registry = HostedRunRegistry(
                PostgresRevisionStore(os.environ["OFC_DATABASE_URL"])
            )
        except (ValueError, psycopg.Error):
            parser.error("durable runs unavailable")
    with HostedFixtureServer(
        (args.bind, args.port), build_sha=sha,
        public_recorded_document=document, hosted_live=live,
        run_registry=registry, tour_proposal=tour,
    ) as server:
        print(f"Fixture preview listening on {args.bind}:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
