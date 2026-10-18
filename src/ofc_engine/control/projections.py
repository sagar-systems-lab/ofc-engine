from __future__ import annotations

import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .canonical import canonical_bytes, digest


def public_projection(proof, *, signing_key=None):
    # Only commitments and the action boundary are licensed for this projection.
    fields = {
        "schema": "ofc.public-control-proof/1",
        "private_proof_digest": proof["proof_digest"],
        "policy_digest": proof["policy_digest"],
        "action_status": "WITHHELD",
        "external_action_executed": False,
        "disclosure": "COMMITMENTS_ONLY",
        "claim_verification": "PRIVATE_RECORD_REQUIRED",
    }
    signature = (
        base64.b64encode(signing_key.sign(canonical_bytes(fields))).decode()
        if signing_key
        else None
    )
    return {
        "projection": fields,
        "projection_digest": digest(fields),
        "signature": signature,
        "signature_status": "ED25519_SIGNED" if signature else "UNSIGNED",
    }


def verify_projection(value, *, expected_digest=None, public_key=None):
    fields = value["projection"]
    if (
        set(fields)
        != {
            "schema",
            "private_proof_digest",
            "policy_digest",
            "action_status",
            "external_action_executed",
            "disclosure",
            "claim_verification",
        }
        or fields["schema"] != "ofc.public-control-proof/1"
        or fields["action_status"] != "WITHHELD"
        or fields["external_action_executed"] is not False
        or fields["disclosure"] != "COMMITMENTS_ONLY"
        or fields["claim_verification"] != "PRIVATE_RECORD_REQUIRED"
        or digest(fields) != value["projection_digest"]
        or expected_digest is not None
        and expected_digest != value["projection_digest"]
    ):
        raise ValueError("public projection mismatch")
    if public_key is not None:
        if value["signature_status"] != "ED25519_SIGNED":
            raise ValueError("public signature required")
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            base64.b64decode(value["signature"], validate=True), canonical_bytes(fields)
        )
    elif expected_digest is None:
        raise ValueError("independent projection anchor required")
    return {
        "valid": True,
        "qualification": "SIGNED_COMMITMENT" if public_key else "ANCHORED_COMMITMENT",
        "claim_verification": "PRIVATE_RECORD_REQUIRED",
    }
