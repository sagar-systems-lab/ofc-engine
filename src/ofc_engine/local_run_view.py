"""Safe same-origin projection of a private recorded agent decision run."""
from __future__ import annotations

from .contracts import SourceMode
from .agent_run import RunSnapshot


def project_local_run(
    snapshot: RunSnapshot, *, operator_constructed_hypotheses: int = 0,
) -> dict:
    if type(operator_constructed_hypotheses) is not int or not 0 <= operator_constructed_hypotheses <= 63:
        raise ValueError("invalid hypothesis count")
    if (
        snapshot.source_mode not in (SourceMode.RECORDED_REDACTED, SourceMode.LIVE_HTTP_UNVERIFIED)
        or snapshot.release_state != "REVIEW_REQUIRED"
        or snapshot.release_authorized
        or snapshot.provider_authenticated
        or snapshot.independent_recheck_completed
        or snapshot.external_action_executed
        or snapshot.real_qloo_requests not in (0, 1)
        or (snapshot.source_mode == SourceMode.RECORDED_REDACTED and snapshot.real_qloo_requests)
        or (snapshot.source_mode == SourceMode.LIVE_HTTP_UNVERIFIED and snapshot.real_qloo_requests != 1)
    ):
        raise ValueError("unqualified local run")

    if (
        snapshot.comparison.same_baseline is not True
        or snapshot.comparison.baseline_plan_digest != snapshot.baseline_plan_digest
        or snapshot.verification.status != "REVIEW_REQUIRED"
        or snapshot.verification.verified_revision != snapshot.plan_revision
        or snapshot.verification.verified_plan_digest != snapshot.plan_digest
        or snapshot.verification.policy_version != snapshot.policy_version
        or snapshot.handoff.plan_id != snapshot.run_id
        or snapshot.handoff.revision != snapshot.plan_revision
        or snapshot.handoff.verified_plan_digest != snapshot.plan_digest
        or snapshot.handoff.verification_status != "REVIEW_REQUIRED"
        or snapshot.handoff.source_mode != snapshot.source_mode
        or not snapshot.events
        or snapshot.events[0].plan_digest != snapshot.baseline_plan_digest
        or snapshot.events[-1].plan_digest != snapshot.plan_digest
        or snapshot.events[-1].revision != snapshot.plan_revision
        or snapshot.events[-1].invalidated_ids != snapshot.invalidated_ids
    ):
        raise ValueError("run proof is not bound to the current revision")

    decisions = []
    for item in snapshot.decision_verdicts:
        decisions.append({
            "decision_id": item.decision_id,
            "domain": item.domain,
            "verdict": item.verdict.value,
            "changed": item.decision_id in snapshot.changed_ids,
            "preserved": item.decision_id in snapshot.preserved_ids,
            "pending_recheck": item.pending_recheck,
            "depends_on": list(item.depends_on),
            "reason_codes": list(item.reason_codes),
        })
    result = {
        "status": ("HOSTED_AGENT_REVIEW" if snapshot.source_mode == SourceMode.LIVE_HTTP_UNVERIFIED
                   else "RECORDED_AGENT_REVIEW"),
        "source_mode": snapshot.source_mode.value,
        "historical_replay": snapshot.historical_replay,
        "revision": snapshot.plan_revision,
        "decision_count": len(decisions),
        "operator_constructed_unverified_decisions": operator_constructed_hypotheses,
        "decisions": decisions,
        "changed_ids": list(snapshot.changed_ids),
        "preserved_ids": list(snapshot.preserved_ids),
        "invalidated_ids": list(snapshot.invalidated_ids),
        "pending_rechecks": list(snapshot.pending_rechecks),
        "recorded_rank_recheck_pass": snapshot.recorded_rank_recheck_pass,
        "off_on": {
            "same_baseline": snapshot.comparison.same_baseline,
            "changed_ids": list(snapshot.comparison.evidence_caused_change_ids),
            "off_verdicts": {
                k: v.value for k, v in snapshot.comparison.evidence_off_verdicts.items()
            },
            "on_verdicts": {
                k: v.value for k, v in snapshot.comparison.evidence_on_verdicts.items()
            },
            "real_causal_proof": False,
        },
        "blockers": list(snapshot.blockers),
        "release_state": "REVIEW_REQUIRED",
        "provider_authenticated": False,
        "independent_recheck_completed": False,
        "release_authorized": False,
        "external_action_executed": False,
        "real_qloo_requests": snapshot.real_qloo_requests,
    }
    if snapshot.source_mode == SourceMode.LIVE_HTTP_UNVERIFIED:
        result["provenance"] = {
            "baseline_digest": snapshot.baseline_digest,
            "baseline_plan_digest": snapshot.baseline_plan_digest,
            "current_plan_digest": snapshot.plan_digest,
            "reviewed_revision": snapshot.plan_revision,
            "policy_version": snapshot.policy_version,
            "journal_head": snapshot.events[-1].journal_hash,
            "journal_event_count": len(snapshot.events),
            "evidence_ref_count": len(snapshot.evidence_refs),
        }
        result["handoff"] = {
            "status": "WITHHELD",
            "reviewed_revision": snapshot.plan_revision,
            "reviewed_plan_digest": snapshot.plan_digest,
            "journal_head": snapshot.events[-1].journal_hash,
            "checked_decision_ids": list(snapshot.verification.checked_decision_ids),
            "pending_rechecks": list(snapshot.pending_rechecks),
            "blockers": list(snapshot.blockers),
            "external_action_executed": False,
        }
    return result
