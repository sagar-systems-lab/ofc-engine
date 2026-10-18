from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .canonical import canonical_bytes, digest
from .graph import claim_fingerprint, topological
from .models import CAUSAL_EDGES, Evidence, Node, Plan, instant

POLICY_VERSION = "control-policy/1"
DOMAIN_REQUIREMENTS = {
    "brand": ("cultural_brand_rank", "contract_rights"),
    "venue": ("venue_capacity", "contract_rights", "schedule_window"),
    "place": ("place_cultural_observation", "contract_rights"),
}
MANIFEST = {
    "version": POLICY_VERSION,
    "cultural_brand_rank": {"sources": ["qloo"], "domains": ["brand"], "attempts": 2,
                            "max_age_seconds": 86400, "level": "POLICY_QUALIFIED"},
    "place_cultural_observation": {"sources": ["qloo"], "domains": ["place", "venue"], "attempts": 1,
                                   "max_age_seconds": 3600, "level": "OBSERVABLE"},
    "budget_quote": {"sources": ["operator"], "domains": ["*"], "attempts": 1,
                     "max_age_seconds": 86400, "level": "ATTESTATION_REQUIRED"},
    "venue_capacity": {"sources": ["operator"], "domains": ["venue", "place"], "attempts": 1,
                       "max_age_seconds": 86400, "level": "ATTESTATION_REQUIRED"},
    "contract_rights": {"sources": ["operator"], "domains": ["*"], "attempts": 1,
                        "max_age_seconds": 31 * 86400, "level": "ATTESTATION_REQUIRED"},
    "schedule_window": {"sources": ["operator"], "domains": ["*"], "attempts": 1,
                        "max_age_seconds": 86400, "level": "ATTESTATION_REQUIRED"},
}
POLICY_SOURCE_DIGESTS = {name: sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                         for name in ("canonical.py", "models.py", "graph.py", "policies.py", "engine.py", "verifier.py")}
POLICY_DIGEST = digest({"manifest": MANIFEST, "implementation": POLICY_SOURCE_DIGESTS})


def required_claims(plan: Plan, node: Node) -> tuple[str, ...]:
    claims = list(node.claims)
    for claim in DOMAIN_REQUIREMENTS.get(node.domain, ()):
        if claim not in claims:
            claims.append(claim)
    if plan.budgets and "budget_quote" not in claims:
        claims.append("budget_quote")
    return tuple(claims)


@dataclass(frozen=True)
class Authority:
    issuer: str
    tenant_id: str
    public_key: bytes
    claims: frozenset[str]
    source: str
    expires_at: str
    revoked: bool = False

    def __post_init__(self):
        if (not self.issuer or not self.tenant_id or len(self.public_key) != 32
                or not self.claims or not self.claims <= set(MANIFEST)
                or self.source not in {"qloo", "operator"}):
            raise ValueError("invalid evidence authority")
        instant(self.expires_at)

    def data(self) -> dict:
        return {"issuer": self.issuer, "tenant_id": self.tenant_id,
                "public_key": base64.b64encode(self.public_key).decode(),
                "claims": sorted(self.claims), "source": self.source,
                "expires_at": self.expires_at, "revoked": self.revoked}


def authority_valid(evidence: Evidence, authorities: dict[str, Authority], at: datetime) -> bool:
    authority = authorities.get(evidence.issuer)
    if authority is None or authority.revoked or evidence.signature is None:
        return False
    if (authority.tenant_id != evidence.tenant_id or evidence.claim not in authority.claims
            or authority.source != evidence.source or instant(authority.expires_at) <= at):
        return False
    try:
        signature = base64.b64decode(evidence.signature, validate=True)
        Ed25519PublicKey.from_public_bytes(authority.public_key).verify(
            signature, canonical_bytes(evidence.signing_fields()))
    except (InvalidSignature, ValueError, TypeError, binascii.Error):
        return False
    return True


def evidence_reason(plan: Plan, node: Node, item: Evidence, *, baseline_digest: str,
                    at: datetime, synthetic: bool, authorities: dict[str, Authority]) -> str | None:
    manifest = MANIFEST[item.claim]
    if item.tenant_id != plan.tenant_id or item.node_id != node.id:
        return "EVIDENCE_SCOPE_MISMATCH"
    if item.plan_digest not in {baseline_digest, plan.digest} or item.policy_version != POLICY_VERSION:
        return "EVIDENCE_REVISION_OR_POLICY_MISMATCH"
    if item.input_digest != claim_fingerprint(plan, node.id, item.claim):
        return "STALE_INPUT_FINGERPRINT"
    if tuple(item.candidate_pool) != tuple(c.id for c in node.candidate_pool) or item.locality != node.locality:
        return "CANDIDATE_OR_LOCALITY_MISMATCH"
    subject = node.subject_id if item.claim in ("cultural_brand_rank", "place_cultural_observation") else node.candidate_id
    if subject is None or item.subject_id != subject:
        return "SUBJECT_MISMATCH"
    if not instant(item.observed_at) <= at < instant(item.expires_at):
        return "EXPIRED_OR_FUTURE_EVIDENCE"
    if (at - instant(item.observed_at)).total_seconds() > manifest["max_age_seconds"]:
        return "EVIDENCE_TOO_OLD"
    if "*" not in manifest["domains"] and node.domain not in manifest["domains"]:
        return "UNSUPPORTED_CLAIM_DOMAIN"
    if synthetic:
        if item.mode != "SYNTHETIC" or item.source != "fixture" or item.license_scope != "synthetic_public":
            return "SOURCE_MODE_MISMATCH"
    elif (item.mode not in ("LIVE", "CAPTURED", "ATTESTED")
          or item.source not in manifest["sources"] or item.license_scope != "private_review"
          or not authority_valid(item, authorities, at)):
        return "SOURCE_AUTHORITY_UNVERIFIED"
    if not synthetic and item.source == "qloo" and (item.query_fingerprint is None or item.source_digest is None):
        return "PROVIDER_PROVENANCE_MISSING"
    return None


def _facts_valid(node: Node, claim: str, item: Evidence) -> bool:
    facts = item.facts
    if claim == "budget_quote":
        selected = next(c for c in node.candidate_pool if c.id == node.candidate_id)
        return (type(facts.get("cost_minor")) is int and selected.cost_minor is not None
                and facts["cost_minor"] == selected.cost_minor
                and facts.get("currency") == selected.currency and facts.get("includes_fees") is True)
    if claim == "venue_capacity":
        capacity, required = facts.get("capacity"), facts.get("required_capacity")
        return (type(capacity) is int and type(required) is int and node.required_capacity is not None
                and required == node.required_capacity and 0 < required <= capacity
                and facts.get("available") is True and facts.get("venue_id") == node.candidate_id
                and facts.get("start_at") == node.start_at and node.start_at is not None
                and facts.get("end_at") == node.end_at)
    if claim == "contract_rights":
        return (facts.get("signed_contract") is True and facts.get("scope_covers_plan") is True
                and facts.get("candidate_id") == node.candidate_id
                and isinstance(facts.get("contract_ref"), str) and bool(facts["contract_ref"].strip()))
    if claim == "schedule_window":
        return (node.start_at is not None and facts.get("start_at") == node.start_at
                and facts.get("end_at") == node.end_at and facts.get("confirmed") is True)
    return False


def evaluate_claim(plan: Plan, node: Node, claim: str, evidence: tuple[Evidence, ...], *,
                   baseline_digest: str, at: datetime, synthetic: bool,
                   authorities: dict[str, Authority], resolve_parent=None) -> dict:
    supplied = [item for item in evidence if item.node_id == node.id and item.claim == claim]
    result = {"claim": claim, "status": "UNCERTAIN", "reason": "MISSING_EVIDENCE",
              "evidence_ids": [item.id for item in supplied], "policy_version": POLICY_VERSION,
              "qualification_scope": "SYNTHETIC_POLICY" if synthetic else "SCOPED_SOURCE_POLICY"}
    if not supplied:
        return result
    for item in supplied:
        reason = evidence_reason(plan, node, item, baseline_digest=baseline_digest,
                                 at=at, synthetic=synthetic, authorities=authorities)
        if reason:
            return {**result, "reason": reason}
        if item.parent_ids and (resolve_parent is None or any(not resolve_parent(key) for key in item.parent_ids)):
            return {**result, "reason": "PARENT_CLAIM_NOT_QUALIFIED"}
    if len({e.attempt_id for e in supplied}) != len(supplied):
        return {**result, "reason": "DUPLICATE_ATTEMPT"}
    if claim == "cultural_brand_rank":
        if len(supplied) < 2 or len({e.observed_at for e in supplied}) < 2:
            return {**result, "reason": "DISTINCT_OBSERVATIONS_REQUIRED"}
        pool = {c.id for c in node.candidate_pool}
        if any(set(e.ranks) != pool or len(e.ranks) != len(pool) for e in supplied):
            return {**result, "reason": "INCOMPLETE_CANDIDATE_COVERAGE"}
        if len({e.ranks for e in supplied}) != 1:
            return {**result, "reason": "RANK_DISAGREEMENT"}
        return {**result, "status": "QUALIFIED", "reason": "SCOPED_REPEATABLE_RANK",
                "ranked_candidates": list(supplied[0].ranks),
                "source_independence": False, "business_permission": False}
    if claim == "place_cultural_observation":
        return {**result, "status": "OBSERVED", "reason": "LOCALITY_OBSERVATION_ONLY",
                "business_permission": False}
    if not all(_facts_valid(node, claim, e) for e in supplied):
        return {**result, "status": "BLOCKED", "reason": "ATTESTED_FACTS_FAIL_POLICY"}
    return {**result, "status": "QUALIFIED", "reason": "SCOPED_ATTESTATION_VALID"}


def evaluate(plan: Plan, evidence: tuple[Evidence, ...], *, baseline_digest: str,
             as_of: str, synthetic: bool, authorities: dict[str, Authority] | None = None) -> dict:
    at, trusted = instant(as_of), authorities or {}
    nodes = {n.id: n for n in plan.nodes}
    evidence_by_id = {item.id: item for item in evidence}
    cache = {}

    def resolve(node_id, claim, active=frozenset()):
        key = (node_id, claim)
        if key in cache:
            return cache[key]
        if key in active or node_id not in nodes:
            return {"status": "UNCERTAIN", "reason": "CYCLIC_OR_UNKNOWN_CLAIM_PROVENANCE"}

        def parent_qualified(parent_id):
            parent = evidence_by_id.get(parent_id)
            if parent is None:
                return False
            return resolve(parent.node_id, parent.claim, active | {key})["status"] == "QUALIFIED"

        value = evaluate_claim(plan, nodes[node_id], claim, evidence, baseline_digest=baseline_digest,
                               at=at, synthetic=synthetic, authorities=trusted, resolve_parent=parent_qualified)
        cache[key] = value
        return value

    result = {}
    for key in topological(plan):
        node = nodes[key]
        claims = [resolve(key, claim) for claim in required_claims(plan, node)]
        reasons = [c["reason"] for c in claims if c["status"] != "QUALIFIED"]
        if not claims:
            reasons.append("CLAIM_POLICY_MISSING")
        upstream = [e.source for e in plan.edges if e.target == key and e.type in CAUSAL_EDGES
                    and result[e.source]["status"] != "QUALIFIED"]
        if upstream:
            reasons.append("UPSTREAM_OBLIGATION_UNRESOLVED")
        status = "BLOCKED" if any(c["status"] == "BLOCKED" for c in claims) else "UNCERTAIN" if reasons else "QUALIFIED"
        result[key] = {"status": status, "claims": claims, "reasons": reasons, "upstream_blockers": upstream}
    return result


def capabilities() -> dict:
    return {"schema": "ofc.capabilities/1", "policy_version": POLICY_VERSION,
            "policy_digest": POLICY_DIGEST, "claims": MANIFEST,
            "domain_requirements": DOMAIN_REQUIREMENTS,
            "max_nodes": 64, "external_actions": False, "provider_calls": 0,
            "live_collection_enabled": False,
            "adapters": ["http", "python", "javascript"],
            "qualification": "LOCAL_POLICY_TESTS_ONLY", "independent_pilot": False}
