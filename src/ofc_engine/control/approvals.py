from __future__ import annotations

from datetime import datetime, timezone
import json
import base64
from dataclasses import dataclass
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .canonical import canonical_bytes
from .ownership import review_actor
import secrets
from typing import Literal

from pydantic import Field

from .canonical import digest
from .models import Id, Strict, instant, utc_now
from .storage import Conflict, NotFound


class IntentInput(Strict):
    request_key: Id
    node_ids: tuple[Id, ...] = Field(min_length=1, max_length=64)
    expires_at: str = Field(max_length=32)
    action: Literal["REVIEW_HANDOFF"] = "REVIEW_HANDOFF"
    maximum_spend_minor: Literal[0] = 0
    signature: str | None = Field(
        default=None, min_length=88, max_length=88, pattern=r"^[A-Za-z0-9+/]{86}==$"
    )


def create_intent(runtime, review_id, identity, data):
    if identity.kind != "human" or "approve" not in identity.roles:
        raise PermissionError("HUMAN_AUTHORITY_REQUIRED")
    request = IntentInput.model_validate_json(json.dumps(data))
    at = datetime.now(timezone.utc)
    if not 0 < (instant(request.expires_at) - at).total_seconds() <= 3600:
        raise ValueError("intent expiry must be within one hour")
    if len(request.node_ids) != len(set(request.node_ids)):
        raise ValueError("duplicate intent resource")
    owner = review_actor(
        runtime, review_id, identity, request.node_ids, permission="read"
    )
    proof = runtime.store.proof(
        review_id, identity.tenant_id, owner, authorities=runtime.authorities
    )
    if not set(request.node_ids) <= {n["id"] for n in proof["current"]["nodes"]}:
        raise ValueError("intent resource outside reviewed plan")
    from .ownership import authorized

    for node in proof["current"]["nodes"]:
        if node["id"] in request.node_ids and not authorized(
            runtime.delegations,
            tenant_id=identity.tenant_id,
            owner=node["owner"],
            delegate=identity.actor_id,
            plan_digest=proof["current_digest"],
            nodes=(node["id"],),
            permission="approve",
            at=utc_now(),
        ):
            raise PermissionError("NODE_APPROVER_DELEGATION_REQUIRED")
    payload = {
        "schema": "ofc.approval-intent/1",
        "review_id": review_id,
        "tenant_id": identity.tenant_id,
        "actor_id": identity.actor_id,
        "revision": proof["revision"],
        "plan_digest": proof["current_digest"],
        "policy_digest": proof["policy_digest"],
        "evidence_root": proof["evidence_root"],
        "proof_digest": proof["proof_digest"],
        "request": request.data(),
        "state": "APPROVAL_PENDING",
        "release_authorized": False,
        "external_action_executed": False,
        "signature_status": "UNSIGNED_INTENT",
        "remaining_obligations": [
            u for u in proof["unresolved"] if u["node_id"] in request.node_ids
        ],
    }
    signed_fields = {
        "review_id": review_id,
        "tenant_id": identity.tenant_id,
        "reviewer_id": identity.actor_id,
        "plan_digest": proof["current_digest"],
        "policy_digest": proof["policy_digest"],
        "evidence_root": proof["evidence_root"],
        "revision": proof["revision"],
        "request": request.model_dump(mode="json", exclude={"signature"}),
    }
    if request.signature:
        key = runtime.approval_keys.get(identity.actor_id)
        if (
            key is None
            or key.revoked
            or key.tenant_id != identity.tenant_id
            or instant(key.expires_at) <= at
        ):
            raise PermissionError("APPROVAL_SIGNER_UNTRUSTED")
        try:
            Ed25519PublicKey.from_public_bytes(key.public_key).verify(
                base64.b64decode(request.signature, validate=True),
                canonical_bytes(signed_fields),
            )
        except Exception as exc:
            raise PermissionError("APPROVAL_SIGNATURE_INVALID") from exc
        payload["signature_status"] = "VERIFIED_HUMAN_INTENT"
        payload["signed_fields"] = signed_fields
    elif owner != identity.actor_id:
        raise PermissionError("DELEGATED_HUMAN_SIGNATURE_REQUIRED")
    payload["review_actor_id"] = owner
    store = runtime.store
    with store._tx(write=True) as db:
        row = store._run(
            db,
            f"select payload,digest from {store.prefix}intents where review_id=? and request_key=?",
            (review_id, request.request_key),
        ).fetchone()
        if row:
            previous = store._decode(row[0], row[1])
            if previous["binding"] != payload:
                raise Conflict("approval request key already binds a different intent")
            state = store._run(
                db,
                f"select state from {store.prefix}intents where intent_id=?",
                (previous["intent_id"],),
            ).fetchone()[0]
            if state == "REVOKED":
                raise Conflict("approval intent revoked")
            return previous
        result = {
            "intent_id": "ai-" + secrets.token_hex(16),
            "created_at": utc_now(),
            "binding": payload,
        }
        store._run(
            db,
            f"insert into {store.prefix}intents values(?,?,?,?,?,?,?,?)",
            (
                result["intent_id"],
                review_id,
                request.request_key,
                identity.tenant_id,
                identity.actor_id,
                "APPROVAL_PENDING",
                store._encode(result, private=proof["mode"] == "PRIVATE"),
                digest(result),
            ),
        )
        store._append(
            db,
            review_id,
            "APPROVAL_INTENT_RECORDED",
            {"intent_digest": digest(result), "release_authorized": False},
        )
    return result


def revoke_intent(runtime, review_id, identity, intent_id):
    if identity.kind != "human" or "approve" not in identity.roles:
        raise PermissionError("HUMAN_AUTHORITY_REQUIRED")
    store = runtime.store
    with store._tx(write=True) as db:
        existing = store._run(
            db,
            f"select state,payload,digest from {store.prefix}intents where intent_id=? and review_id=? and tenant_id=? and actor_id=?",
            (intent_id, review_id, identity.tenant_id, identity.actor_id),
        ).fetchone()
        if existing is None:
            raise NotFound("approval intent unavailable")
        bound = store._decode(existing[1], existing[2])
        review_actor(
            runtime, review_id, identity, bound["binding"]["request"]["node_ids"]
        )
        changed = store._run(
            db,
            f"update {store.prefix}intents set state='REVOKED' where intent_id=? and review_id=? and tenant_id=? and actor_id=? and state='APPROVAL_PENDING'",
            (intent_id, review_id, identity.tenant_id, identity.actor_id),
        ).rowcount
        if changed:
            store._append(
                db,
                review_id,
                "APPROVAL_INTENT_REVOKED",
                {"intent_id": intent_id, "release_authorized": False},
            )
    return {
        "intent_id": intent_id,
        "state": "REVOKED",
        "release_authorized": False,
        "external_action_executed": False,
    }


@dataclass(frozen=True)
class ApprovalKey:
    tenant_id: str
    public_key: bytes
    expires_at: str
    revoked: bool = False


def validate_intent(intent, proof, key, *, as_of, revoked=False):
    binding = intent["binding"]
    if (
        revoked
        or key.revoked
        or instant(key.expires_at) <= instant(as_of)
        or instant(binding["request"]["expires_at"]) <= instant(as_of)
        or any(
            binding[f] != proof[f]
            for f in ("revision", "policy_digest", "evidence_root", "proof_digest")
        )
        or binding["plan_digest"] != proof["current_digest"]
        or binding["signature_status"] != "VERIFIED_HUMAN_INTENT"
    ):
        raise ValueError("approval binding invalidated")
    expected = {
        "review_id": binding["review_id"],
        "tenant_id": binding["tenant_id"],
        "reviewer_id": binding["actor_id"],
        "plan_digest": binding["plan_digest"],
        "policy_digest": binding["policy_digest"],
        "evidence_root": binding["evidence_root"],
        "revision": binding["revision"],
        "request": {k: v for k, v in binding["request"].items() if k != "signature"},
    }
    if binding["signed_fields"] != expected or key.tenant_id != binding["tenant_id"]:
        raise ValueError("approval signed scope differs from binding")
    Ed25519PublicKey.from_public_bytes(key.public_key).verify(
        base64.b64decode(binding["request"]["signature"], validate=True),
        canonical_bytes(expected),
    )
    return {"state": "APPROVAL_PENDING", "release_authorized": False}
