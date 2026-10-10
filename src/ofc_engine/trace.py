"""Revision-bound, human-inspectable traces for selective repair previews.

These are *illustrative integrity traces*, not provider attestations, policy
approval, release permits or externally executed actions. A trusted adapter
and evaluator must be added before any review can become authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .contracts import PlanSnapshot, RepairPatch, SourceMode
from .proof import ReviewEnvelope, check_review_integrity
from .repair import (
    RepairLedger, RepairPreview, RepairReviewRejected, preview_repair,
    record_recheck_integrity,
)
from .revisions import plan_digest


class RecheckState(str, Enum):
    PENDING = "PENDING"
    BLOCKED = "BLOCKED"
    STRUCTURAL_PASS = "STRUCTURAL_PASS"


@dataclass(frozen=True)
class DecisionTrace:
    decision_id: str
    before_candidate_id: str
    after_candidate_id: str
    changed: bool
    requires_recheck: bool
    before_verdict: str | None
    after_verdict: str | None
    before_evidence_ids: tuple[str, ...]
    after_evidence_ids: tuple[str, ...]
    before_reason_codes: tuple[str, ...]
    after_reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class RepairTrace:
    plan_id: str
    baseline_digest: str
    constraints_digest: str
    initial_revision: int
    proposed_revision: int
    before_plan_digest: str
    after_plan_digest: str
    source_mode: SourceMode
    policy_version: str
    changed_decision_id: str
    affected_decision_ids: tuple[str, ...]
    attempt: int
    max_attempts: int
    recheck_state: RecheckState
    pending_recheck_ids: tuple[str, ...]
    issue_codes: tuple[str, ...]
    decisions: tuple[DecisionTrace, ...]
    provider_authenticated: bool = False
    external_action_executed: bool = False
    release_authorized: bool = False

    def as_dict(self) -> dict:
        """JSON-safe trace for a future proof inspector or demo fixture."""
        return {
            "plan_id": self.plan_id,
            "baseline_digest": self.baseline_digest,
            "constraints_digest": self.constraints_digest,
            "initial_revision": self.initial_revision,
            "proposed_revision": self.proposed_revision,
            "before_plan_digest": self.before_plan_digest,
            "after_plan_digest": self.after_plan_digest,
            "source_mode": self.source_mode.value,
            "policy_version": self.policy_version,
            "changed_decision_id": self.changed_decision_id,
            "affected_decision_ids": list(self.affected_decision_ids),
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "recheck_state": self.recheck_state.value,
            "pending_recheck_ids": list(self.pending_recheck_ids),
            "issue_codes": list(self.issue_codes),
            "decisions": [
                {
                    "decision_id": d.decision_id,
                    "before_candidate_id": d.before_candidate_id,
                    "after_candidate_id": d.after_candidate_id,
                    "changed": d.changed,
                    "requires_recheck": d.requires_recheck,
                    "before_verdict": d.before_verdict,
                    "after_verdict": d.after_verdict,
                    "before_evidence_ids": list(d.before_evidence_ids),
                    "after_evidence_ids": list(d.after_evidence_ids),
                    "before_reason_codes": list(d.before_reason_codes),
                    "after_reason_codes": list(d.after_reason_codes),
                }
                for d in self.decisions
            ],
            "provider_authenticated": False,
            "external_action_executed": False,
            "release_authorized": False,
            "qualification": "INTEGRITY_ONLY_NOT_CULTURAL_APPROVAL",
        }


def build_repair_trace(
    initial_plan: PlanSnapshot,
    initial_ledger: RepairLedger,
    before_review: ReviewEnvelope,
    patch: RepairPatch,
    preview: RepairPreview,
    *,
    after_review: ReviewEnvelope | None,
    as_of: datetime,
    max_evidence_age: timedelta,
) -> RepairTrace:
    """Recompute the preview and inspect optional latest-revision recheck.

    Replaying the proposal protects against callers injecting forged change sets,
    attempts, digests or dependent-invalidation lists. An after-review with bad
    provenance is displayed as BLOCKED, never as success. No I/O is performed.
    """
    canonical = preview_repair(
        initial_plan, initial_ledger, before_review, patch,
        as_of=as_of, max_evidence_age=max_evidence_age,
    )
    if canonical != preview:
        raise RepairReviewRejected("preview does not match the validated repair")
    revised = canonical.proposed_plan
    if after_review is not None and after_review.source_mode != before_review.source_mode:
        # Mixed data provenance cannot silently become a success.
        cross_source = True
    else:
        cross_source = False

    issues: tuple[str, ...]
    pending = canonical.ledger.pending_recheck_ids
    if after_review is None:
        state = RecheckState.PENDING
        issues = ("RECHECK_NOT_SUBMITTED",)
    else:
        integrity = check_review_integrity(
            revised, after_review, as_of=as_of,
            max_evidence_age=max_evidence_age,
        )
        problems = {item.code for item in integrity.issues}
        if cross_source:
            problems.add("SOURCE_MODE_CHANGED")
        if after_review.policy_version != before_review.policy_version:
            problems.add("POLICY_VERSION_CHANGED")
        if not problems:
            # The helper validates *all* required decisions, not merely the
            # changed one; no old PASS verdict is carried to a new revision.
            cleared = record_recheck_integrity(
                revised, canonical.ledger, after_review,
                as_of=as_of, max_evidence_age=max_evidence_age,
            )
            if cleared.pending_recheck_ids:
                raise RepairReviewRejected("recheck ledger did not clear")
            pending = ()
            state = RecheckState.STRUCTURAL_PASS
            issues = ()
        else:
            state = RecheckState.BLOCKED
            issues = tuple(sorted(problems))

    before_verdicts = {v.decision_id: v for v in before_review.verdicts}
    after_verdicts = (
        {v.decision_id: v for v in after_review.verdicts}
        if after_review is not None else {}
    )
    initial_by_id = {d.decision_id: d for d in initial_plan.decisions}
    trace_decisions: list[DecisionTrace] = []
    for current in revised.decisions:
        previous = initial_by_id[current.decision_id]
        before_v = before_verdicts.get(current.decision_id)
        after_v = after_verdicts.get(current.decision_id)
        trace_decisions.append(DecisionTrace(
            decision_id=current.decision_id,
            before_candidate_id=previous.candidate_id,
            after_candidate_id=current.candidate_id,
            changed=previous.candidate_id != current.candidate_id,
            requires_recheck=current.decision_id in canonical.affected_decision_ids,
            before_verdict=before_v.verdict.value if before_v else None,
            after_verdict=after_v.verdict.value if after_v else None,
            before_evidence_ids=before_v.evidence_refs if before_v else (),
            after_evidence_ids=after_v.evidence_refs if after_v else (),
            before_reason_codes=before_v.reason_codes if before_v else (),
            after_reason_codes=after_v.reason_codes if after_v else (),
        ))
    return RepairTrace(
        plan_id=initial_plan.plan_id,
        baseline_digest=initial_plan.baseline_digest,
        constraints_digest=initial_plan.constraints_digest,
        initial_revision=initial_plan.revision,
        proposed_revision=revised.revision,
        before_plan_digest=plan_digest(initial_plan),
        after_plan_digest=plan_digest(revised),
        source_mode=before_review.source_mode,
        policy_version=before_review.policy_version,
        changed_decision_id=patch.decision_id,
        affected_decision_ids=canonical.affected_decision_ids,
        attempt=canonical.ledger.attempts_used,
        max_attempts=canonical.ledger.max_attempts,
        recheck_state=state,
        pending_recheck_ids=pending,
        issue_codes=issues,
        decisions=tuple(trace_decisions),
    )
