from __future__ import annotations

import base64
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, model_validator

from .canonical import canonical_bytes, digest
from .models import Id, Strict, instant


class CollectionGrant(Strict):
    id: Id
    tenant_id: Id
    provider: Literal["qloo", "operator"]
    account_scope: Id
    entitlement_id: Id
    claims: tuple[str, ...] = Field(min_length=1, max_length=16)
    expires_at: str
    rpm: int = Field(default=20, ge=1, le=1000)
    rpd: int = Field(default=100, ge=1, le=100000)
    total: int = Field(default=100, ge=1, le=100000)
    concurrency: int = Field(default=2, ge=1, le=4)
    per_review: int = Field(default=16, ge=1, le=128)
    timeout_ms: int = Field(default=10000, ge=1, le=30000)
    allow_live: bool = False
    resolve_entities: bool = False
    free_entitlement: Literal[True] = True
    unit_cost_minor: Literal[0] = 0
    revoked: bool = False

    @model_validator(mode="after")
    def valid(self):
        instant(self.expires_at)
        if len(set(self.claims)) != len(self.claims):
            raise ValueError("duplicate grant claim")
        return self


def verify_collection(record, *, trusted_keys, plan, expected_policy):
    if (
        record.get("schema") != "ofc.collection/1"
        or record.get("policy_digest") != expected_policy
        or record.get("plan_digest") != plan.digest
    ):
        raise ValueError("collection policy mismatch")
    key = trusted_keys.get(record.get("issuer"))
    if key is None:
        raise ValueError("independent collection journal key required")
    body = {k: v for k, v in record.items() if k not in {"signature", "digest"}}
    if record.get("digest") != digest(body):
        raise ValueError("collection digest mismatch")
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(
            base64.b64decode(record["signature"], validate=True), canonical_bytes(body)
        )
    except InvalidSignature as exc:
        raise ValueError("collection signature mismatch") from exc
    calls, identifiers = 0, set()
    for receipt in record["receipts"]:
        if receipt["call_id"] in identifiers:
            raise ValueError("duplicate collection receipt")
        identifiers.add(receipt["call_id"])
        grant = CollectionGrant.model_validate_json(
            __import__("json").dumps(receipt["grant"])
        )
        if (
            grant.tenant_id != plan.tenant_id
            or receipt["review_id"] != record["review_id"]
            or grant.revoked
            or instant(grant.expires_at) <= instant(receipt["sent_at"])
            or receipt["account_scope"] != grant.account_scope
            or receipt["provider"] != grant.provider
            or receipt["spend_minor"] != 0
            or receipt["state"] not in {"COMPLETE", "UNKNOWN_OUTCOME", "ERROR"}
        ):
            raise ValueError("invalid collection receipt scope")
        if receipt["used_live"]:
            if not grant.allow_live or not grant.free_entitlement:
                raise ValueError("live collection lacked explicit grant")
            calls += int(receipt["review_id"] == record["review_id"])
    if (
        calls != record["provider_calls"]
        or calls > plan.limits.provider_calls
        or record["spend_minor"] != 0
    ):
        raise ValueError("collection budget mismatch")
    return {
        "provider_calls": calls,
        "spend_minor": 0,
        "journal_signature": "ED25519_SIGNED",
    }
