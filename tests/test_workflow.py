from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from ofc_engine.contracts import (
    Decision, EvidenceQuerySpec, EvidenceStatus, PlanSnapshot, QlooEvidence,
    SourceMode, Verdict,
)
from ofc_engine.proof import EvidenceBinding
from ofc_engine.revisions import plan_digest
from ofc_engine.workflow import (
    WorkflowState, compare_fixture_arms, evaluate_fixture, run_fixture,
)

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
AGE = timedelta(hours=1)


@dataclass
class Source:
    ranks: dict[str, dict[str, int]]
    status: EvidenceStatus = EvidenceStatus.VALID
    collected_at: datetime = NOW-timedelta(minutes=5)
    mode: SourceMode = SourceMode.SYNTHETIC
    fail: bool = False
    calls: int = 0

    def fetch(self, plan):
        self.calls += 1
        if self.fail:
            raise TimeoutError('simulated outage')
        return tuple(EvidenceBinding(
            decision_id=d.decision_id,
            query=EvidenceQuerySpec(
                query_id='q-'+d.decision_id, capability='test-fixture',
                input_entity_ids=('artist',), candidate_ids=d.candidate_pool,
                locality_ref=d.locality_ref, request_fingerprint='a'*64,
            ),
            evidence=QlooEvidence(
                evidence_id=f'synthetic:{d.decision_id}:r{plan.revision}',
                source_mode=self.mode, status=self.status,
                query_fingerprint='a'*64,
                collected_at=self.collected_at,
                expires_at=NOW+timedelta(minutes=30),
                candidate_ranks=self.ranks.get(d.decision_id, {d.candidate_id: 1}),
            ),
        ) for d in plan.decisions)


def source_for(plan):
    return Source({d.decision_id: {name: idx+1 for idx, name in enumerate(d.candidate_pool)} for d in plan.decisions})


def run(plan, source=None, **kwargs):
    return run_fixture(plan, source or source_for(plan), as_of=NOW, max_evidence_age=AGE, **kwargs)


def test_fixture_pass_never_grants_external_release(plan):
    out=run(plan)
    assert out.status is WorkflowState.INTEGRITY_ONLY
    assert out.plan == plan
    assert out.plan.revision==0
    assert out.release_authorized is False and out.external_action_executed is False
    assert len(out.traces) == 0 and len(out.reviews) == 1


def test_selective_repair_and_dependent_recheck(plan):
    source=source_for(plan)
    source.ranks['brand']={'brand-a':2, 'brand-b':1}
    out=run(plan,source)
    assert out.status is WorkflowState.INTEGRITY_ONLY
    assert out.plan.revision==1 and out.ledger.attempts_used==1
    assert out.ledger.pending_recheck_ids==()
    assert [d.candidate_id for d in out.plan.decisions]==['brand-b','exp-a','venue-a']
    assert out.plan.baseline_digest==plan.baseline_digest
    assert out.plan.constraints_digest==plan.constraints_digest
    assert source.calls==2 and len(out.traces)==1
    trace=out.traces[0]
    assert trace.affected_decision_ids==('brand','afterparty')
    assert trace.recheck_state.value=='STRUCTURAL_PASS'
    assert trace.release_authorized is False


def test_off_on_uses_exact_frozen_baseline(plan):
    source=source_for(plan)
    source.ranks['brand']={'brand-a':2,'brand-b':1}
    a=compare_fixture_arms(plan,source,as_of=NOW,max_evidence_age=AGE)
    assert a.identical_baseline and not a.real_causal_proof
    assert a.disabled.status is WorkflowState.USER_REVIEW
    assert a.disabled.plan==plan and a.disabled.plan.revision==0
    assert all(v.verdict is Verdict.UNCERTAIN for v in a.disabled.reviews[0].verdicts)
    assert a.enabled.status is WorkflowState.INTEGRITY_ONLY
    assert a.enabled.plan.revision==1
    assert source.calls==2 # OFF never queries provider
    assert a.baseline_digest==plan_digest(plan)


def test_unavailable_provider_never_fakes_pass(plan):
    out=run(plan,Source({},fail=True))
    assert out.status is WorkflowState.USER_REVIEW
    assert out.plan.revision==0
    assert out.reviews[0].verdicts[0].verdict is Verdict.UNCERTAIN
    assert out.reason_codes==('INSUFFICIENT_SYNTHETIC_EVIDENCE',)


@pytest.mark.parametrize('status', [EvidenceStatus.UNAVAILABLE,EvidenceStatus.INVALID,EvidenceStatus.INSUFFICIENT,EvidenceStatus.STALE])
def test_bad_evidence_is_uncertain(plan,status):
    src=source_for(plan);src.status=status
    assert run(plan,src).status is WorkflowState.USER_REVIEW


def test_cross_mode_source_never_treated_as_trusted(plan):
    src=source_for(plan);src.mode=SourceMode.VERIFIED_LIVE
    out=run(plan,src)
    assert out.status is WorkflowState.USER_REVIEW
    assert out.source_mode==SourceMode.SYNTHETIC
    assert all(v.verdict is Verdict.UNCERTAIN for v in out.reviews[0].verdicts)


def test_rank_incomplete_and_duplicate_rank_returns_uncertain(plan):
    src=source_for(plan);src.ranks['brand']={'brand-a':1}
    assert run(plan,src).status is WorkflowState.USER_REVIEW
    src.ranks['brand']={'brand-a':1,'brand-b':1}
    assert run(plan,src).status is WorkflowState.USER_REVIEW


def test_evidence_from_future_and_old_are_uncertain(plan):
    src=source_for(plan);src.collected_at=NOW+timedelta(seconds=1)
    assert run(plan,src).status is WorkflowState.USER_REVIEW
    src.collected_at=NOW-timedelta(days=3)
    assert run(plan,src).status is WorkflowState.USER_REVIEW


def test_missing_evidence_never_fakes_pass(plan):
    src=source_for(plan)
    src.ranks.pop('brand')
    # Source supplies default one candidate ranking; incomplete brand -> uncertain
    assert run(plan,src).status is WorkflowState.USER_REVIEW


def test_immutable_repair_requires_review(plan):
    src=source_for(plan)
    # current venue single-choice can't have alternative. Make venue two-choice immutable.
    xs=list(plan.decisions)
    xs[2]=xs[2].model_copy(update={'candidate_pool':('venue-a','venue-b')})
    new_plan=PlanSnapshot.model_validate({**plan.model_dump(), 'decisions':tuple(d.model_dump() for d in xs)})
    src.ranks['venue']={'venue-a':2,'venue-b':1}
    result=run(new_plan,src)
    assert result.status is WorkflowState.USER_REVIEW
    assert result.reason_codes==('IMMUTABLE_DECISION_NEEDS_REPAIR',)


def test_exhausted_budget_keeps_last_revision_unreleased(plan):
    source=source_for(plan)
    source.ranks['brand']={'brand-a':2,'brand-b':1}
    # A source that alternates rank order to provoke a second repair attempt.
    class FlipSource(Source):
        def fetch(self, current):
            self.ranks['brand']={'brand-a':2,'brand-b':1} if current.revision==0 else {'brand-a':1,'brand-b':2}
            return super().fetch(current)
    out=run(plan, FlipSource(source.ranks), max_attempts=1)
    assert out.status is WorkflowState.REPAIR_EXHAUSTED
    assert out.plan.revision==1 and not out.release_authorized
    assert out.reason_codes==('REPAIR_BUDGET_EXHAUSTED',)


def test_missing_dependent_fresh_evidence_blocks_result(plan):
    source=source_for(plan)
    source.ranks['brand']={'brand-a':2,'brand-b':1}
    class BreakAfterRepair(Source):
        def fetch(self, current):
            rows=super().fetch(current)
            if current.revision:
                return tuple(item for item in rows if item.decision_id!='afterparty')
            return rows
    out=run(plan, BreakAfterRepair(source.ranks))
    assert out.status is WorkflowState.USER_REVIEW
    assert out.ledger.pending_recheck_ids==('brand','afterparty')
    assert out.traces[0].recheck_state.value=='BLOCKED'


def test_invalid_clock_and_attempts_rejected(plan):
    with pytest.raises(ValueError):
        run_fixture(plan,source_for(plan),as_of=NOW.replace(tzinfo=None),max_evidence_age=AGE)
    with pytest.raises(ValueError):
        run_fixture(plan,source_for(plan),as_of=NOW,max_evidence_age=timedelta(0))
    with pytest.raises(ValueError):
        run(plan, max_attempts=0)


def test_fixture_evaluator_is_deterministic_and_pure(plan):
    src=source_for(plan)
    a=src.fetch(plan)
    one=evaluate_fixture(plan,a,as_of=NOW,max_evidence_age=AGE)
    two=evaluate_fixture(plan,a,as_of=NOW,max_evidence_age=AGE)
    assert one==two and one.plan_digest==plan_digest(plan)


def test_full_deterministic_replay(plan):
    s=source_for(plan);s.ranks['brand']={'brand-a':2,'brand-b':1}
    a=run(plan,s)
    b=run(plan,s)
    assert a==b
    assert a.ledger.max_attempts==2 and a.ledger.attempts_used==1


def test_unknown_extra_binding_fails_closed(plan):
    source=source_for(plan)
    rows=source.fetch(plan)
    bogus=rows[0].model_copy(update={'decision_id':'unknown'})
    result=evaluate_fixture(plan,rows+(bogus,),as_of=NOW,max_evidence_age=AGE)
    assert all(v.verdict is Verdict.UNCERTAIN for v in result.verdicts)


def test_provenance_fingerprint_mismatch_fails_closed(plan):
    source=source_for(plan)
    rows=list(source.fetch(plan))
    rows[0]=rows[0].model_copy(update={'query': rows[0].query.model_copy(update={'request_fingerprint':'0'*64})})
    result=evaluate_fixture(plan,tuple(rows),as_of=NOW,max_evidence_age=AGE)
    assert result.verdicts[0].verdict is Verdict.UNCERTAIN


def test_duplicate_binding_fails_closed(plan):
    source=source_for(plan)
    rows=source.fetch(plan)
    result=evaluate_fixture(plan,rows+(rows[0],),as_of=NOW,max_evidence_age=AGE)
    assert result.verdicts[0].verdict is Verdict.UNCERTAIN


def test_report_is_not_editable_to_claim_release(plan):
    from dataclasses import FrozenInstanceError
    result=run(plan)
    with pytest.raises(FrozenInstanceError):
        result.release_authorized=True
    with pytest.raises(FrozenInstanceError):
        result.external_action_executed=True
    a=compare_fixture_arms(plan,source_for(plan),as_of=NOW,max_evidence_age=AGE)
    with pytest.raises(FrozenInstanceError):
        a.real_causal_proof=True


def test_multiple_independent_repairs_budgeted_and_ordered(plan):
    from ofc_engine.contracts import Decision
    src=source_for(plan)
    ds=list(plan.decisions)
    ds[2]=Decision(
        decision_id='venue',domain='venue',candidate_id='venue-a',
        candidate_pool=('venue-a','venue-b'),mutable_fields=('candidate_id',),
    )
    new=PlanSnapshot.model_validate({**plan.model_dump(),'decisions':tuple(x.model_dump() for x in ds)})
    src.ranks.update({'brand':{'brand-a':2,'brand-b':1}, 'venue':{'venue-a':2,'venue-b':1}})
    result=run(new,src,max_attempts=2)
    assert result.status is WorkflowState.INTEGRITY_ONLY
    assert result.ledger.attempts_used==2
    assert result.plan.revision==2
    assert [d.candidate_id for d in result.plan.decisions]==['brand-b','exp-a','venue-b']
    assert len(result.traces)==2 and len(result.reviews)==3
    assert result.ledger.pending_recheck_ids==()
    assert [t.changed_decision_id for t in result.traces]==['brand','venue']


def test_incomplete_second_revision_both_arms_never_authorize(plan):
    src=source_for(plan)
    src.ranks['brand']={'brand-a':2,'brand-b':1}
    class UnavailableAfterFirst(Source):
        def fetch(self,current):
            if current.revision>0:
                raise ConnectionError('provider down')
            return super().fetch(current)
    res=run(plan,UnavailableAfterFirst(src.ranks))
    assert res.status is WorkflowState.USER_REVIEW
    assert res.plan.revision==1
    assert res.ledger.pending_recheck_ids==('brand','afterparty')
    assert res.traces[0].recheck_state.value=='BLOCKED'
    assert not res.release_authorized


def test_runnable_synthetic_demo_is_repeatable_and_not_live():
    import json
    import subprocess
    import sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    cmd=[sys.executable,str(root/'examples/synthetic_engine_run.py')]
    a=subprocess.check_output(cmd,cwd=root,text=True)
    b=subprocess.check_output(cmd,cwd=root,text=True)
    assert a==b
    d=json.loads(a)
    assert d['data_origin']=='SYNTHETIC_FIXTURES_ONLY'
    assert d['real_qloo_requests']==0
    assert d['same_frozen_baseline']
    assert d['without_evidence']['status']=='USER_REVIEW'
    assert d['with_fixture_evidence']['status']=='INTEGRITY_ONLY'
    assert d['with_fixture_evidence']['decisions']['brand']=='brand-b'
    assert d['with_fixture_evidence']['decisions']['afterparty']=='party-a'
    assert d['with_fixture_evidence']['repair_trace'][0]['recheck_state']=='STRUCTURAL_PASS'
    assert d['real_causal_proof'] is False
    assert d['release_authorized'] is False
    assert d['external_action_executed'] is False
