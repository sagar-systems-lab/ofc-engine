import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ofc_engine.contracts import ActionIntent, Decision
from ofc_engine.durable import SQLiteRevisionStore
from ofc_engine.intake import AgentProposal, accept_proposal, freeze_proposal
from ofc_engine.qloo import brand_affinity_probe
from ofc_engine.qloo_agent_bridge import AgentEvidenceRejected
from ofc_engine.qloo_recorded_commit import commit_recorded_repair, main
from ofc_engine.revisions import plan_digest


ARTIST = "artist-a"
BRANDS = ("brand-a", "brand-b", "brand-c")
SPEC = brand_affinity_probe(ARTIST, BRANDS)
NOW = datetime(2026, 10, 14, 11, 0, tzinfo=timezone.utc)
AGE = timedelta(hours=1)
ACK = "COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE"


def proposal(*, budget=100, dependent=False):
    decisions = [Decision(
        decision_id="brand", domain="partner", candidate_id=BRANDS[0],
        candidate_pool=BRANDS, mutable_fields=("candidate_id",),
    )]
    if dependent:
        decisions.append(Decision(
            decision_id="venue", domain="venue", candidate_id="venue-a",
            candidate_pool=("venue-a", "venue-b"),
            mutable_fields=("candidate_id",), depends_on=("brand",),
        ))
    return AgentProposal(
        intent=ActionIntent(
            run_id="stored-rank-001", client_request_id="intake-001",
            objective="Review recorded brand rank",
            constraints={"budget_usd": budget, "external_action": False},
            taste_context={"audience": "operator-owned"},
        ),
        decisions=tuple(decisions),
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
                {"entity_id": BRANDS[1], "type": "urn:entity", "query": {
                    "affinity": 0.9, "explainability": {"signal.interests.entities": {}}}},
                {"entity_id": BRANDS[0], "type": "urn:entity", "query": {
                    "affinity": 0.6, "explainability": {"signal.interests.entities": {}}}},
                {"entity_id": BRANDS[2], "type": "urn:entity", "query": {
                    "affinity": 0.2, "explainability": {"signal.interests.entities": {}}}},
            ]},
        },
    }


def submit(tmp_path, p=None):
    db = tmp_path / "agent.sqlite3"
    store = SQLiteRevisionStore(db)
    p = p or proposal()
    accept_proposal(store, p)
    return db, store, p


def commit(store, p, c=None, *, request_id="recorded-commit-001", acknowledgement=ACK):
    return commit_recorded_repair(
        store, p, c if c is not None else capture(),
        decision_id="brand", artist_id=ARTIST, brand_ids=BRANDS,
        request_id=request_id, acknowledgement=acknowledgement,
        as_of=NOW, max_evidence_age=AGE,
    )


def test_recorded_repair_commits_only_candidate_with_pending_recheck(tmp_path):
    _, store, p = submit(tmp_path)
    original, _ = freeze_proposal(p)
    result = commit(store, p)
    saved = store.get(original.plan_id)
    assert result["status"] == "RECORDED_REPAIR_STORED_RECHECK_PENDING"
    assert result["original_plan_digest"] == plan_digest(original)
    assert result["persisted_plan_digest"] == plan_digest(saved)
    assert saved.revision == 1
    assert saved.baseline_digest == original.baseline_digest
    assert saved.constraints_digest == original.constraints_digest
    assert saved.decisions[0].candidate_id == BRANDS[1]
    assert store.pending_rechecks(p.intent.run_id) == ("brand",)
    assert result["pending_recheck_ids"] == ["brand"]
    assert result["idempotent_replay"] is False
    assert len(store.audit_history(p.intent.run_id)) == 2
    assert result["provider_authenticated"] is False
    assert result["fresh_provider_recheck"] is False
    assert result["release_authorized"] is False
    assert result["external_action_executed"] is False
    assert result["real_qloo_requests"] == 0
    assert '"affinity":' not in json.dumps(result)


def test_same_request_id_replays_exact_saved_revision_without_new_event(tmp_path):
    db, store, p = submit(tmp_path)
    first = commit(store, p)
    second = commit(SQLiteRevisionStore(db), p)
    assert second["idempotent_replay"] is True
    assert second["persisted_plan_digest"] == first["persisted_plan_digest"]
    assert second["stored_journal_head"] == first["stored_journal_head"]
    assert len(store.audit_history(p.intent.run_id)) == 2


def test_changed_commit_id_cannot_reuse_already_revised_plan(tmp_path):
    _, store, p = submit(tmp_path)
    commit(store, p)
    with pytest.raises(AgentEvidenceRejected, match="STORED_REPAIR_REPLAY_CONFLICT"):
        commit(store, p, request_id="different-commit-id")
    assert store.get(p.intent.run_id).revision == 1
    assert len(store.audit_history(p.intent.run_id)) == 2


def test_missing_ack_never_mutates_database(tmp_path):
    _, store, p = submit(tmp_path)
    with pytest.raises(AgentEvidenceRejected, match="ACK_REQUIRED"):
        commit(store, p, acknowledgement="YES")
    assert store.get(p.intent.run_id).revision == 0
    assert len(store.audit_history(p.intent.run_id)) == 1


def test_stale_capture_cannot_be_committed(tmp_path):
    _, store, p = submit(tmp_path)
    stale = capture(collected=NOW - timedelta(hours=2))
    with pytest.raises(AgentEvidenceRejected, match="RECORDED_REPAIR_NOT_QUALIFIED"):
        commit(store, p, stale)
    assert store.get(p.intent.run_id).revision == 0


def test_changed_input_constraints_fail_closed(tmp_path):
    _, store, p = submit(tmp_path)
    with pytest.raises(AgentEvidenceRejected, match="STORED_BASELINE_CONFLICT"):
        commit(store, proposal(budget=101))
    assert store.get(p.intent.run_id).revision == 0


def test_dependency_or_unreviewed_decision_cannot_be_committed(tmp_path):
    _, store, p = submit(tmp_path, proposal(dependent=True))
    with pytest.raises(AgentEvidenceRejected, match="RECORDED_REPAIR_NOT_QUALIFIED"):
        commit(store, p)
    assert store.get(p.intent.run_id).revision == 0


def test_corrupt_stored_journal_blocks_commit(tmp_path):
    db, store, p = submit(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE events SET event_hash=? WHERE plan_id=?", ("0" * 64, p.intent.run_id))
    with pytest.raises(AgentEvidenceRejected, match="STORED_JOURNAL_INVALID"):
        commit(store, p)


def test_cli_requires_ack_and_makes_no_qloo_network_call(tmp_path, capsys, monkeypatch):
    db, store, p = submit(tmp_path)
    proposal_path = tmp_path / "proposal.json"
    capture_path = tmp_path / "capture.json"
    proposal_path.write_text(p.model_dump_json())
    capture_path.write_text(json.dumps(capture(
        collected=datetime.now(timezone.utc) - timedelta(minutes=1)
    )))
    from ofc_engine import qloo
    monkeypatch.setattr(qloo.QlooProbeClient, "from_environment",
                        lambda: (_ for _ in ()).throw(AssertionError("no provider calls")))
    args = [
        "--database", str(db), "--proposal", str(proposal_path),
        "--capture", str(capture_path), "--decision-id", "brand",
        "--artist-id", ARTIST, "--brands", ",".join(BRANDS),
        "--request-id", "recorded-commit-001",
    ]
    assert main([*args, "--acknowledge", "NO"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    assert store.get(p.intent.run_id).revision == 0
    assert main([*args, "--acknowledge", ACK]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["persisted_plan_revision"] == 1
    assert result["pending_recheck_ids"] == ["brand"]
    assert main([*args, "--acknowledge", ACK]) == 0
    assert json.loads(capsys.readouterr().out)["idempotent_replay"] is True


def test_missing_or_symlinked_database_does_not_create_or_follow_it(tmp_path, capsys):
    p = proposal()
    proposal_path = tmp_path / "proposal.json"
    capture_path = tmp_path / "capture.json"
    proposal_path.write_text(p.model_dump_json())
    capture_path.write_text(json.dumps(capture()))
    missing = tmp_path / "missing.sqlite3"
    args = [
        "--database", str(missing), "--proposal", str(proposal_path),
        "--capture", str(capture_path), "--decision-id", "brand",
        "--artist-id", ARTIST, "--brands", ",".join(BRANDS),
        "--request-id", "recorded-commit-001", "--acknowledge", ACK,
    ]
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    assert not missing.exists()
    real, _, _ = submit(tmp_path)
    missing.symlink_to(real)
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
