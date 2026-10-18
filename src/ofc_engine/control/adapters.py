from __future__ import annotations

import base64
from datetime import timedelta
import json
import re

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..intake import AgentProposal, freeze_proposal
from ..qloo import brand_affinity_probe
from ..revisions import digest_obj, plan_digest
from .canonical import canonical_bytes
from .graph import claim_fingerprint
from .models import (
    Actor,
    Budget,
    Candidate,
    Edge,
    Evidence,
    Node,
    Plan,
    ReviewRequest,
    instant,
)
from .policies import POLICY_VERSION, required_claims


def from_proposal(
    proposal: AgentProposal,
    *,
    tenant_id: str,
    actor_id: str,
    labels: dict | None = None,
) -> Plan:
    proposal = AgentProposal.model_validate(proposal.model_dump())
    names, nodes = labels or {}, []
    artist_map = proposal.intent.taste_context.get("qloo_artist_ids", {})
    if not isinstance(artist_map, dict):
        raise ValueError("invalid artist scope map")
    for decision in proposal.decisions:
        domain = re.sub(r"[^a-z_]", "_", decision.domain.lower())
        if domain in {"partner", "brand_partner"}:
            domain = "brand"
        claims = (
            ("cultural_brand_rank", "contract_rights")
            if domain == "brand"
            else ("contract_rights",)
        )
        if domain in {"venue", "place"}:
            claims = ("venue_capacity", "contract_rights", "schedule_window")
        subject = artist_map.get(
            decision.decision_id, proposal.intent.taste_context.get("artist_id")
        )
        nodes.append(
            Node(
                id=decision.decision_id,
                domain=domain,
                label=decision.decision_id.replace("_", " ").title(),
                candidate_id=decision.candidate_id,
                candidate_pool=tuple(
                    Candidate(id=key, label=names.get(key, key))
                    for key in decision.candidate_pool
                ),
                mutable_fields=decision.mutable_fields,
                owner=actor_id,
                locality=decision.locality_ref,
                subject_id=subject if domain == "brand" else None,
                required=decision.required,
                claims=claims,
            )
        )
    budget = proposal.intent.constraints.get("budget_usd")
    budgets = (
        (Budget(currency="USD", limit_minor=budget * 100),)
        if type(budget) is int
        else ()
    )
    edges = tuple(
        Edge(source=parent, target=d.decision_id, read_set=(f"{parent}.candidate_id",))
        for d in proposal.decisions
        for parent in d.depends_on
    )
    return Plan(
        tenant_id=tenant_id,
        actor=Actor(id=actor_id),
        idempotency_key=proposal.intent.run_id,
        objective=proposal.intent.objective,
        nodes=tuple(nodes),
        edges=edges,
        budgets=budgets,
    )


def saved_qloo_evidence(
    plan: Plan,
    proposal: AgentProposal,
    store,
    *,
    issuer: str,
    signing_key: Ed25519PrivateKey,
) -> tuple[Evidence, ...]:
    baseline, _ = freeze_proposal(proposal)
    expected_baseline = plan_digest(baseline)
    output = []
    for node in plan.nodes:
        if node.domain != "brand" or node.subject_id is None:
            continue
        if any(e.target == node.id for e in plan.edges) or node.locality is not None:
            continue
        spec = brand_affinity_probe(
            node.subject_id, tuple(c.id for c in node.candidate_pool)
        )
        with store._connect() as db:
            rows = db.execute(
                "select pass_no,baseline_digest,query_fingerprint,state,result,result_digest "
                "from ofc_private.dynamic_qloo_attempts where plan_id=%s and decision_id=%s order by pass_no",
                (proposal.intent.run_id, node.id),
            ).fetchall()
        for pass_no, frozen_digest, query, state, raw, raw_digest in rows:
            if state != "COMPLETE":
                continue
            captured = json.loads(raw)
            if (
                pass_no not in (1, 2)
                or frozen_digest != expected_baseline
                or query != spec.fingerprint
                or digest_obj(captured) != raw_digest
                or captured.get("query_fingerprint") != query
                or len(captured["ranking"]) != len(node.candidate_pool)
                or set(captured["ranking"]) != {c.id for c in node.candidate_pool}
            ):
                raise ValueError("captured Qloo observation scope mismatch")
            stamp = instant(captured["collected_at"])
            item = Evidence(
                id=f"{node.id}-rank-{pass_no}",
                tenant_id=plan.tenant_id,
                plan_digest=plan.digest,
                node_id=node.id,
                claim="cultural_brand_rank",
                subject_id=node.subject_id,
                candidate_pool=tuple(c.id for c in node.candidate_pool),
                input_digest=claim_fingerprint(plan, node.id, "cultural_brand_rank"),
                source="qloo",
                mode="CAPTURED",
                attempt_id=f"{proposal.intent.run_id}-{node.id}-{pass_no}",
                observed_at=stamp.isoformat().replace("+00:00", "Z"),
                expires_at=(stamp + timedelta(hours=24))
                .isoformat()
                .replace("+00:00", "Z"),
                policy_version=POLICY_VERSION,
                license_scope="private_review",
                ranks=tuple(captured["ranking"]),
                query_fingerprint=query,
                source_digest=captured["capture_digest"],
                issuer=issuer,
            )
            signature = base64.b64encode(
                signing_key.sign(canonical_bytes(item.signing_fields()))
            ).decode()
            output.append(item.model_copy(update={"signature": signature}))
    return tuple(output)


def evidence_plan(request: ReviewRequest) -> dict:
    scopes = []
    for node in request.plan.nodes:
        for claim in required_claims(request.plan, node):
            existing = [
                e for e in request.evidence if e.node_id == node.id and e.claim == claim
            ]
            scopes.append(
                {
                    "node_id": node.id,
                    "claim": claim,
                    "provided_observations": len(existing),
                    "missing_observations": max(
                        0, (2 if claim == "cultural_brand_rank" else 1) - len(existing)
                    ),
                    "collection": "SIGNED_INPUT_REQUIRED"
                    if claim != "cultural_brand_rank"
                    else "CAPTURE_REPLAY_ONLY",
                }
            )
    return {
        "plan_digest": request.plan.digest,
        "scopes": scopes,
        "provider_calls_admitted": 0,
        "spend_minor": 0,
        "live_collection_enabled": False,
    }
