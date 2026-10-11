"""Pure plan revisions and dependency invalidation."""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from .contracts import PlanSnapshot, RepairPatch, Decision


class RevisionConflict(Exception):
    pass


class PatchRejected(Exception):
    pass


def canonical_bytes(value: Any) -> bytes:
    """Deterministic Python digest; cross-language parity is unverified."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest_obj(value: Any) -> str:
    return sha256(canonical_bytes(value)).hexdigest()


def plan_digest(plan: PlanSnapshot) -> str:
    return digest_obj(plan.model_dump(mode="json"))


def affected_dependents(plan: PlanSnapshot, changed_id: str) -> tuple[str, ...]:
    if changed_id not in {d.decision_id for d in plan.decisions}:
        raise PatchRejected("unknown changed decision")
    affected={changed_id}
    changed=True
    while changed:
        before=len(affected)
        for d in plan.decisions:
            if any(dep in affected for dep in d.depends_on):
                affected.add(d.decision_id)
        changed=len(affected)!=before
    return tuple(d.decision_id for d in plan.decisions if d.decision_id in affected)


def apply_patch(plan: PlanSnapshot, patch: RepairPatch) -> tuple[PlanSnapshot, tuple[str, ...]]:
    if patch.base_revision != plan.revision:
        raise RevisionConflict("patch references a stale revision")
    if patch.precondition_digest != plan_digest(plan):
        raise RevisionConflict("patch precondition digest mismatched")
    matches=[d for d in plan.decisions if d.decision_id==patch.decision_id]
    if len(matches)!=1:
        raise PatchRejected("unknown decision")
    old=matches[0]
    if "candidate_id" not in old.mutable_fields:
        raise PatchRejected("decision candidate_id is immutable")
    if patch.old_candidate_id != old.candidate_id:
        raise RevisionConflict("old candidate no longer matches")
    if patch.new_candidate_id not in old.candidate_pool:
        raise PatchRejected("replacement outside sealed candidate pool")
    if not patch.evidence_refs:
        raise PatchRejected("repair requires evidence reference")
    revised=[]
    for d in plan.decisions:
        if d.decision_id==old.decision_id:
            revised.append(d.model_copy(update={"candidate_id":patch.new_candidate_id}))
        else:
            revised.append(d)
    # Preserve frozen baseline and constraint digest; verification must be newly obtained.
    updated=plan.model_copy(update={"revision":plan.revision+1,"decisions":tuple(revised)})
    updated=PlanSnapshot.model_validate(updated.model_dump())
    return updated, affected_dependents(plan, old.decision_id)
