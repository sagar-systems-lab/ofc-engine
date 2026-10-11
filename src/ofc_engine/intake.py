"""Freeze agent-proposed choices before evidence collection or repair."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import re

from pydantic import Field, model_validator

from .contracts import ActionIntent, Decision, PlanSnapshot, RunPhase, SCHEMA_VERSION, StrictModel
from .durable import SQLiteRevisionStore, StorageIntegrityError
from .revisions import RevisionConflict, canonical_bytes, digest_obj, plan_digest

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_SUBMISSION_BYTES = 96 * 1024


class ProposalConflict(RuntimeError):
    pass


class AgentProposal(StrictModel):
    intent: ActionIntent
    decisions: tuple[Decision, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def check_intake(self) -> AgentProposal:
        intent = self.intent
        if intent.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported action intent schema version")
        if not _ID.fullmatch(intent.run_id) or not _ID.fullmatch(intent.client_request_id):
            raise ValueError("invalid run or request identifier")
        if not intent.objective.strip() or len(intent.objective) > 2048:
            raise ValueError("objective must be non-blank and bounded")
        if any(len(d.candidate_pool) > 64 for d in self.decisions):
            raise ValueError("candidate pool exceeds limit")
        try:
            encoded = canonical_bytes(self.model_dump(mode="json"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("proposal must be finite, JSON-serializable data") from exc
        if len(encoded) > _MAX_SUBMISSION_BYTES:
            raise ValueError("proposal exceeds size limit")
        return self


@dataclass(frozen=True)
class IntakeReceipt:
    plan_id: str
    run_id: str
    request_id: str
    plan_revision: int
    plan_digest: str
    baseline_digest: str
    constraints_digest: str
    candidate_pool_digest: str
    created: bool
    phase: RunPhase = RunPhase.BASELINE_FROZEN
    source_status: str = "EVIDENCE_PENDING"
    release_authorized: bool = False
    external_action_executed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "plan_revision": self.plan_revision,
            "plan_digest": self.plan_digest,
            "baseline_digest": self.baseline_digest,
            "constraints_digest": self.constraints_digest,
            "candidate_pool_digest": self.candidate_pool_digest,
            "created": self.created,
            "phase": self.phase.value,
            "source_status": self.source_status,
            "release_authorized": self.release_authorized,
            "external_action_executed": self.external_action_executed,
        }


def freeze_proposal(proposal: AgentProposal) -> tuple[PlanSnapshot, str]:
    proposal = AgentProposal.model_validate(proposal.model_dump())
    payload = proposal.model_dump(mode="json")
    constraints_digest = digest_obj(payload["intent"]["constraints"])
    candidate_pool_digest = digest_obj([
        {"decision_id": d.decision_id, "candidate_pool": list(d.candidate_pool)}
        for d in proposal.decisions
    ])
    baseline_digest = digest_obj({
        "profile": "agent-proposal-v1",
        "intent": payload["intent"],
        "decisions": payload["decisions"],
    })
    plan = PlanSnapshot(
        plan_id=proposal.intent.run_id,
        revision=0,
        baseline_digest=baseline_digest,
        constraints_digest=constraints_digest,
        decisions=proposal.decisions,
    )
    return plan, candidate_pool_digest


def accept_proposal(store: SQLiteRevisionStore, proposal: AgentProposal) -> IntakeReceipt:
    plan, pool_digest = freeze_proposal(proposal)
    created = True
    try:
        store.insert(plan)
    except RevisionConflict as exc:
        existing = store.get(plan.plan_id)
        if existing.revision != 0 or existing != plan:
            raise ProposalConflict("run ID already bound to a different or revised proposal") from exc
        created = False

    journal = store.audit_history(plan.plan_id)
    if (
        len(journal) != 1
        or journal[0].kind != "INSERT"
        or journal[0].after_digest != plan_digest(plan)
        or store.pending_rechecks(plan.plan_id)
    ):
        raise StorageIntegrityError("frozen proposal has inconsistent revision state")

    return IntakeReceipt(
        plan_id=plan.plan_id,
        run_id=proposal.intent.run_id,
        request_id=proposal.intent.client_request_id,
        plan_revision=0,
        plan_digest=plan_digest(plan),
        baseline_digest=plan.baseline_digest,
        constraints_digest=plan.constraints_digest,
        candidate_pool_digest=pool_digest,
        created=created,
    )
