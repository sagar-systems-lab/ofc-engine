"""Deterministic fixture workflow for testing evaluation and repair invariants.

Synthetic rank order is deliberately *not* a production cultural-fit policy.
No method in this module authenticates a Qloo response or authorizes an action.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol

from .contracts import (
    DecisionVerdict, EvidenceStatus, PlanSnapshot, RepairPatch,
    SourceMode, Verdict,
)
from .proof import EvidenceBinding, ReviewEnvelope, check_review_integrity
from .repair import (
    RepairLedger, RepairPreview, RepairReviewRejected, begin_repair_ledger,
    preview_repair, record_recheck_integrity,
)
from .revisions import plan_digest
from .trace import RepairTrace, build_repair_trace


FIXTURE_POLICY_VERSION = "fixture-rank-v1"


class WorkflowState(str, Enum):
    INTEGRITY_ONLY = "INTEGRITY_ONLY"
    USER_REVIEW = "USER_REVIEW"
    REPAIR_EXHAUSTED = "REPAIR_EXHAUSTED"


class FixtureEvidenceSource(Protocol):
    def fetch(self, plan: PlanSnapshot) -> tuple[EvidenceBinding, ...]: ...


@dataclass(frozen=True)
class FixtureRun:
    """Immutable output, with no release/execute method or permit."""

    baseline: PlanSnapshot
    plan: PlanSnapshot
    status: WorkflowState
    reason_codes: tuple[str, ...]
    reviews: tuple[ReviewEnvelope, ...]
    traces: tuple[RepairTrace, ...]
    ledger: RepairLedger
    source_mode: SourceMode = SourceMode.SYNTHETIC

    @property
    def release_authorized(self) -> bool:
        return False

    @property
    def external_action_executed(self) -> bool:
        return False


@dataclass(frozen=True)
class FixtureAblation:
    baseline_digest: str
    disabled: FixtureRun
    enabled: FixtureRun
    identical_baseline: bool

    @property
    def real_causal_proof(self) -> bool:
        return False


def _usable_binding(
    decision, binding: EvidenceBinding, *, as_of: datetime,
    max_evidence_age: timedelta,
) -> bool:
    """Check inputs before synthetic ranking; all checks are fail-closed."""
    if binding.decision_id != decision.decision_id:
        return False
    q, e = binding.query, binding.evidence
    if e.source_mode is not SourceMode.SYNTHETIC or e.status is not EvidenceStatus.VALID:
        return False
    if q.request_fingerprint != e.query_fingerprint or q.locality_ref != decision.locality_ref:
        return False
    if not q.input_entity_ids or any(not x for x in q.input_entity_ids):
        return False
    if set(q.candidate_ids) != set(decision.candidate_pool):
        return False
    if len(set(q.candidate_ids)) != len(q.candidate_ids):
        return False
    if set(e.candidate_ranks) != set(decision.candidate_pool):
        return False
    if e.collected_at > as_of or as_of - e.collected_at > max_evidence_age:
        return False
    if e.expires_at is not None and e.expires_at <= as_of:
        return False
    ranks = list(e.candidate_ranks.values())
    # Fixtures require a complete, strict permutation. Real Qloo responses
    # must instead be normalized based on empirically observed API semantics.
    if any(type(x) is not int for x in ranks) or sorted(ranks) != list(range(1, len(ranks) + 1)):
        return False
    return True


def evaluate_fixture(
    plan: PlanSnapshot,
    bindings: tuple[EvidenceBinding, ...],
    *,
    as_of: datetime,
    max_evidence_age: timedelta,
    evidence_enabled: bool = True,
) -> ReviewEnvelope:
    """A deterministic synthetic policy; no Qloo rank is inferred or claimed.

    Without evidence, every decision is UNCERTAIN, not PASS. Each decision
    needs exactly one individually bound, fresh, fully ranked fixture record.
    """
    if as_of.utcoffset() is None or max_evidence_age <= timedelta(0):
        raise ValueError("timezone-aware clock and positive evidence age required")
    indexed: dict[str, list[EvidenceBinding]] = {}
    valid_decision_ids = {d.decision_id for d in plan.decisions}
    has_unknown_binding = any(item.decision_id not in valid_decision_ids for item in bindings)
    for item in bindings:
        indexed.setdefault(item.decision_id, []).append(item)
    verdicts: list[DecisionVerdict] = []
    used: list[EvidenceBinding] = []
    for decision in plan.decisions:
        eligible = indexed.get(decision.decision_id, ()) if evidence_enabled and not has_unknown_binding else ()
        if len(eligible) != 1 or not _usable_binding(
            decision, eligible[0], as_of=as_of, max_evidence_age=max_evidence_age,
        ):
            verdict, reason, refs = Verdict.UNCERTAIN, "FIXTURE_EVIDENCE_UNAVAILABLE", ()
        else:
            item = eligible[0]
            ranks = item.evidence.candidate_ranks
            verdict = Verdict.PASS if ranks[decision.candidate_id] == 1 else Verdict.REPAIR
            reason = "FIXTURE_FIRST_RANK" if verdict is Verdict.PASS else "FIXTURE_ALTERNATIVE_FIRST_RANK"
            refs = (item.evidence.evidence_id,)
            used.append(item)
        verdicts.append(DecisionVerdict(
            decision_id=decision.decision_id, plan_revision=plan.revision,
            verdict=verdict, reason_codes=(reason,), evidence_refs=refs,
            policy_version=FIXTURE_POLICY_VERSION,
        ))
    return ReviewEnvelope(
        plan_id=plan.plan_id, plan_revision=plan.revision,
        plan_digest=plan_digest(plan), source_mode=SourceMode.SYNTHETIC,
        policy_version=FIXTURE_POLICY_VERSION,
        verdicts=tuple(verdicts), bindings=tuple(used),
    )


def _replacement(plan: PlanSnapshot, review: ReviewEnvelope, decision_id: str) -> str:
    decision = next(d for d in plan.decisions if d.decision_id == decision_id)
    binding = next(b for b in review.bindings if b.decision_id == decision_id)
    return min(decision.candidate_pool, key=lambda c: binding.evidence.candidate_ranks[c])


def run_fixture(
    baseline: PlanSnapshot,
    source: FixtureEvidenceSource | None,
    *,
    as_of: datetime,
    max_evidence_age: timedelta,
    max_attempts: int = 2,
    evidence_enabled: bool = True,
) -> FixtureRun:
    """Bounded deterministic orchestration; source is injected, never networked.

    A missing/malformed/untrusted fixture source degrades to USER_REVIEW. The
    public output is never eligible for external release, even when all checks
    pass, because there is no trusted Qloo adapter or qualified live policy.
    """
    ledger = begin_repair_ledger(baseline, max_attempts=max_attempts)
    plan = baseline
    reviews: list[ReviewEnvelope] = []
    traces: list[RepairTrace] = []
    # Hold one pending preview until its replacement revision is evaluated.
    pending: tuple[PlanSnapshot, RepairLedger, ReviewEnvelope, RepairPatch, RepairPreview] | None = None

    def finish(status: WorkflowState, reasons: tuple[str, ...]) -> FixtureRun:
        return FixtureRun(baseline, plan, status, reasons, tuple(reviews), tuple(traces), ledger)

    for _ in range(max_attempts + 2):
        try:
            bindings = source.fetch(plan) if evidence_enabled and source is not None else ()
            if not isinstance(bindings, tuple) or any(not isinstance(x, EvidenceBinding) for x in bindings):
                bindings = ()
        except (ConnectionError, TimeoutError, ValueError, KeyError, OSError):
            bindings = ()
        review = evaluate_fixture(
            plan, bindings, as_of=as_of,
            max_evidence_age=max_evidence_age, evidence_enabled=evidence_enabled,
        )
        reviews.append(review)
        if pending:
            before_plan, before_ledger, before_review, patch, preview = pending
            traces.append(build_repair_trace(
                before_plan, before_ledger, before_review, patch, preview,
                after_review=review, as_of=as_of, max_evidence_age=max_evidence_age,
            ))
            pending = None
        uncertain = [v.decision_id for v in review.verdicts if v.verdict is Verdict.UNCERTAIN]
        if uncertain:
            return finish(WorkflowState.USER_REVIEW, ("INSUFFICIENT_SYNTHETIC_EVIDENCE",))
        structural = check_review_integrity(plan, review, as_of=as_of, max_evidence_age=max_evidence_age)
        blockers = {i.code for i in structural.issues if i.code != "REPAIR_REQUIRED"}
        if blockers:
            return finish(WorkflowState.USER_REVIEW, tuple(sorted(blockers)))
        needs_repair = [v.decision_id for v in review.verdicts if v.verdict is Verdict.REPAIR]
        if not needs_repair:
            if ledger.pending_recheck_ids:
                try:
                    ledger = record_recheck_integrity(
                        plan, ledger, review,
                        as_of=as_of, max_evidence_age=max_evidence_age,
                    )
                except RepairReviewRejected:
                    return finish(WorkflowState.USER_REVIEW, ("RECHECK_INTEGRITY_FAILED",))
            return finish(WorkflowState.INTEGRITY_ONLY, ("SYNTHETIC_NOT_AUTHORIZED",))
        if ledger.attempts_used >= ledger.max_attempts:
            return finish(WorkflowState.REPAIR_EXHAUSTED, ("REPAIR_BUDGET_EXHAUSTED",))
        target = needs_repair[0]
        decision = next(d for d in plan.decisions if d.decision_id == target)
        if "candidate_id" not in decision.mutable_fields:
            return finish(WorkflowState.USER_REVIEW, ("IMMUTABLE_DECISION_NEEDS_REPAIR",))
        target_review = next(v for v in review.verdicts if v.decision_id == target)
        patch = RepairPatch(
            patch_id=f"fixture:{plan.plan_id}:{plan.revision}:{target}",
            decision_id=target, base_revision=plan.revision,
            old_candidate_id=decision.candidate_id,
            new_candidate_id=_replacement(plan, review, target),
            precondition_digest=plan_digest(plan),
            evidence_refs=target_review.evidence_refs,
        )
        try:
            preview = preview_repair(
                plan, ledger, review, patch, as_of=as_of,
                max_evidence_age=max_evidence_age,
            )
        except RepairReviewRejected:
            return finish(WorkflowState.USER_REVIEW, ("REPAIR_VALIDATION_FAILED",))
        pending = (plan, ledger, review, patch, preview)
        plan, ledger = preview.proposed_plan, preview.ledger
    return finish(WorkflowState.USER_REVIEW, ("WORKFLOW_ITERATION_LIMIT",))


def compare_fixture_arms(
    baseline: PlanSnapshot,
    source: FixtureEvidenceSource | None,
    *,
    as_of: datetime,
    max_evidence_age: timedelta,
    max_attempts: int = 2,
) -> FixtureAblation:
    """Identical frozen baseline, OFF/ON fixture contrast; no causal Qloo claim."""
    off = run_fixture(
        baseline, source, as_of=as_of, max_evidence_age=max_evidence_age,
        max_attempts=max_attempts, evidence_enabled=False,
    )
    on = run_fixture(
        baseline, source, as_of=as_of, max_evidence_age=max_evidence_age,
        max_attempts=max_attempts, evidence_enabled=True,
    )
    same = (plan_digest(off.baseline) == plan_digest(on.baseline) == plan_digest(baseline))
    if not same:
        raise ValueError("ablation arms differ in their frozen baseline")
    return FixtureAblation(plan_digest(baseline), off, on, same)
