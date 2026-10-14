from datetime import datetime, timedelta, timezone

import pytest

from ofc_engine.contracts import (
    Decision, PlanSnapshot, RepairPatch, SourceMode, Verdict,
)
from ofc_engine.proof import check_review_integrity
from ofc_engine.qloo import brand_affinity_probe
from ofc_engine.qloo_review import evaluate_recorded_affinity
from ofc_engine.repair import begin_repair_ledger, preview_repair, record_recheck_integrity
from ofc_engine.revisions import digest_obj, plan_digest

AS_OF = datetime(2026, 10, 14, 10, 30, tzinfo=timezone.utc)
AGE = timedelta(hours=1)
ARTIST = "artist-id"
POOL = ("brand-a", "brand-b", "brand-c")
SPEC = brand_affinity_probe(ARTIST, POOL)


def recorded_capture():
    return {
        "summary": {
            "endpoint": "/v2/insights", "http_status": 200,
            "real_qloo_requests": 1, "mode": "LIVE_RESPONSE_UNQUALIFIED",
            "utc_collected_at": "2026-10-14T10:21:49+00:00",
            "query_fingerprint": SPEC.fingerprint,
        },
        "payload": {
            "success": True,
            "results": {"entities": [
                {"entity_id": "brand-b", "type": "urn:entity", "query": {
                    "affinity": 0.88, "explainability": {"signal.interests.entities": {}}}},
                {"entity_id": "brand-a", "type": "urn:entity", "query": {
                    "affinity": 0.66, "explainability": {"signal.interests.entities": {}}}},
                {"entity_id": "brand-c", "type": "urn:entity", "query": {
                    "affinity": 0.22, "explainability": {}}},
            ]},
        },
    }


def plan(*, selected="brand-a", pool=POOL, locality=None, domain="partner",
         with_dependent=False):
    decisions = [Decision(
        decision_id="brand", domain=domain, candidate_id=selected,
        candidate_pool=pool, mutable_fields=("candidate_id",),
        locality_ref=locality,
    )]
    if with_dependent:
        decisions.append(Decision(
            decision_id="afterparty", domain="experience",
            candidate_id="experience-a", candidate_pool=("experience-a",),
            depends_on=("brand",),
        ))
    return PlanSnapshot(
        plan_id="offline-observation", revision=0,
        baseline_digest=digest_obj({"baseline": True}),
        constraints_digest=digest_obj({"constraints": True}),
        decisions=tuple(decisions),
    )


def review(p=None, **kwargs):
    return evaluate_recorded_affinity(
        p or plan(), decision_id="brand",
        capture=recorded_capture(), spec=SPEC, as_of=AS_OF,
        max_evidence_age=AGE, **kwargs,
    )


def test_non_top_choice_produces_recorded_repair_with_complete_binding():
    p = plan()
    result = review(p)
    assert result.source_mode == SourceMode.RECORDED_REDACTED
    assert result.verdicts[0].verdict == Verdict.REPAIR
    binding = result.bindings[0]
    assert binding.query.request_fingerprint == SPEC.fingerprint
    assert binding.query.input_entity_ids == (ARTIST,)
    assert binding.query.locality_ref is None
    assert binding.evidence.candidate_ranks == {"brand-b": 1, "brand-a": 2, "brand-c": 3}
    assert binding.evidence.explainability["signal_explainability_present"]["brand-b"] is True
    assert {x.code for x in check_review_integrity(
        p, result, as_of=AS_OF, max_evidence_age=AGE).issues} == {"REPAIR_REQUIRED"}
    assert not hasattr(result, "release_authorized")


def test_first_choice_observed_pass_is_not_provider_authorization():
    p = plan(selected="brand-b")
    result = review(p)
    assert result.verdicts[0].verdict == Verdict.PASS
    assert result.verdicts[0].reason_codes == ("RECORDED_TOP_AFFINITY_RANK",)
    assert not check_review_integrity(
        p, result, as_of=AS_OF, max_evidence_age=AGE).issues
    assert result.source_mode != SourceMode.VERIFIED_LIVE


@pytest.mark.parametrize(("change", "expected"), [
    ({"evidence_enabled": False}, "RECORDED_EVIDENCE_DISABLED"),
])
def test_off_arm_uncertain_without_provider_evidence(change, expected):
    r = review(**change)
    assert r.verdicts[0].verdict == Verdict.UNCERTAIN
    assert r.verdicts[0].reason_codes == (expected,)
    assert r.bindings == ()


def test_other_decisions_remain_uncertain_and_block_integrity():
    p = plan(with_dependent=True)
    r = review(p)
    assert tuple(v.verdict for v in r.verdicts) == (Verdict.REPAIR, Verdict.UNCERTAIN)
    assert r.verdicts[1].reason_codes == ("NO_RECORDED_EVIDENCE_FOR_DECISION",)
    codes = {i.code for i in check_review_integrity(p, r, as_of=AS_OF, max_evidence_age=AGE).issues}
    assert {"REPAIR_REQUIRED", "UNCERTAIN_DECISION"}.issubset(codes)


def test_unsupported_locality_is_uncertain_not_falsely_grounded():
    r = review(plan(locality="Chicago"))
    assert r.verdicts[0].reason_codes == ("UNQUALIFIED_BRAND_LOCALITY",)
    assert r.verdicts[0].verdict == Verdict.UNCERTAIN
    assert not r.bindings


def test_non_brand_domain_is_uncertain():
    r = review(plan(domain="venue"))
    assert r.verdicts[0].reason_codes == ("UNSUPPORTED_DECISION_DOMAIN",)
    assert not r.bindings


def test_wrong_pool_fails_closed():
    p = plan(pool=("brand-b", "brand-a"), selected="brand-a")
    r = review(p)
    assert r.verdicts[0].reason_codes == ("CANDIDATE_POOL_MISMATCH",)
    assert r.bindings == ()


def test_wrong_query_fingerprint_fails_closed():
    capture = recorded_capture()
    capture["summary"]["query_fingerprint"] = "f" * 64
    r = evaluate_recorded_affinity(
        plan(), decision_id="brand", capture=capture, spec=SPEC,
        as_of=AS_OF, max_evidence_age=AGE,
    )
    assert r.verdicts[0].reason_codes == ("RECORDED_AFFINITY_REJECTED",)
    assert not r.bindings


@pytest.mark.parametrize(("as_of", "expected"), [
    (datetime(2026, 10, 14, 10, 20, tzinfo=timezone.utc), "RECORDED_EVIDENCE_FROM_FUTURE"),
    (datetime(2026, 10, 14, 11, 22, tzinfo=timezone.utc), "RECORDED_EVIDENCE_STALE"),
])
def test_invalid_time_is_uncertain(as_of, expected):
    r = evaluate_recorded_affinity(
        plan(), decision_id="brand", capture=recorded_capture(),
        spec=SPEC, as_of=as_of, max_evidence_age=AGE,
    )
    assert r.verdicts[0].reason_codes == (expected,)
    assert r.bindings == ()


def test_invalid_clock_configuration_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_recorded_affinity(
            plan(), decision_id="brand", capture=recorded_capture(),
            spec=SPEC, as_of=AS_OF.replace(tzinfo=None), max_evidence_age=AGE,
        )


def test_unknown_decision_rejected():
    with pytest.raises(ValueError, match="unknown target decision"):
        evaluate_recorded_affinity(
            plan(), decision_id="missing", capture=recorded_capture(),
            spec=SPEC, as_of=AS_OF, max_evidence_age=AGE,
        )


def test_recorded_rank_repair_preview_tracks_revision_not_release():
    p = plan()
    r = review(p)
    patch = RepairPatch(
        patch_id="recorded-rank-preview", decision_id="brand",
        base_revision=p.revision, old_candidate_id="brand-a",
        new_candidate_id="brand-b",
        precondition_digest=plan_digest(p),
        evidence_refs=r.verdicts[0].evidence_refs,
    )
    preview = preview_repair(
        p, begin_repair_ledger(p, max_attempts=1), r, patch,
        as_of=AS_OF, max_evidence_age=AGE,
    )
    assert preview.proposed_plan.revision == 1
    assert preview.proposed_plan.decisions[0].candidate_id == "brand-b"
    assert preview.ledger.pending_recheck_ids == ("brand",)
    assert preview.ledger.attempts_used == 1
    assert check_review_integrity(
        preview.proposed_plan, r, as_of=AS_OF, max_evidence_age=AGE).issues
    updated = review(preview.proposed_plan)
    assert updated.verdicts[0].verdict == Verdict.PASS
    cleared = record_recheck_integrity(
        preview.proposed_plan, preview.ledger, updated,
        as_of=AS_OF, max_evidence_age=AGE,
    )
    assert cleared.pending_recheck_ids == ()
    assert cleared.attempts_used == 1
    assert updated.source_mode == SourceMode.RECORDED_REDACTED


def test_off_and_on_share_exact_baseline():
    p = plan()
    off = review(p, evidence_enabled=False)
    on = review(p)
    assert off.plan_digest == on.plan_digest == plan_digest(p)
    assert off.verdicts[0].verdict == Verdict.UNCERTAIN
    assert on.verdicts[0].verdict == Verdict.REPAIR
    assert off.source_mode == on.source_mode == SourceMode.RECORDED_REDACTED
