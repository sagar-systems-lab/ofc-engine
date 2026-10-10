"""Provider-neutral integrity checks for evidence-bound decision reviews.

These checks validate provenance *references* and snapshot freshness, not the
truth of external results or the quality of cultural policy judgments. Only a
qualified evaluator and trusted provider adapter may establish those facts.
Nothing in this module authorizes an action or creates a release artifact.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import Field

from .contracts import (
    DecisionVerdict,
    EvidenceQuerySpec,
    EvidenceStatus,
    PlanSnapshot,
    QlooEvidence,
    SourceMode,
    StrictModel,
    Verdict,
)
from .revisions import plan_digest


class EvidenceBinding(StrictModel):
    """Explicitly bind a provider response to one decision and query."""

    decision_id: str = Field(min_length=1)
    query: EvidenceQuerySpec
    evidence: QlooEvidence


class ReviewEnvelope(StrictModel):
    """Caller-submitted review bound to an exact immutable plan snapshot."""

    plan_id: str = Field(min_length=1)
    plan_revision: int = Field(ge=0)
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_mode: SourceMode
    policy_version: str = Field(min_length=1)
    verdicts: tuple[DecisionVerdict, ...]
    bindings: tuple[EvidenceBinding, ...]


@dataclass(frozen=True)
class IntegrityIssue:
    code: str
    decision_id: str | None = None
    evidence_id: str | None = None


@dataclass(frozen=True)
class ReviewIntegrity:
    plan_id: str
    plan_revision: int
    plan_digest: str
    checked_decision_ids: tuple[str, ...]
    issues: tuple[IntegrityIssue, ...]

    @property
    def eligible_for_policy_verification(self) -> bool:
        """Integrity complete; *not* proof of cultural fit or release permission."""
        return not self.issues


def check_review_integrity(
    plan: PlanSnapshot,
    review: ReviewEnvelope,
    *,
    as_of: datetime,
    max_evidence_age: timedelta,
) -> ReviewIntegrity:
    """Fail closed on stale snapshots, unresolved decisions and unbound evidence.

    Both the review's declared source mode and provider response source mode
    must match. A declaration of VERIFIED_LIVE is not proof of authenticity;
    that guarantee belongs to the future trusted Qloo adapter.
    """
    if as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if max_evidence_age <= timedelta(0):
        raise ValueError("max_evidence_age must be positive")

    problems: list[IntegrityIssue] = []

    def issue(code: str, decision_id: str | None = None, evidence_id: str | None = None) -> None:
        problems.append(IntegrityIssue(code, decision_id, evidence_id))

    current_digest = plan_digest(plan)
    if review.plan_id != plan.plan_id:
        issue("PLAN_ID_MISMATCH")
    if review.plan_revision != plan.revision:
        issue("STALE_PLAN_REVISION")
    if review.plan_digest != current_digest:
        issue("PLAN_DIGEST_MISMATCH")

    by_id = {d.decision_id: d for d in plan.decisions}
    verdicts: dict[str, DecisionVerdict] = {}
    for verdict in review.verdicts:
        if verdict.decision_id in verdicts:
            issue("DUPLICATE_VERDICT", verdict.decision_id)
        verdicts[verdict.decision_id] = verdict
        if verdict.decision_id not in by_id:
            issue("UNKNOWN_DECISION", verdict.decision_id)
        if verdict.plan_revision != plan.revision:
            issue("STALE_VERDICT_REVISION", verdict.decision_id)
        if verdict.policy_version != review.policy_version:
            issue("POLICY_VERSION_MISMATCH", verdict.decision_id)

    evidence_by_id: dict[str, EvidenceBinding] = {}
    for binding in review.bindings:
        evidence_id = binding.evidence.evidence_id
        if evidence_id in evidence_by_id:
            issue("DUPLICATE_EVIDENCE_ID", binding.decision_id, evidence_id)
        else:
            evidence_by_id[evidence_id] = binding
        if binding.decision_id not in by_id:
            issue("EVIDENCE_UNKNOWN_DECISION", binding.decision_id, evidence_id)

    used_evidence: set[str] = set()
    for decision in plan.decisions:
        verdict = verdicts.get(decision.decision_id)
        if verdict is None:
            # Optional decisions may be skipped; required ones must be reviewed.
            if decision.required:
                issue("MISSING_REQUIRED_VERDICT", decision.decision_id)
            continue
        if verdict.verdict == Verdict.UNCERTAIN:
            issue("UNCERTAIN_DECISION", decision.decision_id)
        elif verdict.verdict == Verdict.REPAIR:
            issue("REPAIR_REQUIRED", decision.decision_id)
        if len(set(verdict.evidence_refs)) != len(verdict.evidence_refs):
            issue("DUPLICATE_EVIDENCE_REF", decision.decision_id)
        for evidence_id in verdict.evidence_refs:
            used_evidence.add(evidence_id)
            binding = evidence_by_id.get(evidence_id)
            if binding is None:
                issue("MISSING_EVIDENCE_REF", decision.decision_id, evidence_id)
                continue
            evidence = binding.evidence
            query = binding.query
            if binding.decision_id != decision.decision_id:
                issue("CROSS_DECISION_EVIDENCE", decision.decision_id, evidence_id)
            if evidence.query_fingerprint != query.request_fingerprint:
                issue("QUERY_FINGERPRINT_MISMATCH", decision.decision_id, evidence_id)
            if evidence.source_mode != review.source_mode:
                issue("SOURCE_MODE_MISMATCH", decision.decision_id, evidence_id)
            if evidence.status != EvidenceStatus.VALID:
                issue("EVIDENCE_NOT_VALID", decision.decision_id, evidence_id)
            if evidence.collected_at > as_of:
                issue("EVIDENCE_FROM_FUTURE", decision.decision_id, evidence_id)
            if as_of - evidence.collected_at > max_evidence_age:
                issue("EVIDENCE_TOO_OLD", decision.decision_id, evidence_id)
            if evidence.expires_at is not None and evidence.expires_at <= as_of:
                issue("EVIDENCE_EXPIRED", decision.decision_id, evidence_id)
            if query.locality_ref != decision.locality_ref:
                issue("LOCALITY_MISMATCH", decision.decision_id, evidence_id)
            if not query.candidate_ids or not set(query.candidate_ids).issubset(decision.candidate_pool):
                issue("QUERY_OUTSIDE_CANDIDATE_POOL", decision.decision_id, evidence_id)
            if decision.candidate_id not in query.candidate_ids:
                issue("SELECTED_CANDIDATE_NOT_QUERIED", decision.decision_id, evidence_id)
            if not set(evidence.candidate_ranks).issubset(query.candidate_ids):
                issue("EVIDENCE_OUTSIDE_QUERY", decision.decision_id, evidence_id)
            if decision.candidate_id not in evidence.candidate_ranks:
                issue("SELECTED_CANDIDATE_NOT_RANKED", decision.decision_id, evidence_id)

    for evidence_id, binding in evidence_by_id.items():
        if evidence_id not in used_evidence:
            issue("UNREFERENCED_EVIDENCE", binding.decision_id, evidence_id)

    return ReviewIntegrity(
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        plan_digest=current_digest,
        checked_decision_ids=tuple(d.decision_id for d in plan.decisions if d.decision_id in verdicts),
        issues=tuple(dict.fromkeys(problems)),
    )
