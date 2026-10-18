from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .http import ControlAccess, Identity
from .policies import Authority
from .runtime import ControlRuntime
from .storage import ControlStore


def configured_control(environment):
    mode = environment.get("OFC_CONTROL_MODE", "")
    if not mode:
        return False, None, ControlAccess()
    if mode == "SANDBOX":
        return True, None, ControlAccess()
    if mode != "PRIVATE":
        raise ValueError("unsupported control mode")
    dsn = environment.get("OFC_CONTROL_DATABASE_URL", "")
    key = environment.get("OFC_CONTROL_MASTER_KEY", "")
    if not dsn.startswith(("postgres://", "postgresql://")) or not key:
        raise ValueError("private control requires PostgreSQL and an encryption key")
    tokens_raw = environment.get("OFC_CONTROL_TOKENS", "{}")
    authorities_raw = environment.get("OFC_CONTROL_AUTHORITIES", "[]")
    if len(tokens_raw) > 16384 or len(authorities_raw) > 32768:
        raise ValueError("oversized control access configuration")
    tokens = {}
    for token, identity in json.loads(tokens_raw).items():
        identity["roles"] = frozenset(identity.get("roles", ["read", "submit"]))
        tokens[token] = Identity(**identity)
    authorities = {}
    for item in json.loads(authorities_raw):
        item["public_key"] = base64.b64decode(item["public_key"], validate=True)
        item["claims"] = frozenset(item["claims"])
        item["revoked_entitlements"] = frozenset(item.get("revoked_entitlements", []))
        authority = Authority(**item)
        if authority.issuer in authorities:
            raise ValueError("duplicate authority")
        authorities[authority.issuer] = authority
    from .ownership import Delegation
    from .approvals import ApprovalKey

    grants_raw = environment.get("OFC_CONTROL_DELEGATIONS", "[]")
    approvers_raw = environment.get("OFC_CONTROL_APPROVAL_KEYS", "{}")
    if max(len(grants_raw), len(approvers_raw)) > 32768:
        raise ValueError("oversized delegation configuration")
    grants = []
    for item in json.loads(grants_raw):
        item["node_ids"] = frozenset(item["node_ids"])
        item["permissions"] = frozenset(item["permissions"])
        grants.append(Delegation(**item))
    approval_keys = {}
    for actor, item in json.loads(approvers_raw).items():
        item["public_key"] = base64.b64decode(item["public_key"], validate=True)
        approval_keys[actor] = ApprovalKey(**item)
    store = ControlStore(dsn, encryption_key=key)
    signer = environment.get("OFC_CONTROL_CAPTURE_SIGNER", "")
    issuer = environment.get("OFC_CONTROL_CAPTURE_ISSUER", "")
    if bool(signer) != bool(issuer):
        raise ValueError("capture signer and issuer required together")
    keypair = (
        Ed25519PrivateKey.from_private_bytes(base64.b64decode(signer, validate=True))
        if signer
        else None
    )
    acquisition = configured_collection(environment, store, authorities)
    return (
        True,
        ControlRuntime(
            store,
            authorities=authorities,
            capture_signer=keypair,
            capture_issuer=issuer or None,
            delegations=grants,
            approval_keys=approval_keys,
            acquisition=acquisition,
        ),
        ControlAccess(tokens),
    )


def configured_collection(environment, store, authorities):
    if environment.get("OFC_CONTROL_COLLECTION", "") != "ENABLED":
        return None
    from .collection import GraphCollector, SourceBinding
    from .collection_contracts import CollectionGrant
    from .feeds import SourceHTTP, QlooGraphSource, SignedBusinessSource

    raw = environment.get("OFC_CONTROL_SOURCES", "[]")
    if len(raw) > 65536:
        raise ValueError("oversized source configuration")
    entries = json.loads(raw)
    if not 1 <= len(entries) <= 8:
        raise ValueError("one to eight explicitly scoped sources required")
    bindings = []
    for item in entries:
        grant = CollectionGrant.model_validate_json(json.dumps(item["grant"]))
        if not grant.allow_live:
            raise ValueError(
                "source configuration needs an explicit live capability grant"
            )
        secret = environment.get(item["token_env"], "")
        origin = item["origin"]
        if grant.provider == "qloo":
            if origin not in {"https://hackathon.api.qloo.com", "https://api.qloo.com"}:
                raise ValueError("Qloo source origin is not pinned")
            issuer = item["issuer"]
            key = Ed25519PrivateKey.from_private_bytes(
                base64.b64decode(environment.get(item["signer_env"], ""), validate=True)
            )
            authority = authorities.get(issuer)
            if (
                authority is None
                or authority.source != "qloo"
                or authority.tenant_id != grant.tenant_id
                or authority.provider_account_scope != grant.account_scope
                or authority.entitlement_id != grant.entitlement_id
                or authority.public_key != key.public_key().public_bytes_raw()
            ):
                raise ValueError("source signer must match a pinned account authority")
            adapter = QlooGraphSource(
                SourceHTTP(origin, token=secret, header="X-Api-Key"),
                issuer=issuer,
                signing_key=key,
            )
        else:
            adapter = SignedBusinessSource(
                SourceHTTP(origin, token="Bearer " + secret),
                path=item.get("path", "/evidence"),
            )
        bindings.append(SourceBinding(grant, adapter))
    issuer = environment.get("OFC_CONTROL_JOURNAL_ISSUER", "")
    key = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(
            environment.get("OFC_CONTROL_JOURNAL_SIGNER", ""), validate=True
        )
    )
    expected = base64.b64decode(
        environment.get("OFC_CONTROL_JOURNAL_PUBLIC_KEY", ""), validate=True
    )
    if not issuer or expected != key.public_key().public_bytes_raw():
        raise ValueError("independently pinned journal public key required")
    return GraphCollector(
        store, bindings, authorities=authorities, issuer=issuer, signing_key=key
    )
