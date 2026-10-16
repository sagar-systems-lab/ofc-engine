"""PostgreSQL-backed Qloo catalog observations without unsupported policy promotion."""
from __future__ import annotations

from datetime import datetime, timezone
import json

from .durable import StorageIntegrityError
from .dynamic_postgres import PostgresDynamicQlooReview
from .qloo import QlooProbeError
from .qloo_catalog import (
    CatalogEvidenceError, category_for, domain_affinity_probe,
    inspect_domain_rank,
)
from .revisions import digest_obj
from .taste_intelligence import TasteContext, discovery_spec, normalize_discovery, TasteEvidenceError


class HostedDomainObservations:
    def __init__(self, reviewer: PostgresDynamicQlooReview):
        if not isinstance(reviewer, PostgresDynamicQlooReview):
            raise TypeError("private Qloo reviewer required")
        self.reviewer = reviewer

    def inspect(
        self, *, run_id: str, decision_id: str, category: str,
        subject_id: str, candidate_ids: tuple[str, ...],
    ) -> dict:
        domain = category_for(category)
        spec = domain_affinity_probe(subject_id, domain, candidate_ids)
        store = self.reviewer.store
        with store._connect() as db:
            budget = db.execute(
                "select utc_day,reserved from ofc_private.dynamic_qloo_budget "
                "where id=1 for update"
            ).fetchone()
            if budget is None:
                raise StorageIntegrityError("missing shared provider request quota")
            saved = db.execute(
                "select fingerprint,state,result,result_digest "
                "from ofc_private.copilot_domain_observations "
                "where run_id=%s and decision_id=%s for update",
                (run_id, decision_id),
            ).fetchone()
            if saved is not None:
                if saved[0] != spec.fingerprint:
                    raise StorageIntegrityError("catalog observation scope conflict")
                if saved[1] != "COMPLETE":
                    return {
                        "status": "UNCERTAIN",
                        "reason_code": "CATALOG_ATTEMPT_ALREADY_RESERVED",
                        "policy_qualified": False,
                    }
                try:
                    result = json.loads(saved[2])
                except (ValueError, TypeError) as exc:
                    raise StorageIntegrityError("invalid catalog observation") from exc
                if (
                    not isinstance(result, dict)
                    or digest_obj(result) != saved[3]
                    or result.get("query_fingerprint") != spec.fingerprint
                    or result.get("category") != domain
                    or set(result.get("ranking", [])) != set(candidate_ids)
                ):
                    raise StorageIntegrityError("catalog observation digest mismatch")
                return result
            today = datetime.now(timezone.utc).date()
            used = budget[1] if budget[0] == today else 0
            if used >= self.reviewer.daily_limit:
                return {
                    "status": "UNCERTAIN",
                    "reason_code": "QLOO_DAILY_QUOTA_EXHAUSTED",
                    "policy_qualified": False,
                }
            db.execute(
                "update ofc_private.dynamic_qloo_budget "
                "set utc_day=%s,reserved=%s where id=1",
                (today, used + 1),
            )
            db.execute(
                "insert into ofc_private.copilot_domain_observations "
                "(run_id,decision_id,fingerprint,state) values(%s,%s,%s,'RESERVED')",
                (run_id, decision_id, spec.fingerprint),
            )
        try:
            response = self.reviewer.client.probe(spec)
            observed = inspect_domain_rank(response, spec)
        except (QlooProbeError, CatalogEvidenceError):
            return {
                "status": "UNCERTAIN",
                "reason_code": "CATALOG_EVIDENCE_UNAVAILABLE",
                "policy_qualified": False,
            }
        result = {
            **observed,
            "status": "OBSERVED_NOT_POLICY_QUALIFIED",
        }
        with store._connect() as db:
            written = db.execute(
                "update ofc_private.copilot_domain_observations "
                "set state='COMPLETE',result=%s,result_digest=%s "
                "where run_id=%s and decision_id=%s "
                "and fingerprint=%s and state='RESERVED'",
                (json.dumps(result, sort_keys=True), digest_obj(result),
                 run_id, decision_id, spec.fingerprint),
            )
            if written.rowcount != 1:
                raise StorageIntegrityError("lost catalog observation reservation")
        return result

    def discover(self, *, run_id: str, context: TasteContext, seed_id: str) -> dict:
        spec = discovery_spec(context, seed_id)
        store = self.reviewer.store
        with store._connect() as db:
            budget = db.execute(
                "select utc_day,reserved from ofc_private.dynamic_qloo_budget "
                "where id=1 for update"
            ).fetchone()
            if budget is None:
                raise StorageIntegrityError("missing Qloo daily budget")
            saved = db.execute(
                "select fingerprint,state,result,result_digest "
                "from ofc_private.copilot_discoveries where run_id=%s for update",
                (run_id,),
            ).fetchone()
            if saved is not None:
                if saved[0] != spec.fingerprint:
                    raise StorageIntegrityError("discovery request scope conflict")
                if saved[1] != "COMPLETE":
                    return {"status": "UNCERTAIN", "reason_code": "DISCOVERY_ALREADY_RESERVED"}
                try:
                    restored = json.loads(saved[2])
                except (TypeError, ValueError) as exc:
                    raise StorageIntegrityError("invalid saved discovery") from exc
                if digest_obj(restored) != saved[3]:
                    raise StorageIntegrityError("discovery integrity mismatch")
                return restored
            today = datetime.now(timezone.utc).date()
            used = budget[1] if budget[0] == today else 0
            if used >= self.reviewer.daily_limit:
                return {"status": "UNCERTAIN", "reason_code": "QLOO_DAILY_QUOTA_EXHAUSTED"}
            db.execute(
                "update ofc_private.dynamic_qloo_budget "
                "set utc_day=%s,reserved=%s where id=1",
                (today, used + 1),
            )
            db.execute(
                "insert into ofc_private.copilot_discoveries "
                "(run_id,fingerprint,state) values(%s,%s,'RESERVED')",
                (run_id, spec.fingerprint),
            )
        try:
            capture = self.reviewer.client.probe(spec)
            result = normalize_discovery(capture, spec)
        except (QlooProbeError, TasteEvidenceError):
            return {"status": "UNCERTAIN", "reason_code": "QLOO_DISCOVERY_UNAVAILABLE"}
        with store._connect() as db:
            updated = db.execute(
                "update ofc_private.copilot_discoveries "
                "set state='COMPLETE',result=%s,result_digest=%s "
                "where run_id=%s and fingerprint=%s and state='RESERVED'",
                (json.dumps(result, sort_keys=True), digest_obj(result),
                 run_id, spec.fingerprint),
            )
            if updated.rowcount != 1:
                raise StorageIntegrityError("lost discovery reservation")
        return result
