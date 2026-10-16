"""One prompt → one saved Qloo-backed OFF/ON Copilot transaction, no duplicate calls."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from ofc_engine.copilot_llm import CopilotLLM, JSONReply, LLMUnavailable
from ofc_engine.copilot_service import (
    CopilotRunConflict, CopilotRunReserved, LocalCopilotService,
)
from ofc_engine.dynamic import DynamicQlooReview
from ofc_engine.copilot_search import BudgetedQlooResolver
from ofc_engine.durable import SQLiteRevisionStore
from ofc_engine.qloo import QlooProbeClient, TransportReply

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


class FakeAffinity:
    def __init__(self):
        self.calls = 0
        self.search_calls = []

    def get(self, url, headers, *, timeout, max_bytes):
        route = urlsplit(url)
        if route.path == "/search":
            query = parse_qs(route.query)
            name = query["query"][0]
            kind = query["types"][0]
            self.search_calls.append((name, kind))
            return TransportReply(200, json.dumps({
                "success": True,
                "results": {"entities": [{
                    "name": name, "entity_id": IDS[name],
                    "type": "urn:entity", "subtype": kind,
                }]},
            }).encode())
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


def setup(tmp_path, *, model=None, qloo=None, qloo_limit=16, llm_limit=8):
    model = model or FakeModel()
    qloo = qloo or FakeAffinity()
    store = SQLiteRevisionStore(tmp_path / "copilot.sqlite3")
    flow = DynamicQlooReview(
        store, QlooProbeClient(api_key="test-qloo", transport=qloo),
        daily_limit=qloo_limit,
    )
    service = LocalCopilotService(
        llm=CopilotLLM(provider="gemini", api_key="test-model", transport=model),
        engine=flow, resolver=BudgetedQlooResolver(flow),
        llm_daily_limit=llm_limit,
    )
    return service, model, qloo


def test_real_contract_with_injected_apis_and_durable_replay(tmp_path):
    service, model, qloo = setup(tmp_path)
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
    assert len(qloo.search_calls) == 3
    assert qloo.calls == 2
    assert model.calls == 2

    # Same job ID uses the saved immutable output even after service restart.
    again, new_model, new_qloo = setup(tmp_path)
    cached = again.submit(
        run_id="visitor-1", request_id="req-1",
        prompt="Find a culturally aligned sponsor",
    )
    assert cached == answer
    assert new_model.calls == new_qloo.calls == len(new_qloo.search_calls) == 0
    with pytest.raises(CopilotRunConflict):
        again.submit(
            run_id="visitor-1", request_id="req-1",
            prompt="A different task with same run id",
        )


def test_new_visitor_gets_own_frozen_scope(tmp_path):
    service, model, qloo = setup(tmp_path)
    one = service.submit(
        run_id="visitor-1", request_id="request-1", prompt="Find a sponsor"
    )
    other = service.submit(
        run_id="visitor-2", request_id="request-2", prompt="Find a sponsor"
    )
    assert one["run_id"] != other["run_id"]
    assert one["baseline_digest"] != other["baseline_digest"]
    assert model.calls == 4 and qloo.calls == 4 and len(qloo.search_calls) == 6


def test_failed_inference_cannot_respend_by_retry(tmp_path):
    service, model, qloo = setup(tmp_path, model=FakeModel(fail=True))
    with pytest.raises(LLMUnavailable):
        service.submit(
            run_id="unstable", request_id="id", prompt="Make me a proposal"
        )
    with pytest.raises(CopilotRunReserved):
        service.submit(
            run_id="unstable", request_id="id", prompt="Make me a proposal"
        )
    assert model.calls == 1
    assert qloo.calls == len(qloo.search_calls) == 0


def test_search_shares_daily_qloo_budget_with_rank_review(tmp_path):
    service, model, qloo = setup(tmp_path, qloo_limit=4)
    result = service.submit(
        run_id="budget-run", request_id="budget-request",
        prompt="Find a sponsor that fits this artist",
    )
    assert len(qloo.search_calls) == 3
    assert qloo.calls == 1
    assert result["qloo_requests_this_run"] == 1
    assert result["on"]["decisions"][0]["ofc_verdict"] == "UNCERTAIN"
    assert result["on"]["decisions"][0]["ofc_reason"] == "DAILY_QLOO_BUDGET_EXHAUSTED"
    assert result["on"]["release_authorized"] is False

    from sqlite3 import connect
    with connect(tmp_path / "copilot.sqlite3") as db:
        used = db.execute("SELECT sum(reserved) FROM dynamic_qloo_daily").fetchone()[0]
        assert used == 4  # Three entity resolutions plus one rank request.
    assert model.calls == 2


def test_search_budget_exhaustion_fails_before_unadmitted_request(tmp_path):
    service, model, qloo = setup(tmp_path, qloo_limit=2)
    from ofc_engine.copilot_intake import EntityResolutionRequired
    with pytest.raises(EntityResolutionRequired, match="DAILY_QLOO_BUDGET_EXHAUSTED"):
        service.submit(
            run_id="limited", request_id="limit-req", prompt="Find a sponsor",
        )
    assert len(qloo.search_calls) == 2
    assert qloo.calls == 0
    with pytest.raises(CopilotRunReserved):
        service.submit(
            run_id="limited", request_id="limit-req", prompt="Find a sponsor",
        )
    assert model.calls == 1
    assert len(qloo.search_calls) == 2


def test_llm_global_budget_counts_both_inference_requests(tmp_path):
    service, model, qloo = setup(tmp_path, llm_limit=1)
    with pytest.raises(CopilotRunReserved, match="DAILY_LLM_BUDGET_EXHAUSTED"):
        service.submit(
            run_id="llm-capped", request_id="cap-1", prompt="Find a sponsor",
        )
    assert model.calls == 1
    assert len(qloo.search_calls) == 3
    assert qloo.calls == 2
    with pytest.raises(CopilotRunReserved):
        service.submit(
            run_id="llm-capped", request_id="cap-1", prompt="Find a sponsor",
        )
    assert model.calls == 1


def test_corrupted_saved_projection_blocks_replay(tmp_path):
    import sqlite3
    from ofc_engine.durable import StorageIntegrityError
    service, model, qloo = setup(tmp_path)
    service.submit(
        run_id="tampered", request_id="tamper-1", prompt="Find a sponsor",
    )
    with sqlite3.connect(tmp_path / "copilot.sqlite3") as db:
        db.execute(
            "UPDATE copilot_reviews SET projection='{}' WHERE run_id='tampered'"
        )
    with pytest.raises(StorageIntegrityError):
        service.submit(
            run_id="tampered", request_id="tamper-1", prompt="Find a sponsor",
        )
    assert model.calls == 2
    assert qloo.calls == 2
    assert len(qloo.search_calls) == 3


def test_parallel_same_run_cannot_double_spend(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    started, allow = Event(), Event()

    class HeldModel(FakeModel):
        def post(self, *args, **kwargs):
            started.set()
            assert allow.wait(timeout=8)
            return super().post(*args, **kwargs)

    service, model, qloo = setup(tmp_path, model=HeldModel())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            service.submit, run_id="parallel", request_id="req-p",
            prompt="Find a sponsor",
        )
        assert started.wait(timeout=8)
        second = pool.submit(
            service.submit, run_id="parallel", request_id="req-p",
            prompt="Find a sponsor",
        )
        with pytest.raises(CopilotRunReserved):
            second.result(timeout=8)
        allow.set()
        result = first.result(timeout=12)
    assert result["status"] == "REVIEW_REQUIRED"
    assert model.calls == 2 and qloo.calls == 2
    assert len(qloo.search_calls) == 3
