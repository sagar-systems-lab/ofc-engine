"""Independent business evidence gate; Qloo ranks are not authoritative business proofs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import binascii
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contracts import PlanSnapshot
from .revisions import canonical_bytes, plan_digest

BUSINESS_REQUIREMENTS = (
    "budget_feasibility",
    "contract_rights",
    "venue_availability",
    "local_audience",
)


@dataclass(frozen=True)
class SignedBusinessProof:
    requirement: str
    decision_id: str
    plan_id: str
    plan_revision: int
    plan_digest: str
    issuer: str
    issued_at: str
    expires_at: str
    claims: dict
    signature_b64: str

    def signed_fields(self) -> dict:
        return {
            "requirement": self.requirement,
            "decision_id": self.decision_id,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "plan_digest": self.plan_digest,
            "issuer": self.issuer,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "claims": self.claims,
        }


@dataclass(frozen=True)
class TrustedAuthority:
    issuer: str
    requirements: frozenset[str]
    public_key: bytes

    def __post_init__(self):
        if (
            not self.issuer or not self.requirements
            or not self.requirements.issubset(BUSINESS_REQUIREMENTS)
            or len(self.public_key) != 32
        ):
            raise ValueError("invalid trusted verification authority")


def _valid_time(raw: str):
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.utcoffset() is None:
        return None
    return value


def _verify_claim(
    plan: PlanSnapshot, proof: SignedBusinessProof,
    authority: TrustedAuthority, *, at: datetime,
) -> bool:
    if (
        proof.plan_id != plan.plan_id
        or proof.plan_revision != plan.revision
        or proof.plan_digest != plan_digest(plan)
        or proof.issuer != authority.issuer
        or proof.requirement not in authority.requirements
        or proof.requirement not in BUSINESS_REQUIREMENTS
        or proof.decision_id not in {d.decision_id for d in plan.decisions}
        or not isinstance(proof.claims, dict)
    ):
        return False
    issued, expiry = _valid_time(proof.issued_at), _valid_time(proof.expires_at)
    if issued is None or expiry is None or not issued <= at < expiry:
        return False
    if (expiry - issued).total_seconds() > 31 * 86400:
        return False
    try:
        signature = base64.b64decode(proof.signature_b64, validate=True)
        if len(signature) != 64:
            return False
        Ed25519PublicKey.from_public_bytes(authority.public_key).verify(
            signature, canonical_bytes(proof.signed_fields()),
        )
    except (InvalidSignature, ValueError, TypeError, binascii.Error):
        return False
    return _claims_satisfy(proof)


def _claims_satisfy(proof: SignedBusinessProof) -> bool:
    claims = proof.claims
    if proof.requirement == "budget_feasibility":
        approved = claims.get("approved_budget_usd")
        quoted = claims.get("fully_loaded_cost_usd")
        return (
            type(approved) is int and type(quoted) is int
            and 0 <= quoted <= approved <= 100_000_000
            and claims.get("currency") == "USD"
        )
    if proof.requirement == "contract_rights":
        return (
            claims.get("signed_contract") is True
            and isinstance(claims.get("counterparty"), str)
            and bool(claims["counterparty"].strip())
            and claims.get("scope_covers_plan") is True
        )
    if proof.requirement == "venue_availability":
        return (
            claims.get("inventory_confirmed") is True
            and isinstance(claims.get("venue_id"), str)
            and bool(claims["venue_id"].strip())
            and isinstance(claims.get("reservation_window"), str)
            and bool(claims["reservation_window"].strip())
        )
    if proof.requirement == "local_audience":
        return (
            claims.get("segment_validated") is True
            and isinstance(claims.get("geography"), str)
            and bool(claims["geography"].strip())
            and isinstance(claims.get("sample_source"), str)
            and bool(claims["sample_source"].strip())
        )
    return False


def assess_business_readiness(
    plan: PlanSnapshot, *,
    required: Mapping[str, tuple[str, ...]],
    proofs: tuple[SignedBusinessProof, ...] = (),
    authorities: Mapping[str, TrustedAuthority] | None = None,
    as_of: datetime | None = None,
) -> dict:
    """Fail closed on absent authority; do not turn Qloo rank into a contract check."""
    if not isinstance(plan, PlanSnapshot):
        raise TypeError("frozen plan required")
    at = as_of or datetime.now(timezone.utc)
    if at.utcoffset() is None:
        raise ValueError("verification requires timezone-aware clock")
    known_decisions = {d.decision_id for d in plan.decisions}
    if (
        not isinstance(required, Mapping)
        or not set(required).issubset(known_decisions)
        or any(
            not isinstance(values, tuple)
            or not set(values).issubset(BUSINESS_REQUIREMENTS)
            for values in required.values()
        )
    ):
        raise ValueError("invalid decision requirements")
    trusted = authorities or {}
    fulfilled: dict[str, set[str]] = {key: set() for key in required}
    invalid_claims = 0
    for item in proofs:
        if not isinstance(item, SignedBusinessProof):
            raise ValueError("invalid signed proof")
        source = trusted.get(item.issuer)
        if (
            item.decision_id in fulfilled and source is not None
            and _verify_claim(plan, item, source, at=at)
        ):
            fulfilled[item.decision_id].add(item.requirement)
        else:
            invalid_claims += 1
    missing = {
        decision_id: sorted(set(requirements) - fulfilled[decision_id])
        for decision_id, requirements in required.items()
    }
    for decision_id in known_decisions - set(required):
        missing[decision_id] = ["UNQUALIFIED_DECISION_DOMAIN"]
    for decision_id, requirements in required.items():
        if not requirements:
            missing[decision_id] = ["BUSINESS_REQUIREMENT_POLICY_MISSING"]
    clean = bool(known_decisions) and not any(missing.values()) and invalid_claims == 0
    # Even complete verified inputs do not execute or release a third-party action.
    return {
        "status": "BUSINESS_EVIDENCE_COMPLETE" if clean else "REVIEW_REQUIRED",
        "plan_id": plan.plan_id,
        "revision": plan.revision,
        "plan_digest": plan_digest(plan),
        "missing_requirements": missing,
        "invalid_evidence_count": invalid_claims,
        "trusted_authority_count": len(trusted),
        "business_evidence_satisfied": clean,
        "external_action_authorized": False,
        "external_action_executed": False,
    }
