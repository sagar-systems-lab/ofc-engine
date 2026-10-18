from __future__ import annotations

import base64
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json

from .canonical import canonical_bytes, digest
from .collection_contracts import CollectionGrant
from .graph import claim_fingerprint, topological
from .models import Evidence, Plan, instant
from .policies import POLICY_DIGEST, evidence_reason, required_claims, evaluate_claim
from .storage import CapacityReached, Conflict


class CoordinatorStopped(Conflict):
    pass


TABLES = (
    "provider_io",
    "claim_cache",
    "entity_cache",
    "collection_rounds",
    "source_revocations",
)


def migrate_tables(store, db):
    p = store.prefix
    db.execute(f"""create table if not exists {p}provider_io (
        call_id text primary key, review_id text not null, tenant_id text not null,
        provider text not null, account_scope text not null, grant_id text not null,
        state text not null, created_at bigint not null, transport_live integer not null,
        request_digest text not null, payload text not null, payload_digest text not null,
        response text, response_digest text)""")
    db.execute(
        f"create index if not exists control_provider_admission on {p}provider_io(provider,account_scope,transport_live,created_at)"
    )
    db.execute(f"""create table if not exists {p}claim_cache (
        cache_key text primary key, tenant_id text not null, expires_at bigint not null,
        payload text not null, payload_digest text not null)""")
    db.execute(f"""create table if not exists {p}entity_cache (
        cache_key text primary key, tenant_id text not null, expires_at bigint not null,
        payload text not null, payload_digest text not null)""")
    db.execute(f"""create table if not exists {p}collection_rounds (
        review_id text not null references {p}reviews(review_id), round integer not null,
        parent_digest text not null, policy_digest text not null, payload text not null,
        payload_digest text not null, primary key(review_id,round))""")
    db.execute(
        f"create table if not exists {p}source_revocations (tenant_id text not null,entitlement_id text not null,revoked_at bigint not null,primary key(tenant_id,entitlement_id))"
    )
    if store.postgres:
        for table in TABLES:
            db.execute(f"alter table {p}{table} enable row level security")


@dataclass(frozen=True)
class SourceBinding:
    grant: CollectionGrant
    adapter: object


def timestamp(clock):
    return (
        datetime.fromtimestamp(clock(), timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class GraphCollector:
    def __init__(
        self, store, bindings, *, authorities, issuer, signing_key, clock=None
    ):
        self.store, self.bindings, self.authorities = (
            store,
            tuple(bindings),
            authorities,
        )
        self.issuer, self.key, self.clock = issuer, signing_key, clock or store.clock
        self.public_keys = {issuer: signing_key.public_key().public_bytes_raw()}
        if (
            not self.bindings
            or len(self.bindings) > 8
            or len({b.grant.id for b in self.bindings}) != len(self.bindings)
        ):
            raise ValueError("bounded distinct source grants required")
        with store._tx() as db:
            db.execute(f"select call_id from {store.prefix}provider_io limit 0")
            revocations = store._run(
                db,
                f"select tenant_id,entitlement_id from {store.prefix}source_revocations",
            ).fetchall()
        for tenant, entitlement in revocations:
            self._apply_revocation(tenant, entitlement)

    def _active(self, binding, plan, at):
        g = binding.grant
        with self.store._tx() as db:
            revoked = self.store._run(
                db,
                f"select revoked_at from {self.store.prefix}source_revocations where tenant_id=? and entitlement_id=?",
                (g.tenant_id, g.entitlement_id),
            ).fetchone()
        if revoked:
            self._apply_revocation(g.tenant_id, g.entitlement_id)
            raise PermissionError("SOURCE_ENTITLEMENT_REVOKED")
        if (
            g.revoked
            or instant(g.expires_at) <= instant(at)
            or g.tenant_id != plan.tenant_id
        ):
            raise PermissionError("SOURCE_GRANT_INVALID")
        if not getattr(binding.adapter, "offline", False) and (
            not g.allow_live
            or not g.free_entitlement
            or plan.limits.provider_calls == 0
        ):
            raise PermissionError("LIVE_COLLECTION_NOT_AUTHORIZED")

    def scopes(self, plan, evidence, *, anchor_digest, as_of, only_nodes=None):
        nodes = {n.id: n for n in plan.nodes}
        output = []
        for key in topological(plan):
            if only_nodes is not None and key not in only_nodes:
                continue
            node = nodes[key]
            windows = {(node.start_at, node.end_at)}
            if {"start_at", "end_at"} <= set(node.mutable_fields):
                windows.update(
                    (e.facts.get("start_at"), e.facts.get("end_at"))
                    for e in evidence
                    if e.node_id == key
                    and e.claim in {"schedule_window", "venue_capacity"}
                )
            for claim in required_claims(plan, node):
                binding = next(
                    (
                        b
                        for b in self.bindings
                        if b.grant.tenant_id == plan.tenant_id
                        and claim in b.grant.claims
                        and claim in b.adapter.claims
                    ),
                    None,
                )
                if binding is None:
                    output.append(
                        {
                            "node_id": key,
                            "claim": claim,
                            "state": "NOT_EVALUATED",
                            "reason": "SOURCE_UNAVAILABLE",
                        }
                    )
                    continue
                cultural = claim in {
                    "cultural_brand_rank",
                    "place_cultural_relevance",
                    "place_cultural_observation",
                }
                candidates = (
                    (node.candidate_id,)
                    if cultural or "candidate_id" not in node.mutable_fields
                    else tuple(c.id for c in node.candidate_pool)
                )
                for candidate in candidates:
                    for start, end in sorted(windows, key=str):
                        payload = plan.data()
                        for n in payload["nodes"]:
                            if n["id"] == key:
                                n.update(
                                    candidate_id=candidate, start_at=start, end_at=end
                                )
                        try:
                            variant = Plan.parse(payload)
                        except ValueError:
                            continue
                        selected = next(n for n in variant.nodes if n.id == key)
                        verdict = evaluate_claim(
                            variant,
                            selected,
                            claim,
                            evidence,
                            baseline_digest=anchor_digest,
                            at=instant(as_of),
                            synthetic=False,
                            authorities=self.authorities,
                        )
                        if verdict["status"] in {"QUALIFIED", "OBSERVED"}:
                            continue
                        g = binding.grant
                        fields = {
                            "tenant_id": plan.tenant_id,
                            "node_id": key,
                            "claim": claim,
                            "provider": g.provider,
                            "account_scope": g.account_scope,
                            "entitlement_id": g.entitlement_id,
                            "grant_id": g.id,
                            "anchor_digest": anchor_digest,
                            "policy_digest": POLICY_DIGEST,
                            "input_digest": claim_fingerprint(variant, key, claim),
                            "subject_id": node.subject_id if cultural else candidate,
                            "candidate_id": candidate,
                            "pool": [c.id for c in node.candidate_pool],
                            "locality": node.locality,
                            "required_capacity": node.required_capacity,
                            "start_at": start,
                            "end_at": end,
                        }
                        output.append(
                            fields
                            | {
                                "scope_id": digest(fields),
                                "cache_key": digest(
                                    {
                                        k: v
                                        for k, v in fields.items()
                                        if k != "anchor_digest"
                                    }
                                )
                                if g.provider == "qloo"
                                else digest(fields),
                                "variant": variant.data(),
                                "state": "RECHECK_QUEUED",
                            }
                        )
                        if len(output) >= 512:
                            return output
        return output

    def _reserve_group(self, review_id, plan, binding, scope, requests):
        store, g = self.store, binding.grant
        now = int(self.clock())
        live = int(not binding.adapter.offline)
        self._active(binding, plan, timestamp(self.clock))
        with store._tx() as db:
            admitted = store._run(
                db,
                f"select state from {store.prefix}reviews where review_id=? and tenant_id=? and actor_id=?",
                (review_id, plan.tenant_id, plan.actor.id),
            ).fetchone()
        if admitted is None or admitted[0] in {"FINISHED", "FAILED", "CANCELLED_SAFE"}:
            raise Conflict("DURABLE_ACTIVE_ADMISSION_REQUIRED")
        specs = []
        for stage, index, method, path, payload in requests:
            request_digest = digest(
                {"method": method, "path": path, "payload": payload}
            )
            call_id = digest(
                {
                    "scope_id": scope["scope_id"],
                    "stage": stage,
                    "observation": index,
                    "request_digest": request_digest,
                }
            )
            specs.append((call_id, stage, index, request_digest))
        with store._tx(write=True) as db:
            if store.postgres:
                db.execute(
                    f"select id from {store.prefix}capacity where id=1 for update"
                )
            failures = store._run(
                db,
                f"select count(*) from {store.prefix}provider_io where provider=? and account_scope=? and state='UNKNOWN_OUTCOME' and created_at>?",
                (g.provider, g.account_scope, now - 60),
            ).fetchone()[0]
            if failures >= 3:
                raise CapacityReached("SOURCE_CIRCUIT_OPEN")
            missing = []
            for spec in specs:
                if (
                    store._run(
                        db,
                        f"select state from {store.prefix}provider_io where call_id=?",
                        (spec[0],),
                    ).fetchone()
                    is None
                ):
                    missing.append(spec)
            counts = store._run(
                db,
                f"""select count(*),sum(case when created_at>? then 1 else 0 end),sum(case when created_at>? then 1 else 0 end) from {store.prefix}provider_io where provider=? and account_scope=? and transport_live=?""",
                (now - 60, now - 86400, g.provider, g.account_scope, live),
            ).fetchone()
            tenant_count = store._run(
                db,
                f"select count(*) from {store.prefix}provider_io where grant_id=? and tenant_id=?",
                (g.id, g.tenant_id),
            ).fetchone()[0]
            review_count = store._run(
                db,
                f"select count(*) from {store.prefix}provider_io where review_id=? and transport_live=?",
                (review_id, live),
            ).fetchone()[0]
            limit = (
                min(g.per_review, plan.limits.provider_calls) if live else g.per_review
            )
            if (
                (counts[0] or 0) + len(missing)
                > min(
                    b.grant.total
                    for b in self.bindings
                    if b.grant.provider == g.provider
                    and b.grant.account_scope == g.account_scope
                )
                or tenant_count + len(missing) > g.total
                or (counts[1] or 0) + len(missing) > g.rpm
                or (counts[2] or 0) + len(missing) > g.rpd
                or review_count + len(missing) > limit
            ):
                raise CapacityReached("SOURCE_QUOTA_UNAVAILABLE")
            for call_id, stage, index, fingerprint in missing:
                packed = {
                    "scope": {k: v for k, v in scope.items() if k != "variant"},
                    "grant": g.data(),
                    "stage": stage,
                    "observation": index,
                    "review_id": review_id,
                    "call_id": call_id,
                    "request_digest": fingerprint,
                    "sent_at": None,
                    "used_live": bool(live),
                }
                store._run(
                    db,
                    f"insert into {store.prefix}provider_io values(?,?,?,?,?,?,?, ?,?,?, ?,?,null,null)",
                    (
                        call_id,
                        review_id,
                        plan.tenant_id,
                        g.provider,
                        g.account_scope,
                        g.id,
                        "RESERVED",
                        now,
                        live,
                        fingerprint,
                        store._encode(packed, private=True),
                        digest(packed),
                    ),
                )
        return specs

    def _execute(
        self,
        review_id,
        plan,
        binding,
        scope,
        deadline,
        trace,
        parent,
        checkpoint=None,
        requests_override=None,
    ):
        requests = (
            binding.adapter.requests(scope)
            if requests_override is None
            else requests_override
        )
        self._reserve_group(review_id, plan, binding, scope, requests)

        def execute(stage, index, method, path, payload, transport):
            store, g = self.store, binding.grant
            fingerprint = digest({"method": method, "path": path, "payload": payload})
            call_id = digest(
                {
                    "scope_id": scope["scope_id"],
                    "stage": stage,
                    "observation": index,
                    "request_digest": fingerprint,
                }
            )
            self._active(binding, plan, timestamp(self.clock))
            with store._tx(write=True) as db:
                if store.postgres:
                    db.execute(
                        f"select id from {store.prefix}capacity where id=1 for update"
                    )
                row = store._run(
                    db,
                    f"select state,payload,payload_digest,response,response_digest from {store.prefix}provider_io where call_id=?",
                    (call_id,),
                ).fetchone()
                if row is None:
                    raise Conflict("SOURCE_REQUEST_NOT_RESERVED")
                if row[0] == "COMPLETE":
                    return store._decode(row[3], row[4])
                if row[0] != "RESERVED":
                    raise Conflict("SOURCE_UNKNOWN_OUTCOME_RECONCILIATION_REQUIRED")
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TimeoutError("COLLECTION_DEADLINE")
                active = store._run(
                    db,
                    f"select count(*) from {store.prefix}provider_io where provider=? and account_scope=? and state='SENT'",
                    (g.provider, g.account_scope),
                ).fetchone()[0]
                if active >= g.concurrency:
                    raise CapacityReached("SOURCE_CONCURRENCY_EXHAUSTED")
                packed = store._decode(row[1], row[2])
                packed["sent_at"] = timestamp(self.clock)
                changed = store._run(
                    db,
                    f"update {store.prefix}provider_io set state='SENT',payload=?,payload_digest=? where call_id=? and state='RESERVED'",
                    (store._encode(packed, private=True), digest(packed), call_id),
                ).rowcount
                if changed != 1:
                    raise Conflict("SOURCE_CALL_LEASE_LOST")
            if checkpoint:
                checkpoint("SOURCE_SENT", {"call_id": call_id})
            span = (
                trace.span(stage, parent=parent, provider=g.provider)
                if trace
                else nullcontext()
            )
            try:
                with span:
                    status, body = transport.request(
                        method,
                        path,
                        payload,
                        timeout=min(remaining, g.timeout_ms / 1000),
                    )
                if self.clock() > deadline:
                    raise TimeoutError("COLLECTION_DEADLINE")
                if (
                    type(status) is not int
                    or status != 200
                    or not isinstance(body, bytes)
                    or len(body) > 512 * 1024
                ):
                    raise ValueError("SOURCE_RESPONSE_REJECTED")
                response = {
                    "body": base64.b64encode(body).decode(),
                    "body_digest": sha256(body).hexdigest(),
                    "observed_at": timestamp(self.clock),
                    "status": status,
                    "call_id": call_id,
                    "request_digest": fingerprint,
                }
                with store._tx(write=True) as db:
                    store._run(
                        db,
                        f"update {store.prefix}provider_io set state='COMPLETE',response=?,response_digest=? where call_id=? and state='SENT'",
                        (
                            store._encode(response, private=True),
                            digest(response),
                            call_id,
                        ),
                    )
                if checkpoint:
                    checkpoint("SOURCE_OBSERVED", {"call_id": call_id})
                return response
            except Exception:
                with store._tx(write=True) as db:
                    store._run(
                        db,
                        f"update {store.prefix}provider_io set state='UNKNOWN_OUTCOME' where call_id=? and state='SENT'",
                        (call_id,),
                    )
                raise

        return execute

    def collect_round(
        self,
        review_id,
        plan,
        evidence,
        *,
        anchor_digest,
        round_index,
        as_of,
        trace=None,
        parent=None,
        checkpoint=None,
        frozen_as_of=None,
    ):
        store = self.store
        with store._tx() as db:
            saved = store._run(
                db,
                f"select parent_digest,policy_digest,payload,payload_digest from {store.prefix}collection_rounds where review_id=? and round=?",
                (review_id, round_index),
            ).fetchone()
        if saved:
            if saved[0] != plan.digest or saved[1] != POLICY_DIGEST:
                raise Conflict("COLLECTION_REVISION_MISMATCH")
            data = store._decode(saved[2], saved[3])
            return (
                tuple(
                    Evidence.model_validate_json(json.dumps(e))
                    for e in data["evidence"]
                ),
                data["as_of"],
                data["states"],
            )
        result = list(evidence)
        states = []
        deadline = (
            instant(frozen_as_of or as_of).timestamp()
            + plan.limits.collection_deadline_ms / 1000
        )
        scopes = self.scopes(plan, evidence, anchor_digest=anchor_digest, as_of=as_of)
        visited = {s.get("scope_id") for s in scopes}
        for scope in scopes:
            if scope["state"] == "NOT_EVALUATED":
                states.append(scope)
                continue
            binding = next(b for b in self.bindings if b.grant.id == scope["grant_id"])
            try:
                self._active(binding, plan, timestamp(self.clock))
                with store._tx() as db:
                    cached = store._run(
                        db,
                        f"select payload,payload_digest,expires_at from {store.prefix}claim_cache where cache_key=? and tenant_id=?",
                        (scope["cache_key"], plan.tenant_id),
                    ).fetchone()
                if cached and cached[2] > int(self.clock()):
                    incoming = tuple(
                        Evidence.model_validate_json(json.dumps(e))
                        for e in store._decode(cached[0], cached[1])
                    )
                    if binding.grant.provider == "qloo":
                        from .policies import authority_valid

                        if any(
                            not authority_valid(
                                e, self.authorities, instant(timestamp(self.clock))
                            )
                            or e.input_digest != scope["input_digest"]
                            or e.locality != scope["locality"]
                            for e in incoming
                        ):
                            raise ValueError("CACHED_SOURCE_OR_SCOPE_REVOKED")
                        incoming = binding.adapter.rebind(incoming, scope)
                else:
                    if cached:
                        scope = dict(scope, refresh_after=cached[2])
                        scope["scope_id"] = digest(
                            {
                                k: v
                                for k, v in scope.items()
                                if k
                                not in {"variant", "cache_key", "state", "scope_id"}
                            }
                        )
                    execute = self._execute(
                        review_id,
                        plan,
                        binding,
                        scope,
                        deadline,
                        trace,
                        parent,
                        checkpoint,
                    )
                    incoming = binding.adapter.collect(scope, execute)
                variant = Plan.parse(scope["variant"])
                node = next(n for n in variant.nodes if n.id == scope["node_id"])
                at = timestamp(self.clock)
                for item in incoming:
                    item_variant = variant
                    if item.claim in {"schedule_window", "venue_capacity"} and {
                        "start_at",
                        "end_at",
                    } <= set(node.mutable_fields):
                        payload = variant.data()
                        for n in payload["nodes"]:
                            if n["id"] == node.id:
                                n.update(
                                    start_at=item.facts.get("start_at"),
                                    end_at=item.facts.get("end_at"),
                                )
                        item_variant = Plan.parse(payload)
                    checked_node = next(
                        n for n in item_variant.nodes if n.id == node.id
                    )
                    if (
                        item.provider_account_scope != binding.grant.account_scope
                        or item.entitlement_id != binding.grant.entitlement_id
                        or item.claim != scope["claim"]
                        or not item.extraction_rule_version
                        or not item.raw_payload_ref
                        or evidence_reason(
                            item_variant,
                            checked_node,
                            item,
                            baseline_digest=anchor_digest,
                            at=instant(at),
                            synthetic=False,
                            authorities=self.authorities,
                        )
                    ):
                        raise ValueError("COLLECTED_EVIDENCE_UNQUALIFIED")
                if not incoming:
                    raise ValueError("SOURCE_RETURNED_NO_EVIDENCE")
                packed = [e.data() for e in incoming]
                expiry = min(int(instant(e.expires_at).timestamp()) for e in incoming)
                with store._tx(write=True) as db:
                    store._run(
                        db,
                        f"insert into {store.prefix}claim_cache values(?,?,?,?,?) on conflict(cache_key) do update set payload=excluded.payload,payload_digest=excluded.payload_digest,expires_at=excluded.expires_at",
                        (
                            scope["cache_key"],
                            plan.tenant_id,
                            expiry,
                            store._encode(packed, private=True),
                            digest(packed),
                        ),
                    )
                for item in incoming:
                    if item.id in {e.id for e in result}:
                        if item.data() != next(
                            e.data() for e in result if e.id == item.id
                        ):
                            raise Conflict("EVIDENCE_ID_REUSED")
                    elif len(result) < 512:
                        result.append(item)
                if (
                    any(
                        e.claim in {"schedule_window", "venue_capacity"}
                        for e in incoming
                    )
                    and len(scopes) < 512
                ):
                    for extra in self.scopes(
                        plan,
                        tuple(result),
                        anchor_digest=anchor_digest,
                        as_of=at,
                        only_nodes={node.id},
                    ):
                        if (
                            extra.get("scope_id") not in visited
                            and extra["state"] != "NOT_EVALUATED"
                        ):
                            visited.add(extra["scope_id"])
                            scopes.append(extra)
                            if len(scopes) >= 512:
                                break
                states.append(
                    {
                        "node_id": scope["node_id"],
                        "claim": scope["claim"],
                        "candidate_id": scope["candidate_id"],
                        "state": "RECHECK_COMPLETED",
                        "scope_id": scope["scope_id"],
                        "evidence_ids": [e.id for e in incoming],
                    }
                )
            except (
                ValueError,
                PermissionError,
                Conflict,
                CapacityReached,
                TimeoutError,
            ) as exc:
                if isinstance(exc, CoordinatorStopped):
                    raise
                states.append(
                    {
                        "node_id": scope["node_id"],
                        "claim": scope["claim"],
                        "state": "STALE_PENDING_RECHECK",
                        "reason": "SOURCE_UNKNOWN_OUTCOME"
                        if isinstance(exc, Conflict)
                        else "SOURCE_UNAVAILABLE",
                        "scope_id": scope["scope_id"],
                    }
                )
        at = max((as_of, timestamp(self.clock)), key=instant)
        packed = {"evidence": [e.data() for e in result], "as_of": at, "states": states}
        with store._tx(write=True) as db:
            row = store._run(
                db,
                f"select payload_digest from {store.prefix}collection_rounds where review_id=? and round=?",
                (review_id, round_index),
            ).fetchone()
            if row and row[0] != digest(packed):
                raise Conflict("COLLECTION_ROUND_CAS_LOST")
            if not row:
                store._run(
                    db,
                    f"insert into {store.prefix}collection_rounds values(?,?,?,?,?,?)",
                    (
                        review_id,
                        round_index,
                        plan.digest,
                        POLICY_DIGEST,
                        store._encode(packed, private=True),
                        digest(packed),
                    ),
                )
        return tuple(result), at, states

    def resolve(self, review_id, plan, query, entity_type, *, locality=None):
        from urllib.parse import urlencode

        binding = next(
            (
                b
                for b in self.bindings
                if b.grant.tenant_id == plan.tenant_id
                and b.grant.provider == "qloo"
                and b.grant.resolve_entities
            ),
            None,
        )
        if binding is None:
            raise PermissionError("ENTITY_RESOLUTION_CAPABILITY_REQUIRED")
        g = binding.grant
        self._active(binding, plan, timestamp(self.clock))
        fields = {
            "tenant_id": plan.tenant_id,
            "provider": g.provider,
            "account_scope": g.account_scope,
            "entitlement_id": g.entitlement_id,
            "claim": "entity_resolution",
            "query": query.strip().casefold(),
            "entity_type": entity_type,
            "locality": locality,
            "policy_digest": POLICY_DIGEST,
        }
        key = digest(fields)
        with self.store._tx() as db:
            row = self.store._run(
                db,
                f"select payload,payload_digest,expires_at from {self.store.prefix}entity_cache where cache_key=? and tenant_id=?",
                (key, plan.tenant_id),
            ).fetchone()
        if row and row[2] > int(self.clock()):
            return self.store._decode(row[0], row[1])
        scope = fields | {
            "scope_id": digest(
                fields
                | {"anchor_digest": plan.digest, "refresh_after": row[2] if row else 0}
            )
        }
        path = "/search?" + urlencode(
            {"query": query.strip(), "types": "urn:entity:" + entity_type, "take": 8}
        )
        execute = self._execute(
            review_id,
            plan,
            binding,
            scope,
            self.clock() + g.timeout_ms / 1000,
            None,
            None,
            requests_override=[("search", 0, "GET", path, None)],
        )
        resolved = binding.adapter.resolve(query, entity_type, execute) | {
            "locality": locality,
            "account_scope": g.account_scope,
            "taxonomy_assurance": "QUERY_FILTER_SCOPE",
        }
        with self.store._tx(write=True) as db:
            self.store._run(
                db,
                f"insert into {self.store.prefix}entity_cache values(?,?,?,?,?) on conflict(cache_key) do update set expires_at=excluded.expires_at,payload=excluded.payload,payload_digest=excluded.payload_digest",
                (
                    key,
                    plan.tenant_id,
                    int(self.clock()) + 86400,
                    self.store._encode(resolved, private=True),
                    digest(resolved),
                ),
            )
        return resolved

    def capabilities(self):
        return {
            "pipeline_available": True,
            "live_collection_enabled": any(
                b.grant.allow_live and not b.adapter.offline for b in self.bindings
            ),
            "source_types": sorted({b.grant.provider for b in self.bindings}),
            "production_qualified": False,
            "paid_calls_enabled": False,
        }

    def _apply_revocation(self, tenant, entitlement):
        for key, authority in tuple(self.authorities.items()):
            if authority.tenant_id == tenant:
                self.authorities[key] = replace(
                    authority,
                    revoked_entitlements=authority.revoked_entitlements | {entitlement},
                )

    def revoke_entitlement(self, tenant_id, entitlement_id):
        with self.store._tx(write=True) as db:
            self.store._run(
                db,
                f"insert into {self.store.prefix}source_revocations values(?,?,?) on conflict(tenant_id,entitlement_id) do nothing",
                (tenant_id, entitlement_id, int(self.clock())),
            )
        self._apply_revocation(tenant_id, entitlement_id)

    def reconcile(self, call_id, response, *, tenant_id, usage_checked=False):
        if not usage_checked:
            raise PermissionError("SOURCE_USAGE_RECONCILIATION_REQUIRED")
        store = self.store
        if (
            response.get("call_id") != call_id
            or sha256(base64.b64decode(response["body"], validate=True)).hexdigest()
            != response["body_digest"]
        ):
            raise ValueError("RECONCILIATION_PAYLOAD_MISMATCH")
        with store._tx(write=True) as db:
            changed = store._run(
                db,
                f"update {store.prefix}provider_io set state='COMPLETE',response=?,response_digest=? where call_id=? and tenant_id=? and state in ('SENT','UNKNOWN_OUTCOME')",
                (
                    store._encode(response, private=True),
                    digest(response),
                    call_id,
                    tenant_id,
                ),
            ).rowcount
            if changed != 1:
                raise Conflict("ATTEMPT_NOT_RECONCILABLE")

    def record(self, review_id, plan):
        store = self.store
        receipts = []
        with store._tx() as db:
            rows = store._run(
                db,
                f"select state,payload,payload_digest,response,response_digest from {store.prefix}provider_io where review_id=? and tenant_id=? order by call_id",
                (review_id, plan.tenant_id),
            ).fetchall()
        for state, raw, hashed, response, response_digest in rows:
            packed = store._decode(raw, hashed)
            # Unsent reservations consume admission quota but are not represented as completed upstream calls.
            if state == "RESERVED":
                continue
            data = store._decode(response, response_digest) if response else None
            receipts.append(
                {
                    "call_id": packed["call_id"],
                    "review_id": review_id,
                    "grant": packed["grant"],
                    "provider": packed["scope"]["provider"],
                    "account_scope": packed["scope"]["account_scope"],
                    "request_digest": packed["request_digest"],
                    "response_digest": data["body_digest"] if data else None,
                    "sent_at": packed["sent_at"],
                    "state": "UNKNOWN_OUTCOME" if state == "SENT" else state,
                    "used_live": packed["used_live"],
                    "stage": packed["stage"],
                    "spend_minor": 0,
                }
            )
        record = {
            "schema": "ofc.collection/1",
            "review_id": review_id,
            "issuer": self.issuer,
            "policy_digest": POLICY_DIGEST,
            "plan_digest": plan.digest,
            "receipts": receipts,
            "provider_calls": sum(r["used_live"] for r in receipts),
            "spend_minor": 0,
            "origin_assurance": "SIGNED_COORDINATOR_JOURNAL",
            "upstream_independence": False,
        }
        return record | {
            "digest": digest(record),
            "signature": base64.b64encode(
                self.key.sign(canonical_bytes(record))
            ).decode(),
        }


def collected_record(control, collection):
    result = dict(control)
    result.update(
        schema="ofc.collected-review/1",
        control=control,
        collection=collection,
        provider_calls=collection["provider_calls"],
        spend_minor=collection["spend_minor"],
    )
    result.pop("proof_digest", None)
    result["proof_digest"] = digest(result)
    return result
