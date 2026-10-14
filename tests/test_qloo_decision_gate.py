from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from ofc_engine.contracts import ActionIntent, Decision
from ofc_engine.durable import SQLiteRevisionStore
from ofc_engine.intake import AgentProposal, accept_proposal
from ofc_engine.qloo import brand_affinity_probe, QlooProbeClient
from ofc_engine.qloo_agent_bridge import AgentEvidenceRejected
from ofc_engine.qloo_decision_gate import run_integrated_gate, main
from ofc_engine.qloo_flow import RecordedSource

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
AGE = timedelta(hours=1)
ACK = "COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE"
BRANDS_1 = ("brand-a", "brand-b", "brand-c")
BRANDS_2 = ("sponsor-a", "sponsor-b", "sponsor-c")
ARTIST_1 = "artist-a"
ARTIST_2 = "artist-b"


def proposal(*, second=False, dependent=False, locality=None):
    decisions = [Decision(
        decision_id="brand", domain="partner", candidate_id=BRANDS_1[0],
        candidate_pool=BRANDS_1, mutable_fields=("candidate_id",),
        locality_ref=locality,
    )]
    if second:
        decisions.append(Decision(
            decision_id="sponsor", domain="brand", candidate_id=BRANDS_2[0],
            candidate_pool=BRANDS_2,
            mutable_fields=("candidate_id",),
            depends_on=("brand",) if dependent else (),
        ))
    return AgentProposal(
        intent=ActionIntent(
            run_id="integrated-test-001", client_request_id="intake-001",
            objective="Review cultural brand shortlist",
            constraints={"external_action": False},
            taste_context={"audience": "private"},
        ),
        decisions=tuple(decisions),
    )


def capture(spec, order, *, collected=None, offset=0):
    return {
        "summary": {
            "endpoint": spec.path,
            "http_status": 200,
            "real_qloo_requests": 1,
            "mode": "LIVE_RESPONSE_UNQUALIFIED",
            "query_fingerprint": spec.fingerprint,
            "utc_collected_at": (collected or NOW - timedelta(minutes=20)).isoformat(),
        },
        "payload": {
            "success": True,
            "results": {"entities": [{
                "entity_id": identity,
                "type": "urn:entity",
                "query": {
                    "affinity": 0.9 - i * 0.2 + offset,
                    "explainability": {"signal.interests.entities": {}},
                },
            } for i, identity in enumerate(order)]},
        },
    }


def sources(*, second=False):
    first = brand_affinity_probe(ARTIST_1, BRANDS_1)
    mapping = {
        "brand": RecordedSource(
            capture(first, (BRANDS_1[1], BRANDS_1[0], BRANDS_1[2])), first
        ),
    }
    if second:
        next_spec = brand_affinity_probe(ARTIST_2, BRANDS_2)
        mapping["sponsor"] = RecordedSource(
            capture(next_spec, BRANDS_2), next_spec
        )
    return mapping


def seeded(tmp_path, *, second=False, dependent=False, locality=None):
    store = SQLiteRevisionStore(tmp_path / "agent.sqlite3")
    p = proposal(second=second, dependent=dependent, locality=locality)
    accept_proposal(store, p)
    return store, p


def run(store, p, mapping, **overrides):
    opts = dict(as_of=NOW, max_evidence_age=AGE)
    opts.update(overrides)
    return run_integrated_gate(store, p, mapping, **opts)


def test_single_decision_complete_recorded_chain_preserves_pending(tmp_path):
    store, p = seeded(tmp_path)
    mapping = sources()
    initial = run(store, p, mapping)
    assert initial["status"] == "RECORDED_REPAIR_PROPOSED"
    assert initial["revision"] == 0
    assert initial["changed_decision_id"] == "brand"
    assert initial["decision_reviews"][0]["verdict"] == "REPAIR"
    assert store.get(p.intent.run_id).revision == 0
    assert len(store.audit_history(p.intent.run_id)) == 1

    with pytest.raises(AgentEvidenceRejected, match="EXPLICIT_COMMIT_ACK_REQUIRED"):
        run(store, p, mapping, commit=True)

    committed = run(
        store, p, mapping, commit=True, request_id="recorded-integration-001",
        acknowledgement=ACK,
    )
    assert committed["status"] == "RECORDED_REPAIR_STORED_RECHECK_PENDING"
    assert committed["revision"] == 1
    assert committed["pending_recheck_ids"] == ["brand"]
    assert committed["release_authorized"] is False
    assert committed["provider_authenticated"] is False
    assert len(store.audit_history(p.intent.run_id)) == 2
    assert store.get(p.intent.run_id).decisions[0].candidate_id == BRANDS_1[1]
    assert run(store, p, mapping)["status"] == "FRESH_RECHECK_REQUIRED"

    replay = run(store, p, mapping, recheck_sources={"brand": mapping["brand"]})
    assert replay["status"] == "RECHECK_CAPTURE_REPLAYED_OR_NOT_LATER"
    later = sources()
    original = mapping["brand"]
    later["brand"] = RecordedSource(
        capture(original.spec, (BRANDS_1[1], BRANDS_1[0], BRANDS_1[2]),
                collected=NOW - timedelta(minutes=1), offset=0.03),
        original.spec,
    )
    result = run(store, p, mapping, recheck_sources=later)
    assert result["status"] == "RECORDED_RANK_RECHECK_PASS_NO_RELEASE"
    assert result["recorded_rank_recheck_pass"] is True
    assert result["independent_recheck_completed"] is False
    assert result["overall_plan_verified"] is False
    assert result["release_authorized"] is False
    assert result["pending_recheck_ids"] == ["brand"]
    assert len(store.audit_history(p.intent.run_id)) == 2
    assert store.pending_rechecks(p.intent.run_id) == ("brand",)


def test_multiple_independent_decisions_one_selective_repair(tmp_path):
    store, p = seeded(tmp_path, second=True)
    mapping = sources(second=True)
    proposal_review = run(store, p, mapping)
    assert [x["verdict"] for x in proposal_review["decision_reviews"]] == ["REPAIR", "PASS"]
    commit = run(store, p, mapping, commit=True, request_id="multi-001", acknowledgement=ACK)
    assert commit["status"] == "RECORDED_REPAIR_STORED_RECHECK_PENDING"
    assert commit["pending_recheck_ids"] == ["brand"]
    after = store.get(p.intent.run_id)
    assert after.revision == 1
    assert [d.candidate_id for d in after.decisions] == [BRANDS_1[1], BRANDS_2[0]]
    first = mapping["brand"]
    later = RecordedSource(
        capture(first.spec, (BRANDS_1[1], BRANDS_1[0], BRANDS_1[2]),
                collected=NOW - timedelta(minutes=2), offset=0.01),
        first.spec,
    )
    result = run(store, p, mapping, recheck_sources={"brand": later})
    assert [v["verdict"] for v in result["decision_reviews"]] == ["PASS", "PASS"]
    assert result["status"] == "RECORDED_RANK_RECHECK_PASS_NO_RELEASE"
    assert result["pending_recheck_ids"] == ["brand"]
    assert result["release_authorized"] is False
    assert result["real_qloo_requests"] == 0


def test_absent_second_source_keeps_untouched_decision_uncertain(tmp_path):
    store, p = seeded(tmp_path, second=True)
    result = run(store, p, sources())
    assert result["status"] == "REVIEW_REQUIRED"
    assert [v["verdict"] for v in result["decision_reviews"]] == ["REPAIR", "UNCERTAIN"]
    with pytest.raises(AgentEvidenceRejected, match="RECORDED_PLAN_NOT_REPAIR_QUALIFIED"):
        run(store, p, sources(), commit=True, request_id="id-001", acknowledgement=ACK)
    assert store.get(p.intent.run_id).revision == 0


def test_dependency_and_locality_cannot_be_certified(tmp_path):
    store, p = seeded(tmp_path, second=True, dependent=True)
    with pytest.raises(AgentEvidenceRejected, match="UNSUPPORTED_DEPENDENT_OR_LOCAL_DECISION"):
        run(store, p, sources(second=True))


def test_historical_mode_cannot_mutate_and_repeated_commit_is_blocked(tmp_path):
    store, p = seeded(tmp_path)
    mapping = sources()
    with pytest.raises(AgentEvidenceRejected, match="HISTORICAL_CAPTURE_CANNOT_COMMIT"):
        run(store, p, mapping, commit=True, historical=True,
            request_id="history", acknowledgement=ACK)
    with pytest.raises(AgentEvidenceRejected, match="RECHECK_BEFORE_REPAIR"):
        run(store, p, mapping, recheck_sources=mapping)
    run(store, p, mapping, commit=True, request_id="initial", acknowledgement=ACK)
    with pytest.raises(AgentEvidenceRejected, match="STORED_REPAIR_ALREADY_PRESENT"):
        run(store, p, mapping, commit=True, request_id="another", acknowledgement=ACK)


def test_locality_source_rejected(tmp_path):
    store, p = seeded(tmp_path, locality="Chicago")
    with pytest.raises(AgentEvidenceRejected, match="UNSUPPORTED_DEPENDENT_OR_LOCAL_DECISION"):
        run(store, p, sources())


def test_invalid_rank_or_query_scope_not_committed(tmp_path):
    store, p = seeded(tmp_path)
    mapping = sources()
    raw = mapping["brand"].capture
    mutated = json.loads(json.dumps(raw))
    mutated["payload"]["results"]["entities"][0]["query"]["affinity"] = 0.1
    bad = {"brand": RecordedSource(mutated, mapping["brand"].spec)}
    result = run(store, p, bad)
    assert result["status"] == "REVIEW_REQUIRED"
    with pytest.raises(AgentEvidenceRejected):
        run(store, p, bad, commit=True, request_id="id", acknowledgement=ACK)
    wrong_spec = brand_affinity_probe("wrong-artist", ("wrong-a", "wrong-b"))
    with pytest.raises(AgentEvidenceRejected, match="EVIDENCE_SCOPE_MISMATCH"):
        run(store, p, {"brand": RecordedSource(mutated, wrong_spec)})
    assert store.get(p.intent.run_id).revision == 0


def test_stale_and_future_rank_cannot_commit(tmp_path):
    store, p = seeded(tmp_path)
    mapping = sources()
    stale_result = run(store, p, mapping, as_of=NOW + timedelta(days=5))
    assert stale_result["status"] == "REVIEW_REQUIRED"
    with pytest.raises(AgentEvidenceRejected):
        run(store, p, mapping, commit=True, request_id="stale", acknowledgement=ACK,
            as_of=NOW + timedelta(days=5))
    future_result = run(store, p, mapping, as_of=NOW - timedelta(hours=1))
    assert future_result["status"] == "REVIEW_REQUIRED"


def test_conflicting_modified_proposal_rejected_before_recheck(tmp_path):
    store, p = seeded(tmp_path)
    run(store, p, sources(), commit=True, request_id="original", acknowledgement=ACK)
    modified = p.model_copy(update={
        "intent": p.intent.model_copy(update={"objective": "Different objective"})
    })
    with pytest.raises(AgentEvidenceRejected, match="STORED_BASELINE_CONFLICT"):
        run(store, modified, sources())


def test_failed_later_rank_does_not_qualify(tmp_path):
    store, p = seeded(tmp_path)
    mapping = sources()
    run(store, p, mapping, commit=True, request_id="original", acknowledgement=ACK)
    initial = mapping["brand"]
    reversed_capture = capture(
        initial.spec, BRANDS_1, collected=NOW - timedelta(minutes=1),
    )
    result = run(store, p, mapping, recheck_sources={"brand": RecordedSource(reversed_capture, initial.spec)})
    assert result["status"] == "RECHECK_REVIEW_REQUIRED"
    assert result["release_authorized"] is False


def test_browser_and_network_boundaries_are_not_touched(tmp_path, monkeypatch):
    store, p = seeded(tmp_path, second=True)
    monkeypatch.setattr(QlooProbeClient, "from_environment",
                        lambda: (_ for _ in ()).throw(AssertionError("unexpected live network")))
    result = run(store, p, sources(second=True))
    assert result["real_qloo_requests"] == 0
    assert result["source_mode"] == "RECORDED_REDACTED"


def test_cli_loads_private_index_without_credential(tmp_path, monkeypatch, capsys):
    store, p = seeded(tmp_path)
    source = sources()["brand"]
    capture_path = tmp_path / "private-capture.json"
    capture_path.write_text(json.dumps(source.capture))
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(p.model_dump_json())
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps({
        "decisions": {
            "brand": {
                "artist_id": ARTIST_1, "brand_ids": list(BRANDS_1),
                "capture_file": str(capture_path),
            }
        }
    }))
    monkeypatch.setattr(QlooProbeClient, "from_environment",
                        lambda: (_ for _ in ()).throw(AssertionError("no key in recorded CLI")))
    opts = [
        "--database", str(tmp_path / "agent.sqlite3"),
        "--proposal", str(proposal_path), "--evidence-index", str(index_path),
        "--historical",
    ]
    assert main(opts) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "RECORDED_REPAIR_PROPOSED"
    assert report["release_authorized"] is False
    assert report["real_qloo_requests"] == 0
    assert main([*opts, "--commit"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
