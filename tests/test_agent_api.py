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
def running(database):
    server = AgentAPIServer(("127.0.0.1", 0), database=str(database), auth_token=TOKEN)
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
