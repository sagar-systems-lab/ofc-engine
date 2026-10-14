import json
from datetime import datetime, timedelta, timezone

import pytest

from ofc_engine.contracts import Decision, PlanSnapshot, Verdict
from ofc_engine.qloo import brand_affinity_probe
from ofc_engine.qloo_flow import (
    RecordedSource, RecordedState, compare_recorded_arms,
    review_recorded_plan, run_recorded_plan,
)
from ofc_engine.qloo_run import main as cli_main
from ofc_engine.revisions import digest_obj, plan_digest

AS_OF = datetime(2026, 10, 14, 10, 30, tzinfo=timezone.utc)
AGE = timedelta(hours=1)
ARTIST = "artist-a"
POOL = ("brand-a", "brand-b", "brand-c")
SPEC = brand_affinity_probe(ARTIST, POOL)


def capture(spec=SPEC, *, collected_at=None, scores=(0.92, 0.65, 0.2)):
    pool = dict(spec.params)["filter.results.entities"].split(",")
    assert len(pool) == len(scores)
    ordering = [pool[1], pool[0], pool[2]] if len(pool) == 3 else [pool[1], pool[0]]
    return {
        "summary": {
            "endpoint": spec.path, "http_status": 200,
            "real_qloo_requests": 1, "mode": "LIVE_RESPONSE_UNQUALIFIED",
            "utc_collected_at": (collected_at or AS_OF - timedelta(minutes=10)).isoformat(),
            "query_fingerprint": spec.fingerprint,
        },
        "payload": {
            "success": True,
            "results": {"entities": [
                {"entity_id": item, "type": "urn:entity",
                 "query": {"affinity": score, "explainability": {"signal.interests.entities": {}}}}
                for item, score in zip(ordering, scores)
            ]},
        },
    }


def make_plan(*, selected="brand-a", with_dependent=False, immutable=False, extra=None):
    decisions = [Decision(
        decision_id="brand", domain="partner", candidate_id=selected,
        candidate_pool=POOL, mutable_fields=() if immutable else ("candidate_id",),
    )]
    if with_dependent:
        decisions.append(Decision(
            decision_id="afterparty", domain="experience", candidate_id="experience-a",
            candidate_pool=("experience-a",), depends_on=("brand",),
        ))
    if extra is not None:
        decisions.append(extra)
    return PlanSnapshot(
        plan_id="recorded-comparison-1", revision=0,
        baseline_digest=digest_obj({"original": "frozen"}),
        constraints_digest=digest_obj({"budget": 5000}),
        decisions=tuple(decisions),
    )


def source():
    return {"brand": RecordedSource(capture(), SPEC)}


def run(plan=None, sources=None, **kwargs):
    return run_recorded_plan(
        plan or make_plan(), source() if sources is None else sources,
        as_of=AS_OF, max_evidence_age=AGE, **kwargs,
    )


def test_one_recorded_capture_drives_repair_revision_recheck_without_release():
    original = make_plan()
    result = run(original)
    assert result.status == RecordedState.RANK_REVIEW_COMPLETE
    assert result.plan.revision == 1
    assert result.plan.decisions[0].candidate_id == "brand-b"
    assert result.baseline is original
    assert result.baseline.decisions[0].candidate_id == "brand-a"
    assert result.plan.baseline_digest == original.baseline_digest
    assert result.plan.constraints_digest == original.constraints_digest
    assert result.ledger.attempts_used == 1
    assert result.ledger.pending_recheck_ids == ()
    assert [v.verdict for r in result.reviews for v in r.verdicts] == [
        Verdict.REPAIR, Verdict.PASS
    ]
    assert len(result.traces) == 1
    assert result.traces[0].affected_decision_ids == ("brand",)
    assert result.traces[0].recheck_state.value == "STRUCTURAL_PASS"
    assert not result.provider_authenticated
    assert not result.release_authorized
    assert not result.external_action_executed
    assert result.as_dict()["real_qloo_requests"] == 0
    assert result.as_dict()["recheck_basis"] == "RECORDED_CAPTURE_REEVALUATION_NOT_FRESH_PROVIDER_CALL"


def test_top_rank_needs_no_repair():
    result = run(make_plan(selected="brand-b"))
    assert result.status == RecordedState.RANK_REVIEW_COMPLETE
    assert result.plan.revision == 0
    assert result.ledger.attempts_used == 0
    assert len(result.reviews) == 1
    assert result.traces == ()


def test_evidence_off_vs_on_is_same_immutable_baseline():
    plan = make_plan()
    arms = compare_recorded_arms(
        plan, source(), as_of=AS_OF, max_evidence_age=AGE,
    )
    assert arms.same_baseline is True
    assert arms.real_causal_proof is False
    assert arms.disabled.status == RecordedState.REVIEW_REQUIRED
    assert arms.enabled.status == RecordedState.RANK_REVIEW_COMPLETE
    assert arms.disabled.plan.revision == 0
    assert arms.enabled.plan.revision == 1
    assert arms.as_dict()["real_qloo_requests"] == 0
    assert arms.baseline_digest == plan_digest(plan)


def test_missing_source_blocks_repair_and_preserves_baseline():
    result = run(sources={})
    assert result.status == RecordedState.REVIEW_REQUIRED
    assert result.plan.revision == 0
    assert result.reviews[0].verdicts[0].reason_codes == ("RECORDED_EVIDENCE_MISSING",)
    assert result.ledger.attempts_used == 0


def test_wrong_fingerprint_blocks_repair():
    altered = capture()
    altered["summary"]["query_fingerprint"] = "f" * 64
    result = run(sources={"brand": RecordedSource(altered, SPEC)})
    assert result.status == RecordedState.REVIEW_REQUIRED
    assert result.plan.revision == 0
    assert result.reviews[0].verdicts[0].verdict == Verdict.UNCERTAIN


def test_unsupported_dependent_blocks_mutation_even_with_brand_evidence():
    result = run(make_plan(with_dependent=True))
    assert result.status == RecordedState.REVIEW_REQUIRED
    assert result.plan.revision == 0
    assert result.ledger.attempts_used == 0
    assert result.reviews[0].verdicts[1].reason_codes == ("DEPENDENCY_QUERY_NOT_QUALIFIED",)
    assert not result.traces


def test_dependent_source_cannot_bypass_unqualified_dependency_binding():
    p = make_plan(with_dependent=True)
    result = run(p, sources={
        **source(),
        "afterparty": RecordedSource(capture(), SPEC),
    })
    assert result.status == RecordedState.REVIEW_REQUIRED
    assert result.plan.revision == 0
    assert result.reviews[0].verdicts[1].reason_codes == ("DEPENDENCY_QUERY_NOT_QUALIFIED",)
    assert len(result.reviews[0].bindings) == 1


def test_recorded_capture_stale_fails_closed():
    stale = capture(collected_at=AS_OF - timedelta(hours=2))
    result = run(sources={"brand": RecordedSource(stale, SPEC)})
    assert result.status == RecordedState.REVIEW_REQUIRED
    assert result.reviews[0].verdicts[0].reason_codes == ("RECORDED_EVIDENCE_STALE",)


def test_immutable_candidate_blocks_rank_repair():
    result = run(make_plan(immutable=True))
    assert result.status == RecordedState.REVIEW_REQUIRED
    assert result.reason_codes == ("IMMUTABLE_REPAIR_TARGET",)
    assert result.plan.revision == 0


def test_duplicate_recorded_capture_between_decisions_is_rejected():
    extra = Decision(
        decision_id="brand2", domain="partner", candidate_id="brand-b",
        candidate_pool=POOL, mutable_fields=("candidate_id",),
    )
    p = make_plan(extra=extra)
    result = run(p, sources={
        "brand": RecordedSource(capture(), SPEC),
        "brand2": RecordedSource(capture(), SPEC),
    })
    assert result.status == RecordedState.REVIEW_REQUIRED
    assert result.reviews[0].verdicts[1].reason_codes == ("DUPLICATE_RECORDED_CAPTURE",)
    assert result.plan.revision == 0


def test_two_independent_brand_decisions_are_bounded_and_rechecked():
    second_pool = ("brand-d", "brand-e")
    spec2 = brand_affinity_probe("artist-b", second_pool)
    extra = Decision(
        decision_id="other_brand", domain="partner",
        candidate_id="brand-d", candidate_pool=second_pool,
        mutable_fields=("candidate_id",),
    )
    p = make_plan(extra=extra)
    result = run(p, sources={
        **source(),
        "other_brand": RecordedSource(capture(spec2, scores=(0.8, 0.5)), spec2),
    })
    assert result.status == RecordedState.RANK_REVIEW_COMPLETE
    assert result.plan.revision == 2
    assert result.ledger.attempts_used == 2
    assert result.ledger.pending_recheck_ids == ()
    assert [d.candidate_id for d in result.plan.decisions] == ["brand-b", "brand-e"]
    assert len(result.traces) == 2
    assert all(not trace.external_action_executed for trace in result.traces)


def test_exhausted_budget_stops_second_independent_repair():
    spec2 = brand_affinity_probe("artist-b", ("brand-d", "brand-e"))
    p = make_plan(extra=Decision(
        decision_id="other_brand", domain="partner", candidate_id="brand-d",
        candidate_pool=("brand-d", "brand-e"), mutable_fields=("candidate_id",),
    ))
    result = run(p, sources={
        **source(),
        "other_brand": RecordedSource(capture(spec2, scores=(0.8, 0.5)), spec2),
    }, max_attempts=1)
    assert result.status == RecordedState.REPAIR_EXHAUSTED
    assert result.plan.revision == 1
    assert result.ledger.attempts_used == 1


@pytest.mark.parametrize("attempts", [0, 3, True, -1, 1.5])
def test_attempts_must_be_strictly_bounded(attempts):
    with pytest.raises(ValueError, match="max_attempts"):
        run(max_attempts=attempts)


def test_unknown_source_id_is_error():
    with pytest.raises(ValueError, match="unknown evidence source"):
        run(sources={"alien": RecordedSource(capture(), SPEC)})


def test_cli_performs_offline_comparison_without_leaking_raw(tmp_path, capsys):
    captured = capture(collected_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    p = tmp_path / "capture.json"
    p.write_text(json.dumps(captured))
    assert cli_main([
        "--capture", str(p), "--artist-id", ARTIST,
        "--brands", ",".join(POOL), "--selected", "brand-a",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["same_baseline"] is True
    assert result["real_causal_proof"] is False
    assert result["real_qloo_requests"] == 0
    assert result["evidence_off"]["status"] == "REVIEW_REQUIRED"
    assert result["evidence_on"]["status"] == "RECORDED_RANK_REVIEW_ONLY"
    assert '"affinity":' not in json.dumps(result)
    assert '"secret"' not in json.dumps(result)
    assert result["evidence_on"]["selected_candidates"]["brand"] == "brand-b"


def test_cli_rejects_bad_or_symlink_capture_without_echo(tmp_path, capsys):
    p = tmp_path / "capture.json"
    p.write_text('{"secret":"private-data"}')
    assert cli_main([
        "--capture", str(p), "--artist-id", ARTIST,
        "--brands", ",".join(POOL), "--selected", "brand-a",
    ]) == 0
    out = capsys.readouterr().out
    assert "private-data" not in out
    assert json.loads(out)["evidence_on"]["status"] == "REVIEW_REQUIRED"

    link = tmp_path / "redirected.json"
    link.symlink_to(p)
    assert cli_main([
        "--capture", str(link), "--artist-id", ARTIST,
        "--brands", ",".join(POOL), "--selected", "brand-a",
    ]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
