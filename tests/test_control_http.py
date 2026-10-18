from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from threading import Thread
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ofc_engine.control.fixtures import campaign
from ofc_engine.control.http import ControlAccess, Identity
from ofc_engine.control.runtime import ControlRuntime
from ofc_engine.control.sdk import ControlClient
from ofc_engine.control.storage import ControlStore
from ofc_engine.hosted_preview import HostedFixtureServer

TOKEN = "control-test-token-" + "a" * 40
OTHER = "control-test-token-" + "b" * 40
HUMAN = "control-test-token-" + "c" * 40


@contextmanager
def serve(tmp_path, *, enabled=True, private=True):
    runtime = ControlRuntime(ControlStore(tmp_path / "reviews.db")) if private else None
    access = ControlAccess({TOKEN: Identity("sandbox", "campaign-agent"), OTHER: Identity("other", "other"),
                            HUMAN: Identity("sandbox", "campaign-agent", frozenset({"read", "submit", "approve"}), "human")})
    server = HostedFixtureServer(("127.0.0.1", 0), control=runtime, control_enabled=enabled, control_access=access)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def request(origin, path, payload=None, *, token=None, headers=None):
    headers = dict(headers or {})
    if token: headers["Authorization"] = "Bearer " + token
    if payload is not None: headers["Content-Type"] = "application/json"
    req = Request(origin + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
    try:
        with urlopen(req, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)
    except HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def finished(client, review_id):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        value = client.status(review_id)
        if value["state"] == "FINISHED": return value
        time.sleep(.02)
    raise AssertionError("review did not finish")


def test_feature_default_and_old_api_are_preserved(tmp_path):
    with serve(tmp_path, enabled=False, private=False) as origin:
        assert request(origin, "/plans")[0] == 404
        assert request(origin, "/api/control/capabilities")[0] == 404
        assert request(origin, "/api/demo?scenario=repair")[0] == 200


def test_public_sandbox_is_keyless_and_cannot_accept_private_inputs(tmp_path):
    with serve(tmp_path, private=False) as origin:
        status, raw, _ = request(origin, "/api/control/simulate", campaign().data())
        assert status == 200
        proof = json.loads(raw)
        assert proof["impact"]["changed"] == ["B01"] and proof["provider_calls"] == 0
        payload = campaign().data(); payload["mode"] = "PRIVATE"
        assert request(origin, "/api/control/simulate", payload)[0] == 403
        assert request(origin, "/api/control/examples/repair/puma")[0] == 200
        assert request(origin, "/plans")[0] == 200
        assert b"script-src 'self'" in request(origin, "/plans")[2]["Content-Security-Policy"].encode()


def test_authenticated_async_api_sse_proof_and_tenant_scope(tmp_path):
    with serve(tmp_path) as origin:
        client = ControlClient(origin, TOKEN)
        receipt = client.submit(campaign())
        assert client.submit(campaign()) == receipt
        status = finished(client, receipt["review_id"])
        proof = client.proof(receipt["review_id"])
        assert proof["proof_digest"] == status["proof_digest"]
        path = receipt["events_url"]
        code, raw, headers = request(origin, path, token=TOKEN, headers={"Accept": "text/event-stream"})
        assert code == 200 and "text/event-stream" in headers["Content-Type"]
        assert b"event: review" in raw and b"REVIEW_COMMITTED" in raw
        assert request(origin, receipt["proof_url"], token=OTHER)[0] == 404
        assert request(origin, receipt["proof_url"])[0] == 403
        assert request(origin, receipt["proof_url"] + "?secret=1", token=TOKEN)[0] == 422


def test_actor_spoof_and_cross_origin_are_rejected_before_submission(tmp_path):
    with serve(tmp_path) as origin:
        assert request(origin, "/api/control/plans", campaign().data(), token=OTHER)[0] == 403
        assert request(origin, "/api/control/plans", campaign().data(), token=TOKEN, headers={"Origin": "https://evil.invalid"})[0] == 403
        code, raw, _ = request(origin, "/api/control/plans", {"not": "a plan"}, token=TOKEN)
        assert code == 422 and json.loads(raw)["validation_errors"]


def test_approval_intent_is_scoped_revocable_and_never_authorizes_actions(tmp_path):
    with serve(tmp_path) as origin:
        client = ControlClient(origin, TOKEN)
        receipt = client.submit(campaign())
        finished(client, receipt["review_id"])
        path = receipt["status_url"] + "/approvals"
        payload = {"request_key": "request-1", "node_ids": ["B01"], "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")}
        assert request(origin, path, payload, token=TOKEN)[0] == 403
        code, raw, _ = request(origin, path, payload, token=HUMAN)
        assert code == 201
        intent = json.loads(raw)
        assert intent["binding"]["state"] == "APPROVAL_PENDING"
        assert intent["binding"]["release_authorized"] is False
        assert request(origin, path, payload | {"signature": "approve"}, token=HUMAN)[0] == 422
        assert request(origin, path, payload, token=HUMAN)[1] == raw
        assert request(origin, path, {"action": "REVOKE", "intent_id": intent["intent_id"]}, token=HUMAN)[0] == 200
        assert request(origin, path, payload, token=HUMAN)[0] == 409
        assert client.proof(receipt["review_id"])["action_status"] == "WITHHELD"


def test_patch_preview_requires_exact_revision_and_does_not_commit(tmp_path):
    with serve(tmp_path) as origin:
        client = ControlClient(origin, TOKEN)
        receipt = client.submit(campaign())
        finished(client, receipt["review_id"])
        proof = client.proof(receipt["review_id"])
        payload = {"revision": proof["revision"], "plan_digest": proof["current_digest"],
                   "policy_digest": proof["policy_digest"], "evidence_root": proof["evidence_root"],
                   "patches": [{"node_id": "B01", "changes": {"candidate_id": "puma"}, "evidence_ids": ["brand-observation-1"]}]}
        path = receipt["status_url"] + "/proposals"
        code, raw, _ = request(origin, path, payload, token=TOKEN)
        assert code == 200 and json.loads(raw)["revision_committed"] is False
        assert client.proof(receipt["review_id"]) == proof
        assert request(origin, path, payload | {"revision": 999}, token=TOKEN)[0] == 409


def test_agent_cannot_self_grant_approval_role():
    with pytest.raises(ValueError): Identity("tenant", "agent", frozenset({"approve"}), "agent")
