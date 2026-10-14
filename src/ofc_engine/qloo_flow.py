"""Offline recorded-affinity orchestration with bounded, revision-safe repair."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Mapping

from .contracts import DecisionVerdict, PlanSnapshot, RepairPatch, SourceMode, Verdict
from .proof import EvidenceBinding, ReviewEnvelope, check_review_integrity
from .qloo import ProbeSpec
from .qloo_review import RECORDED_RANK_POLICY, evaluate_recorded_affinity
from .repair import (
    RepairLedger, RepairPreview, RepairReviewRejected, begin_repair_ledger,
    preview_repair, record_recheck_integrity,
)
from .revisions import PatchRejected, RevisionConflict, plan_digest
from .trace import RepairTrace, build_repair_trace


class RecordedState(str, Enum):
    RANK_REVIEW_COMPLETE = "RECORDED_RANK_REVIEW_ONLY"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REPAIR_EXHAUSTED = "REPAIR_EXHAUSTED"


@dataclass(frozen=True)
class RecordedSource:
    capture: Any
    spec: ProbeSpec


@dataclass(frozen=True)
class RecordedRun:
    baseline: PlanSnapshot
    plan: PlanSnapshot
    status: RecordedState
    reason_codes: tuple[str, ...]
    reviews: tuple[ReviewEnvelope, ...]
    traces: tuple[RepairTrace, ...]
    ledger: RepairLedger
    source_mode: SourceMode = SourceMode.RECORDED_REDACTED

    @property
    def provider_authenticated(self) -> bool:
        return False

    @property
    def release_authorized(self) -> bool:
        return False

    @property
    def external_action_executed(self) -> bool:
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason_codes": list(self.reason_codes),
            "baseline_plan_digest": plan_digest(self.baseline),
            "initial_revision": self.baseline.revision,
            "current_revision": self.plan.revision,
            "selected_candidates": {
                decision.decision_id: decision.candidate_id
                for decision in self.plan.decisions
            },
            "reviews": [
                {
                    "revision": review.plan_revision,
                    "verdicts": [
                        {"decision_id": v.decision_id,
                         "verdict": v.verdict.value,
                         "reason_codes": list(v.reason_codes)}
                        for v in review.verdicts
                    ],
                }
                for review in self.reviews
            ],
            "repair_attempts": self.ledger.attempts_used,
            "pending_recheck_ids": list(self.ledger.pending_recheck_ids),
            "traces": [trace.as_dict() for trace in self.traces],
            "source_mode": self.source_mode.value,
            "recheck_basis": "RECORDED_CAPTURE_REEVALUATION_NOT_FRESH_PROVIDER_CALL",
            "provider_authenticated": False,
            "release_authorized": False,
            "external_action_executed": False,
            "real_qloo_requests": 0,
        }


@dataclass(frozen=True)
class RecordedComparison:
    disabled: RecordedRun
    enabled: RecordedRun
    baseline_digest: str

    @property
    def same_baseline(self) -> bool:
        return (plan_digest(self.disabled.baseline) ==
                plan_digest(self.enabled.baseline) == self.baseline_digest)

    @property
    def real_causal_proof(self) -> bool:
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "same_baseline": self.same_baseline,
            "real_causal_proof": False,
            "evidence_off": self.disabled.as_dict(),
            "evidence_on": self.enabled.as_dict(),
            "release_authorized": False,
            "real_qloo_requests": 0,
        }


def review_recorded_plan(
    plan: PlanSnapshot,
    sources: Mapping[str, RecordedSource],
    *,
    as_of: datetime,
    max_evidence_age: timedelta,
    evidence_enabled: bool = True,
) -> ReviewEnvelope:
    """Preserve the full decision set; do not certify dependent brand queries."""
    if as_of.utcoffset() is None or max_evidence_age <= timedelta(0):
        raise ValueError("timezone-aware clock and positive evidence age required")
    known = {d.decision_id for d in plan.decisions}
    if not set(sources).issubset(known):
        raise ValueError("unknown evidence source decision")
    if any(not isinstance(s, RecordedSource) or not isinstance(s.spec, ProbeSpec)
           for s in sources.values()):
        raise ValueError("invalid recorded evidence source")

    verdicts: list[DecisionVerdict] = []
    bindings: list[EvidenceBinding] = []
    used_evidence: set[str] = set()
    for decision in plan.decisions:
        binding = None
        if not evidence_enabled:
            reason = "RECORDED_EVIDENCE_DISABLED"
        elif decision.depends_on:
            # Existing affinity probes do not bind dependency selections to queries.
            reason = "DEPENDENCY_QUERY_NOT_QUALIFIED"
        elif decision.decision_id not in sources:
            reason = "RECORDED_EVIDENCE_MISSING"
        else:
            source = sources[decision.decision_id]
            scoped = evaluate_recorded_affinity(
                plan, decision_id=decision.decision_id,
                capture=source.capture, spec=source.spec,
                as_of=as_of, max_evidence_age=max_evidence_age,
            )
            item = next(v for v in scoped.verdicts if v.decision_id == decision.decision_id)
            if scoped.bindings:
                binding = scoped.bindings[0]
            if binding is not None and binding.evidence.evidence_id in used_evidence:
                binding = None
                reason = "DUPLICATE_RECORDED_CAPTURE"
            elif binding is None:
                reason = item.reason_codes[0]

        if binding is None:
            verdicts.append(DecisionVerdict(
                decision_id=decision.decision_id, plan_revision=plan.revision,
                verdict=Verdict.UNCERTAIN, reason_codes=(reason,),
                evidence_refs=(), policy_version=RECORDED_RANK_POLICY,
            ))
        else:
            used_evidence.add(binding.evidence.evidence_id)
            bindings.append(binding)
            verdicts.append(item)

    return ReviewEnvelope(
        plan_id=plan.plan_id, plan_revision=plan.revision,
        plan_digest=plan_digest(plan),
        source_mode=SourceMode.RECORDED_REDACTED,
        policy_version=RECORDED_RANK_POLICY,
        verdicts=tuple(verdicts), bindings=tuple(bindings),
    )


def run_recorded_plan(
    baseline: PlanSnapshot,
    sources: Mapping[str, RecordedSource],
    *,
    as_of: datetime,
    max_evidence_age: timedelta,
    max_attempts: int = 2,
    evidence_enabled: bool = True,
) -> RecordedRun:
    """Rank-only replay; no Qloo requests, releases, or externally executed actions."""
    if type(max_attempts) is not int or not 1 <= max_attempts <= 2:
        raise ValueError("max_attempts must be 1 or 2")
    if as_of.utcoffset() is None or max_evidence_age <= timedelta(0):
        raise ValueError("timezone-aware clock and positive evidence age required")
    if not isinstance(baseline, PlanSnapshot):
        raise ValueError("baseline must be a PlanSnapshot")
    if not isinstance(sources, Mapping):
        raise ValueError("sources must be a mapping")

    ledger = begin_repair_ledger(baseline, max_attempts=max_attempts)
    current = baseline
    reviews: list[ReviewEnvelope] = []
    traces: list[RepairTrace] = []
    pending: tuple[PlanSnapshot, RepairLedger, ReviewEnvelope,
                   RepairPatch, RepairPreview] | None = None

    def finish(status: RecordedState, codes: tuple[str, ...]) -> RecordedRun:
        return RecordedRun(
            baseline, current, status, codes,
            tuple(reviews), tuple(traces), ledger,
        )

    for _ in range(max_attempts + 2):
        review = review_recorded_plan(
            current, sources, as_of=as_of,
            max_evidence_age=max_evidence_age, evidence_enabled=evidence_enabled,
        )
        reviews.append(review)
        if pending is not None:
            original, old_ledger, old_review, patch, preview = pending
            traces.append(build_repair_trace(
                original, old_ledger, old_review, patch, preview,
                after_review=review, as_of=as_of,
                max_evidence_age=max_evidence_age,
            ))
            pending = None

        if any(v.verdict == Verdict.UNCERTAIN for v in review.verdicts):
            return finish(RecordedState.REVIEW_REQUIRED, ("RECORDED_PLAN_INCOMPLETE",))

        integrity = check_review_integrity(
            current, review, as_of=as_of, max_evidence_age=max_evidence_age,
        )
        blockers = {i.code for i in integrity.issues if i.code != "REPAIR_REQUIRED"}
        if blockers:
            return finish(RecordedState.REVIEW_REQUIRED, tuple(sorted(blockers)))

        needs_repair = [v for v in review.verdicts if v.verdict == Verdict.REPAIR]
        if not needs_repair:
            if ledger.pending_recheck_ids:
                try:
                    ledger = record_recheck_integrity(
                        current, ledger, review,
                        as_of=as_of, max_evidence_age=max_evidence_age,
                    )
                except RepairReviewRejected:
                    return finish(RecordedState.REVIEW_REQUIRED, ("RECHECK_INTEGRITY_FAILED",))
            return finish(
                RecordedState.RANK_REVIEW_COMPLETE,
                ("RECORDED_RANKS_ONLY_NOT_PROVIDER_AUTHENTICATED",),
            )

        if ledger.attempts_used >= ledger.max_attempts:
            return finish(RecordedState.REPAIR_EXHAUSTED, ("REPAIR_BUDGET_EXHAUSTED",))

        target = needs_repair[0]
        decision = next(d for d in current.decisions if d.decision_id == target.decision_id)
        if "candidate_id" not in decision.mutable_fields:
            return finish(RecordedState.REVIEW_REQUIRED, ("IMMUTABLE_REPAIR_TARGET",))

        binding = next(
            b for b in review.bindings if b.decision_id == target.decision_id
        )
        replacement = min(
            decision.candidate_pool, key=lambda c: binding.evidence.candidate_ranks[c],
        )
        patch = RepairPatch(
            patch_id=f"recorded:{current.plan_id}:{current.revision}:{target.decision_id}",
            decision_id=target.decision_id,
            base_revision=current.revision,
            old_candidate_id=decision.candidate_id,
            new_candidate_id=replacement,
            precondition_digest=plan_digest(current),
            evidence_refs=target.evidence_refs,
        )
        try:
            preview = preview_repair(
                current, ledger, review, patch,
                as_of=as_of, max_evidence_age=max_evidence_age,
            )
        except (RepairReviewRejected, PatchRejected, RevisionConflict):
            return finish(RecordedState.REVIEW_REQUIRED, ("REPAIR_PREVIEW_REJECTED",))

        pending = (current, ledger, review, patch, preview)
        current, ledger = preview.proposed_plan, preview.ledger

    return finish(RecordedState.REVIEW_REQUIRED, ("WORKFLOW_ITERATION_LIMIT",))


def compare_recorded_arms(
    baseline: PlanSnapshot,
    sources: Mapping[str, RecordedSource],
    *,
    as_of: datetime,
    max_evidence_age: timedelta,
    max_attempts: int = 2,
) -> RecordedComparison:
    disabled = run_recorded_plan(
        baseline, sources, as_of=as_of, max_evidence_age=max_evidence_age,
        max_attempts=max_attempts, evidence_enabled=False,
    )
    enabled = run_recorded_plan(
        baseline, sources, as_of=as_of, max_evidence_age=max_evidence_age,
        max_attempts=max_attempts, evidence_enabled=True,
    )
    fingerprint = plan_digest(baseline)
    if plan_digest(disabled.baseline) != fingerprint or plan_digest(enabled.baseline) != fingerprint:
        raise ValueError("evidence arms must use the same immutable baseline")
    return RecordedComparison(disabled, enabled, fingerprint)
