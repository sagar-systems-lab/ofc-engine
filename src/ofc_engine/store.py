"""Reference single-process atomic patch store. It has NO restart durability.

Only a validated RepairPatch can mutate plan state. Durable journals and a
multi-process CAS backend require separate qualification before hosted use.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from .contracts import PlanSnapshot, RepairPatch
from .revisions import RevisionConflict, apply_patch, digest_obj


class IdempotencyConflict(Exception):
    pass


@dataclass(frozen=True)
class _Dedup:
    digest: str
    plan: PlanSnapshot
    invalidated_ids: tuple[str, ...]


class MemoryRevisionStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._plans: dict[str, PlanSnapshot] = {}
        self._dedup: dict[tuple[str, str], _Dedup] = {}

    def insert(self, plan: PlanSnapshot) -> None:
        with self._lock:
            if plan.plan_id in self._plans:
                raise RevisionConflict("plan already exists")
            self._plans[plan.plan_id] = plan

    def get(self, plan_id: str) -> PlanSnapshot:
        with self._lock:
            return self._plans[plan_id]

    def apply_patch_once(
        self,
        plan_id: str,
        patch: RepairPatch,
        *,
        client_request_id: str,
    ) -> tuple[PlanSnapshot, tuple[str, ...]]:
        """Atomic within one Python process; no durable journal or cross-worker lock."""
        if not client_request_id:
            raise ValueError("client_request_id is required")
        fp = digest_obj(patch.model_dump(mode="json"))
        key = (plan_id, client_request_id)
        with self._lock:
            previous = self._dedup.get(key)
            if previous is not None:
                if previous.digest != fp:
                    raise IdempotencyConflict("same request key with different patch")
                return previous.plan, previous.invalidated_ids
            current = self._plans.get(plan_id)
            if current is None:
                raise KeyError("unknown plan")
            next_plan, invalidated = apply_patch(current, patch)
            self._plans[plan_id] = next_plan
            self._dedup[key] = _Dedup(fp, next_plan, invalidated)
            return next_plan, invalidated
