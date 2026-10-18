from __future__ import annotations

from dataclasses import dataclass

from .canonical import digest
from .graph import claim_fingerprint
from .models import Evidence, instant
from .policies import evidence_reason, required_claims
from .storage import CapacityReached, Conflict


@dataclass(frozen=True)
class Capability:
    tenant_id: str
    provider: str
    account_scope: str
    entitlement_id: str
    claims: frozenset[str]
    expires_at: str
    rpm: int = 30
    rpd: int = 100
    total: int = 100
    concurrency: int = 2
    offline_only: bool = True
    revoked: bool = False

    def __post_init__(self):
        if not 1 <= self.concurrency <= 4 or min(self.rpm, self.rpd, self.total) < 1:
            raise ValueError("invalid acquisition limits")


class EvidencePlanner:
    """Durable admission for explicitly injected offline transports. No HTTP transport is installed."""

    def __init__(self, store, capability, adapter, *, authorities=None):
        self.store, self.capability, self.adapter = store, capability, adapter
        self.authorities = authorities or {}
        if (
            not capability.offline_only
            or getattr(adapter, "offline", False) is not True
        ):
            raise PermissionError("LIVE_COLLECTION_NOT_AUTHORIZED")

    def scopes(self, plan, evidence, *, as_of, baseline_digest=None):
        from .policies import evaluate_claim

        scopes = []
        for node in plan.nodes:
            for claim in required_claims(plan, node):
                verdict = evaluate_claim(
                    plan,
                    node,
                    claim,
                    evidence,
                    baseline_digest=baseline_digest or plan.digest,
                    at=instant(as_of),
                    synthetic=False,
                    authorities=self.authorities,
                )
                if verdict["status"] in {"QUALIFIED", "OBSERVED"}:
                    continue
                if claim not in self.capability.claims:
                    scopes.append(
                        {"node_id": node.id, "claim": claim, "state": "UNAVAILABLE"}
                    )
                    continue
                fields = {
                    "tenant_id": plan.tenant_id,
                    "provider": self.capability.provider,
                    "account_scope": self.capability.account_scope,
                    "entitlement_id": self.capability.entitlement_id,
                    "claim": claim,
                    "node_id": node.id,
                    "input_digest": claim_fingerprint(plan, node.id, claim),
                    "plan_digest": baseline_digest or plan.digest,
                    "locality": node.locality,
                    "pool": [c.id for c in node.candidate_pool],
                }
                scopes.append(
                    fields | {"scope_id": digest(fields), "state": "RECHECK_QUEUED"}
                )
        return scopes

    def _reserve(self, scope, as_of):
        cap, store = self.capability, self.store
        now = int(store.clock())
        if (
            cap.revoked
            or instant(as_of) >= instant(cap.expires_at)
            or scope["tenant_id"] != cap.tenant_id
        ):
            raise PermissionError("ACQUISITION_CAPABILITY_INVALID")
        with store._tx(write=True) as db:
            if store.postgres:
                db.execute(
                    f"select id from {store.prefix}capacity where id=1 for update"
                )
            row = store._run(
                db,
                f"select state,payload,payload_digest,expires_at from {store.prefix}attempts where scope_id=?",
                (scope["scope_id"],),
            ).fetchone()
            if row:
                if row[0] == "COMPLETE" and row[3] > now:
                    return store._decode(row[1], row[2])
                # Unknown outcome is reconciled explicitly; it is never automatically sent again.
                raise Conflict("EVIDENCE_ATTEMPT_RECONCILIATION_REQUIRED")
            counts = store._run(
                db,
                f"select count(*),sum(case when created_at>? then 1 else 0 end),sum(case when created_at>? then 1 else 0 end),sum(case when state='RESERVED' then 1 else 0 end) from {store.prefix}attempts where tenant_id=? and provider=? and account_scope=?",
                (now - 60, now - 86400, cap.tenant_id, cap.provider, cap.account_scope),
            ).fetchone()
            if (
                counts[0] >= cap.total
                or (counts[1] or 0) >= cap.rpm
                or (counts[2] or 0) >= cap.rpd
                or (counts[3] or 0) >= cap.concurrency
            ):
                raise CapacityReached(
                    "evidence quota exhausted; retry_after_seconds=60"
                )
            store._run(
                db,
                f"insert into {store.prefix}attempts values(?,?,?,?,?,'RESERVED',?,null,null,0)",
                (
                    scope["scope_id"],
                    cap.tenant_id,
                    cap.provider,
                    cap.account_scope,
                    scope["input_digest"],
                    now,
                ),
            )
        return None

    def collect(self, plan, evidence, *, as_of, baseline_digest=None):
        store, result = self.store, list(evidence)
        scopes = self.scopes(
            plan, evidence, as_of=as_of, baseline_digest=baseline_digest
        )
        deadline = store.clock() + plan.limits.deadline_ms / 1000
        for scope in scopes:
            if scope["state"] == "UNAVAILABLE":
                continue
            if store.clock() >= deadline:
                break
            cached = self._reserve(scope, as_of)
            if cached is None:
                # An adapter must honor the absolute deadline. No retries and no installed live transport.
                incoming = self.adapter.collect(scope, deadline=deadline)
                if store.clock() > deadline:
                    raise TimeoutError("EVIDENCE_COLLECTION_DEADLINE")
                incoming = tuple(
                    Evidence.model_validate_json(__import__("json").dumps(e))
                    for e in incoming
                )
                node = next(n for n in plan.nodes if n.id == scope["node_id"])
                for item in incoming:
                    if (
                        item.provider_account_scope != self.capability.account_scope
                        or item.entitlement_id != self.capability.entitlement_id
                        or item.source != self.capability.provider
                        or item.claim != scope["claim"]
                        or item.extraction_rule_version is None
                        or item.raw_payload_ref is None
                        or evidence_reason(
                            plan,
                            node,
                            item,
                            baseline_digest=baseline_digest or plan.digest,
                            at=instant(as_of),
                            synthetic=False,
                            authorities=self.authorities,
                        )
                    ):
                        raise ValueError("COLLECTOR_RESULT_SCOPE_MISMATCH")
                cached = [e.data() for e in incoming]
                expiry = min(
                    (int(instant(e.expires_at).timestamp()) for e in incoming),
                    default=int(store.clock()),
                )
                with store._tx(write=True) as db:
                    changed = store._run(
                        db,
                        f"update {store.prefix}attempts set state='COMPLETE',payload=?,payload_digest=?,expires_at=? where scope_id=? and state='RESERVED'",
                        (
                            store._encode(cached, private=True),
                            digest(cached),
                            expiry,
                            scope["scope_id"],
                        ),
                    ).rowcount
                    if changed != 1:
                        raise Conflict("collector reservation lost")
            for raw in cached:
                item = Evidence.model_validate_json(__import__("json").dumps(raw))
                node = next(n for n in plan.nodes if n.id == item.node_id)
                if evidence_reason(
                    plan,
                    node,
                    item,
                    baseline_digest=baseline_digest or plan.digest,
                    at=instant(as_of),
                    synthetic=False,
                    authorities=self.authorities,
                ):
                    raise ValueError("CACHED_EVIDENCE_NO_LONGER_QUALIFIED")
                if item.id not in {e.id for e in result}:
                    result.append(item)
        return tuple(result)


def resolve_entity(records, *, entity_id, provider, account_scope, locality, now):
    matches = [
        r
        for r in records
        if (
            r.get("entity_id"),
            r.get("provider"),
            r.get("account_scope"),
            r.get("locality"),
        )
        == (entity_id, provider, account_scope, locality)
        and r.get("expires_at", 0) > now
        and not r.get("revoked")
    ]
    if len(matches) != 1 or not matches[0].get("source_digest"):
        raise ValueError("ENTITY_RESOLUTION_UNQUALIFIED")
    return matches[0]
