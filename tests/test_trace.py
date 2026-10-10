"""The trace is revision-bound and cannot imply provider or release authority."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ofc_engine.contracts import (
    DecisionVerdict, EvidenceQuerySpec, EvidenceStatus, QlooEvidence,
    RepairPatch, SourceMode, Verdict,
)
from ofc_engine.proof import EvidenceBinding, ReviewEnvelope
from ofc_engine.repair import (
    RepairReviewRejected, begin_repair_ledger, preview_repair,
)
from ofc_engine.revisions import plan_digest
from ofc_engine.trace import RecheckState, build_repair_trace

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
AGE = timedelta(hours=1)


def synthetic_review(plan, *, needs_repair=False):
    bs, vs = [], []
    for i, d in enumerate(plan.decisions):
        eid = f'e-{d.decision_id}-r{plan.revision}'
        fingerprint = str(i + 1) * 64
        bs.append(EvidenceBinding(
            decision_id=d.decision_id,
            query=EvidenceQuerySpec(
                query_id='q-'+d.decision_id, capability='synthetic-test',
                input_entity_ids=('artist',), candidate_ids=d.candidate_pool,
                locality_ref=d.locality_ref, request_fingerprint=fingerprint,
            ),
            evidence=QlooEvidence(
                evidence_id=eid, status=EvidenceStatus.VALID,
                source_mode=SourceMode.SYNTHETIC, query_fingerprint=fingerprint,
                collected_at=NOW-timedelta(minutes=5),
                expires_at=NOW+timedelta(minutes=30),
                candidate_ranks={c: i+1 for i, c in enumerate(d.candidate_pool)},
            ),
        ))
        vs.append(DecisionVerdict(
            decision_id=d.decision_id, plan_revision=plan.revision,
            verdict=Verdict.REPAIR if needs_repair and d.decision_id == 'brand' else Verdict.PASS,
            reason_codes=('SYNTHETIC_ONLY',), evidence_refs=(eid,),
            policy_version='fixture-v1',
        ))
    return ReviewEnvelope(
        plan_id=plan.plan_id, plan_revision=plan.revision,
        plan_digest=plan_digest(plan), source_mode=SourceMode.SYNTHETIC,
        policy_version='fixture-v1', verdicts=tuple(vs), bindings=tuple(bs),
    )


def state(plan):
    ledger = begin_repair_ledger(plan)
    initial = synthetic_review(plan, needs_repair=True)
    patch = RepairPatch(
        patch_id='p', decision_id='brand', base_revision=0,
        old_candidate_id='brand-a', new_candidate_id='brand-b',
        precondition_digest=plan_digest(plan), evidence_refs=('e-brand-r0',),
    )
    proposed = preview_repair(plan, ledger, initial, patch, as_of=NOW, max_evidence_age=AGE)
    return ledger, initial, patch, proposed


def trace(plan, after=None, **changes):
    ledger, initial, patch, preview = state(plan)
    return build_repair_trace(
        plan, changes.get('ledger', ledger), changes.get('initial', initial),
        changes.get('patch', patch), changes.get('preview', preview),
        after_review=after, as_of=NOW, max_evidence_age=AGE,
    )


def edit_verdict(r, id, **kwargs):
    return r.model_copy(update={'verdicts': tuple(
        v.model_copy(update=kwargs) if v.decision_id == id else v for v in r.verdicts
    )})


def edit_binding(r, id, *, evidence_changes=None, query_changes=None):
    bs = []
    for b in r.bindings:
        if b.decision_id == id:
            if evidence_changes:
                b = b.model_copy(update={'evidence': b.evidence.model_copy(update=evidence_changes)})
            if query_changes:
                b = b.model_copy(update={'query': b.query.model_copy(update=query_changes)})
        bs.append(b)
    return r.model_copy(update={'bindings': tuple(bs)})


def test_pending_trace_is_explicitly_non_authoritative(plan):
    out = trace(plan)
    assert out.recheck_state == RecheckState.PENDING
    assert out.issue_codes == ('RECHECK_NOT_SUBMITTED',)
    assert out.pending_recheck_ids == ('brand', 'afterparty')
    assert out.provider_authenticated is False
    assert out.release_authorized is False
    assert out.external_action_executed is False


def test_single_repair_preserves_other_choices_and_digests(plan):
    out = trace(plan)
    d = {v.decision_id: v for v in out.decisions}
    assert d['brand'].changed and d['brand'].requires_recheck
    assert d['brand'].before_candidate_id == 'brand-a'
    assert d['brand'].after_candidate_id == 'brand-b'
    assert not d['afterparty'].changed and d['afterparty'].requires_recheck
    assert not d['venue'].changed and not d['venue'].requires_recheck
    assert d['venue'].before_candidate_id == d['venue'].after_candidate_id == 'venue-a'
    assert out.baseline_digest == plan.baseline_digest
    assert out.constraints_digest == plan.constraints_digest
    assert out.proposed_revision == 1
    assert out.before_plan_digest != out.after_plan_digest
    assert out.attempt == 1 and out.max_attempts == 2


def test_recheck_new_revision_structural_pass_not_real_release(plan):
    _, _, _, proposed = state(plan)
    after = synthetic_review(proposed.proposed_plan)
    out = trace(plan, after)
    assert out.recheck_state == RecheckState.STRUCTURAL_PASS
    assert out.pending_recheck_ids == ()
    assert out.issue_codes == ()
    assert all(d.after_verdict == 'PASS' for d in out.decisions)
    assert not out.release_authorized
    assert not out.provider_authenticated
    assert not out.external_action_executed


def test_stale_old_revision_after_repair_is_blocked(plan):
    out = trace(plan, synthetic_review(plan))
    assert out.recheck_state == RecheckState.BLOCKED
    assert 'STALE_PLAN_REVISION' in out.issue_codes
    assert 'PLAN_DIGEST_MISMATCH' in out.issue_codes
    assert out.pending_recheck_ids == ('brand', 'afterparty')


def test_repair_verdict_in_postcheck_is_blocked(plan):
    _, _, _, proposed = state(plan)
    out = trace(plan, synthetic_review(proposed.proposed_plan, needs_repair=True))
    assert out.recheck_state == RecheckState.BLOCKED
    assert 'REPAIR_REQUIRED' in out.issue_codes


def test_uncertain_recheck_is_blocked(plan):
    _, _, _, proposed = state(plan)
    r = synthetic_review(proposed.proposed_plan)
    out = trace(plan, edit_verdict(r, 'afterparty', verdict=Verdict.UNCERTAIN))
    assert out.recheck_state == RecheckState.BLOCKED
    assert 'UNCERTAIN_DECISION' in out.issue_codes
    assert out.pending_recheck_ids


def test_missing_recheck_verdict_fails_closed(plan):
    _, _, _, proposed = state(plan)
    r = synthetic_review(proposed.proposed_plan)
    r = r.model_copy(update={'verdicts': tuple(v for v in r.verdicts if v.decision_id != 'afterparty')})
    out = trace(plan, r)
    assert 'MISSING_REQUIRED_VERDICT' in out.issue_codes
    assert out.recheck_state == RecheckState.BLOCKED


def test_expired_after_evidence_fails_closed(plan):
    _, _, _, proposed = state(plan)
    r = edit_binding(synthetic_review(proposed.proposed_plan), 'brand', evidence_changes={'expires_at': NOW})
    out = trace(plan, r)
    assert out.recheck_state == RecheckState.BLOCKED
    assert 'EVIDENCE_EXPIRED' in out.issue_codes


def test_cross_decision_binding_fails_closed(plan):
    _, _, _, proposed = state(plan)
    r = synthetic_review(proposed.proposed_plan)
    bs = tuple(b.model_copy(update={'decision_id':'venue'}) if b.decision_id == 'brand' else b for b in r.bindings)
    out = trace(plan, r.model_copy(update={'bindings':bs}))
    assert 'CROSS_DECISION_EVIDENCE' in out.issue_codes


def test_after_review_source_mode_change_blocks(plan):
    _, _, _, proposed = state(plan)
    r = synthetic_review(proposed.proposed_plan)
    out = trace(plan, r.model_copy(update={'source_mode': SourceMode.VERIFIED_LIVE}))
    assert 'SOURCE_MODE_CHANGED' in out.issue_codes
    assert 'SOURCE_MODE_MISMATCH' in out.issue_codes
    assert out.recheck_state == RecheckState.BLOCKED


def test_policy_version_change_blocks(plan):
    _, _, _, proposed = state(plan)
    r = synthetic_review(proposed.proposed_plan)
    r = r.model_copy(update={
        'policy_version': 'fixture-v2',
        'verdicts': tuple(v.model_copy(update={'policy_version':'fixture-v2'}) for v in r.verdicts),
    })
    out = trace(plan, r)
    assert 'POLICY_VERSION_CHANGED' in out.issue_codes


def test_forged_candidate_preview_rejected(plan):
    _, _, _, p = state(plan)
    decisions = list(p.proposed_plan.decisions)
    decisions[-1] = decisions[-1].model_copy(update={'candidate_id':'venue-other'})
    tampered = replace(p, proposed_plan=p.proposed_plan.model_copy(update={'decisions':tuple(decisions)}))
    with pytest.raises(RepairReviewRejected, match='preview does not match'):
        trace(plan, preview=tampered)


def test_forged_attempt_and_invalidations_rejected(plan):
    _, _, _, p = state(plan)
    for tampered in (
        replace(p, affected_decision_ids=('brand',)),
        replace(p, ledger=replace(p.ledger, attempts_used=0)),
        replace(p, ledger=replace(p.ledger, pending_recheck_ids=())),
    ):
        with pytest.raises(RepairReviewRejected, match='preview does not match'):
            trace(plan, preview=tampered)


def test_forged_initial_source_review_rejected(plan):
    _, initial, _, _ = state(plan)
    bad = initial.model_copy(update={'plan_digest':'0'*64})
    with pytest.raises(RepairReviewRejected, match='PLAN_DIGEST_MISMATCH'):
        trace(plan, initial=bad)


def test_trace_json_has_no_success_or_action_claims(plan):
    _, _, _, p = state(plan)
    payload = trace(plan, synthetic_review(p.proposed_plan)).as_dict()
    encoded = json.dumps(payload, sort_keys=True)
    assert payload['qualification'] == 'INTEGRITY_ONLY_NOT_CULTURAL_APPROVAL'
    assert payload['source_mode'] == 'SYNTHETIC'
    assert payload['provider_authenticated'] is False
    assert payload['external_action_executed'] is False
    assert payload['release_authorized'] is False
    assert 'STRUCTURAL_PASS' in encoded
    assert 'VERIFIED_LIVE' not in encoded


def test_pure_idempotent_trace(plan):
    _, _, _, proposed = state(plan)
    r = synthetic_review(proposed.proposed_plan)
    assert trace(plan, r) == trace(plan, r)


def test_replay_checks_patch_candidate(plan):
    _, _, patch, _ = state(plan)
    bad = patch.model_copy(update={'new_candidate_id': 'candidate-not-allowed'})
    with pytest.raises((RepairReviewRejected, ValueError)):
        trace(plan, patch=bad)


def test_fixture_cli_is_deterministic_and_labeled():
    repo = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(repo/'examples'/'synthetic_three_city_trace.py')]
    a = subprocess.check_output(command, text=True, cwd=repo)
    b = subprocess.check_output(command, text=True, cwd=repo)
    assert a == b
    payload = json.loads(a)
    assert payload['data_provenance'] == 'ALL SYNTHETIC; NO LIVE QLOO DATA'
    assert payload['trace']['source_mode'] == 'SYNTHETIC'
    assert payload['trace']['recheck_state'] == 'STRUCTURAL_PASS'
    assert payload['trace']['changed_decision_id'] == 'brand'
    assert payload['trace']['affected_decision_ids'] == ['brand', 'afterparty']
    assert len(payload['trace']['decisions']) == 6
    assert payload['trace']['release_authorized'] is False
