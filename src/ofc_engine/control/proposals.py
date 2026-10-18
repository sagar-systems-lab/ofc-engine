from __future__ import annotations

import json

from pydantic import Field

from .engine import apply_patches, decision_options
from .graph import constraints, impact
from .models import Hash, Patch, Plan, Strict, ReviewRequest
from .storage import Conflict


class ProposalInput(Strict):
    revision: int = Field(ge=0)
    plan_digest: Hash
    policy_digest: Hash
    evidence_root: Hash
    patches: tuple[Patch, ...] = Field(min_length=1, max_length=64)


def preview(runtime, review_id, identity, payload):
    proposal = ProposalInput.model_validate_json(json.dumps(payload))
    proof = runtime.store.proof(
        review_id,
        identity.tenant_id,
        identity.actor_id,
        authorities=runtime.authorities,
    )
    if (
        proposal.revision != proof["revision"]
        or proposal.plan_digest != proof["current_digest"]
        or proposal.policy_digest != proof["policy_digest"]
        or proposal.evidence_root != proof["evidence_root"]
    ):
        raise Conflict("proposal scope differs from reviewed revision")
    plan = Plan.parse(proof["current"])
    known_evidence = {e["id"] for e in proof["evidence"]}
    if any(not set(p.evidence_ids) <= known_evidence for p in proposal.patches):
        raise ValueError("proposal evidence outside review")
    request = ReviewRequest.parse(
        {
            "plan": proof["current"],
            "evidence": proof["evidence"],
            "mode": proof["mode"],
            "as_of": proof["as_of"],
        }
    )
    from .policies import evaluate

    evaluation = {
        "baseline_digest": proof["baseline_digest"],
        "as_of": proof["as_of"],
        "synthetic": proof["mode"] == "SYNTHETIC",
        "authorities": runtime.authorities,
    }
    verdicts = evaluate(plan, request.evidence, **evaluation)
    options = decision_options(
        plan, verdicts, evidence=request.evidence, evaluation=evaluation
    )
    eligibility = []
    for patch in proposal.patches:
        alternatives = options.get(patch.node_id, {}).get("alternatives", [])
        qualified = any(
            a["changes"] == patch.changes
            and set(a["evidence_ids"]) <= set(patch.evidence_ids)
            for a in alternatives
        )
        eligibility.append(
            {
                "node_id": patch.node_id,
                "eligible": qualified,
                "fields": [
                    {
                        "field": f,
                        "reason": "SCOPED_POLICY_SUPPORTED"
                        if qualified
                        else "NO_QUALIFIED_FIELD_EVIDENCE",
                    }
                    for f in patch.changes
                ],
            }
        )
    proposed = apply_patches(plan, proposal.patches)
    return {
        "status": "DRY_RUN_ONLY",
        "eligibility": eligibility,
        "policy_status": "POLICY_QUALIFIED_PREVIEW"
        if all(e["eligible"] for e in eligibility)
        else "POLICY_UNQUALIFIED_PREVIEW",
        "parent_digest": plan.digest,
        "proposed_digest": proposed.digest,
        "proposal": proposed.data(),
        "impact": impact(plan, {p.node_id: set(p.changes) for p in proposal.patches}),
        "constraints": constraints(proposed),
        "evidence_status": "RECHECK_REQUIRED",
        "revision_committed": False,
        "external_action_executed": False,
        "provider_calls": 0,
        "spend_minor": 0,
    }
