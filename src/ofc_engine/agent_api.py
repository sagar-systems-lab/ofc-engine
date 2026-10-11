"""Loopback-only authenticated ingress for frozen agent proposals."""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import sqlite3
from urllib.parse import urlsplit

from pydantic import ValidationError

from .durable import SQLiteRevisionStore, StorageIntegrityError
from .intake import AgentProposal, ProposalConflict, accept_proposal
from .revisions import plan_digest

_MAX_BODY = 96 * 1024
_MAX_TARGET = 256
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class AgentAPIHandler(BaseHTTPRequestHandler):
    server_version = "OFCAgentAPI"
    sys_version = ""

    def log_message(self, format, *args):
        return

    def _reply(self, status: int, value: dict) -> None:
        body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, status: int, code: str) -> None:
        self._reply(status, {"error": code, "release_authorized": False})

    def _authorize(self) -> bool:
        host = self.headers.get("Host", "")
        port = self.server.server_port
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._deny(403, "ORIGIN_REJECTED")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + host:
            self._deny(403, "ORIGIN_REJECTED")
            return False
        if self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
            self._deny(403, "ORIGIN_REJECTED")
            return False
        provided = self.headers.get("Authorization", "")
        if not hmac.compare_digest(provided, "Bearer " + self.server.auth_token):
            self._deny(401, "AUTH_REQUIRED")
            return False
        return True

    def do_GET(self) -> None:
        if not self._authorize():
            return
        if len(self.path) > _MAX_TARGET:
            self._deny(414, "URI_TOO_LONG")
            return
        parts = urlsplit(self.path)
        if parts.query or parts.fragment:
            self._deny(400, "INVALID_QUERY")
            return
        if parts.path == "/healthz":
            self._reply(200, {"status": "ok", "mode": "LOCAL", "release_authorized": False})
            return
        prefix = "/api/proposals/"
        if not parts.path.startswith(prefix) or not _RUN_ID.fullmatch(parts.path[len(prefix):]):
            self._deny(404, "NOT_FOUND")
            return
        run_id = parts.path[len(prefix):]
        try:
            plan = self.server.store.get(run_id)
            audit = self.server.store.audit_history(run_id)
            if not audit or audit[-1].after_digest != plan_digest(plan):
                raise StorageIntegrityError("plan and journal disagree")
            pending = self.server.store.pending_rechecks(run_id)
        except KeyError:
            self._deny(404, "NOT_FOUND")
            return
        except (StorageIntegrityError, sqlite3.Error):
            self._deny(503, "STATE_UNAVAILABLE")
            return
        self._reply(200, {
            "plan_id": plan.plan_id,
            "revision": plan.revision,
            "plan_digest": plan_digest(plan),
            "pending_rechecks": list(pending),
            "source_status": "EVIDENCE_PENDING",
            "release_authorized": False,
            "external_action_executed": False,
        })

    def do_POST(self) -> None:
        if not self._authorize():
            return
        if self.path != "/api/proposals":
            self._deny(405, "METHOD_NOT_ALLOWED")
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._deny(400, "INVALID_FRAMING")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            self._deny(411, "CONTENT_LENGTH_REQUIRED")
            return
        length = int(lengths[0])
        if length <= 0 or length > _MAX_BODY:
            self._deny(413, "INVALID_SIZE")
            return
        if self.headers.get("Content-Type", "").lower() != "application/json":
            self._deny(415, "EXPECTED_JSON")
            return
        try:
            proposal = AgentProposal.model_validate_json(self.rfile.read(length))
            receipt = accept_proposal(self.server.store, proposal)
        except (ValidationError, ValueError, TypeError):
            self._deny(400, "INVALID_PROPOSAL")
            return
        except ProposalConflict:
            self._deny(409, "PROPOSAL_CONFLICT")
            return
        except (StorageIntegrityError, sqlite3.Error):
            self._deny(503, "STATE_UNAVAILABLE")
            return
        self._reply(201 if receipt.created else 200, receipt.as_dict())

    def do_PUT(self) -> None:
        self._deny(405, "METHOD_NOT_ALLOWED")

    do_PATCH = do_PUT
    do_DELETE = do_PUT
    do_OPTIONS = do_PUT
    def do_HEAD(self) -> None:
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()


class AgentAPIServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address: tuple[str, int], *, database: str, auth_token: str) -> None:
        if address[0] != "127.0.0.1":
            raise ValueError("agent API must bind to IPv4 loopback")
        if (
            not isinstance(auth_token, str) or not 32 <= len(auth_token) <= 256
            or not auth_token.isascii() or any(ord(x) < 33 or ord(x) > 126 for x in auth_token)
        ):
            raise ValueError("a 32–256 character ASCII operator token is required")
        self.store = SQLiteRevisionStore(database)
        self.auth_token = auth_token
        super().__init__(address, AgentAPIHandler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local authenticated agent proposal intake")
    parser.add_argument("--database", required=True)
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    token = os.environ.get("OFC_AGENT_TOKEN", "")
    with AgentAPIServer(("127.0.0.1", args.port), database=args.database, auth_token=token) as server:
        print(f"Agent API listening at http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
