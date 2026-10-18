from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
from threading import Barrier

from cryptography.fernet import Fernet
import pytest

from ofc_engine.control.fixtures import campaign
from ofc_engine.control.models import ReviewRequest
from ofc_engine.control.runtime import ControlRuntime
from ofc_engine.control.storage import CapacityReached, Conflict, ControlStore, IntegrityError, NotFound
from ofc_engine.control.verifier import ProofRejected, verify


def scope(request):
    return request.plan.tenant_id, request.plan.actor.id


def test_idempotent_admission_replay_and_scope_conflict(tmp_path):
    store = ControlStore(tmp_path / "reviews.db")
    request = campaign()
    receipt = store.admit(request)
    assert store.admit(request) == receipt
    runtime = ControlRuntime(store)
    assert runtime.run_once()
    assert not runtime.run_once()
    assert store.admit(request) == receipt
    proof = store.proof(receipt["review_id"], *scope(request))
    assert verify(proof)["valid"]
    assert store.snapshot(receipt["review_id"], *scope(request))["proof_digest"] == proof["proof_digest"]
    assert ControlStore(tmp_path / "reviews.db").proof(receipt["review_id"], *scope(request)) == proof
    changed = request.data()
    changed["plan"]["objective"] = "A different objective"
    with pytest.raises(Conflict): store.admit(ReviewRequest.parse(changed))


def test_concurrent_worker_claim_has_single_winner(tmp_path):
    path = tmp_path / "reviews.db"
    store = ControlStore(path)
    request = campaign()
    receipt = store.admit(request)
    barrier = Barrier(4)

    def claim(index):
        local = ControlStore(path)
        barrier.wait()
        return local.claim(f"worker-{index}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(claim, range(4)))
    assert sum(outcome is not None for outcome in outcomes) == 1
    events = store.events(receipt["review_id"], *scope(request))
    assert [e["kind"] for e in events] == ["ACCEPTED", "RESERVED"]


@pytest.mark.parametrize("phase", ["FROZEN", "EVALUATING", "REPAIR_PROPOSED", "RECHECKING", "HANDOFF_READY"])
def test_crash_recovery_requires_explicit_resume_and_never_reissues_provider_work(tmp_path, phase):
    now = [1000]
    store = ControlStore(tmp_path / "reviews.db", clock=lambda: now[0])
    request = campaign()
    receipt = store.admit(request)

    def crash(state, job):
        if state == phase:
            raise RuntimeError("injected worker crash")

    with pytest.raises(RuntimeError): ControlRuntime(store).run_once(checkpoint=crash)
    now[0] += 31
    assert store.recover_expired() == 1
    assert not ControlRuntime(store).run_once()
    assert store.snapshot(receipt["review_id"], *scope(request))["state"] == "RECOVERY_REQUIRED"
    store.resume(receipt["review_id"], *scope(request))
    assert ControlRuntime(store).run_once()
    proof = store.proof(receipt["review_id"], *scope(request))
    assert proof["provider_calls"] == 0
    assert proof["impact"]["changed"] == ["B01"]


def test_cross_tenant_and_cross_actor_reads_fail_without_disclosing_existence(tmp_path):
    store = ControlStore(tmp_path / "reviews.db")
    request = campaign()
    receipt = store.admit(request)
    ControlRuntime(store).run_once()
    for tenant, actor in [("another", request.plan.actor.id), (request.plan.tenant_id, "another")]:
        for operation in (store.snapshot, store.events, store.request, store.proof):
            with pytest.raises(NotFound): operation(receipt["review_id"], tenant, actor)


def test_cancellation_invalidates_worker_lease(tmp_path):
    store = ControlStore(tmp_path / "reviews.db")
    request = campaign()
    receipt = store.admit(request)
    job = store.claim("worker")
    store.cancel(receipt["review_id"], *scope(request))
    with pytest.raises(Conflict): store.advance(job, "FROZEN")
    assert not ControlRuntime(store).run_once()


def test_queue_admission_and_encryption_are_enforced_before_work(tmp_path):
    store = ControlStore(tmp_path / "reviews.db")
    with pytest.raises(CapacityReached): store.admit(campaign(), max_pending=0)
    request = campaign().data()
    request["mode"], request["as_of"] = "PRIVATE", None
    request = ReviewRequest.parse(request)
    with pytest.raises(ValueError, match="encryption"): store.admit(request)
    key = Fernet.generate_key()
    store = ControlStore(tmp_path / "private.db", encryption_key=key)
    receipt = store.admit(request)
    with sqlite3.connect(tmp_path / "private.db") as db:
        raw = db.execute("select request_payload from control_reviews").fetchone()[0]
    assert request.plan.objective not in raw
    assert store.request(receipt["review_id"], *scope(request)).as_of is not None
    wrong = ControlStore(tmp_path / "private.db", encryption_key=Fernet.generate_key())
    with pytest.raises(IntegrityError): wrong.request(receipt["review_id"], *scope(request))


@pytest.mark.parametrize("target", ["request", "event", "tail", "result", "revision"])
def test_storage_tamper_fails_closed(tmp_path, target):
    path = tmp_path / "reviews.db"
    store = ControlStore(path)
    request = campaign()
    receipt = store.admit(request)
    ControlRuntime(store).run_once()
    with sqlite3.connect(path) as db:
        if target == "request": db.execute("update control_reviews set request_digest='bad'")
        if target == "event": db.execute("delete from control_events where sequence=2")
        if target == "tail": db.execute("delete from control_events where sequence=(select max(sequence) from control_events)")
        if target == "result": db.execute("update control_reviews set result_digest='bad'")
        if target == "revision": db.execute("update control_reviews set revision=9")
    with pytest.raises((IntegrityError, ProofRejected)):
        if target == "request": store.request(receipt["review_id"], *scope(request))
        else: store.proof(receipt["review_id"], *scope(request))


@pytest.mark.skipif(not os.getenv("OFC_TEST_POSTGRES_URL"), reason="isolated PostgreSQL service not configured")
def test_postgres_queue_cas_restart_and_private_schema():
    from uuid import uuid4
    import psycopg
    dsn = os.environ["OFC_TEST_POSTGRES_URL"]
    with psycopg.connect(dsn) as db:
        db.execute("do $$ begin if not exists (select from pg_roles where rolname='control_untrusted') then create role control_untrusted; end if; end $$")
    key = Fernet.generate_key()
    store = ControlStore(dsn, encryption_key=key, initialize=True)
    request = campaign(tenant="pg-" + uuid4().hex)
    receipt = store.admit(request)
    runtime = ControlRuntime(store)
    while store.snapshot(receipt["review_id"], *scope(request))["state"] != "FINISHED":
        assert runtime.run_once()
    assert ControlStore(dsn, encryption_key=key).proof(receipt["review_id"], *scope(request))
    with psycopg.connect(dsn) as db:
        assert db.execute("select has_schema_privilege('control_untrusted','ofc_control','USAGE')").fetchone() == (False,)
