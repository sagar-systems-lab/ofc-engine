"""Evidence integrity is a gate to future policy verification, not a cultural score."""
from datetime import datetime, timedelta, timezone

import pytest

from ofc_engine.contracts import (
    DecisionVerdict,
    EvidenceQuerySpec,
    EvidenceStatus,
    QlooEvidence,
    RepairPatch,
    SourceMode,
    Verdict,
)
from ofc_engine.proof import (
    EvidenceBinding,
    ReviewEnvelope,
    check_review_integrity,
)
from ofc_engine.revisions import apply_patch, plan_digest

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
MAX_AGE = timedelta(hours=24)


def sample(plan):
    bindings = []
    verdicts = []
    for decision in plan.decisions:
        fp = {"brand": "a", "afterparty": "b", "venue": "c"}[decision.decision_id] * 64
        eid = "e-" + decision.decision_id
        bindings.append(EvidenceBinding(
            decision_id=decision.decision_id,
            query=EvidenceQuerySpec(
                query_id="q-" + decision.decision_id,
                capability="affinity",
                input_entity_ids=("artist-a",),
                candidate_ids=decision.candidate_pool,
                locality_ref=decision.locality_ref,
                request_fingerprint=fp,
            ),
            evidence=QlooEvidence(
                evidence_id=eid,
                source_mode=SourceMode.SYNTHETIC,
                status=EvidenceStatus.VALID,
                query_fingerprint=fp,
                collected_at=NOW - timedelta(minutes=5),
                expires_at=NOW + timedelta(hours=1),
                candidate_ranks={decision.candidate_id: 1},
            ),
        ))
        verdicts.append(DecisionVerdict(
            decision_id=decision.decision_id,
            plan_revision=plan.revision,
            verdict=Verdict.PASS,
            reason_codes=("FIXTURE_POLICY_PASS",),
            evidence_refs=(eid,),
            policy_version="synthetic-test-v1",
        ))
    return ReviewEnvelope(
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        plan_digest=plan_digest(plan),
        source_mode=SourceMode.SYNTHETIC,
        policy_version="synthetic-test-v1",
        verdicts=tuple(verdicts),
        bindings=tuple(bindings),
    )


def codes(plan, review):
    result = check_review_integrity(plan, review, as_of=NOW, max_evidence_age=MAX_AGE)
    return {problem.code for problem in result.issues}


def with_verdict(review, index, **changes):
    vs = list(review.verdicts)
    vs[index] = vs[index].model_copy(update=changes)
    return review.model_copy(update={"verdicts": tuple(vs)})


def with_binding(review, index, **changes):
    es = list(review.bindings)
    es[index] = es[index].model_copy(update=changes)
    return review.model_copy(update={"bindings": tuple(es)})


def with_evidence(review, index, **changes):
    old = review.bindings[index]
    return with_binding(review, index, evidence=old.evidence.model_copy(update=changes))


def with_query(review, index, **changes):
    old = review.bindings[index]
    return with_binding(review, index, query=old.query.model_copy(update=changes))


def test_integrity_complete_is_not_an_action_authorization(plan):
    result = check_review_integrity(plan, sample(plan), as_of=NOW, max_evidence_age=MAX_AGE)
    assert result.eligible_for_policy_verification
    assert result.checked_decision_ids == ("brand", "afterparty", "venue")
    assert not hasattr(result, "release_artifact")


@pytest.mark.parametrize("field,expected", [
    ("plan_id", "PLAN_ID_MISMATCH"),
    ("plan_revision", "STALE_PLAN_REVISION"),
    ("plan_digest", "PLAN_DIGEST_MISMATCH"),
])
def test_review_bound_to_exact_plan(plan, field, expected):
    value = "wrong" if field == "plan_id" else (2 if field == "plan_revision" else "f" * 64)
    assert expected in codes(plan, sample(plan).model_copy(update={field: value}))


def test_missing_required_verdict(plan):
    review = sample(plan)
    assert "MISSING_REQUIRED_VERDICT" in codes(plan, review.model_copy(update={"verdicts": review.verdicts[1:]}))


def test_unknown_and_duplicate_verdict(plan):
    review = sample(plan)
    assert "DUPLICATE_VERDICT" in codes(plan, review.model_copy(update={"verdicts": review.verdicts + review.verdicts[:1]}))
    assert "UNKNOWN_DECISION" in codes(plan, with_verdict(review, 0, decision_id="alien"))


def test_stale_verdict_and_policy_version(plan):
    assert "STALE_VERDICT_REVISION" in codes(plan, with_verdict(sample(plan), 0, plan_revision=9))
    assert "POLICY_VERSION_MISMATCH" in codes(plan, with_verdict(sample(plan), 0, policy_version="other"))


@pytest.mark.parametrize("verdict,expected", [(Verdict.REPAIR, "REPAIR_REQUIRED"), (Verdict.UNCERTAIN, "UNCERTAIN_DECISION")])
def test_non_pass_blocks_verification(plan, verdict, expected):
    assert expected in codes(plan, with_verdict(sample(plan), 0, verdict=verdict))


def test_missing_duplicate_and_cross_decision_evidence(plan):
    review = sample(plan)
    assert "MISSING_EVIDENCE_REF" in codes(plan, with_verdict(review, 0, evidence_refs=("missing",)))
    assert "DUPLICATE_EVIDENCE_REF" in codes(plan, with_verdict(review, 0, evidence_refs=("e-brand", "e-brand")))
    assert "CROSS_DECISION_EVIDENCE" in codes(plan, with_binding(review, 0, decision_id="venue"))
    assert "DUPLICATE_EVIDENCE_ID" in codes(plan, with_evidence(review, 1, evidence_id="e-brand"))
    assert "UNREFERENCED_EVIDENCE" in codes(plan, with_verdict(review, 0, evidence_refs=("e-venue",)))


def test_query_fingerprint_and_scope(plan):
    review = sample(plan)
    assert "QUERY_FINGERPRINT_MISMATCH" in codes(plan, with_query(review, 0, request_fingerprint="f" * 64))
    assert "QUERY_OUTSIDE_CANDIDATE_POOL" in codes(plan, with_query(review, 0, candidate_ids=("unsealed",)))
    assert "SELECTED_CANDIDATE_NOT_QUERIED" in codes(plan, with_query(review, 0, candidate_ids=("brand-b",)))
    assert "SELECTED_CANDIDATE_NOT_RANKED" in codes(plan, with_evidence(review, 0, candidate_ranks={"brand-b": 1}))
    assert "EVIDENCE_OUTSIDE_QUERY" in codes(plan, with_evidence(review, 0, candidate_ranks={"brand-a": 1, "unsealed": 2}))
    assert "LOCALITY_MISMATCH" in codes(plan, with_query(review, 0, locality_ref="another-city"))


def test_status_source_and_timing(plan):
    review = sample(plan)
    assert "EVIDENCE_NOT_VALID" in codes(plan, with_evidence(review, 0, status=EvidenceStatus.UNAVAILABLE))
    assert "SOURCE_MODE_MISMATCH" in codes(plan, with_evidence(review, 0, source_mode=SourceMode.VERIFIED_LIVE))
    assert "EVIDENCE_FROM_FUTURE" in codes(plan, with_evidence(review, 0, collected_at=NOW + timedelta(minutes=1)))
    assert "EVIDENCE_TOO_OLD" in codes(plan, with_evidence(review, 0, collected_at=NOW - timedelta(hours=48)))
    assert "EVIDENCE_EXPIRED" in codes(plan, with_evidence(review, 0, expires_at=NOW))


def test_explicit_clock_and_freshness_config_required(plan):
    review = sample(plan)
    with pytest.raises(ValueError, match="timezone-aware"):
        check_review_integrity(plan, review, as_of=NOW.replace(tzinfo=None), max_evidence_age=MAX_AGE)
    with pytest.raises(ValueError, match="positive"):
        check_review_integrity(plan, review, as_of=NOW, max_evidence_age=timedelta(0))


def test_patch_requires_new_review_for_new_revision(plan):
    review = sample(plan)
    patch = RepairPatch(
        patch_id="patch1", decision_id="brand", base_revision=0,
        old_candidate_id="brand-a", new_candidate_id="brand-b",
        evidence_refs=("SYNTHETIC:e-brand",), precondition_digest=plan_digest(plan),
    )
    amended, affected = apply_patch(plan, patch)
    assert affected == ("brand", "afterparty")
    c = codes(amended, review)
    assert "STALE_PLAN_REVISION" in c
    assert "PLAN_DIGEST_MISMATCH" in c
    assert "STALE_VERDICT_REVISION" in c


def test_repeated_check_is_pure(plan):
    review = sample(plan)
    before = (plan_digest(plan), review.model_dump())
    check_review_integrity(plan, review, as_of=NOW, max_evidence_age=MAX_AGE)
    assert before == (plan_digest(plan), review.model_dump())
