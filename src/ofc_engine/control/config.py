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
        authority = Authority(**item)
        if authority.issuer in authorities:
            raise ValueError("duplicate authority")
        authorities[authority.issuer] = authority
    store = ControlStore(dsn, encryption_key=key)
    signer = environment.get("OFC_CONTROL_CAPTURE_SIGNER", "")
    issuer = environment.get("OFC_CONTROL_CAPTURE_ISSUER", "")
    if bool(signer) != bool(issuer):
        raise ValueError("capture signer and issuer required together")
    keypair = Ed25519PrivateKey.from_private_bytes(base64.b64decode(signer, validate=True)) if signer else None
    return True, ControlRuntime(store, authorities=authorities, capture_signer=keypair,
                                capture_issuer=issuer or None), ControlAccess(tokens)
