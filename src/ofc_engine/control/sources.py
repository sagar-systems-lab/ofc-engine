from __future__ import annotations

import base64

from .canonical import canonical_bytes, digest
from .models import Evidence
from .policies import POLICY_VERSION


class RecordedEvidenceAdapter:
    """Normalize entitlement-scoped saved records; signatures attest adapter origin only."""

    offline = True

    def __init__(
        self, records, *, provider, account_scope, entitlement_id, issuer, signing_key
    ):
        self.records, self.provider, self.account_scope = (
            tuple(records),
            provider,
            account_scope,
        )
        self.entitlement_id, self.issuer, self.key = entitlement_id, issuer, signing_key

    def collect(self, scope, *, deadline):
        if (scope["provider"], scope["account_scope"], scope["entitlement_id"]) != (
            self.provider,
            self.account_scope,
            self.entitlement_id,
        ):
            raise ValueError("SOURCE_ACCOUNT_OR_ENTITLEMENT_MISMATCH")
        matches = [
            r
            for r in self.records
            if r["claim"] == scope["claim"]
            and r["input_digest"] == scope["input_digest"]
            and r.get("locality") == scope["locality"]
        ]
        output = []
        for record in matches:
            if (
                record.get("account_scope") != self.account_scope
                or record.get("entitlement_id") != self.entitlement_id
            ):
                raise ValueError("RECORDED_SOURCE_SCOPE_MISMATCH")
            fields = {
                "id": "ev-"
                + digest({"scope": scope["scope_id"], "attempt": record["attempt_id"]})[
                    :32
                ],
                "tenant_id": scope["tenant_id"],
                "plan_digest": scope["plan_digest"],
                "node_id": scope["node_id"],
                "claim": scope["claim"],
                "subject_id": record["subject_id"],
                "candidate_pool": scope["pool"],
                "locality": scope["locality"],
                "input_digest": scope["input_digest"],
                "source": self.provider,
                "mode": "CAPTURED" if self.provider == "qloo" else "ATTESTED",
                "attempt_id": record["attempt_id"],
                "observed_at": record["observed_at"],
                "expires_at": record["expires_at"],
                "policy_version": POLICY_VERSION,
                "license_scope": "private_review",
                "facts": record.get("facts", {}),
                "ranks": record.get("ranks", []),
                "issuer": self.issuer,
                "provider_account_scope": self.account_scope,
                "entitlement_id": self.entitlement_id,
                "extraction_rule_version": record["extraction_rule_version"],
                "raw_payload_ref": record["raw_payload_ref"],
                "source_digest": record["raw_payload_ref"],
                "query_fingerprint": record.get("query_fingerprint"),
            }
            item = Evidence.model_validate_json(__import__("json").dumps(fields))
            output.append(
                item.model_copy(
                    update={
                        "signature": base64.b64encode(
                            self.key.sign(canonical_bytes(item.signing_fields()))
                        ).decode()
                    }
                ).data()
            )
        return output
