"""Bind recorded Qloo affinity to a revision without granting live authority."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .contracts import (
    DecisionVerdict, EvidenceQuerySpec, EvidenceStatus, PlanSnapshot,
    QlooEvidence, SourceMode, Verdict,
)
from .proof import EvidenceBinding, ReviewEnvelope
from .qloo import ProbeSpec
from .qloo_affinity import AffinityCaptureError, normalize_affinity_capture
from .revisions import plan_digest


RECORDED_RANK_POLICY = "recorded-qloo-affinity-rank-v1"


def evaluate_recorded_affinity(
    plan: PlanSnapshot,
    *,
    decision_id: str,
    capture: Any,
    spec: ProbeSpec,
    as_of: datetime,
    max_evidence_age: timedelta,
    evidence_enabled: bool = True,
) -> ReviewEnvelope:
    """Review fixed-pool brand rank evidence; do not claim cultural or release approval."""
    if as_of.utcoffset() is None or max_evidence_age <= timedelta(0):
        raise ValueError("timezone-aware clock and positive evidence age required")
    if not isinstance(plan, PlanSnapshot) or not isinstance(spec, ProbeSpec):
        raise ValueError("plan and query specification required")

    by_id = {decision.decision_id: decision for decision in plan.decisions}
    if decision_id not in by_id:
        raise ValueError("unknown target decision")
    decision = by_id[decision_id]
    reason = "RECORDED_EVIDENCE_DISABLED" if not evidence_enabled else "RECORDED_EVIDENCE_UNAVAILABLE"
    binding: EvidenceBinding | None = None
    choice_rank: int | None = None

    if evidence_enabled:
        if decision.domain not in ("brand", "partner"):
            reason = "UNSUPPORTED_DECISION_DOMAIN"
        elif decision.locality_ref is not None:
            reason = "UNQUALIFIED_BRAND_LOCALITY"
        else:
            try:
                observed = normalize_affinity_capture(capture, spec)
            except (AffinityCaptureError, TypeError, ValueError):
                reason = "RECORDED_AFFINITY_REJECTED"
            else:
                ranks = {item["entity_id"]: item["rank"] for item in observed["ranking"]}
                if set(ranks) != set(decision.candidate_pool):
                    reason = "CANDIDATE_POOL_MISMATCH"
                elif not observed["ranking"] or observed["candidate_count"] != len(ranks):
                    reason = "INCOMPLETE_RECORDED_RANKS"
                else:
                    collected_at = datetime.fromisoformat(observed["collected_at"])
                    if collected_at > as_of:
                        reason = "RECORDED_EVIDENCE_FROM_FUTURE"
                    elif as_of - collected_at >= max_evidence_age:
                        reason = "RECORDED_EVIDENCE_STALE"
                    else:
                        fingerprint = observed["query_fingerprint"]
                        evidence_id = f"qloo-recorded-{fingerprint}"
                        binding = EvidenceBinding(
                            decision_id=decision_id,
                            query=EvidenceQuerySpec(
                                query_id=f"qloo-affinity-{fingerprint}",
                                capability="recorded-brand-affinity-rank",
                                input_entity_ids=(observed["input_entity_id"],),
                                candidate_ids=tuple(item["entity_id"] for item in observed["ranking"]),
                                locality_ref=None,
                                request_fingerprint=fingerprint,
                            ),
                            evidence=QlooEvidence(
                                evidence_id=evidence_id,
                                source_mode=SourceMode.RECORDED_REDACTED,
                                status=EvidenceStatus.VALID,
                                query_fingerprint=fingerprint,
                                collected_at=collected_at,
                                expires_at=collected_at + max_evidence_age,
                                candidate_ranks=ranks,
                                explainability={
                                    "signal_explainability_present": {
                                        item["entity_id"]: item["signal_explainability_present"]
                                        for item in observed["ranking"]
                                    }
                                },
                            ),
                        )
                        choice_rank = ranks[decision.candidate_id]

    verdicts: list[DecisionVerdict] = []
    for item in plan.decisions:
        if item.decision_id != decision_id:
            verdict = Verdict.UNCERTAIN
            codes = ("NO_RECORDED_EVIDENCE_FOR_DECISION",)
            refs: tuple[str, ...] = ()
        elif binding is None:
            verdict = Verdict.UNCERTAIN
            codes = (reason,)
            refs = ()
        else:
            verdict = Verdict.PASS if choice_rank == 1 else Verdict.REPAIR
            codes = ("RECORDED_TOP_AFFINITY_RANK",) if choice_rank == 1 else (
                "RECORDED_ALTERNATIVE_HIGHER_AFFINITY",)
            refs = (binding.evidence.evidence_id,)
        verdicts.append(DecisionVerdict(
            decision_id=item.decision_id,
            plan_revision=plan.revision,
            verdict=verdict,
            reason_codes=codes,
            evidence_refs=refs,
            policy_version=RECORDED_RANK_POLICY,
        ))

    return ReviewEnvelope(
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        plan_digest=plan_digest(plan),
        source_mode=SourceMode.RECORDED_REDACTED,
        policy_version=RECORDED_RANK_POLICY,
        verdicts=tuple(verdicts),
        bindings=(binding,) if binding is not None else (),
    )
