import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ofc_engine.contracts import ActionIntent, Decision, RepairPatch
from ofc_engine.durable import SQLiteRevisionStore
from ofc_engine.intake import AgentProposal, accept_proposal, freeze_proposal
from ofc_engine.qloo import brand_affinity_probe
from ofc_engine.qloo_agent_bridge import (
    AgentEvidenceRejected, compare_stored_agent_proposal, main,
)
from ofc_engine.revisions import plan_digest


ARTIST = "artist-a"
BRANDS = ("brand-a", "brand-b", "brand-c")
SPEC = brand_affinity_probe(ARTIST, BRANDS)
NOW = datetime(2026, 10, 14, 11, 0, tzinfo=timezone.utc)
AGE = timedelta(hours=1)


def proposal(*, budget=100, run_id="agent-stored-001"):
    return AgentProposal(
        intent=ActionIntent(
            run_id=run_id, client_request_id="request-001",
            objective="Review fixed partner choices",
            constraints={"budget_usd": budget, "external_action": False},
            taste_context={"audience": "operator-owned context"},
        ),
        decisions=(Decision(
            decision_id="brand", domain="partner", candidate_id=BRANDS[0],
            candidate_pool=BRANDS, mutable_fields=("candidate_id",),
        ),),
    )


def capture(*, collected=None):
    return {
        "summary": {
            "endpoint": SPEC.path, "http_status": 200,
            "real_qloo_requests": 1, "mode": "LIVE_RESPONSE_UNQUALIFIED",
            "query_fingerprint": SPEC.fingerprint,
            "utc_collected_at": (collected or NOW - timedelta(minutes=5)).isoformat(),
        },
        "payload": {
            "success": True,
            "results": {"entities": [
                {"entity_id": BRANDS[1], "type": "urn:entity",
                 "query": {"affinity": 0.9, "explainability": {"signal.interests.entities": {}}}},
                {"entity_id": BRANDS[0], "type": "urn:entity",
                 "query": {"affinity": 0.6, "explainability": {"signal.interests.entities": {}}}},
                {"entity_id": BRANDS[2], "type": "urn:entity",
                 "query": {"affinity": 0.2, "explainability": {"signal.interests.entities": {}}}},
            ]},
        },
    }


def review(store, p, c=None):
    return compare_stored_agent_proposal(
        store, p, c or capture(),
        decision_id="brand", artist_id=ARTIST,
        brand_ids=BRANDS, as_of=NOW, max_evidence_age=AGE,
    )


def test_persisted_agent_baseline_and_journal_are_bound_before_repair(tmp_path):
    db = tmp_path / "agent.sqlite3"
    store = SQLiteRevisionStore(db)
    p = proposal()
    created = accept_proposal(store, p)
    assert created.created
    initial, _ = freeze_proposal(p)
    first_digest = plan_digest(initial)
    result = review(store, p)
    assert result["status"] == "RECORDED_AGENT_PLAN_REVIEW"
    assert result["stored_baseline_matched"] is True
    assert result["stored_plan_revision"] == 0
    assert result["stored_journal_event_count"] == 1
    assert result["persisted_repair_committed"] is False
    assert result["stored_plan_mutated"] is False
    assert result["comparison"]["evidence_on"]["current_revision"] == 1
    assert result["comparison"]["evidence_on"]["selected_candidates"]["brand"] == BRANDS[1]
    assert result["comparison"]["evidence_off"]["current_revision"] == 0
    assert result["comparison"]["same_baseline"]
    assert result["baseline_plan_digest"] == first_digest
    assert store.get(p.intent.run_id) == initial
    assert store.pending_rechecks(p.intent.run_id) == ()
    assert len(store.audit_history(p.intent.run_id)) == 1
    assert result["provider_authenticated"] is False
    assert result["release_authorized"] is False
    assert result["real_qloo_requests"] == 0


def test_reopening_sqlite_retains_same_frozen_proposal(tmp_path):
    db = tmp_path / "agent.sqlite3"
    p = proposal()
    accept_proposal(SQLiteRevisionStore(db), p)
    reopened = SQLiteRevisionStore(db)
    assert review(reopened, p)["stored_baseline_matched"] is True


def test_missing_stored_intake_blocks_review(tmp_path):
    store = SQLiteRevisionStore(tmp_path / "agent.sqlite3")
    with pytest.raises(AgentEvidenceRejected, match="STORED_PLAN_MISSING"):
        review(store, proposal())


def test_changed_objective_constraints_cannot_reuse_existing_plan(tmp_path):
    store = SQLiteRevisionStore(tmp_path / "agent.sqlite3")
    accept_proposal(store, proposal())
    with pytest.raises(AgentEvidenceRejected, match="STORED_BASELINE_CONFLICT"):
        review(store, proposal(budget=101))


def test_modified_plan_revision_is_rejected(tmp_path):
    store = SQLiteRevisionStore(tmp_path / "agent.sqlite3")
    p = proposal()
    accept_proposal(store, p)
    baseline, _ = freeze_proposal(p)
    patch = RepairPatch(
        patch_id="not-authorized", decision_id="brand",
        base_revision=0, old_candidate_id=BRANDS[0],
        new_candidate_id=BRANDS[1],
        precondition_digest=plan_digest(baseline),
        evidence_refs=("synthetic:manual",),
    )
    store.apply_patch_once(p.intent.run_id, patch, client_request_id="manual-patch")
    with pytest.raises(AgentEvidenceRejected, match="STORED_BASELINE_CONFLICT"):
        review(store, p)


def test_corrupted_journal_blocks_review(tmp_path):
    db = tmp_path / "agent.sqlite3"
    store = SQLiteRevisionStore(db)
    p = proposal()
    accept_proposal(store, p)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE events SET event_hash=? WHERE plan_id=?", ("0" * 64, p.intent.run_id))
    with pytest.raises(AgentEvidenceRejected, match="STORED_JOURNAL_INVALID"):
        review(store, p)


def test_concurrent_mutation_detected_during_review(tmp_path, monkeypatch):
    from ofc_engine import qloo_agent_bridge
    db = tmp_path / "agent.sqlite3"
    store = SQLiteRevisionStore(db)
    p = proposal()
    accept_proposal(store, p)
    baseline, _ = freeze_proposal(p)
    original = qloo_agent_bridge.compare_agent_proposal

    def racing(*args, **kwargs):
        patch = RepairPatch(
            patch_id="concurrent", decision_id="brand", base_revision=0,
            old_candidate_id=BRANDS[0], new_candidate_id=BRANDS[1],
            precondition_digest=plan_digest(baseline), evidence_refs=("synthetic:manual",),
        )
        store.apply_patch_once(p.intent.run_id, patch, client_request_id="racing")
        return original(*args, **kwargs)

    monkeypatch.setattr(qloo_agent_bridge, "compare_agent_proposal", racing)
    with pytest.raises(AgentEvidenceRejected, match="STORED_BASELINE_CONFLICT"):
        review(store, p)


def test_stale_capture_keeps_database_at_revision_zero(tmp_path):
    store = SQLiteRevisionStore(tmp_path / "agent.sqlite3")
    p = proposal()
    accept_proposal(store, p)
    stale = capture(collected=NOW - timedelta(hours=2))
    result = review(store, p, stale)
    assert result["comparison"]["evidence_on"]["status"] == "REVIEW_REQUIRED"
    assert result["comparison"]["evidence_on"]["current_revision"] == 0
    assert store.get(p.intent.run_id).revision == 0


def test_cli_reads_existing_sqlite_without_mutating(tmp_path, capsys, monkeypatch):
    db, pth, cap = (tmp_path / filename for filename in
                    ("agent.sqlite3", "proposal.json", "capture.json"))
    p = proposal()
    accept_proposal(SQLiteRevisionStore(db), p)
    pth.write_text(p.model_dump_json())
    cap.write_text(json.dumps(capture(collected=datetime.now(timezone.utc) - timedelta(minutes=1))))
    from ofc_engine import qloo
    monkeypatch.setattr(qloo.QlooProbeClient, "from_environment",
                        lambda: (_ for _ in ()).throw(AssertionError("no Qloo requests")))
    assert main([
        "--database", str(db), "--proposal", str(pth), "--capture", str(cap),
        "--decision-id", "brand", "--artist-id", ARTIST,
        "--brands", ",".join(BRANDS),
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["stored_baseline_matched"]
    assert result["stored_plan_revision"] == 0
    assert result["comparison"]["evidence_on"]["current_revision"] == 1
    assert SQLiteRevisionStore(db).get(p.intent.run_id).revision == 0
    assert '"affinity":' not in json.dumps(result)


def test_cli_missing_or_symlinked_database_blocks_without_creation(tmp_path, capsys):
    pth, cap = tmp_path / "proposal.json", tmp_path / "capture.json"
    pth.write_text(proposal().model_dump_json())
    cap.write_text(json.dumps(capture()))
    db = tmp_path / "does-not-exist.sqlite3"
    args = [
        "--database", str(db), "--proposal", str(pth), "--capture", str(cap),
        "--decision-id", "brand", "--artist-id", ARTIST, "--brands", ",".join(BRANDS),
    ]
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    assert not db.exists()

    target = tmp_path / "real.sqlite3"
    accept_proposal(SQLiteRevisionStore(target), proposal())
    db.symlink_to(target)
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
