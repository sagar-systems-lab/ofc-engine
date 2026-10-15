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
        snapshot.source_mode != SourceMode.RECORDED_REDACTED
        or snapshot.release_state != "REVIEW_REQUIRED"
        or snapshot.release_authorized
        or snapshot.provider_authenticated
        or snapshot.independent_recheck_completed
        or snapshot.external_action_executed
        or snapshot.real_qloo_requests
    ):
        raise ValueError("unqualified local run")

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
    return {
        "status": "RECORDED_AGENT_REVIEW",
        "source_mode": "RECORDED_REDACTED",
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
        "real_qloo_requests": 0,
    }
