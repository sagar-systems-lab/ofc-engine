"""Owner-controlled signing authorities and revision-bound business proof intake."""
from __future__ import annotations

import base64
from dataclasses import asdict
import json
import re

from cryptography.fernet import InvalidToken
from .business_assurance import (
    BUSINESS_REQUIREMENTS, SignedBusinessProof, TrustedAuthority,
    assess_business_readiness,
)
from .durable import StorageIntegrityError
from .revisions import digest_obj

_ISSUER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{2,95}$")


class BusinessProofIntake:
    def __init__(self, copilot):
        self.copilot = copilot
        self.store = copilot.store
        self.vault = copilot.vault

    def register_authority(
        self, owner: str, *, issuer: str, public_key_b64: str,
        requirements: list[str],
    ) -> dict:
        self.vault.authorize(owner)
        if (
            not isinstance(issuer, str) or not _ISSUER.fullmatch(issuer)
            or not isinstance(requirements, list) or not requirements
            or any(not isinstance(x, str) for x in requirements)
            or not set(requirements).issubset(BUSINESS_REQUIREMENTS)
            or len(set(requirements)) != len(requirements)
        ):
            raise ValueError("INVALID_TRUSTED_AUTHORITY")
        try:
            raw = base64.b64decode(public_key_b64, validate=True)
        except (ValueError, TypeError):
            raise ValueError("INVALID_AUTHORITY_KEY") from None
        authority = TrustedAuthority(
            issuer=issuer, requirements=frozenset(requirements), public_key=raw,
        )
        with self.store._connect() as db:
            db.execute(
                "insert into ofc_private.copilot_business_authorities "
                "(issuer,requirements,public_key,active) values(%s,%s,%s,true) "
                "on conflict(issuer) do update set requirements=excluded.requirements,"
                "public_key=excluded.public_key,active=true",
                (issuer, json.dumps(sorted(authority.requirements)), raw),
            )
        return {"issuer": issuer, "status": "OWNER_REGISTERED_AUTHORITY"}

    def revoke_authority(self, owner: str, issuer: str) -> dict:
        self.vault.authorize(owner)
        if not isinstance(issuer, str) or not _ISSUER.fullmatch(issuer):
            raise ValueError("INVALID_AUTHORITY")
        with self.store._connect() as db:
            db.execute(
                "update ofc_private.copilot_business_authorities "
                "set active=false where issuer=%s",
                (issuer,),
            )
        return {"issuer": issuer, "status": "REVOKED"}

    def _trusted(self) -> dict[str, TrustedAuthority]:
        with self.store._connect() as db:
            rows = db.execute(
                "select issuer,requirements,public_key from "
                "ofc_private.copilot_business_authorities where active=true"
            ).fetchall()
        return {
            name: TrustedAuthority(
                issuer=name, requirements=frozenset(json.loads(requirements)),
                public_key=bytes(pubkey),
            )
            for name, requirements, pubkey in rows
        }

    def _requirements(self, run_id: str) -> dict:
        with self.store._connect() as db:
            row = db.execute(
                "select status,result,result_ciphertext,result_digest "
                "from ofc_private.copilot_messages where run_id=%s",
                (run_id,),
            ).fetchone()
        if row is None or row[0] != "COMPLETE":
            raise ValueError("BUSINESS_RUN_UNAVAILABLE")
        value = self.copilot._decode_result(row[1], row[2], row[3])
        choices = value.get("plan", {}).get("choices", [])
        return {
            c["decision_id"]: (
                ("budget_feasibility", "contract_rights", "local_audience")
                if c["domain"].lower() in ("brand", "partner", "brand_partner")
                else ("venue_availability",)
            )
            for c in choices
            if c["domain"].lower() in (
                "brand", "partner", "brand_partner", "venue", "place", "restaurant"
            )
        }

    def _proofs(self, run_id: str) -> tuple[SignedBusinessProof, ...]:
        with self.store._connect() as db:
            rows = db.execute(
                "select proof_ciphertext,proof_digest from "
                "ofc_private.copilot_business_proofs where run_id=%s",
                (run_id,),
            ).fetchall()
        proofs = []
        for ciphertext, digest in rows:
            try:
                raw = self.vault.cipher.decrypt(bytes(ciphertext))
                value = json.loads(raw)
                if not isinstance(value, dict) or digest_obj(value) != digest:
                    raise ValueError("digest")
                proofs.append(SignedBusinessProof(**value))
            except (InvalidToken, ValueError, TypeError, KeyError) as exc:
                raise StorageIntegrityError("invalid encrypted business proof") from exc
        return tuple(proofs)

    def report(self, owner: str, run_id: str) -> dict:
        self.vault.authorize(owner)
        required = self._requirements(run_id)
        review = assess_business_readiness(
            self.store.get(run_id), required=required,
            proofs=self._proofs(run_id), authorities=self._trusted(),
        )
        return {**review, "trust_status": "OWNER_REGISTERED_SOURCES",
                "external_authority_provenance_independently_verified": False}

    def attach(self, owner: str, *, run_id: str, proof: dict) -> dict:
        self.vault.authorize(owner)
        if not isinstance(proof, dict) or len(json.dumps(proof)) > 16000:
            raise ValueError("INVALID_BUSINESS_PROOF")
        try:
            signed = SignedBusinessProof(**proof)
        except (ValueError, TypeError):
            raise ValueError("INVALID_BUSINESS_PROOF") from None
        allowed = self._requirements(run_id)
        if signed.decision_id not in allowed or signed.requirement not in allowed[signed.decision_id]:
            raise ValueError("BUSINESS_POLICY_SCOPE_MISMATCH")
        trusted = self._trusted()
        authority = trusted.get(signed.issuer)
        if authority is None:
            raise ValueError("UNTRUSTED_BUSINESS_ISSUER")
        inspection = assess_business_readiness(
            self.store.get(run_id),
            required={signed.decision_id: (signed.requirement,)},
            proofs=(signed,), authorities={signed.issuer: authority},
        )
        if (
            inspection["invalid_evidence_count"] != 0
            or inspection["missing_requirements"].get(signed.decision_id)
        ):
            raise ValueError("SIGNED_BUSINESS_EVIDENCE_INVALID")
        content = asdict(signed)
        with self.store._connect() as db:
            db.execute(
                "insert into ofc_private.copilot_business_proofs "
                "(run_id,decision_id,requirement,issuer,proof_ciphertext,proof_digest) "
                "values(%s,%s,%s,%s,%s,%s) on conflict("
                "run_id,decision_id,requirement,issuer) do update set "
                "proof_ciphertext=excluded.proof_ciphertext,"
                "proof_digest=excluded.proof_digest",
                (run_id, signed.decision_id, signed.requirement, signed.issuer,
                 self.vault.cipher.encrypt(json.dumps(content).encode()),
                 digest_obj(content)),
            )
        return self.report(owner, run_id)
