"""Read-only server preflight for browser-authored agent proposals."""
from __future__ import annotations

import json
from typing import Any

from .intake import AgentProposal, freeze_proposal
from .revisions import plan_digest
from .webdemo import canonical_proposal

_MAX_DECISIONS = 64


def preflight(payload: Any) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"intent", "decisions"}:
        raise ValueError("invalid agent proposal")
    if not isinstance(payload["decisions"], list) or not 1 <= len(payload["decisions"]) <= _MAX_DECISIONS:
        raise ValueError("invalid decision count")
    proposal = AgentProposal.model_validate_json(
        json.dumps(payload, allow_nan=False, separators=(",", ":"))
    )
    plan, _ = freeze_proposal(proposal)
    canonical = canonical_proposal()
    matched = (
        proposal.intent.objective == canonical.intent.objective
        and proposal.intent.constraints == canonical.intent.constraints
        and proposal.intent.taste_context == canonical.intent.taste_context
        and proposal.decisions == canonical.decisions
    )
    return {
        "status": "REVIEW_REQUIRED",
        "source_mode": "EVIDENCE_UNAVAILABLE",
        "revision": plan.revision,
        "plan_digest": plan_digest(plan),
        "baseline_digest": plan.baseline_digest,
        "decision_count": len(plan.decisions),
        "synthetic_fixture_available": matched,
        "same_run_as_fixture": matched and proposal.intent.run_id == canonical.intent.run_id
            and proposal.intent.client_request_id == canonical.intent.client_request_id,
        "decisions": [
            {
                "decision_id": decision.decision_id,
                "verdict": "UNCERTAIN",
                "required": decision.required,
                "depends_on": list(decision.depends_on),
                "reason_codes": ["EVIDENCE_UNAVAILABLE"],
            }
            for decision in plan.decisions
        ],
        "provider_authenticated": False,
        "independent_recheck_completed": False,
        "release_authorized": False,
        "external_action_executed": False,
        "real_qloo_requests": 0,
        "stored_on_server": False,
    }
