"""Regression tests for revision-journal and pending-state integrity."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from ofc_engine.contracts import RepairPatch
from ofc_engine.durable import SQLiteRevisionStore, StorageIntegrityError
from ofc_engine.revisions import plan_digest


def _patch(plan, new_candidate_id, request_id):
    decision = next(d for d in plan.decisions if d.decision_id == "brand")
    return RepairPatch(
        patch_id=request_id,
        decision_id="brand",
        base_revision=plan.revision,
        old_candidate_id=decision.candidate_id,
        new_candidate_id=new_candidate_id,
        precondition_digest=plan_digest(plan),
        evidence_refs=("SYNTHETIC:fixture",),
    )


def test_corrupt_journal_rejects_new_patch_without_mutating_plan(tmp_path, plan):
    db = tmp_path / "revision.sqlite3"
    store = SQLiteRevisionStore(db)
    store.insert(plan)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE events SET event_payload=? WHERE plan_id=? AND sequence=0",
            ("{}", plan.plan_id),
        )
    with pytest.raises(StorageIntegrityError):
        store.apply_patch_once(
            plan.plan_id, _patch(plan, "brand-b", "new-patch"),
            client_request_id="new-patch",
        )
    assert store.get(plan.plan_id) == plan
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM requests").fetchone()[0] == 0


def test_corrupt_old_event_rejects_idempotent_replay(tmp_path, plan):
    db = tmp_path / "revision.sqlite3"
    store = SQLiteRevisionStore(db)
    store.insert(plan)
    patch = _patch(plan, "brand-b", "first-patch")
    store.apply_patch_once(plan.plan_id, patch, client_request_id="request-001")
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE events SET event_payload=? WHERE plan_id=? AND sequence=0",
            ('{"modified":true}', plan.plan_id),
        )
    with pytest.raises(StorageIntegrityError):
        store.apply_patch_once(plan.plan_id, patch, client_request_id="request-001")
    assert store.get(plan.plan_id).revision == 1


@pytest.mark.parametrize("invalid", [
    '["unknown-decision"]',
    '["brand", "brand"]',
    '{"brand": true}',
    '["brand", 7]',
    'null',
])
def test_corrupt_pending_rechecks_cannot_be_discarded_by_next_patch(tmp_path, plan, invalid):
    db = tmp_path / "revision.sqlite3"
    store = SQLiteRevisionStore(db)
    store.insert(plan)
    updated, _ = store.apply_patch_once(
        plan.plan_id, _patch(plan, "brand-b", "first-patch"),
        client_request_id="request-001",
    )
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE plans SET pending_rechecks=? WHERE plan_id=?",
            (invalid, plan.plan_id),
        )
    with pytest.raises(StorageIntegrityError):
        store.apply_patch_once(
            plan.plan_id, _patch(updated, "brand-a", "second-patch"),
            client_request_id="request-002",
        )
    assert store.get(plan.plan_id).revision == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM requests").fetchone()[0] == 1


def test_clean_concurrent_replay_and_audit_survive_restart(tmp_path, plan):
    db = tmp_path / "revision.sqlite3"
    store = SQLiteRevisionStore(db)
    store.insert(plan)
    payload = _patch(plan, "brand-b", "first-patch")

    def attempt(_):
        return SQLiteRevisionStore(db).apply_patch_once(
            plan.plan_id, payload, client_request_id="request-001"
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(8)))
    assert all(item == results[0] for item in results)
    reopened = SQLiteRevisionStore(db)
    assert reopened.get(plan.plan_id).revision == 1
    assert reopened.pending_rechecks(plan.plan_id) == ("brand", "afterparty")
    assert len(reopened.audit_history(plan.plan_id)) == 2


def test_corrupt_digest_does_not_advance_journal(tmp_path, plan):
    db = tmp_path / "revision.sqlite3"
    store = SQLiteRevisionStore(db)
    store.insert(plan)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE plans SET journal_head=? WHERE plan_id=?", ("0" * 64, plan.plan_id)
        )
    with pytest.raises(StorageIntegrityError):
        store.apply_patch_once(
            plan.plan_id, _patch(plan, "brand-b", "new-patch"),
            client_request_id="request-001",
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
