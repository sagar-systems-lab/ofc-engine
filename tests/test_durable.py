"""Transactions and recovery tests for the durable SQLite state boundary."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import json
import sqlite3
import subprocess
import sys

import pytest

from ofc_engine.contracts import RepairPatch
from ofc_engine.durable import SQLiteRevisionStore, StorageIntegrityError
from ofc_engine.revisions import RevisionConflict, plan_digest
from ofc_engine.store import IdempotencyConflict


def patch(plan, *, patch_id="p1", replacement="brand-b"):
    return RepairPatch(
        patch_id=patch_id, decision_id="brand", base_revision=plan.revision,
        old_candidate_id="brand-a", new_candidate_id=replacement,
        precondition_digest=plan_digest(plan), evidence_refs=("SYNTHETIC:fixture",),
    )


@pytest.fixture
def db(tmp_path):
    return tmp_path / "ofc.sqlite3"


def test_restart_recovers_exact_current_snapshot_and_history(db,plan):
    store=SQLiteRevisionStore(db)
    store.insert(plan)
    after,affected=store.apply_patch_once(plan.plan_id,patch(plan),client_request_id="req1")
    reopened=SQLiteRevisionStore(db)
    assert reopened.get(plan.plan_id)==after
    assert affected==('brand','afterparty')
    events=reopened.audit_history(plan.plan_id)
    assert len(events)==2 and events[0].kind=='INSERT' and events[1].kind=='PATCH'
    assert events[1].before_digest==events[0].after_digest
    assert events[1].after_digest==plan_digest(after)


def test_idempotent_replay_survives_restart(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    request=patch(plan)
    first=store.apply_patch_once(plan.plan_id,request,client_request_id='req')
    later=SQLiteRevisionStore(db)
    assert later.apply_patch_once(plan.plan_id,request,client_request_id='req')==first
    assert len(later.audit_history(plan.plan_id))==2


def test_same_request_id_with_different_patch_fails_closed(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    store.apply_patch_once(plan.plan_id,patch(plan),client_request_id='once')
    with pytest.raises(IdempotencyConflict):
        store.apply_patch_once(plan.plan_id,patch(plan,patch_id='another'),client_request_id='once')
    assert store.get(plan.plan_id).revision==1


def test_cas_prevents_second_independent_attempt(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    first=patch(plan)
    store.apply_patch_once(plan.plan_id,first,client_request_id='a')
    with pytest.raises(RevisionConflict):
        store.apply_patch_once(plan.plan_id,first,client_request_id='b')
    assert len(store.audit_history(plan.plan_id))==2


def test_two_store_instances_racing_have_one_winner(db,plan):
    s1=SQLiteRevisionStore(db);s2=SQLiteRevisionStore(db);s1.insert(plan)
    def attempt(args):
        store,request=args
        try:
            return store.apply_patch_once(plan.plan_id,patch(plan),client_request_id=request)[0].revision
        except RevisionConflict:
            return 'STALE'
    with ThreadPoolExecutor(max_workers=2) as pool:
        values=list(pool.map(attempt,[(s1,'a'),(s2,'b')]))
    assert sorted(values,key=str)==sorted([1,'STALE'],key=str)
    assert len(s1.audit_history(plan.plan_id))==2


def test_identical_replays_across_independent_instances(db,plan):
    s1=SQLiteRevisionStore(db);s2=SQLiteRevisionStore(db);s1.insert(plan)
    def attempt(_):
        return s1.apply_patch_once(plan.plan_id,patch(plan),client_request_id='same') if _%2 else s2.apply_patch_once(plan.plan_id,patch(plan),client_request_id='same')
    with ThreadPoolExecutor(max_workers=6) as pool:
        result=list(pool.map(attempt,range(12)))
    assert all(x==result[0] for x in result)
    assert len(s1.audit_history(plan.plan_id))==2


def test_rollback_on_rejected_patch_never_mutates(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    with pytest.raises(Exception):
        store.apply_patch_once(plan.plan_id,patch(plan,replacement='not-in-pool'),client_request_id='bad')
    assert store.get(plan.plan_id)==plan
    assert len(store.audit_history(plan.plan_id))==1
    store.apply_patch_once(plan.plan_id,patch(plan),client_request_id='bad')
    assert store.get(plan.plan_id).revision==1


def test_missing_plan_and_duplicate_insert(db,plan):
    store=SQLiteRevisionStore(db)
    with pytest.raises(KeyError):store.get('missing')
    with pytest.raises(KeyError):store.apply_patch_once('missing',patch(plan),client_request_id='x')
    store.insert(plan)
    with pytest.raises(RevisionConflict):store.insert(plan)


def test_reject_blank_request_ids(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    for x in ('', '  ', None):
        with pytest.raises(ValueError):store.apply_patch_once(plan.plan_id,patch(plan),client_request_id=x)


def test_invalid_database_paths(tmp_path):
    for val in (':memory:', 'file:bad?mode=memory', ''):
        with pytest.raises(ValueError):SQLiteRevisionStore(val)
    with pytest.raises(FileNotFoundError):SQLiteRevisionStore(tmp_path/'absent'/'db.sqlite3')


def test_invalid_timeouts(db):
    for val in (0,-1,True,999):
        with pytest.raises(ValueError):SQLiteRevisionStore(db,timeout_seconds=val)


def test_insert_requires_initial_revision_zero(db,plan):
    with pytest.raises(ValueError):SQLiteRevisionStore(db).insert(plan.model_copy(update={'revision':1}))


def test_digest_integrity_detects_snapshot_corruption(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE plans SET digest=? WHERE plan_id=?",('0'*64,plan.plan_id))
    with pytest.raises(StorageIntegrityError):store.get(plan.plan_id)


def test_event_tampering_detected_by_chain(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    store.apply_patch_once(plan.plan_id,patch(plan),client_request_id='one')
    with sqlite3.connect(db) as c:
        c.execute("UPDATE events SET event_payload=? WHERE plan_id=? AND sequence=1",('{}',plan.plan_id))
    with pytest.raises(StorageIntegrityError):store.audit_history(plan.plan_id)


def test_deleted_event_detected(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    store.apply_patch_once(plan.plan_id,patch(plan),client_request_id='one')
    with sqlite3.connect(db) as c:
        c.execute("DELETE FROM events WHERE plan_id=? AND sequence=1",(plan.plan_id,))
    with pytest.raises(StorageIntegrityError):store.audit_history(plan.plan_id)


def test_other_plan_id_replay_cannot_access_first_plan(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    first=store.apply_patch_once(plan.plan_id,patch(plan),client_request_id='x')
    with pytest.raises(KeyError):store.apply_patch_once('other-plan',patch(plan),client_request_id='x')
    assert store.apply_patch_once(plan.plan_id,patch(plan),client_request_id='x')==first


def test_sql_special_characters_are_parameterized(db,plan):
    store=SQLiteRevisionStore(db)
    crafted=plan.model_copy(update={'plan_id':"tour'; DROP TABLE events; --"})
    store.insert(crafted)
    assert store.get(crafted.plan_id).plan_id==crafted.plan_id
    assert len(store.audit_history(crafted.plan_id))==1


def test_read_only_event_record_and_no_action_authority(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    event=store.audit_history(plan.plan_id)[0]
    with pytest.raises(FrozenInstanceError):event.event_hash='fake'
    assert not hasattr(store,'release')
    assert not hasattr(store,'execute_action')


def test_state_can_be_read_by_new_python_process(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    script="import sys;from ofc_engine.durable import SQLiteRevisionStore;print(SQLiteRevisionStore(sys.argv[1]).get(sys.argv[2]).revision)"
    child=subprocess.check_output([sys.executable,'-c',script,str(db),plan.plan_id],text=True)
    assert child.strip()=='0'


def test_stale_patch_does_not_create_request_row(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    bad=patch(plan).model_copy(update={'precondition_digest':'f'*64})
    with pytest.raises(RevisionConflict):store.apply_patch_once(plan.plan_id,bad,client_request_id='x')
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM requests').fetchone()[0]==0
    assert len(store.audit_history(plan.plan_id))==1


def test_pending_rechecks_survive_restart_and_are_not_implicitly_cleared(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    assert store.pending_rechecks(plan.plan_id)==()
    store.apply_patch_once(plan.plan_id,patch(plan),client_request_id='r1')
    reopened=SQLiteRevisionStore(db)
    assert reopened.pending_rechecks(plan.plan_id)==('brand','afterparty')
    assert not hasattr(reopened,'approve_review') and not hasattr(reopened,'clear_rechecks')


def test_corrupt_idempotency_result_is_rejected(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    store.apply_patch_once(plan.plan_id,patch(plan),client_request_id='r1')
    with sqlite3.connect(db) as conn:
        row=conn.execute('SELECT response_snapshot FROM requests WHERE plan_id=?',(plan.plan_id,)).fetchone()
        p=json.loads(row[0]);p['decisions'][0]['candidate_id']='brand-a'
        conn.execute('UPDATE requests SET response_snapshot=? WHERE plan_id=?',(json.dumps(p),plan.plan_id))
    with pytest.raises(StorageIntegrityError):
        store.apply_patch_once(plan.plan_id,patch(plan),client_request_id='r1')


def test_corrupt_idempotency_invalidations_rejected(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    store.apply_patch_once(plan.plan_id,patch(plan),client_request_id='r1')
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE requests SET invalidated=? WHERE plan_id=?',('[]',plan.plan_id))
    with pytest.raises(StorageIntegrityError):
        store.apply_patch_once(plan.plan_id,patch(plan),client_request_id='r1')


def test_invalid_pending_rechecks_detected(db,plan):
    store=SQLiteRevisionStore(db);store.insert(plan)
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE plans SET pending_rechecks=? WHERE plan_id=?',('["unknown"]',plan.plan_id))
    with pytest.raises(StorageIntegrityError):store.pending_rechecks(plan.plan_id)
