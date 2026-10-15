"""One loopback origin for the frozen studio, private proof, and agent intake."""
from __future__ import annotations

import json
from importlib.resources import files
from urllib.parse import urlsplit

from .agent_api import AgentAPIHandler
from .agent_run import RunSnapshot
from .local_run_view import project_local_run
from .agent_evidence_session import OperatorEvidenceSession
from .durable import SQLiteRevisionStore
from .hosted_preview import HostedFixtureHandler, HostedFixtureServer


class ConnectedLocalHandler(AgentAPIHandler, HostedFixtureHandler):
    server_version = "OFCConnectedLocal"

    def _same_origin(self) -> bool:
        port = self.server.server_port
        host = self.headers.get("Host", "")
        if (
            host not in (f"127.0.0.1:{port}", f"localhost:{port}")
            or self.headers.get("Origin") not in (None, "http://" + host)
            or self.headers.get("Sec-Fetch-Site") not in (
                None, "none", "same-origin"
            )
        ):
            self._send(
                403, b'{"error":"ORIGIN_REJECTED","release_authorized":false}',
                "application/json",
            )
            return False
        return True

    def do_GET(self) -> None:
        if not self._same_origin():
            return
        parts = urlsplit(self.path)
        if parts.path == "/api/local-run" and not parts.query:
            try:
                snapshot = RunSnapshot.model_validate(self.server.evidence_session.snapshot(
                    self.server.store, self.server.evidence_session.proposal.intent.run_id
                ))
                result = project_local_run(snapshot)
            except Exception:
                self._send(503, b'{"error":"EVIDENCE_UNAVAILABLE","release_authorized":false}', "application/json")
                return
            self._send(200, json.dumps(result, sort_keys=True, separators=(",", ":")).encode(), "application/json")
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
            page = page.replace(marker, b'<script defer src="/local-run.js"></script>\n</head>', 1)
            self._send(200, page, "text/html; charset=utf-8")
            return
        if parts.path.startswith("/api/proposals/") or parts.path == "/api/proposals":
            AgentAPIHandler.do_GET(self)
            return
        if parts.path == "/api/local-proof":
            if self.server.proof_body is None:
                self._send(404, b'{"error":"PROOF_UNAVAILABLE"}', "application/json")
                return
            if parts.query or parts.fragment:
                self._send(400, b'{"error":"INVALID_QUERY"}', "application/json")
                return
            if len(self.path) > 256:
                self._send(414, b'{"error":"URI_TOO_LONG"}', "application/json")
                return
            self._send(200, self.server.proof_body, "application/json")
            return
        if parts.path in ("/recorded", "/recorded.html") and not parts.query:
            if self.server.proof_body is None:
                self.send_response(303)
                self.send_header("Location", "/#provider")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            page = files("ofc_engine").joinpath("web", "recorded.html").read_bytes()
            marker = b'<html lang="en">'
            if marker not in page:
                self._send(503, b'{"error":"UNAVAILABLE"}', "application/json")
                return
            page = page.replace(
                marker, b'<html lang="en" data-ofc-local-proof="true">', 1
            )
            self._send(200, page, "text/html; charset=utf-8")
            return
        HostedFixtureHandler.do_GET(self)

    def do_POST(self) -> None:
        if not self._same_origin():
            return
        if self.path == "/api/proposals":
            AgentAPIHandler.do_POST(self)
            return
        HostedFixtureHandler.do_POST(self)

    def do_HEAD(self) -> None:
        HostedFixtureHandler.do_HEAD(self)

    do_PUT = HostedFixtureHandler.do_POST
    do_PATCH = HostedFixtureHandler.do_POST
    do_DELETE = HostedFixtureHandler.do_POST
    do_OPTIONS = HostedFixtureHandler.do_POST


class ConnectedLocalServer(HostedFixtureServer):
    def __init__(
        self,
        address: tuple[str, int],
        *,
        database: str,
        token: str,
        session: OperatorEvidenceSession,
        manifest: dict | None,
    ) -> None:
        if address[0] != "127.0.0.1":
            raise ValueError("connected product must bind to loopback")
        if not isinstance(token, str) or not 32 <= len(token) <= 256 or (
            not token.isascii()
        ) or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("invalid agent API token")
        if manifest is not None and (
            not isinstance(manifest, dict) or set(manifest) != {"payload", "sha256"}
        ):
            raise ValueError("invalid recorded proof manifest")
        store = SQLiteRevisionStore(database)
        session.snapshot(store, session.proposal.intent.run_id)
        if manifest is not None:
            session.review(store, session.proposal.intent.run_id)
        self.store = store
        self.auth_token = token
        self.evidence_session = session
        self.proof_body = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            if manifest is not None else None
        )
        super().__init__(address)
        self.RequestHandlerClass = ConnectedLocalHandler
