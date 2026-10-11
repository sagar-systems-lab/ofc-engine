"""Pure selective repair previews and revision-bound recheck tracking."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .contracts import PlanSnapshot, RepairPatch, Verdict
from .proof import ReviewEnvelope, check_review_integrity
from .revisions import apply_patch, plan_digest


class RepairReviewRejected(ValueError):
    """Review provenance, proposed mutation or recheck is not admissible."""


class RepairBudgetExceeded(RepairReviewRejected):
    """Repair attempt budget exceeded."""


@dataclass(frozen=True)
class RepairLedger:
    """In-memory repair ledger bound to one plan digest."""

    plan_id: str
    plan_revision: int
    plan_digest: str
    max_attempts: int
    attempts_used: int = 0
    pending_recheck_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepairPreview:
    """Proposed revision without storage changes or action execution."""

    proposed_plan: PlanSnapshot
    ledger: RepairLedger
    affected_decision_ids: tuple[str, ...]


def begin_repair_ledger(plan: PlanSnapshot, *, max_attempts: int = 2) -> RepairLedger:
    if type(max_attempts) is not int or max_attempts <= 0:
        raise ValueError("max_attempts must be a positive integer")
    return RepairLedger(
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        plan_digest=plan_digest(plan),
        max_attempts=max_attempts,
    )


def _require_current_ledger(plan: PlanSnapshot, ledger: RepairLedger) -> None:
    if (
        ledger.plan_id != plan.plan_id
        or ledger.plan_revision != plan.revision
        or ledger.plan_digest != plan_digest(plan)
    ):
        raise RepairReviewRejected("repair ledger does not match current plan revision/digest")
    if (
        type(ledger.max_attempts) is not int
        or ledger.max_attempts <= 0
        or type(ledger.attempts_used) is not int
        or not 0 <= ledger.attempts_used <= ledger.max_attempts
    ):
        raise RepairReviewRejected("invalid repair attempt ledger")
    decision_ids = {d.decision_id for d in plan.decisions}
    if len(set(ledger.pending_recheck_ids)) != len(ledger.pending_recheck_ids):
        raise RepairReviewRejected("duplicate pending recheck IDs")
    if not set(ledger.pending_recheck_ids).issubset(decision_ids):
        raise RepairReviewRejected("pending recheck references unknown decision")


def _require_review_integrity(
    plan: PlanSnapshot,
    review: ReviewEnvelope,
    *,
    as_of: datetime,
    max_evidence_age: timedelta,
    allow_repair: bool,
) -> None:
    result = check_review_integrity(
        plan, review, as_of=as_of, max_evidence_age=max_evidence_age
    )
    allowed = {"REPAIR_REQUIRED"} if allow_repair else set()
    failures = {issue.code for issue in result.issues if issue.code not in allowed}
    if failures:
        raise RepairReviewRejected("review integrity failure: " + ", ".join(sorted(failures)))


def preview_repair(
    plan: PlanSnapshot,
    ledger: RepairLedger,
    review: ReviewEnvelope,
    patch: RepairPatch,
    *,
    as_of: datetime,
    max_evidence_age: timedelta,
) -> RepairPreview:
    """Preview one REPAIR change and invalidate affected decision reviews."""
    _require_current_ledger(plan, ledger)
    if ledger.attempts_used >= ledger.max_attempts:
        raise RepairBudgetExceeded("repair attempt budget exhausted")
    _require_review_integrity(
        plan, review, as_of=as_of, max_evidence_age=max_evidence_age,
        allow_repair=True,
    )
    verdicts = {verdict.decision_id: verdict for verdict in review.verdicts}
    target = verdicts.get(patch.decision_id)
    if target is None or target.verdict != Verdict.REPAIR:
        raise RepairReviewRejected("target decision must carry a REPAIR verdict")
    if not set(ledger.pending_recheck_ids).issubset(verdicts):
        raise RepairReviewRejected("affected decisions require current-revision recheck")
    if any(
        verdicts[decision_id].verdict != Verdict.PASS
        for decision_id in ledger.pending_recheck_ids
        if decision_id != patch.decision_id
    ):
        raise RepairReviewRejected("dependent rechecks must PASS before another repair")
    if len(set(patch.evidence_refs)) != len(patch.evidence_refs):
        raise RepairReviewRejected("duplicate patch evidence references")
    if set(patch.evidence_refs) != set(target.evidence_refs):
        raise RepairReviewRejected("patch evidence differs from target REPAIR verdict")
    bindings = {b.evidence.evidence_id: b for b in review.bindings}
    for evidence_id in patch.evidence_refs:
        binding = bindings[evidence_id]
        if binding.decision_id != patch.decision_id:
            raise RepairReviewRejected("patch evidence belongs to another decision")
        if patch.new_candidate_id not in binding.query.candidate_ids:
            raise RepairReviewRejected("replacement candidate was not queried")
        if patch.new_candidate_id not in binding.evidence.candidate_ranks:
            raise RepairReviewRejected("replacement candidate has no bound ranking evidence")

    next_plan, affected = apply_patch(plan, patch)
    pending = set(ledger.pending_recheck_ids) | set(affected)
    next_ledger = replace(
        ledger,
        plan_revision=next_plan.revision,
        plan_digest=plan_digest(next_plan),
        attempts_used=ledger.attempts_used + 1,
        pending_recheck_ids=tuple(
            d.decision_id for d in next_plan.decisions if d.decision_id in pending
        ),
    )
    return RepairPreview(next_plan, next_ledger, affected)


def record_recheck_integrity(
    plan: PlanSnapshot,
    ledger: RepairLedger,
    review: ReviewEnvelope,
    *,
    as_of: datetime,
    max_evidence_age: timedelta,
) -> RepairLedger:
    """Clear rechecks after a fresh integrity-valid all-PASS review.

    Does not authenticate Qloo evidence or authorize actions.
    """
    _require_current_ledger(plan, ledger)
    if not ledger.pending_recheck_ids:
        raise RepairReviewRejected("no pending rechecks")
    _require_review_integrity(
        plan, review, as_of=as_of, max_evidence_age=max_evidence_age,
        allow_repair=False,
    )
    verdicts = {v.decision_id: v for v in review.verdicts}
    if any(
        decision_id not in verdicts or verdicts[decision_id].verdict != Verdict.PASS
        for decision_id in ledger.pending_recheck_ids
    ):
        raise RepairReviewRejected("not all affected decisions received PASS reviews")
    return replace(ledger, pending_recheck_ids=())
