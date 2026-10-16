"""Same Qloo observation in OFF and ON; only OFC's decision control differs."""
import pytest

from ofc_engine.contracts import ActionIntent, Decision
from ofc_engine.copilot_compare import build_copilot_pair
from ofc_engine.dynamic import DynamicQlooReview
from ofc_engine.durable import SQLiteRevisionStore, StorageIntegrityError
from ofc_engine.intake import AgentProposal, freeze_proposal
from ofc_engine.qloo import QlooProbeClient, TransportReply
import json


class FakeQloo:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def get(self, url, headers, *, timeout, max_bytes):
        self.calls += 1
        if self.fail:
            return TransportReply(429, b"{}")
        return TransportReply(200, json.dumps({
            "success": True,
            "results": {"entities": [
                {"entity_id": name, "type": "urn:entity",
                 "query": {"affinity": score, "explainability": {
                     "signal.interests.entities": {}
                 }}}
                for name, score in (("brand-b", 0.90), ("brand-a", 0.60))
            ]},
        }).encode())


def proposal():
    return AgentProposal(
        intent=ActionIntent(
            run_id="qloo-pair-customer", client_request_id="customer-001",
            objective="Review culturally aligned brand sponsors",
            constraints={"external_action": False},
            taste_context={"artist_id": "artist-001"},
        ),
        decisions=(
            Decision(
                decision_id="partner", domain="brand",
                candidate_id="brand-a",
                candidate_pool=("brand-a", "brand-b"),
                mutable_fields=("candidate_id",),
            ),
            Decision(
                decision_id="afterparty", domain="event",
                candidate_id="event-a", candidate_pool=("event-a",),
                depends_on=("partner",),
            ),
        ),
    )


def setup(tmp_path, *, fail=False):
    api = FakeQloo(fail=fail)
    store = SQLiteRevisionStore(tmp_path / "pair.sqlite3")
    dynamic = DynamicQlooReview(
        store, QlooProbeClient(api_key="never-real", transport=api), daily_limit=4
    )
    return store, dynamic, api


def test_off_keeps_qloo_on_and_only_ofc_changes(tmp_path):
    store, flow, api = setup(tmp_path)
    item = proposal()
    result = flow.evaluate(item)
    baseline, _ = freeze_proposal(item)
    pair = build_copilot_pair(
        prompt="Find a suitable brand sponsor", model_id="gemini-3.1-flash-lite",
        baseline=baseline, reviewed=store.get(baseline.plan_id), evaluated=result,
    )
    assert api.calls == 2
    assert pair.off["llm_enabled"] is pair.on["llm_enabled"] is True
    assert pair.off["ofc_enabled"] is False
    assert pair.on["ofc_enabled"] is True
    assert pair.off["qloo"] == pair.on["qloo"]
    assert pair.off["qloo"] is pair.on["qloo"]
    assert pair.off["qloo"]["source_mode"] == "LIVE_HTTP_UNVERIFIED"
    assert pair.off["decisions"][0]["candidate_id"] == "brand-a"
    assert pair.off["decisions"][0]["cultural_rank_observed"] is True
    assert pair.on["decisions"][0]["candidate_id"] == "brand-b"
    assert pair.on["decisions"][0]["ofc_verdict"] == "PASS"
    assert pair.on["decisions"][1]["ofc_verdict"] == "UNCERTAIN"
    assert pair.on["decisions"][1]["pending_recheck"] is True
    assert pair.on["release_authorized"] is False
    assert pair.off["verification_status"] == "NOT_EVALUATED_BY_OFC"
    assert pair.qloo_evidence_digest == pair.off["qloo"]["evidence_digest"]
    assert pair.as_dict()["external_action_executed"] is False

    for _ in range(5):
        build_copilot_pair(
            prompt="Find a suitable brand sponsor", model_id="gemini-3.1-flash-lite",
            baseline=baseline, reviewed=store.get(baseline.plan_id),
            evaluated=result,
        )
    assert api.calls == 2  # Read-only OFF/ON views never contact Qloo.


def test_missing_qloo_evidence_never_promises_verified_taste(tmp_path):
    store, flow, api = setup(tmp_path, fail=True)
    item = proposal()
    result = flow.evaluate(item)
    baseline, _ = freeze_proposal(item)
    pair = build_copilot_pair(
        prompt="Brand partners", model_id="groq",
        baseline=baseline, reviewed=store.get(baseline.plan_id), evaluated=result,
    )
    assert api.calls == 1
    assert pair.off["qloo"]["source_mode"] == "EVIDENCE_UNAVAILABLE"
    assert pair.on["qloo"]["source_mode"] == "EVIDENCE_UNAVAILABLE"
    assert pair.on["decisions"][0]["ofc_verdict"] == "UNCERTAIN"
    assert pair.on["changed_ids"] == []
    assert pair.on["release_authorized"] is False


def test_revisions_and_evidence_scopes_cannot_be_swapped(tmp_path):
    store, flow, _ = setup(tmp_path)
    item = proposal()
    result = flow.evaluate(item)
    baseline, _ = freeze_proposal(item)
    current = store.get(baseline.plan_id)
    bad = {**result, "baseline_plan_digest": "0" * 64}
    with pytest.raises(StorageIntegrityError):
        build_copilot_pair(
            prompt="Brand", model_id="gemini", baseline=baseline,
            reviewed=current, evaluated=bad,
        )
    corrupted = {**result, "decisions": [
        {**row, "qloo_rank_evidence": {
            **row["qloo_rank_evidence"], "ranked_candidate_ids": ["wrong-id"]
        }} if "qloo_rank_evidence" in row else row
        for row in result["decisions"]
    ]}
    with pytest.raises(StorageIntegrityError):
        build_copilot_pair(
            prompt="Brand", model_id="gemini", baseline=baseline,
            reviewed=current, evaluated=corrupted,
        )
