from __future__ import annotations

from typing import Literal
from pydantic import Field

from .canonical import digest
from .models import Hash, Id, Plan, Strict

SUPPORTED = {
    "plan": ("ofc.plan/1",),
    "evidence": ("ofc.evidence/1",),
    "proof": ("ofc.control-proof/1", "ofc.control-cascade/1"),
    "action": ("ofc.action/1",),
}


def negotiate(kind, versions):
    for version in SUPPORTED.get(kind, ()):
        if version in versions:
            return version
    raise ValueError("UNSUPPORTED_SCHEMA_VERSION")


class Action(Strict):
    schema_version: Literal["ofc.action/1"] = Field(
        default="ofc.action/1", alias="schema"
    )
    tenant_id: Id
    plan_digest: Hash
    policy_digest: Hash
    evidence_root: Hash
    node_ids: tuple[Id, ...] = Field(min_length=1, max_length=64)
    action: Literal["REVIEW_HANDOFF"] = "REVIEW_HANDOFF"
    execution: Literal["WITHHELD"] = "WITHHELD"
    spend_minor: Literal[0] = 0


def migrate_supported(payload):
    """Keep the exact source alongside a typed conversion; unsupported fields cannot disappear."""
    if payload.get("schema") == "ofc.plan/1":
        plan = Plan.parse(payload)
        return {
            "status": "LOSSLESS",
            "plan": plan.data(),
            "source": payload,
            "source_digest": digest(payload),
            "field_ledger": [],
        }
    known = {
        "plan_id",
        "objective",
        "choices",
        "dependencies",
        "budget",
        "schema_version",
    }
    unsupported = sorted(set(payload) - known)
    ledger = [{"path": key, "status": "UNSUPPORTED_FIELD"} for key in unsupported]
    # Legacy proposals are deliberately reviewed rather than guessing nested field mappings.
    return {
        "status": "REVIEW_REQUIRED",
        "source": payload,
        "source_digest": digest(payload),
        "plan": None,
        "field_ledger": ledger
        or [{"path": "$", "status": "TYPED_LEGACY_ADAPTER_REQUIRED"}],
    }


def restore_source(envelope):
    if digest(envelope["source"]) != envelope["source_digest"]:
        raise ValueError("migration source mismatch")
    return envelope["source"]


def migrate_proposal(payload, *, tenant_id, actor_id):
    from ..intake import AgentProposal
    from .adapters import from_proposal

    legacy = AgentProposal.model_validate(payload)
    plan = from_proposal(legacy, tenant_id=tenant_id, actor_id=actor_id)
    mapping = {
        "intent.run_id": "idempotency_key",
        "intent.objective": "objective",
        "intent.constraints.budget_usd": "budgets.USD.limit_minor",
        "decisions.*.decision_id": "nodes.*.id",
        "decisions.*.candidate_id": "nodes.*.candidate_id",
        "decisions.*.candidate_pool": "nodes.*.candidate_pool",
        "decisions.*.mutable_fields": "nodes.*.mutable_fields",
        "decisions.*.depends_on": "edges.REQUIRES",
        "decisions.*.locality_ref": "nodes.*.locality",
        "decisions.*.required": "nodes.*.required",
        "decisions.*.domain": "nodes.*.domain",
    }
    ledger = []

    def visit(value, path=""):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, path + "." + key if path else key)
        elif isinstance(value, list):
            for item in value:
                visit(item, path + ".*")
        else:
            target = mapping.get(path)
            ledger.append(
                {
                    "path": path,
                    "status": "MAPPED" if target else "REVIEW_REQUIRED",
                    "target": target,
                }
            )

    visit(payload)
    return {
        "status": "REVIEW_REQUIRED"
        if any(x["status"] == "REVIEW_REQUIRED" for x in ledger)
        else "LOSSLESS_SUPPORTED_FIELDS",
        "plan": plan.data(),
        "source": payload,
        "source_digest": digest(payload),
        "field_ledger": ledger,
    }
