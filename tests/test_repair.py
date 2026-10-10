"""Fixture-only checks: previews cannot authenticate evidence or release actions."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ofc_engine.contracts import (
    DecisionVerdict, EvidenceQuerySpec, EvidenceStatus, QlooEvidence,
    RepairPatch, SourceMode, Verdict,
)
from ofc_engine.proof import EvidenceBinding, ReviewEnvelope
from ofc_engine.repair import (
    RepairBudgetExceeded, RepairReviewRejected, begin_repair_ledger,
    preview_repair, record_recheck_integrity,
)
from ofc_engine.revisions import PatchRejected, RevisionConflict, plan_digest

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
AGE = timedelta(hours=1)


def review(plan, *, repair_id=None, unranked_replacement=False):
    bindings, verdicts = [], []
    for i, decision in enumerate(plan.decisions):
        fid = str(i+1) * 64
        ranks = {candidate: index+1 for index, candidate in enumerate(decision.candidate_pool)}
        if unranked_replacement and decision.decision_id == 'brand':
            ranks.pop('brand-b', None)
        eid = f'e-{decision.decision_id}-r{plan.revision}'
        bindings.append(EvidenceBinding(
            decision_id=decision.decision_id,
            query=EvidenceQuerySpec(
                query_id=f'q-{decision.decision_id}-r{plan.revision}',
                capability='fixture-only',
                input_entity_ids=('artist-id',),
                candidate_ids=decision.candidate_pool,
                locality_ref=decision.locality_ref,
                request_fingerprint=fid,
            ),
            evidence=QlooEvidence(
                evidence_id=eid, source_mode=SourceMode.SYNTHETIC,
                status=EvidenceStatus.VALID,
                query_fingerprint=fid,
                collected_at=NOW-timedelta(minutes=5),
                expires_at=NOW+timedelta(minutes=10),
                candidate_ranks=ranks,
            ),
        ))
        verdicts.append(DecisionVerdict(
            decision_id=decision.decision_id,
            plan_revision=plan.revision,
            verdict=Verdict.REPAIR if decision.decision_id == repair_id else Verdict.PASS,
            reason_codes=('FIXTURE_ONLY',),
            evidence_refs=(eid,),
            policy_version='fixture-v1',
        ))
    return ReviewEnvelope(
        plan_id=plan.plan_id, plan_revision=plan.revision,
        plan_digest=plan_digest(plan), source_mode=SourceMode.SYNTHETIC,
        policy_version='fixture-v1', verdicts=tuple(verdicts),
        bindings=tuple(bindings),
    )


def patch(plan, *, target='brand', new='brand-b', evidence=None):
    return RepairPatch(
        patch_id=f'p-{plan.revision}', decision_id=target,
        base_revision=plan.revision,
        old_candidate_id=next(d.candidate_id for d in plan.decisions if d.decision_id == target),
        new_candidate_id=new,
        precondition_digest=plan_digest(plan),
        evidence_refs=(evidence or f'e-{target}-r{plan.revision}',),
    )


def preview(plan, ledger, *, review_override=None, patch_override=None):
    return preview_repair(
        plan, ledger, review_override or review(plan, repair_id='brand'),
        patch_override or patch(plan), as_of=NOW, max_evidence_age=AGE,
    )


def test_first_repair_is_selective_and_binds_recheck(plan):
    ledger = begin_repair_ledger(plan)
    result = preview(plan, ledger)
    assert result.proposed_plan.revision == 1
    assert [d.candidate_id for d in result.proposed_plan.decisions] == ['brand-b', 'exp-a', 'venue-a']
    assert result.proposed_plan.baseline_digest == plan.baseline_digest
    assert result.proposed_plan.constraints_digest == plan.constraints_digest
    assert result.affected_decision_ids == ('brand', 'afterparty')
    assert result.ledger.pending_recheck_ids == ('brand', 'afterparty')
    assert result.ledger.attempts_used == 1
    assert ledger.attempts_used == plan.revision == 0
    assert not hasattr(result, 'release_artifact')


def test_no_repair_for_pass_or_uncertain(plan):
    ledger = begin_repair_ledger(plan)
    with pytest.raises(RepairReviewRejected, match='target decision'):
        preview(plan, ledger, review_override=review(plan))
    r = review(plan, repair_id='brand')
    verdicts = list(r.verdicts)
    verdicts[0] = verdicts[0].model_copy(update={'verdict': Verdict.UNCERTAIN})
    with pytest.raises(RepairReviewRejected):
        preview(plan, ledger, review_override=r.model_copy(update={'verdicts': tuple(verdicts)}))


def test_no_repair_with_unresolved_unrelated_decision(plan):
    r = review(plan, repair_id='brand')
    vs = list(r.verdicts)
    vs[-1] = vs[-1].model_copy(update={'verdict': Verdict.UNCERTAIN})
    with pytest.raises(RepairReviewRejected, match='UNCERTAIN_DECISION'):
        preview(plan, begin_repair_ledger(plan), review_override=r.model_copy(update={'verdicts': tuple(vs)}))


def test_patch_evidence_must_match_target_verdict(plan):
    with pytest.raises(RepairReviewRejected, match='patch evidence differs'):
        preview(plan, begin_repair_ledger(plan), patch_override=patch(plan, evidence='e-venue-r0'))


def test_replacement_needs_direct_ranking_evidence(plan):
    with pytest.raises(RepairReviewRejected, match='no bound ranking'):
        preview(plan, begin_repair_ledger(plan), review_override=review(plan, repair_id='brand', unranked_replacement=True))


def test_invalid_evidence_blocks_preview(plan):
    r = review(plan, repair_id='brand')
    b = list(r.bindings)
    b[0] = b[0].model_copy(update={'evidence': b[0].evidence.model_copy(update={'status': EvidenceStatus.INVALID})})
    with pytest.raises(RepairReviewRejected, match='EVIDENCE_NOT_VALID'):
        preview(plan, begin_repair_ledger(plan), review_override=r.model_copy(update={'bindings': tuple(b)}))


def test_mismatched_fingerprint_blocks_preview(plan):
    r = review(plan, repair_id='brand')
    b = list(r.bindings)
    b[0] = b[0].model_copy(update={'query': b[0].query.model_copy(update={'request_fingerprint': 'f'*64})})
    with pytest.raises(RepairReviewRejected, match='QUERY_FINGERPRINT_MISMATCH'):
        preview(plan, begin_repair_ledger(plan), review_override=r.model_copy(update={'bindings': tuple(b)}))


def test_expired_evidence_blocks_preview(plan):
    r = review(plan, repair_id='brand')
    b = list(r.bindings)
    b[0] = b[0].model_copy(update={'evidence': b[0].evidence.model_copy(update={'expires_at': NOW})})
    with pytest.raises(RepairReviewRejected, match='EVIDENCE_EXPIRED'):
        preview(plan, begin_repair_ledger(plan), review_override=r.model_copy(update={'bindings': tuple(b)}))


def test_revision_and_digest_cas_preconditions(plan):
    ledger = begin_repair_ledger(plan)
    with pytest.raises(RepairReviewRejected, match='ledger does not match'):
        preview(plan, replace(ledger, plan_revision=3))
    with pytest.raises(RepairReviewRejected, match='ledger does not match'):
        preview(plan, replace(ledger, plan_digest='0'*64))
    with pytest.raises(RepairReviewRejected, match='STALE_PLAN_REVISION'):
        r = review(plan, repair_id='brand')
        preview(plan, ledger, review_override=r.model_copy(update={'plan_revision': 1}))
    with pytest.raises(RevisionConflict):
        preview(plan, ledger, patch_override=patch(plan).model_copy(update={'base_revision': 3}))


def test_candidate_pool_and_immutable_decisions_preserved(plan):
    with pytest.raises(RepairReviewRejected, match='not queried'):
        preview(plan, begin_repair_ledger(plan), patch_override=patch(plan, new='brand-z'))
    with pytest.raises(RepairReviewRejected, match='target decision'):
        preview(plan, begin_repair_ledger(plan), patch_override=patch(plan, target='venue', new='venue-b'))


def test_budget_exhaustion_is_fail_closed(plan):
    # One attempt only; ledger can't be reset by this operation.
    a = preview(plan, begin_repair_ledger(plan, max_attempts=1))
    with pytest.raises(RepairBudgetExceeded):
        preview(a.proposed_plan, a.ledger, review_override=review(a.proposed_plan, repair_id='brand'),
                patch_override=patch(a.proposed_plan, new='brand-a'))


def test_second_repair_requires_all_pending_rechecks(plan):
    first = preview(plan, begin_repair_ledger(plan))
    updated = first.proposed_plan
    r = review(updated, repair_id='brand')
    vs = tuple(v for v in r.verdicts if v.decision_id != 'afterparty')
    with pytest.raises(RepairReviewRejected, match='MISSING_REQUIRED_VERDICT'):
        preview(updated, first.ledger, review_override=r.model_copy(update={'verdicts': vs}),
                patch_override=patch(updated, new='brand-a'))


def test_second_repair_with_rechecks_hits_limit(plan):
    first = preview(plan, begin_repair_ledger(plan))
    second = preview(first.proposed_plan, first.ledger,
                     review_override=review(first.proposed_plan, repair_id='brand'),
                     patch_override=patch(first.proposed_plan, new='brand-a'))
    assert second.ledger.attempts_used == 2
    assert second.ledger.pending_recheck_ids == ('brand', 'afterparty')
    with pytest.raises(RepairBudgetExceeded):
        preview(second.proposed_plan, second.ledger,
                review_override=review(second.proposed_plan, repair_id='brand'),
                patch_override=patch(second.proposed_plan, new='brand-b'))


def test_record_recheck_requires_fresh_pass_review(plan):
    first = preview(plan, begin_repair_ledger(plan))
    with pytest.raises(RepairReviewRejected, match='STALE_PLAN_REVISION'):
        record_recheck_integrity(first.proposed_plan, first.ledger, review(plan),
                                 as_of=NOW, max_evidence_age=AGE)
    with pytest.raises(RepairReviewRejected, match='REPAIR_REQUIRED'):
        record_recheck_integrity(first.proposed_plan, first.ledger,
                                 review(first.proposed_plan, repair_id='brand'),
                                 as_of=NOW, max_evidence_age=AGE)
    cleared = record_recheck_integrity(first.proposed_plan, first.ledger,
                                       review(first.proposed_plan), as_of=NOW,
                                       max_evidence_age=AGE)
    assert cleared.pending_recheck_ids == ()
    assert cleared.attempts_used == 1
    assert first.ledger.pending_recheck_ids == ('brand', 'afterparty')


def test_no_recheck_to_record_before_repair(plan):
    with pytest.raises(RepairReviewRejected, match='no pending'):
        record_recheck_integrity(plan, begin_repair_ledger(plan), review(plan),
                                 as_of=NOW, max_evidence_age=AGE)


@pytest.mark.parametrize('budget', [0, -1, True, 1.5, '3'])
def test_budget_must_be_positive_integer(plan, budget):
    with pytest.raises(ValueError, match='positive integer'):
        begin_repair_ledger(plan, max_attempts=budget)


def test_tampered_pending_rechecks_fail_closed(plan):
    ledger = begin_repair_ledger(plan)
    with pytest.raises(RepairReviewRejected, match='duplicate pending'):
        preview(plan, replace(ledger, pending_recheck_ids=('brand','brand')))
    with pytest.raises(RepairReviewRejected, match='unknown decision'):
        preview(plan, replace(ledger, pending_recheck_ids=('unknown',)))


def test_out_of_scope_verdicts_fail_closed(plan):
    r = review(plan, repair_id='brand')
    vs = list(r.verdicts)
    vs[1] = vs[1].model_copy(update={'policy_version': 'other'})
    with pytest.raises(RepairReviewRejected, match='POLICY_VERSION_MISMATCH'):
        preview(plan, begin_repair_ledger(plan), review_override=r.model_copy(update={'verdicts': tuple(vs)}))
