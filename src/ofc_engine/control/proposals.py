from __future__ import annotations

import json

from pydantic import Field

from .engine import apply_patches
from .graph import constraints, impact
from .models import Hash, Patch, Plan, Strict
from .storage import Conflict


class ProposalInput(Strict):
    revision: int = Field(ge=0)
    plan_digest: Hash
    policy_digest: Hash
    evidence_root: Hash
    patches: tuple[Patch, ...] = Field(min_length=1, max_length=64)


def preview(runtime, review_id, identity, payload):
    proposal = ProposalInput.model_validate_json(json.dumps(payload))
    proof = runtime.store.proof(review_id, identity.tenant_id, identity.actor_id, authorities=runtime.authorities)
    if (proposal.revision != proof["revision"] or proposal.plan_digest != proof["current_digest"]
            or proposal.policy_digest != proof["policy_digest"] or proposal.evidence_root != proof["evidence_root"]):
        raise Conflict("proposal scope differs from reviewed revision")
    plan = Plan.parse(proof["current"])
    known_evidence = {e["id"] for e in proof["evidence"]}
    if any(not set(p.evidence_ids) <= known_evidence for p in proposal.patches):
        raise ValueError("proposal evidence outside review")
    proposed = apply_patches(plan, proposal.patches)
    return {"status": "DRY_RUN_ONLY", "parent_digest": plan.digest, "proposed_digest": proposed.digest,
            "proposal": proposed.data(), "impact": impact(plan, {p.node_id: set(p.changes) for p in proposal.patches}),
            "constraints": constraints(proposed), "evidence_status": "RECHECK_REQUIRED",
            "revision_committed": False, "external_action_executed": False,
            "provider_calls": 0, "spend_minor": 0}
