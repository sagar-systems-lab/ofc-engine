"""One prompt → one saved Qloo-backed OFF/ON Copilot transaction, no duplicate calls."""
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from ofc_engine.copilot_llm import CopilotLLM, JSONReply, LLMUnavailable
from ofc_engine.copilot_service import (
    CopilotRunConflict, CopilotRunReserved, LocalCopilotService,
)
from ofc_engine.dynamic import DynamicQlooReview
from ofc_engine.durable import SQLiteRevisionStore
from ofc_engine.qloo import QlooProbeClient, ProbeObservation, TransportReply, search_probe

ART = "0a0a0a0a-0000-4000-8000-000000000001"
A = "0a0a0a0a-0000-4000-8000-000000000002"
B = "0a0a0a0a-0000-4000-8000-000000000003"
IDS = {"Artist Example": ART, "Brand A": A, "Brand B": B}


class FakeModel:
    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    def post(self, url, headers, data, *, timeout, max_bytes):
        self.calls += 1
        if self.fail:
            return JSONReply(429, b"{}")
        body = json.loads(data)
        schema = (
            body["generationConfig"]["responseJsonSchema"]
            if "generationConfig" in body else
            body["response_format"]["json_schema"]["schema"]
        )
        if "off_answer" in schema["properties"]:
            answer = {
                "off_answer": "Qloo ranks Brand B first, and my original plan proposed Brand A.",
                "on_answer": "With the same Qloo ranking, OFC repairs to Brand B and withholds handoff.",
            }
        else:
            answer = {
                "objective": "Plan a culturally aligned sponsor for a music act",
                "artist_name": "Artist Example",
                "budget_usd": None,
                "cities": [],
                "choices": [{
                    "decision_id": "brand", "domain": "brand",
                    "selected_name": "Brand A",
                    "candidate_names": ["Brand A", "Brand B"],
                    "mutable": True,
                    "depends_on": [],
                }],
                "baseline_answer": "Propose Brand A pending research.",
                "clarification_question": None,
            }
        envelope = {"candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": json.dumps(answer)}]},
        }]}
        return JSONReply(200, json.dumps(envelope).encode())


class FakeSearch:
    def __init__(self):
        self.calls = []

    def __call__(self, name, kind):
        self.calls.append((name, kind))
        spec = search_probe(name, entity_type="urn:entity:" + kind)
        return ProbeObservation(
            path="/search", query_fingerprint=spec.fingerprint,
            utc_collected_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=1, http_status=200,
            top_level_fields=("results", "success"),
            payload={"success": True, "results": {"entities": [{
                "name": name, "entity_id": IDS[name],
                "type": "urn:entity", "subtype": "urn:entity:" + kind,
            }]}},
        )


class FakeAffinity:
    def __init__(self):
        self.calls = 0

    def get(self, url, headers, *, timeout, max_bytes):
        self.calls += 1
        return TransportReply(200, json.dumps({
            "success": True, "results": {"entities": [
                {"entity_id": bid, "type": "urn:entity",
                 "query": {"affinity": score, "explainability": {
                     "signal.interests.entities": {}
                 }}}
                for bid, score in ((B, .9), (A, .6))
            ]},
        }).encode())


def setup(tmp_path, *, model=None, qloo=None, search=None):
    model = model or FakeModel()
    qloo = qloo or FakeAffinity()
    search = search or FakeSearch()
    store = SQLiteRevisionStore(tmp_path / "copilot.sqlite3")
    flow = DynamicQlooReview(
        store, QlooProbeClient(api_key="test-qloo", transport=qloo), daily_limit=8,
    )
    service = LocalCopilotService(
        llm=CopilotLLM(provider="gemini", api_key="test-model", transport=model),
        engine=flow, search=search,
    )
    return service, model, qloo, search


def test_real_contract_with_injected_apis_and_durable_replay(tmp_path):
    service, model, qloo, search = setup(tmp_path)
    answer = service.submit(
        run_id="visitor-1", request_id="req-1",
        prompt="Find a culturally aligned sponsor",
    )
    assert answer["status"] == "REVIEW_REQUIRED"
    assert answer["off"]["llm_enabled"] is True
    assert answer["on"]["llm_enabled"] is True
    assert answer["off"]["qloo"] == answer["on"]["qloo"]
    assert answer["off"]["ofc_enabled"] is False
    assert answer["on"]["ofc_enabled"] is True
    assert answer["off"]["decisions"][0]["candidate_id"] == A
    assert answer["on"]["decisions"][0]["candidate_id"] == B
    assert answer["on"]["changed_ids"] == ["brand"]
    assert answer["release_authorized"] is False
    assert answer["external_action_executed"] is False
    assert answer["qloo_requests_this_run"] == 2
    assert len(search.calls) == 3
    assert qloo.calls == 2
    assert model.calls == 2

    # Same job ID uses the saved immutable output even after service restart.
    again, new_model, new_qloo, new_search = setup(tmp_path)
    cached = again.submit(
        run_id="visitor-1", request_id="req-1",
        prompt="Find a culturally aligned sponsor",
    )
    assert cached == answer
    assert new_model.calls == new_qloo.calls == len(new_search.calls) == 0
    with pytest.raises(CopilotRunConflict):
        again.submit(
            run_id="visitor-1", request_id="req-1",
            prompt="A different task with same run id",
        )


def test_new_visitor_gets_own_frozen_scope(tmp_path):
    service, model, qloo, search = setup(tmp_path)
    one = service.submit(
        run_id="visitor-1", request_id="request-1", prompt="Find a sponsor"
    )
    other = service.submit(
        run_id="visitor-2", request_id="request-2", prompt="Find a sponsor"
    )
    assert one["run_id"] != other["run_id"]
    assert one["baseline_digest"] != other["baseline_digest"]
    assert model.calls == 4 and qloo.calls == 4 and len(search.calls) == 6


def test_failed_inference_cannot_respend_by_retry(tmp_path):
    service, model, qloo, search = setup(tmp_path, model=FakeModel(fail=True))
    with pytest.raises(LLMUnavailable):
        service.submit(
            run_id="unstable", request_id="id", prompt="Make me a proposal"
        )
    with pytest.raises(CopilotRunReserved):
        service.submit(
            run_id="unstable", request_id="id", prompt="Make me a proposal"
        )
    assert model.calls == 1
    assert qloo.calls == len(search.calls) == 0
