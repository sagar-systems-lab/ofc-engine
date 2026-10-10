import pytest
from ofc_engine.store import MemoryRevisionStore,IdempotencyConflict
from ofc_engine.revisions import RevisionConflict,plan_digest
from ofc_engine.contracts import RepairPatch


def patch(plan, *, new="brand-b", patch_id="p1"):
    return RepairPatch(
        patch_id=patch_id, decision_id="brand", base_revision=plan.revision,
        old_candidate_id="brand-a", new_candidate_id=new,
        precondition_digest=plan_digest(plan), evidence_refs=("SYNTHETIC:e1",),
    )


def test_atomic_patch_and_idempotency(plan):
    store = MemoryRevisionStore()
    store.insert(plan)
    cmd = patch(plan)
    updated, invalidated = store.apply_patch_once(plan.plan_id, cmd, client_request_id="request1")
    assert updated.revision == 1
    assert invalidated == ("brand", "afterparty")
    assert store.get(plan.plan_id) == updated
    assert store.apply_patch_once(plan.plan_id, cmd, client_request_id="request1") == (updated, invalidated)
    with pytest.raises(IdempotencyConflict):
        store.apply_patch_once(plan.plan_id, patch(plan, patch_id="other"), client_request_id="request1")
    with pytest.raises(RevisionConflict):
        store.apply_patch_once(plan.plan_id, cmd, client_request_id="request2")


def test_rejected_patch_does_not_commit(plan):
    store = MemoryRevisionStore()
    store.insert(plan)
    with pytest.raises(Exception):
        store.apply_patch_once(plan.plan_id, patch(plan, new="outside-pool"), client_request_id="k1")
    assert store.get(plan.plan_id) == plan


def test_duplicate_insert_rejected(plan):
    store = MemoryRevisionStore()
    store.insert(plan)
    with pytest.raises(RevisionConflict):
        store.insert(plan)


def test_missing_request_id_rejected(plan):
    store = MemoryRevisionStore()
    store.insert(plan)
    with pytest.raises(ValueError):
        store.apply_patch_once(plan.plan_id, patch(plan), client_request_id="")


def test_concurrent_patches_have_one_winner(plan):
    from concurrent.futures import ThreadPoolExecutor
    store = MemoryRevisionStore()
    store.insert(plan)
    command = patch(plan)
    def attempt(request_id):
        try:
            return store.apply_patch_once(plan.plan_id, command, client_request_id=request_id)[0].revision
        except RevisionConflict:
            return "STALE"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ("race-a", "race-b")))
    assert results.count(1) == 1 and results.count("STALE") == 1
    assert store.get(plan.plan_id).revision == 1
