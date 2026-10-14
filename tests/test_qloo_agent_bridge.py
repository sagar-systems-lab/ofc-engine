import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ofc_engine.contracts import ActionIntent, Decision
from ofc_engine.intake import AgentProposal, freeze_proposal
from ofc_engine.qloo import brand_affinity_probe
from ofc_engine.qloo_agent_bridge import AgentEvidenceRejected, compare_agent_proposal, main
from ofc_engine.revisions import plan_digest

AS_OF = datetime(2026, 10, 14, 11, 10, tzinfo=timezone.utc)
ARTIST = "artist-a"
POOL = ("brand-a", "brand-b", "brand-c")
SPEC = brand_affinity_probe(ARTIST, POOL)


def proposal(*, selected="brand-a", locality=None, dependent=False, pool=POOL):
    decisions = [Decision(
        decision_id="partner",
        domain="partner", candidate_id=selected,
        candidate_pool=pool, mutable_fields=("candidate_id",),
        locality_ref=locality,
    )]
    if dependent:
        decisions.append(Decision(
            decision_id="experience", domain="experience",
            candidate_id="experience-a", candidate_pool=("experience-a",),
            depends_on=("partner",), locality_ref="Chicago",
        ))
    return AgentProposal(
        intent=ActionIntent(
            run_id="agent-real-bridge-001", client_request_id="request-01",
            objective="Review partner affinity before human approval",
            constraints={"release": False, "budget_usd": 100},
            taste_context={"audience": "operator-supplied example"},
        ),
        decisions=tuple(decisions),
    )


def capture(*, timestamp=None):
    return {
        "summary": {
            "endpoint": "/v2/insights", "http_status": 200,
            "real_qloo_requests": 1, "mode": "LIVE_RESPONSE_UNQUALIFIED",
            "utc_collected_at": (timestamp or AS_OF - timedelta(minutes=5)).isoformat(),
            "query_fingerprint": SPEC.fingerprint,
        },
        "payload": {
            "success": True, "results": {"entities": [
                {"entity_id": "brand-b", "type": "urn:entity",
                 "query": {"affinity": 0.9, "explainability": {"signal.interests.entities": {}}}},
                {"entity_id": "brand-a", "type": "urn:entity",
                 "query": {"affinity": 0.6, "explainability": {"signal.interests.entities": {}}}},
                {"entity_id": "brand-c", "type": "urn:entity",
                 "query": {"affinity": 0.2, "explainability": {"signal.interests.entities": {}}}},
            ]},
        },
    }


def compare(p=None, c=None):
    return compare_agent_proposal(
        p or proposal(), c or capture(), decision_id="partner",
        artist_id=ARTIST, brand_ids=POOL, as_of=AS_OF,
        max_evidence_age=timedelta(hours=1),
    )


def test_agent_proposal_digest_is_real_freeze_source():
    p = proposal()
    frozen, candidate_digest = freeze_proposal(p)
    result = compare(p)
    assert result["status"] == "RECORDED_AGENT_PLAN_REVIEW"
    assert result["plan_id"] == p.intent.run_id
    assert result["baseline_plan_digest"] == plan_digest(frozen)
    assert result["baseline_digest"] == frozen.baseline_digest
    assert result["candidate_pool_digest"] == candidate_digest
    assert result["constraints_digest"] == frozen.constraints_digest
    assert result["query_fingerprint"] == SPEC.fingerprint
    assert result["mapping_basis"] == "OPERATOR_SUPPLIED_ARTIST_ID_NOT_IDENTITY_ATTESTED"
    off = result["comparison"]["evidence_off"]
    on = result["comparison"]["evidence_on"]
    assert off["status"] == "REVIEW_REQUIRED"
    assert off["current_revision"] == 0
    assert on["status"] == "RECORDED_RANK_REVIEW_ONLY"
    assert on["current_revision"] == 1
    assert on["selected_candidates"]["partner"] == "brand-b"
    assert on["repair_attempts"] == 1
    assert on["pending_recheck_ids"] == []
    assert result["comparison"]["same_baseline"] is True
    assert result["comparison"]["real_causal_proof"] is False
    assert not result["overall_plan_verified"]
    assert not result["provider_authenticated"]
    assert not result["release_authorized"]
    assert not result["external_action_executed"]
    assert result["real_qloo_requests"] == 0
    assert '"affinity":' not in json.dumps(result)


def test_top_rank_keeps_original_agent_plan():
    result = compare(proposal(selected="brand-b"))
    on = result["comparison"]["evidence_on"]
    assert on["current_revision"] == 0
    assert on["repair_attempts"] == 0
    assert on["reviews"][0]["verdicts"][0]["verdict"] == "PASS"


def test_dependent_is_not_falsely_verified_or_repaired():
    result = compare(proposal(dependent=True))
    on = result["comparison"]["evidence_on"]
    assert on["status"] == "REVIEW_REQUIRED"
    assert on["current_revision"] == 0
    assert on["selected_candidates"] == {"partner": "brand-a", "experience": "experience-a"}
    assert on["reviews"][0]["verdicts"][0]["verdict"] == "REPAIR"
    assert on["reviews"][0]["verdicts"][1]["verdict"] == "UNCERTAIN"
    assert not on["traces"]


def test_unsupported_locality_fails_closed():
    result = compare(proposal(locality="Chicago"))
    on = result["comparison"]["evidence_on"]
    assert on["status"] == "REVIEW_REQUIRED"
    assert on["reviews"][0]["verdicts"][0]["reason_codes"] == ["UNQUALIFIED_BRAND_LOCALITY"]
    assert on["current_revision"] == 0


def test_wrong_brand_pool_rejected_before_evaluation():
    with pytest.raises(AgentEvidenceRejected, match="CANDIDATE_POOL_MISMATCH"):
        compare(proposal(pool=("brand-a", "brand-b")))


def test_stale_capture_is_uncertain_not_refreshed():
    stale = capture(timestamp=AS_OF - timedelta(hours=3))
    on = compare(c=stale)["comparison"]["evidence_on"]
    assert on["status"] == "REVIEW_REQUIRED"
    assert on["current_revision"] == 0
    assert on["reviews"][0]["verdicts"][0]["reason_codes"] == ["RECORDED_EVIDENCE_STALE"]


def test_wrong_fingerprint_is_uncertain_not_recycled():
    bad = capture()
    bad["summary"]["query_fingerprint"] = "f" * 64
    on = compare(c=bad)["comparison"]["evidence_on"]
    assert on["status"] == "REVIEW_REQUIRED"
    assert on["current_revision"] == 0
    assert on["reviews"][0]["verdicts"][0]["reason_codes"] == ["RECORDED_AFFINITY_REJECTED"]


def test_unknown_decision_rejected():
    with pytest.raises(AgentEvidenceRejected, match="DECISION_NOT_FOUND"):
        compare_agent_proposal(
            proposal(), capture(), decision_id="missing",
            artist_id=ARTIST, brand_ids=POOL, as_of=AS_OF,
            max_evidence_age=timedelta(hours=1),
        )


def test_invalid_naive_clock_rejected():
    with pytest.raises(AgentEvidenceRejected, match="INVALID_EVIDENCE_CLOCK"):
        compare_agent_proposal(
            proposal(), capture(), decision_id="partner",
            artist_id=ARTIST, brand_ids=POOL,
            as_of=AS_OF.replace(tzinfo=None), max_evidence_age=timedelta(hours=1),
        )


def test_cli_bridge_does_not_need_qloo_key_or_network(tmp_path, capsys, monkeypatch):
    from ofc_engine import qloo
    monkeypatch.setattr(qloo.QlooProbeClient, "from_environment",
                        lambda: (_ for _ in ()).throw(AssertionError("no network")))
    p = tmp_path / "proposal.json"
    c = tmp_path / "capture.json"
    p.write_text(proposal().model_dump_json())
    c.write_text(json.dumps(capture(timestamp=datetime.now(timezone.utc) - timedelta(minutes=1))))
    assert main([
        "--proposal", str(p), "--capture", str(c),
        "--decision-id", "partner", "--artist-id", ARTIST,
        "--brands", ",".join(POOL),
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "RECORDED_AGENT_PLAN_REVIEW"
    assert result["real_qloo_requests"] == 0
    assert result["comparison"]["evidence_on"]["current_revision"] == 1
    assert result["provider_authenticated"] is False


def test_symlinked_proposal_or_capture_rejected(tmp_path, capsys):
    real_p = tmp_path / "proposal.json"
    real_c = tmp_path / "capture.json"
    real_p.write_text(proposal().model_dump_json())
    real_c.write_text(json.dumps(capture()))
    for source, other in ((real_p, real_c), (real_c, real_p)):
        link = tmp_path / f"link-{source.name}"
        link.symlink_to(source)
        args = [
            "--proposal", str(link if source == real_p else other),
            "--capture", str(link if source == real_c else other),
            "--decision-id", "partner", "--artist-id", ARTIST,
            "--brands", ",".join(POOL),
        ]
        assert main(args) == 2
        assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"


def test_malformed_or_untrusted_proposal_does_not_echo_data(tmp_path, capsys):
    p = tmp_path / "proposal.json"
    c = tmp_path / "capture.json"
    p.write_text('{"private":"sensitive-objective"}')
    c.write_text(json.dumps(capture()))
    assert main([
        "--proposal", str(p), "--capture", str(c),
        "--decision-id", "partner", "--artist-id", ARTIST,
        "--brands", ",".join(POOL),
    ]) == 2
    output = capsys.readouterr().out
    assert "sensitive-objective" not in output
    assert json.loads(output)["release_authorized"] is False
