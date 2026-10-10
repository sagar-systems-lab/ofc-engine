from datetime import datetime,timezone,timedelta
import pytest
from pydantic import ValidationError
from ofc_engine.contracts import Decision,PlanSnapshot,QlooEvidence,DecisionVerdict,ReleaseArtifact,SourceMode,EvidenceStatus,Verdict
from ofc_engine.revisions import digest_obj,plan_digest,affected_dependents

def test_plan_valid_and_digest_deterministic(plan):
    assert len(plan.decisions)==3
    assert plan_digest(plan)==plan_digest(plan)

def test_unknown_keys_rejected(plan):
    p=plan.model_dump()
    p["secret_override"]="allow"
    with pytest.raises(ValidationError): PlanSnapshot.model_validate(p)

def test_duplicate_ids_rejected(plan):
    with pytest.raises(ValidationError): PlanSnapshot.model_validate({**plan.model_dump(),"decisions":[plan.decisions[0].model_dump(),plan.decisions[0].model_dump()]})

def test_unknown_dependency_rejected(plan):
    xs=[d.model_dump() for d in plan.decisions]
    xs[0]["depends_on"]=("missing",)
    with pytest.raises(ValidationError):PlanSnapshot.model_validate({**plan.model_dump(),"decisions":xs})

def test_cycle_rejected(plan):
    xs=[d.model_dump() for d in plan.decisions]
    xs[0]["depends_on"]=("afterparty",)
    with pytest.raises(ValidationError):PlanSnapshot.model_validate({**plan.model_dump(),"decisions":xs})

def test_outside_candidate_pool_rejected():
    with pytest.raises(ValidationError):Decision(decision_id="brand",domain="x",candidate_id="other",candidate_pool=("a",))

def test_unallowed_field_rejected():
    with pytest.raises(ValidationError):Decision(decision_id="brand",domain="x",candidate_id="a",candidate_pool=("a",),mutable_fields=("budget",))

def test_dependency_closure(plan):
    assert affected_dependents(plan,"brand")==('brand','afterparty')
    assert affected_dependents(plan,"venue")==('venue',)

def test_pass_requires_evidence():
    with pytest.raises(ValidationError):DecisionVerdict(decision_id="d",plan_revision=0,verdict=Verdict.PASS,reason_codes=("OK",),evidence_refs=(),policy_version="p1")

def test_timezone_required_on_evidence():
    with pytest.raises(ValidationError): QlooEvidence(evidence_id="e",source_mode=SourceMode.SYNTHETIC,status=EvidenceStatus.VALID,query_fingerprint='a'*64,collected_at=datetime.now(),candidate_ranks={})

def test_evidence_expiry_validation():
    t=datetime.now(timezone.utc)
    with pytest.raises(ValidationError): QlooEvidence(evidence_id="e",source_mode=SourceMode.SYNTHETIC,status=EvidenceStatus.VALID,query_fingerprint='a'*64,collected_at=t,expires_at=t-timedelta(seconds=1),candidate_ranks={})

def test_release_never_claims_side_effect():
    with pytest.raises(ValidationError):ReleaseArtifact(plan_id="p",revision=0,verified_plan_digest='a'*64,verification_status='VERIFIED',source_mode=SourceMode.SYNTHETIC,external_action_executed=True)


def test_synthetic_release_cannot_claim_verified():
    with pytest.raises(ValidationError):
        ReleaseArtifact(
            plan_id="p", revision=0, verified_plan_digest="a" * 64,
            verification_status="VERIFIED", source_mode=SourceMode.SYNTHETIC,
        )
