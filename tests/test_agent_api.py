"""Local authenticated agent ingress: durability and fail-closed HTTP contracts."""
from contextlib import contextmanager
import http.client
import json
import sqlite3
from pathlib import Path
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ofc_engine.agent_api import AgentAPIServer

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-only-" + "a" * 38
EXAMPLE = ROOT / "examples" / "agent_proposal.json"


@contextmanager
def running(database, *, session=None):
    server = AgentAPIServer(("127.0.0.1", 0), database=str(database), auth_token=TOKEN, evidence_session=session)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def call(base, path, *, method="GET", body=None, token=TOKEN, headers=None):
    supplied = {"Authorization": "Bearer " + token} if token is not None else {}
    if body is not None:
        supplied["Content-Type"] = "application/json"
    supplied.update(headers or {})
    request = Request(base + path, method=method, data=body, headers=supplied)
    try:
        with urlopen(request, timeout=5) as response:
            raw = response.read()
            return response.status, dict(response.headers), json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read()
        return exc.code, dict(exc.headers), json.loads(raw) if raw else {}


def test_create_read_and_retry_after_restart(tmp_path):
    database = tmp_path / "runs.sqlite3"
    original = EXAMPLE.read_bytes()
    with running(database) as (base, _):
        created, headers, body = call(base, "/api/proposals", method="POST", body=original)
        assert created == 201
        assert body["phase"] == "BASELINE_FROZEN"
        assert body["source_status"] == "EVIDENCE_PENDING"
        assert body["release_authorized"] is False
        assert body["external_action_executed"] is False
        assert "no-store" == headers["Cache-Control"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert not any(k.lower().startswith("access-control-") for k in headers)
        read, _, state = call(base, "/api/proposals/" + body["run_id"])
        assert read == 200 and state["revision"] == 0
        assert state["plan_digest"] == body["plan_digest"]
        assert state["pending_rechecks"] == []
        assert "taste_context" not in json.dumps(state)
        assert "constraints" not in json.dumps(state)
        assert "fictional" not in json.dumps(state)
        assert "AUTH" not in json.dumps(state)
        health, _, ping = call(base, "/healthz")
        assert health == 200 and ping["release_authorized"] is False

    with running(database) as (base, _):
        retry, _, replay = call(base, "/api/proposals", method="POST", body=original)
        assert retry == 200 and replay["created"] is False
        assert replay["plan_digest"] == body["plan_digest"]
        assert call(base, "/api/proposals/" + body["run_id"])[0] == 200
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM plans").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_reusing_id_with_changed_constraints_is_conflict(tmp_path):
    database = tmp_path / "runs.sqlite3"
    with running(database) as (base, _):
        assert call(base, "/api/proposals", method="POST", body=EXAMPLE.read_bytes())[0] == 201
        changed = json.loads(EXAMPLE.read_bytes())
        changed["intent"]["constraints"]["budget_usd"] += 100
        status, _, body = call(
            base, "/api/proposals", method="POST", body=json.dumps(changed).encode(),
        )
        assert status == 409
        assert body == {"error": "PROPOSAL_CONFLICT", "release_authorized": False}


def test_unauthorized_requests_never_mutate(tmp_path):
    db = tmp_path / "runs.sqlite3"
    with running(db) as (base, _):
        for token in (None, "", "bad", TOKEN + "x"):
            status, _, error = call(base, "/api/proposals", method="POST", body=EXAMPLE.read_bytes(), token=token)
            assert status == 401
            assert error["release_authorized"] is False
        assert call(base, "/healthz", token=None)[0] == 401
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM plans").fetchone()[0] == 0


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.invalid"},
    {"Origin": "https://evil.invalid"},
    {"Origin": "http://localhost:9999"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
])
def test_dns_rebinding_and_cross_origin_blocked(tmp_path, headers):
    with running(tmp_path / "runs.sqlite3") as (base, _):
        status, response, error = call(
            base, "/api/proposals", method="POST", body=EXAMPLE.read_bytes(), headers=headers,
        )
        assert status == 403
        assert error["error"] == "ORIGIN_REJECTED"
        assert "Access-Control-Allow-Origin" not in response


def test_invalid_json_and_input_shape_are_rejected(tmp_path):
    with running(tmp_path / "runs.sqlite3") as (base, _):
        for data in (b"not-json", b"[]", b'{"intent":{}}', b"{}"):
            code, _, body = call(base, "/api/proposals", method="POST", body=data)
            assert code == 400
            assert body["release_authorized"] is False
        assert call(base, "/api/proposals", method="POST", body=EXAMPLE.read_bytes(),
                    headers={"Content-Type": "text/plain"})[0] == 415
        assert call(base, "/api/proposals", method="POST", body=b"x" * (96 * 1024 + 1))[0] == 413


def test_rejects_missing_duplicate_and_chunked_framing(tmp_path):
    with running(tmp_path / "runs.sqlite3") as (_, port):
        for extra in ([], [("Content-Length", "8"), ("Content-Length", "8")],
                      [("Transfer-Encoding", "chunked"), ("Content-Length", "8")]):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.putrequest("POST", "/api/proposals")
            connection.putheader("Authorization", "Bearer " + TOKEN)
            connection.putheader("Content-Type", "application/json")
            for key, value in extra:
                connection.putheader(key, value)
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == (411 if not extra or extra[0][0] == "Content-Length" else 400)
            response.read()
            connection.close()


def test_unknown_routes_and_mutations_are_inaccessible(tmp_path):
    with running(tmp_path / "runs.sqlite3") as (base, _):
        for path in ("/api/runs", "/api/release", "/api/execute", "/.env",
                     "/api/proposals/../../etc/passwd", "/api/proposals/missing?revision=0"):
            assert call(base, path)[0] in (400, 404)
        for method in ("PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
            assert call(base, "/api/proposals", method=method)[0] == 405
        assert call(base, "/api/execute", method="POST", body=b"{}")[0] == 405


def test_tampering_fails_closed(tmp_path):
    db = tmp_path / "runs.sqlite3"
    with running(db) as (base, _):
        _, _, initial = call(base, "/api/proposals", method="POST", body=EXAMPLE.read_bytes())
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE events SET event_payload='{}' WHERE plan_id=?", (initial["plan_id"],))
        status, _, body = call(base, "/api/proposals/" + initial["plan_id"])
        assert status == 503
        assert body["error"] == "STATE_UNAVAILABLE"
        assert call(base, "/api/proposals", method="POST", body=EXAMPLE.read_bytes())[0] == 503


def test_parallel_identical_intake_has_one_insert(tmp_path):
    db = tmp_path / "runs.sqlite3"
    with running(db) as (base, _):
        def attempt(_):
            return call(base, "/api/proposals", method="POST", body=EXAMPLE.read_bytes())[0]
        with ThreadPoolExecutor(max_workers=6) as pool:
            codes = list(pool.map(attempt, range(10)))
        assert codes.count(201) == 1
        assert codes.count(200) == 9
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM plans").fetchone()[0] == 1


def test_token_and_bind_preconditions(tmp_path):
    db = tmp_path / "runs.sqlite3"
    for token in ("", "short", "contains spaces " + "x" * 30, "x" * 257, "x\n" * 20):
        with pytest.raises(ValueError):
            AgentAPIServer(("127.0.0.1", 0), database=str(db), auth_token=token)
    with pytest.raises(ValueError):
        AgentAPIServer(("0.0.0.0", 0), database=str(db), auth_token=TOKEN)
    with pytest.raises(ValueError):
        AgentAPIServer(("::1", 0), database=str(db), auth_token=TOKEN)


def test_operator_review_is_bound_to_one_authenticated_local_plan(tmp_path):
    from datetime import datetime, timedelta, timezone

    from ofc_engine.agent_evidence_session import OperatorEvidenceSession
    from ofc_engine.contracts import ActionIntent, Decision
    from ofc_engine.durable import SQLiteRevisionStore
    from ofc_engine.intake import AgentProposal, accept_proposal
    from ofc_engine.qloo import QlooProbeClient, brand_affinity_probe
    from ofc_engine.qloo_recorded_commit import commit_recorded_repair

    brands = ("brand-a", "brand-b", "brand-c")
    spec = brand_affinity_probe("artist-a", brands)
    now = datetime.now(timezone.utc)

    def example_capture(collected):
        return {
            "summary": {
                "endpoint": spec.path,
                "http_status": 200,
                "real_qloo_requests": 1,
                "mode": "LIVE_RESPONSE_UNQUALIFIED",
                "query_fingerprint": spec.fingerprint,
                "utc_collected_at": collected.isoformat(),
            },
            "payload": {
                "success": True,
                "results": {"entities": [
                    {"entity_id": entity_id, "type": "urn:entity",
                     "query": {"affinity": score, "explainability": {
                         "signal.interests.entities": {}}}}
                    for entity_id, score in zip(
                        (brands[1], brands[0], brands[2]), (0.91, 0.72, 0.49)
                    )
                ]},
            },
        }

    original = example_capture(now - timedelta(minutes=15))
    later = example_capture(now - timedelta(minutes=1))
    proposal = AgentProposal(
        intent=ActionIntent(
            run_id="agent-proof-api-001", client_request_id="intake-001",
            objective="Review partner fit",
            constraints={"external_action": False},
            taste_context={"audience": "operator"},
        ),
        decisions=(Decision(
            decision_id="brand", domain="partner", candidate_id=brands[0],
            candidate_pool=brands, mutable_fields=("candidate_id",),
        ),),
    )
    database = tmp_path / "operator.sqlite3"
    store = SQLiteRevisionStore(database)
    accept_proposal(store, proposal)
    commit_recorded_repair(
        store, proposal, original, decision_id="brand",
        artist_id="artist-a", brand_ids=brands,
        request_id="rank-001",
        acknowledgement="COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE",
        as_of=now, max_evidence_age=timedelta(hours=1),
    )
    proposal_file = tmp_path / "proposal.json"
    proposal_file.write_text(proposal.model_dump_json())
    first_file = tmp_path / "private-original.json"
    later_file = tmp_path / "private-later.json"
    first_file.write_text(json.dumps(original))
    later_file.write_text(json.dumps(later))
    index = tmp_path / "original-index.json"
    next_index = tmp_path / "later-index.json"
    for path, capture_file in ((index, first_file), (next_index, later_file)):
        path.write_text(json.dumps({"decisions": {
            "brand": {
                "artist_id": "artist-a", "brand_ids": list(brands),
                "capture_file": str(capture_file),
            }
        }}))

    session = OperatorEvidenceSession.load(
        proposal_path=proposal_file,
        evidence_index=index,
        recheck_index=next_index,
        age_hours=1,
    )
    with running(database, session=session) as (base, _):
        url = "/api/proposals/agent-proof-api-001/review"
        http_status, headers, result = call(base, url)
        assert http_status == 200
        assert headers["Cache-Control"] == "no-store"
        assert result["status"] == "RECORDED_RANK_RECHECK_PASS_NO_RELEASE"
        assert result["recorded_rank_recheck_pass"] is True
        assert result["pending_recheck_ids"] == ["brand"]
        assert result["release_authorized"] is False
        assert result["independent_recheck_completed"] is False
        assert result["provider_authenticated"] is False
        assert result["real_qloo_requests"] == 0
        assert result["decision_reviews"][0]["verdict"] == "PASS"
        raw = json.dumps(result)
        assert str(first_file) not in raw
        assert str(later_file) not in raw
        assert "affinity" not in raw
        assert "test-only-" not in raw
        assert "QLOO_API_KEY" not in raw
        assert call(base, url, token=None)[0] == 401
        assert call(base, url, headers={"Host": "hostile.example"})[0] == 403
        assert call(base, "/api/proposals/other-run/review")[0] == 404
        assert call(base, url, method="POST", body=b"{}")[0] == 405
        stored_status, _, stored = call(base, "/api/proposals/agent-proof-api-001")
        assert stored_status == 200
        assert stored["pending_rechecks"] == ["brand"]
    assert len(store.audit_history("agent-proof-api-001")) == 2

    with running(database) as (base, _):
        assert call(base, url)[0] == 404
    assert store.pending_rechecks("agent-proof-api-001") == ("brand",)


def test_operator_review_session_refuses_unknown_baseline(tmp_path):
    from ofc_engine.agent_evidence_session import OperatorEvidenceSession
    from ofc_engine.contracts import ActionIntent, Decision
    from ofc_engine.intake import AgentProposal
    from ofc_engine.qloo import brand_affinity_probe

    proposal = AgentProposal(
        intent=ActionIntent(
            run_id="not-stored-001", client_request_id="req-001",
            objective="No stored plan", constraints={}, taste_context={},
        ),
        decisions=(Decision(
            decision_id="brand", domain="brand", candidate_id="a",
            candidate_pool=("a", "b"),
        ),),
    )
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(proposal.model_dump_json())
    original_index = tmp_path / "index.json"
    original_index.write_text('{"decisions":{}}')
    session = OperatorEvidenceSession.load(
        proposal_path=proposal_path,
        evidence_index=original_index,
    )
    with pytest.raises(KeyError):
        AgentAPIServer(
            ("127.0.0.1", 0), database=str(tmp_path / "empty.sqlite3"),
            auth_token=TOKEN, evidence_session=session,
        )
