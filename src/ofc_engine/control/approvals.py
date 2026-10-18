from __future__ import annotations

from datetime import datetime, timezone
import json
import secrets
from typing import Literal

from pydantic import Field

from .canonical import digest
from .models import Id, Strict, instant, utc_now
from .storage import Conflict


class IntentInput(Strict):
    request_key: Id
    node_ids: tuple[Id, ...] = Field(min_length=1, max_length=64)
    expires_at: str = Field(max_length=32)
    action: Literal["REVIEW_HANDOFF"] = "REVIEW_HANDOFF"
    maximum_spend_minor: Literal[0] = 0


def create_intent(runtime, review_id, identity, data):
    if identity.kind != "human" or "approve" not in identity.roles:
        raise PermissionError("HUMAN_AUTHORITY_REQUIRED")
    request = IntentInput.model_validate_json(json.dumps(data))
    at = datetime.now(timezone.utc)
    if not 0 < (instant(request.expires_at) - at).total_seconds() <= 3600:
        raise ValueError("intent expiry must be within one hour")
    if len(request.node_ids) != len(set(request.node_ids)):
        raise ValueError("duplicate intent resource")
    proof = runtime.store.proof(review_id, identity.tenant_id, identity.actor_id, authorities=runtime.authorities)
    if not set(request.node_ids) <= {n["id"] for n in proof["current"]["nodes"]}:
        raise ValueError("intent resource outside reviewed plan")
    payload = {"schema": "ofc.approval-intent/1", "review_id": review_id,
               "tenant_id": identity.tenant_id, "actor_id": identity.actor_id,
               "revision": proof["revision"], "plan_digest": proof["current_digest"],
               "policy_digest": proof["policy_digest"], "evidence_root": proof["evidence_root"],
               "proof_digest": proof["proof_digest"], "request": request.data(),
               "state": "APPROVAL_PENDING", "release_authorized": False,
               "external_action_executed": False, "signature_status": "UNSIGNED_INTENT",
               "remaining_obligations": proof["unresolved"]}
    store = runtime.store
    with store._tx(write=True) as db:
        row = store._run(db, f"select payload,digest from {store.prefix}intents where review_id=? and request_key=?", (review_id, request.request_key)).fetchone()
        if row:
            previous = store._decode(row[0], row[1])
            if previous["binding"] != payload:
                raise Conflict("approval request key already binds a different intent")
            state = store._run(db, f"select state from {store.prefix}intents where intent_id=?", (previous["intent_id"],)).fetchone()[0]
            if state == "REVOKED":
                raise Conflict("approval intent revoked")
            return previous
        result = {"intent_id": "ai-" + secrets.token_hex(16), "created_at": utc_now(), "binding": payload}
        store._run(db, f"insert into {store.prefix}intents values(?,?,?,?,?,?,?,?)",
                   (result["intent_id"], review_id, request.request_key, identity.tenant_id, identity.actor_id,
                    "APPROVAL_PENDING", store._encode(result, private=proof["mode"] == "PRIVATE"), digest(result)))
        store._append(db, review_id, "APPROVAL_INTENT_RECORDED", {"intent_digest": digest(result), "release_authorized": False})
    return result


def revoke_intent(runtime, review_id, identity, intent_id):
    if identity.kind != "human" or "approve" not in identity.roles:
        raise PermissionError("HUMAN_AUTHORITY_REQUIRED")
    store = runtime.store
    store.snapshot(review_id, identity.tenant_id, identity.actor_id)
    with store._tx(write=True) as db:
        changed = store._run(db, f"update {store.prefix}intents set state='REVOKED' where intent_id=? and review_id=? and tenant_id=? and actor_id=? and state='APPROVAL_PENDING'",
                             (intent_id, review_id, identity.tenant_id, identity.actor_id)).rowcount
        if changed:
            store._append(db, review_id, "APPROVAL_INTENT_REVOKED", {"intent_id": intent_id, "release_authorized": False})
    return {"intent_id": intent_id, "state": "REVOKED", "release_authorized": False,
            "external_action_executed": False}
