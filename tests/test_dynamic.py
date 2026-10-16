"""Dynamic live-Qloo admission, replay, quota and repair contracts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import pytest

from ofc_engine.agent_api import AgentAPIServer
from ofc_engine.contracts import ActionIntent, Decision
from ofc_engine.dynamic import DynamicQlooReview, DynamicScopeConflict
from ofc_engine.durable import SQLiteRevisionStore, StorageIntegrityError
from ofc_engine.intake import AgentProposal
from ofc_engine.qloo import QlooProbeClient, TransportReply

_TOKEN = "local-test-" + "t" * 40


class FakeInsights:
    def __init__(self, *, reverse_recheck=False, invalid=False):
        self.calls = []
        self.reverse_recheck = reverse_recheck
        self.invalid = invalid

    def get(self, url, headers, *, timeout, max_bytes):
        assert url.startswith("https://hackathon.api.qloo.com/v2/insights?")
        assert headers["X-Api-Key"] == "ci-not-a-real-key"
        params = parse_qs(urlsplit(url).query)
        assert params["filter.type"] == ["urn:entity:brand"]
        assert params["feature.explainability"] == ["true"]
        ids = params["filter.results.entities"][0].split(",")
        self.calls.append((params["signal.interests.entities"][0], tuple(ids)))
        if self.invalid:
            data = {"success": True, "results": {"entities": []}}
        else:
            order = (ids if self.reverse_recheck and len(self.calls) % 2 == 0
                     else ids[1:] + ids[:1])
            data = {
                "success": True,
                "results": {"entities": [
                    {"entity_id": entity, "type": "urn:entity",
                     "query": {"affinity": 0.95 - i * 0.2,
                               "explainability": {"signal.interests.entities": {}}}}
                    for i, entity in enumerate(order)
                ]},
            }
        return TransportReply(200, json.dumps(data).encode())


def plan(run_id="customer-1", *, brand_ids=("brand-a", "brand-b"),
         budget=60000, artist_id="artist-1"):
    return AgentProposal(
        intent=ActionIntent(
            run_id=run_id, client_request_id="request-" + run_id,
            objective="Check proposed partner before agent proceeds",
            constraints={"external_action": False, "budget_usd": budget},
            taste_context={"artist_id": artist_id},
        ),
        decisions=(
            Decision(
                decision_id="partner", domain="brand",
                candidate_id=brand_ids[0], candidate_pool=brand_ids,
                mutable_fields=("candidate_id",),
            ),
            Decision(
                decision_id="venue", domain="venue",
                candidate_id="venue-a", candidate_pool=("venue-a", "venue-b"),
            ),
            Decision(
                decision_id="afterparty", domain="event",
                candidate_id="event-a", candidate_pool=("event-a", "event-b"),
                depends_on=("partner",),
            ),
        ),
    )


def reviewer(tmp_path, transport, *, limit=6):
    store = SQLiteRevisionStore(tmp_path / "run.sqlite3")
    flow = DynamicQlooReview(
        store, QlooProbeClient(api_key="ci-not-a-real-key", transport=transport),
        daily_limit=limit,
    )
    return store, flow


def test_arbitrary_customer_scope_real_adapter_and_replay(tmp_path):
    fake = FakeInsights()
    store, flow = reviewer(tmp_path, fake)
    original = plan()
    first = flow.evaluate(original)
    assert first["run_id"] == "customer-1"
    assert first["source_mode"] == "LIVE_HTTP_UNVERIFIED"
    assert first["revision"] == 1
    assert first["new_qloo_requests"] == 2
    assert first["changed_ids"] == ["partner"]
    assert set(first["pending_rechecks"]) == {"partner", "afterparty"}
    assert first["decisions"][0]["verdict"] == "PASS"
    assert first["decisions"][0]["rank_recheck_completed"] is True
    assert first["decisions"][1]["verdict"] == "UNCERTAIN"
    assert first["decisions"][2]["verdict"] == "UNCERTAIN"
    assert first["provider_authenticated"] is False
    assert first["full_plan_independently_verified"] is False
    assert first["release_authorized"] is False
    assert first["external_action_executed"] is False
    assert len(store.audit_history("customer-1")) == 2

    repeat = flow.evaluate(original)
    assert repeat["new_qloo_requests"] == 0
    assert repeat["plan_digest"] == first["plan_digest"]
    assert repeat["journal_head"] == first["journal_head"]
    assert repeat["decisions"] == first["decisions"]
    assert len(fake.calls) == 2

    another = plan("customer-2", brand_ids=("other-a", "other-b"), artist_id="artist-2")
    second = flow.evaluate(another)
    assert second["new_qloo_requests"] == 2
    assert second["revision"] == 1
    assert fake.calls[-1] == ("artist-2", ("other-a", "other-b"))
    assert store.get("customer-1").decisions[0].candidate_id == "brand-b"
    assert store.get("customer-2").decisions[0].candidate_id == "other-b"


def test_daily_budget_consumed_before_network_and_no_unbounded_retry(tmp_path):
    fake = FakeInsights()
    store, flow = reviewer(tmp_path, fake, limit=3)
    flow.evaluate(plan())
    second = flow.evaluate(plan("second", brand_ids=("c", "d")))
    assert second["new_qloo_requests"] == 1
    assert second["decisions"][0]["verdict"] == "UNCERTAIN"
    assert second["decisions"][0]["reason_code"] == "DAILY_QLOO_BUDGET_EXHAUSTED"
    assert second["release_authorized"] is False
    assert flow.evaluate(plan("third"))["new_qloo_requests"] == 0
    assert len(fake.calls) == 3
    with sqlite3.connect(tmp_path / "run.sqlite3") as db:
        assert db.execute("SELECT sum(reserved) FROM dynamic_qloo_daily").fetchone()[0] == 3


def test_fresh_rank_disagreement_blocks_pass(tmp_path):
    fake = FakeInsights(reverse_recheck=True)
    _, flow = reviewer(tmp_path, fake)
    result = flow.evaluate(plan())
    assert result["changed_ids"] == ["partner"]
    assert result["decisions"][0]["reason_code"] == "FRESH_RANK_RECHECK_DISAGREES"
    assert result["decisions"][0]["verdict"] == "UNCERTAIN"
    assert result["rank_evidence_rechecked"] == 0
    assert result["release_authorized"] is False


def test_invalid_provider_payload_spends_once_and_stays_uncertain(tmp_path):
    fake = FakeInsights(invalid=True)
    _, flow = reviewer(tmp_path, fake)
    one = flow.evaluate(plan())
    assert one["new_qloo_requests"] == 1
    assert one["decisions"][0]["verdict"] == "UNCERTAIN"
    assert one["decisions"][0]["reason_code"] == "QLOO_EVIDENCE_UNAVAILABLE"
    replay = flow.evaluate(plan())
    assert replay["new_qloo_requests"] == 0
    assert replay["decisions"][0]["reason_code"] == "ATTEMPT_ALREADY_RESERVED"
    assert len(fake.calls) == 1


def test_conflicting_baseline_and_tampered_capture_blocked(tmp_path):
    fake = FakeInsights()
    _, flow = reviewer(tmp_path, fake)
    flow.evaluate(plan())
    with pytest.raises(DynamicScopeConflict):
        flow.evaluate(plan(budget=70000))
    assert len(fake.calls) == 2
    with sqlite3.connect(tmp_path / "run.sqlite3") as db:
        db.execute(
            "UPDATE dynamic_qloo_attempts SET result='{}' "
            "WHERE plan_id='customer-1' AND decision_id='partner' AND pass_no=1"
        )
    with pytest.raises(StorageIntegrityError):
        flow.evaluate(plan())
    assert len(fake.calls) == 2


def test_unsupported_scopes_do_not_trigger_provider_calls(tmp_path):
    fake = FakeInsights()
    _, flow = reviewer(tmp_path, fake)
    no_subject = plan().model_copy(update={
        "intent": plan().intent.model_copy(update={"taste_context": {}})
    })
    result = flow.evaluate(no_subject)
    assert result["new_qloo_requests"] == 0
    assert result["decisions"][0]["reason_code"] == "QLOO_ARTIST_ID_REQUIRED"
    assert result["release_authorized"] is False
    assert not fake.calls


def test_parallel_review_never_spends_double(tmp_path):
    from threading import Event
    gate = Event()
    class HeldTransport(FakeInsights):
        def get(self, *args, **kwargs):
            gate.wait(timeout=2)
            return super().get(*args, **kwargs)

    fake = HeldTransport()
    _, flow = reviewer(tmp_path, fake)
    proposal = plan()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(flow.evaluate, proposal)
        b = pool.submit(flow.evaluate, proposal)
        gate.set()
        outcomes = (a.result(), b.result())
    assert len(fake.calls) == 2
    assert sum(outcome["new_qloo_requests"] for outcome in outcomes) == 2
    assert all(outcome["release_authorized"] is False for outcome in outcomes)


@contextmanager
def serve(tmp_path, *, fake=None, limit=0):
    server = AgentAPIServer(
        ("127.0.0.1", 0), database=str(tmp_path / "api.sqlite3"),
        auth_token=_TOKEN,
        dynamic_api_key="ci-not-a-real-key" if limit else None,
        dynamic_daily_limit=limit,
        dynamic_transport=fake,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post(base, payload, *, token=_TOKEN):
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token}
    request = Request(
        base + "/api/proposals/evaluate", method="POST",
        headers=headers, data=payload.model_dump_json().encode(),
    )
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def test_authenticated_dynamic_api_disabled_by_default(tmp_path):
    with serve(tmp_path) as base:
        status, body = post(base, plan())
        assert status == 405
        assert body["error"] == "DYNAMIC_QLOO_DISABLED"


def test_dynamic_api_uses_real_submission_without_exposing_key(tmp_path):
    fake = FakeInsights()
    with serve(tmp_path, fake=fake, limit=4) as base:
        assert post(base, plan(), token="bad")[0] == 401
        code, report = post(base, plan())
        assert code == 200
        assert report["new_qloo_requests"] == 2
        assert report["decisions"][0]["verdict"] == "PASS"
        assert report["verification_status"] == "REVIEW_REQUIRED"
        assert "ci-not-a-real-key" not in json.dumps(report)
        assert "api_key" not in json.dumps(report)
        code, repeat = post(base, plan())
        assert code == 200 and repeat["new_qloo_requests"] == 0
        assert len(fake.calls) == 2
