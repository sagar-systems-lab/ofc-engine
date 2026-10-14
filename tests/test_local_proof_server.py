"""Local recorded proof session requires strict manifest lineage and read-only loopback."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ofc_engine.hosted_preview import HostedFixtureServer
from ofc_engine.local_proof_server import LocalProofRejected, LocalProofServer, load_manifest
from ofc_engine.revisions import digest_obj


def report():
    return {
        "schema": "ofc-private-operator-workspace-v1",
        "title": "Recorded decision proof",
        "source_mode": "RECORDED_REDACTED",
        "historical_replay": True,
        "generated_at": "2026-10-14T12:00:00+00:00",
        "captured_at": "2026-10-13T12:00:00+00:00",
        "plan": {
            "baseline_digest": "a" * 64, "stored_digest": "b" * 64,
            "revision": 1, "journal_events": 2, "decision_count": 1,
        },
        "decision": {
            "initial": "Candidate 01", "replacement": "Candidate 02",
            "changed": True, "rank_order": ["Candidate 02", "Candidate 01", "Candidate 03"],
            "evidence_off": "UNCERTAIN", "evidence_on_initial": "REPAIR",
            "evidence_on_after_repair": "PASS", "repair_attempts": 1,
            "affected_decision_count": 1, "structural_recheck": "STRUCTURAL_PASS",
        },
        "recheck": {
            "status": "ORIGINAL_CAPTURE_REPLAY_ONLY", "recorded_rank_verdict": "PASS",
            "same_capture": True, "later_capture_observed": False,
            "pending_count": 1, "pending_recheck_cleared": False,
        },
        "qualifications": {
            "operator_supplied_capture_not_commit_attested": True,
            "provider_authenticated": False, "independent_recheck_completed": False,
            "overall_plan_verified": False, "release_authorized": False,
            "external_action_executed": False, "real_qloo_requests_this_export": 0,
        },
    }


def manifest(value=None):
    value = report() if value is None else value
    return {"payload": value, "sha256": digest_obj(value)}


def write_manifest(path, doc):
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


@contextmanager
def serve(server):
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(url, *, method="GET", headers=None):
    with urlopen(Request(url, method=method, headers=headers or {}), timeout=5) as response:
        return response.status, response.headers, response.read()


def rejected(url, expected, *, method="GET", headers=None):
    with pytest.raises(HTTPError) as exc:
        request(url, method=method, headers=headers)
    assert exc.value.code == expected


def test_manifest_load_validates_digest_and_schema(tmp_path):
    path = write_manifest(tmp_path / "proof.json", manifest())
    assert load_manifest(path) == manifest()
    changed = deepcopy(manifest())
    changed["payload"]["plan"]["revision"] = 2
    with pytest.raises(LocalProofRejected, match="CHECKSUM_MISMATCH"):
        load_manifest(write_manifest(path, changed))
    changed["sha256"] = digest_obj(changed["payload"])
    with pytest.raises(LocalProofRejected, match="INVALID_REVISION"):
        load_manifest(write_manifest(path, changed))
    changed = deepcopy(manifest())
    changed["payload"]["qualifications"]["release_authorized"] = True
    with pytest.raises(LocalProofRejected, match="UNQUALIFIED_AUTHORITY"):
        load_manifest(write_manifest(path, manifest(changed["payload"])))


@pytest.mark.parametrize("key", ["revision", "journal_events", "decision_count"])
def test_integer_fields_cannot_be_boolean(tmp_path, key):
    body = report()
    body["plan"][key] = True
    with pytest.raises(LocalProofRejected, match="INVALID_REVISION"):
        load_manifest(write_manifest(tmp_path / "bool.json", manifest(body)))


def test_symlink_missing_and_oversize_manifest_are_rejected(tmp_path):
    original = write_manifest(tmp_path / "proof.json", manifest())
    alias = tmp_path / "alias.json"
    alias.symlink_to(original)
    with pytest.raises(LocalProofRejected, match="UNSAFE_MANIFEST"):
        load_manifest(alias)
    with pytest.raises(OSError):
        load_manifest(tmp_path / "missing.json")
    enormous = tmp_path / "large.json"
    enormous.write_bytes(b" " * (128 * 1024 + 1))
    with pytest.raises(LocalProofRejected, match="UNSAFE_MANIFEST"):
        load_manifest(enormous)


def test_local_session_auto_mode_and_no_http_mutations():
    with serve(LocalProofServer(("127.0.0.1", 0), manifest=manifest())) as base:
        code, headers, html = request(base + "/")
        assert code == 200
        assert b'data-ofc-local-proof="true"' in html
        assert "connect-src 'self'" in headers["Content-Security-Policy"]
        assert headers["Cache-Control"] == "no-store"
        code, _, body = request(base + "/api/local-proof")
        assert code == 200
        assert json.loads(body) == manifest()
        for path in ("/recorded.js", "/recorded.css"):
            assert request(base + path)[0] == 200
        for path in ("/api/proposals", "/api/local-proof?debug=1", "/.env"):
            rejected(base + path, 400 if "?" in path else 404)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            rejected(base + "/api/local-proof", 405, method=method)


def test_cross_origin_and_hostile_host_rejected():
    with serve(LocalProofServer(("127.0.0.1", 0), manifest=manifest())) as base:
        rejected(base + "/api/local-proof", 403, headers={"Host": "evil.example"})
        rejected(base + "/api/local-proof", 403, headers={"Origin": "https://evil.example"})
        rejected(base + "/api/local-proof", 403, headers={"Sec-Fetch-Site": "cross-site"})


def test_public_preview_never_serves_private_local_proof():
    with serve(HostedFixtureServer(("127.0.0.1", 0))) as base:
        rejected(base + "/api/local-proof", 404)
        _, _, html = request(base + "/recorded")
        assert b'data-ofc-local-proof="true"' not in html


def test_bind_to_nonloopback_blocked():
    with pytest.raises(ValueError, match="loopback"):
        LocalProofServer(("0.0.0.0", 0), manifest=manifest())
