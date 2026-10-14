"""Serve one redacted recorded proof locally without uploads or action routes."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
from pathlib import Path
import re
from stat import S_ISREG
from urllib.parse import urlsplit

from .qloo_agent_bridge import _read_private_json
from .revisions import digest_obj

_HEX = re.compile(r"^[a-f0-9]{64}$")
_ALIAS = re.compile(r"^Candidate [0-9]{2}$")
_ASSETS = {
    "/": ("recorded.html", "text/html; charset=utf-8"),
    "/recorded": ("recorded.html", "text/html; charset=utf-8"),
    "/recorded.js": ("recorded.js", "text/javascript; charset=utf-8"),
    "/recorded.css": ("recorded.css", "text/css; charset=utf-8"),
}


class LocalProofRejected(ValueError):
    pass


def load_manifest(path: Path) -> dict:
    info = path.lstat()
    if not S_ISREG(info.st_mode) or not 0 < info.st_size <= 128 * 1024:
        raise LocalProofRejected("UNSAFE_MANIFEST")
    document = _read_private_json(path, max_bytes=128 * 1024)
    if set(document) != {"payload", "sha256"}:
        raise LocalProofRejected("INVALID_ENVELOPE")
    payload, digest = document["payload"], document["sha256"]
    if not isinstance(payload, dict) or not isinstance(digest, str) or not _HEX.fullmatch(digest):
        raise LocalProofRejected("INVALID_CHECKSUM")
    if digest_obj(payload) != digest:
        raise LocalProofRejected("MANIFEST_CHECKSUM_MISMATCH")
    if set(payload) != {
        "schema", "title", "source_mode", "historical_replay", "generated_at",
        "captured_at", "plan", "decision", "recheck", "qualifications",
    }:
        raise LocalProofRejected("INVALID_PROOF_FIELDS")
    if (
        payload["schema"] != "ofc-private-operator-workspace-v1"
        or payload["title"] != "Recorded decision proof"
        or payload["source_mode"] != "RECORDED_REDACTED"
        or type(payload["historical_replay"]) is not bool
    ):
        raise LocalProofRejected("UNSUPPORTED_PROOF")
    plan, decision, recheck, qualifications = (
        payload["plan"], payload["decision"], payload["recheck"], payload["qualifications"],
    )
    if not all(isinstance(x, dict) for x in (plan, decision, recheck, qualifications)):
        raise LocalProofRejected("INVALID_PROOF_STRUCTURE")
    if (
        set(plan) != {"baseline_digest", "stored_digest", "revision", "journal_events", "decision_count"}
        or plan["revision"] != 1 or plan["journal_events"] != 2 or plan["decision_count"] != 1
        or not all(isinstance(plan[k], str) and _HEX.fullmatch(plan[k])
                   for k in ("baseline_digest", "stored_digest"))
    ):
        raise LocalProofRejected("INVALID_REVISION")
    if (
        set(decision) != {
            "initial", "replacement", "changed", "rank_order",
            "evidence_off", "evidence_on_initial", "evidence_on_after_repair",
            "repair_attempts", "affected_decision_count", "structural_recheck",
        }
        or not all(isinstance(decision.get(k), str) and _ALIAS.fullmatch(decision[k])
                   for k in ("initial", "replacement"))
        or decision["initial"] == decision["replacement"]
        or decision["changed"] is not True
        or decision["repair_attempts"] != 1
        or decision["affected_decision_count"] != 1
        or decision["structural_recheck"] != "STRUCTURAL_PASS"
        or decision["evidence_off"] != "UNCERTAIN"
        or decision["evidence_on_initial"] != "REPAIR"
        or decision["evidence_on_after_repair"] != "PASS"
    ):
        raise LocalProofRejected("INVALID_REPAIR")
    ranks = decision["rank_order"]
    if (
        not isinstance(ranks, list) or not 2 <= len(ranks) <= 5
        or not all(isinstance(k, str) and _ALIAS.fullmatch(k) for k in ranks)
        or len(set(ranks)) != len(ranks)
        or decision["initial"] not in ranks
        or ranks[0] != decision["replacement"]
    ):
        raise LocalProofRejected("INVALID_RANKS")
    if (
        set(recheck) != {
            "status", "recorded_rank_verdict", "same_capture", "later_capture_observed",
            "pending_count", "pending_recheck_cleared",
        }
        or recheck["status"] not in {
            "ORIGINAL_CAPTURE_REPLAY_ONLY", "RECORDED_LATER_CAPTURE_RANK_PASS_ONLY",
            "RECORDED_CAPTURE_NOT_LATER", "RECORDED_RECHECK_UNRESOLVED",
        }
        or recheck["recorded_rank_verdict"] not in {"PASS", "REPAIR", "UNCERTAIN"}
        or type(recheck["same_capture"]) is not bool
        or type(recheck["later_capture_observed"]) is not bool
        or recheck["same_capture"] and recheck["later_capture_observed"]
        or recheck["pending_count"] != 1
        or recheck["pending_recheck_cleared"] is not False
    ):
        raise LocalProofRejected("UNQUALIFIED_RECHECK")
    if (
        set(qualifications) != {
            "operator_supplied_capture_not_commit_attested", "provider_authenticated",
            "independent_recheck_completed", "overall_plan_verified",
            "release_authorized", "external_action_executed", "real_qloo_requests_this_export",
        }
        or qualifications["operator_supplied_capture_not_commit_attested"] is not True
        or any(qualifications[x] is not False for x in (
            "provider_authenticated", "independent_recheck_completed",
            "overall_plan_verified", "release_authorized", "external_action_executed",
        ))
        or qualifications["real_qloo_requests_this_export"] != 0
    ):
        raise LocalProofRejected("UNQUALIFIED_AUTHORITY")
    return document


class LocalProofHandler(BaseHTTPRequestHandler):
    server_version = "OFCLocalProof"
    sys_version = ""

    def log_message(self, format, *args):
        return

    def _reply(self, status: int, body: bytes, media: str) -> None:
        self.send_response(status)
        for key, value in (
            ("Content-Type", media),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Cross-Origin-Resource-Policy", "same-origin"),
            ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'"),
        ):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self) -> bool:
        host = self.headers.get("Host", "")
        port = self.server.server_port
        if host not in (f"localhost:{port}", f"127.0.0.1:{port}"):
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + host:
            return False
        if self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            self._reply(403, b'{"error":"ORIGIN_REJECTED"}', "application/json")
            return
        if len(self.path) > 256:
            self._reply(414, b'{"error":"URI_TOO_LONG"}', "application/json")
            return
        parts = urlsplit(self.path)
        if parts.query or parts.fragment:
            self._reply(400, b'{"error":"INVALID_QUERY"}', "application/json")
            return
        if parts.path == "/api/local-proof":
            self._reply(200, self.server.proof_body, "application/json")
            return
        asset = _ASSETS.get(parts.path)
        if asset is None:
            self._reply(404, b'{"error":"NOT_FOUND"}', "application/json")
            return
        data = files("ofc_engine").joinpath("web", asset[0]).read_bytes()
        if asset[0] == "recorded.html":
            original = b'<html lang="en">'
            if original not in data:
                self._reply(503, b'{"error":"UNAVAILABLE"}', "application/json")
                return
            data = data.replace(original, b'<html lang="en" data-ofc-local-proof="true">', 1)
        self._reply(200, data, asset[1])

    def do_POST(self):
        self._reply(405, b'{"error":"READ_ONLY"}', "application/json")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST

    def do_HEAD(self):
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()


class LocalProofServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address: tuple[str, int], *, manifest: dict):
        if address[0] != "127.0.0.1":
            raise ValueError("private operator viewer binds to loopback only")
        if not isinstance(manifest, dict):
            raise ValueError("invalid manifest")
        self.proof_body = json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        super().__init__(address, LocalProofHandler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="View a private recorded proof in a loopback-only browser session")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be 0 or 1-65535")
    try:
        manifest = load_manifest(args.manifest)
    except (OSError, ValueError, TypeError, UnicodeError):
        print(json.dumps({"status": "BLOCKED", "release_authorized": False, "real_qloo_requests": 0}))
        return 2
    with LocalProofServer(("127.0.0.1", args.port), manifest=manifest) as server:
        print(f"OPEN_LOCAL_PROOF: http://127.0.0.1:{server.server_port}/", flush=True)
        print("READ_ONLY: true | QLOO_REQUESTS: 0 | RELEASE_AUTHORIZED: false", flush=True)
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
