import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ofc_engine.contracts import ActionIntent, Decision
from ofc_engine.durable import SQLiteRevisionStore
from ofc_engine.intake import AgentProposal, accept_proposal
from ofc_engine.qloo import brand_affinity_probe
from ofc_engine.qloo_agent_bridge import AgentEvidenceRejected
from ofc_engine.qloo_recorded_commit import commit_recorded_repair
from ofc_engine.qloo_recorded_recheck import inspect_recorded_recheck, main


ARTIST = "artist-a"
BRANDS = ("brand-a", "brand-b", "brand-c")
SPEC = brand_affinity_probe(ARTIST, BRANDS)
NOW = datetime(2026, 10, 14, 11, 0, tzinfo=timezone.utc)
AGE = timedelta(hours=1)
ACK = "COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE"


def proposal(*, budget=100):
    return AgentProposal(
        intent=ActionIntent(
            run_id="postcommit-001", client_request_id="intake-001",
            objective="Inspect one recorded affinity repair",
            constraints={"budget_usd": budget, "external_action": False},
            taste_context={"audience": "operator-owned"},
        ),
        decisions=(Decision(
            decision_id="brand", domain="partner", candidate_id=BRANDS[0],
            candidate_pool=BRANDS, mutable_fields=("candidate_id",),
        ),),
    )


def capture(*, collected=None, scores=(0.9, 0.6, 0.2)):
    return {
        "summary": {
            "endpoint": SPEC.path, "http_status": 200,
            "real_qloo_requests": 1, "mode": "LIVE_RESPONSE_UNQUALIFIED",
            "query_fingerprint": SPEC.fingerprint,
            "utc_collected_at": (collected or NOW - timedelta(minutes=20)).isoformat(),
        },
        "payload": {
            "success": True,
            "results": {"entities": [
                {"entity_id": ident, "type": "urn:entity", "query": {
                    "affinity": score,
                    "explainability": {"signal.interests.entities": {}}}}
                for ident, score in zip((BRANDS[1], BRANDS[0], BRANDS[2]), scores)
            ]},
        },
    }


def seeded(tmp_path):
    db = tmp_path / "agent.sqlite3"
    store = SQLiteRevisionStore(db)
    p = proposal()
    accept_proposal(store, p)
    original = capture()
    commit_recorded_repair(
        store, p, original,
        decision_id="brand", artist_id=ARTIST, brand_ids=BRANDS,
        request_id="recorded-commit-001", acknowledgement=ACK,
        as_of=NOW, max_evidence_age=AGE,
    )
    return db, store, p, original


def inspect(store, p, original, updated):
    return inspect_recorded_recheck(
        store, p, original, updated,
        decision_id="brand", artist_id=ARTIST, brand_ids=BRANDS,
        as_of=NOW, max_evidence_age=AGE,
    )


def test_same_original_capture_is_only_a_replay_and_never_clears_pending(tmp_path):
    db, store, p, original = seeded(tmp_path)
    result = inspect(SQLiteRevisionStore(db), p, original, original)
    assert result["status"] == "ORIGINAL_CAPTURE_REPLAY_ONLY"
    assert result["recorded_rank_verdict"] == "PASS"
    assert result["same_capture"] is True
    assert result["later_capture_observed"] is False
    assert result["pending_recheck_ids"] == ["brand"]
    assert result["pending_recheck_cleared"] is False
    assert result["independent_recheck_completed"] is False
    assert result["release_authorized"] is False
    assert result["stored_plan_mutated"] is False
    assert len(store.audit_history(p.intent.run_id)) == 2
    assert store.pending_rechecks(p.intent.run_id) == ("brand",)
    assert '"affinity":' not in json.dumps(result)


def test_later_distinct_recorded_capture_is_not_authenticated(tmp_path):
    _, store, p, original = seeded(tmp_path)
    updated = capture(collected=NOW - timedelta(minutes=1), scores=(0.91, 0.61, 0.21))
    result = inspect(store, p, original, updated)
    assert result["status"] == "RECORDED_LATER_CAPTURE_RANK_PASS_ONLY"
    assert result["recorded_rank_verdict"] == "PASS"
    assert result["same_capture"] is False
    assert result["later_capture_observed"] is True
    assert result["provider_independence_established"] is False
    assert result["fresh_provider_recheck"] is False
    assert result["provider_authenticated"] is False
    assert result["pending_recheck_cleared"] is False
    assert store.pending_rechecks(p.intent.run_id) == ("brand",)


def test_modified_or_older_capture_cannot_claim_later_recheck(tmp_path):
    _, store, p, original = seeded(tmp_path)
    older = capture(collected=NOW - timedelta(minutes=25), scores=(0.91, 0.61, 0.21))
    result = inspect(store, p, original, older)
    assert result["status"] == "RECORDED_CAPTURE_NOT_LATER"
    assert result["pending_recheck_cleared"] is False


def test_stale_later_capture_is_unresolved(tmp_path):
    _, store, p, original = seeded(tmp_path)
    later_but_stale = capture(collected=NOW - timedelta(minutes=10), scores=(0.91, 0.61, 0.21))
    # A shorter permitted evidence window makes the later recorded sample stale.
    result = inspect_recorded_recheck(
        store, p, original, later_but_stale,
        decision_id="brand", artist_id=ARTIST, brand_ids=BRANDS,
        as_of=NOW, max_evidence_age=timedelta(minutes=5),
    )
    assert result["status"] == "RECORDED_RECHECK_UNRESOLVED"
    assert result["recorded_rank_verdict"] == "UNCERTAIN"
    assert result["pending_recheck_ids"] == ["brand"]


def test_changed_proposal_fails_before_evidence_review(tmp_path):
    _, store, _, original = seeded(tmp_path)
    with pytest.raises(AgentEvidenceRejected, match="STORED_REPAIR_STATE_MISMATCH"):
        inspect(store, proposal(budget=101), original, original)


def test_uncommitted_plan_is_not_qualified(tmp_path):
    store = SQLiteRevisionStore(tmp_path / "empty-repair.sqlite3")
    p = proposal()
    accept_proposal(store, p)
    with pytest.raises(AgentEvidenceRejected, match="STORED_REPAIR_STATE_MISMATCH"):
        inspect(store, p, capture(), capture())


def test_corrupted_journal_is_rejected(tmp_path):
    db, store, p, original = seeded(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE events SET event_hash=? WHERE plan_id=? AND sequence=1",
                     ("0" * 64, p.intent.run_id))
    with pytest.raises(AgentEvidenceRejected, match="STORED_JOURNAL_INVALID"):
        inspect(store, p, original, original)


def test_wrong_query_capture_rejected(tmp_path):
    _, store, p, original = seeded(tmp_path)
    bad = capture()
    bad["summary"]["query_fingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="CAPTURE_QUERY_MISMATCH"):
        inspect(store, p, original, bad)


def test_cli_offline_replay_and_database_remains_unchanged(tmp_path, monkeypatch, capsys):
    db, store, p, original = seeded(tmp_path)
    proposal_path, capture_path = tmp_path / "proposal.json", tmp_path / "capture.json"
    proposal_path.write_text(p.model_dump_json())
    capture_path.write_text(json.dumps(original))
    from ofc_engine import qloo
    monkeypatch.setattr(qloo.QlooProbeClient, "from_environment",
                        lambda: (_ for _ in ()).throw(AssertionError("no network")))
    args = [
        "--database", str(db), "--proposal", str(proposal_path),
        "--original-capture", str(capture_path), "--recheck-capture", str(capture_path),
        "--decision-id", "brand", "--artist-id", ARTIST,
        "--brands", ",".join(BRANDS),
    ]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ORIGINAL_CAPTURE_REPLAY_ONLY"
    assert result["real_qloo_requests"] == 0
    assert result["pending_recheck_ids"] == ["brand"]
    assert store.get(p.intent.run_id).revision == 1


def test_cli_missing_or_symlink_database_fails_closed(tmp_path, capsys):
    db, _, p, original = seeded(tmp_path)
    proposal_path, capture_path = tmp_path / "proposal.json", tmp_path / "capture.json"
    proposal_path.write_text(p.model_dump_json())
    capture_path.write_text(json.dumps(original))
    missing = tmp_path / "unknown.sqlite3"
    args = [
        "--database", str(missing), "--proposal", str(proposal_path),
        "--original-capture", str(capture_path), "--recheck-capture", str(capture_path),
        "--decision-id", "brand", "--artist-id", ARTIST, "--brands", ",".join(BRANDS),
    ]
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    assert not missing.exists()
    missing.symlink_to(db)
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
