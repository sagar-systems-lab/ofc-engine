import pytest
from ofc_engine.contracts import RepairPatch
from ofc_engine.revisions import apply_patch, plan_digest,PatchRejected,RevisionConflict

def patch(plan,*,new="brand-b",old="brand-a",rev=None,digest=None):
    return RepairPatch(patch_id="p1",decision_id="brand",base_revision=plan.revision if rev is None else rev,old_candidate_id=old,new_candidate_id=new,precondition_digest=plan_digest(plan) if digest is None else digest,evidence_refs=("SYNTHETIC:e1",))

def test_selective_repair_and_dependents(plan):
    next_plan,affected=apply_patch(plan,patch(plan))
    assert next_plan.revision==1
    assert affected==('brand','afterparty')
    assert next_plan.decisions[0].candidate_id=="brand-b"
    assert next_plan.decisions[1:]==plan.decisions[1:]
    assert next_plan.baseline_digest==plan.baseline_digest

def test_stale_revision_rejected(plan):
    with pytest.raises(RevisionConflict):apply_patch(plan,patch(plan,rev=1))

def test_stale_digest_rejected(plan):
    with pytest.raises(RevisionConflict):apply_patch(plan,patch(plan,digest='0'*64))

def test_outside_pool_rejected(plan):
    with pytest.raises(PatchRejected):apply_patch(plan,patch(plan,new="brand-z"))

def test_old_candidate_mismatch_rejected(plan):
    with pytest.raises(RevisionConflict):apply_patch(plan,patch(plan,new="brand-a",old="brand-b"))

def test_immutable_decision_rejected(plan):
    p=RepairPatch(patch_id="p2",decision_id="venue",base_revision=0,old_candidate_id="venue-a",new_candidate_id="venue-b",precondition_digest=plan_digest(plan),evidence_refs=("SYNTHETIC:e1",))
    with pytest.raises(PatchRejected):apply_patch(plan,p)
