"""Durable PostgreSQL quota gates for hosted Qloo lookup and model requests."""
from __future__ import annotations

from datetime import datetime, timezone
import json

from .copilot_intake import ResolvedEntity, EntityResolutionRequired
from .copilot_resolver import normalize_qloo_search
from .durable import StorageIntegrityError
from .dynamic_postgres import PostgresDynamicQlooReview
from .postgres_store import PostgresRevisionStore
from .qloo import QlooProbeError, search_probe
from .revisions import digest_obj


class CopilotQuotaReached(ValueError):
    pass


def reserve_model_call(
    store: PostgresRevisionStore, *, daily_limit: int,
    run_id: str | None = None,
) -> None:
    if type(daily_limit) is not int or not 1 <= daily_limit <= 64:
        raise ValueError("invalid model budget")
    with store._connect() as db:
        row = db.execute(
            "select utc_day,reserved from ofc_private.copilot_llm_budget "
            "where id=1 for update"
        ).fetchone()
        if row is None:
            raise StorageIntegrityError("model quota migration missing")
        day = datetime.now(timezone.utc).date()
        used = row[1] if row[0] == day else 0
        if used >= daily_limit:
            raise CopilotQuotaReached("LLM_DAILY_QUOTA_EXHAUSTED")
        db.execute(
            "update ofc_private.copilot_llm_budget "
            "set utc_day=%s,reserved=%s where id=1",
            (day, used + 1),
        )
        if run_id is not None:
            updated = db.execute(
                "update ofc_private.copilot_messages "
                "set llm_attempts=llm_attempts+1 "
                "where run_id=%s and status='RESERVED' and llm_attempts<4",
                (run_id,),
            )
            if updated.rowcount != 1:
                raise StorageIntegrityError("model admission lost its active run")


def reserve_qloo_request(store: PostgresRevisionStore, *, daily_limit: int) -> bool:
    if type(daily_limit) is not int or not 1 <= daily_limit <= 64:
        raise ValueError("invalid Qloo budget")
    with store._connect() as db:
        row = db.execute(
            "select utc_day,reserved from ofc_private.dynamic_qloo_budget "
            "where id=1 for update"
        ).fetchone()
        if row is None:
            raise StorageIntegrityError("Qloo quota migration missing")
        day = datetime.now(timezone.utc).date()
        used = row[1] if row[0] == day else 0
        if used >= daily_limit:
            return False
        db.execute(
            "update ofc_private.dynamic_qloo_budget "
            "set utc_day=%s,reserved=%s where id=1", (day, used + 1),
        )
        return True


class HostedQlooResolver:
    def __init__(self, reviewer: PostgresDynamicQlooReview):
        if not isinstance(reviewer, PostgresDynamicQlooReview):
            raise TypeError("Postgres Qloo reviewer required")
        self.reviewer = reviewer

    def resolve(self, *, run_id: str, name: str, kind: str) -> ResolvedEntity:
        if kind not in ("brand", "artist"):
            raise EntityResolutionRequired("UNSUPPORTED_QLOO_SEARCH_TYPE")
        spec = search_probe(name, entity_type="urn:entity:" + kind)
        store = self.reviewer.store
        with store._connect() as db:
            # One serialized gate for search and independent insights requests.
            budget = db.execute(
                "select utc_day,reserved from ofc_private.dynamic_qloo_budget "
                "where id=1 for update"
            ).fetchone()
            if budget is None:
                raise StorageIntegrityError("missing shared Qloo quota")
            found = db.execute(
                "select status,result,result_digest from ofc_private.copilot_search "
                "where run_id=%s and fingerprint=%s for update",
                (run_id, spec.fingerprint),
            ).fetchone()
            if found is not None:
                if found[0] != "COMPLETE":
                    raise EntityResolutionRequired("QLOO_SEARCH_ALREADY_RESERVED")
                try:
                    stored = json.loads(found[1])
                    if not isinstance(stored, dict) or digest_obj(stored) != found[2]:
                        raise StorageIntegrityError("invalid saved Qloo lookup")
                    saved = ResolvedEntity(**stored)
                except (TypeError, ValueError) as exc:
                    raise StorageIntegrityError("corrupted Qloo identity") from exc
                if (
                    saved.name != name or saved.entity_type != "urn:entity:" + kind
                    or saved.query_fingerprint != spec.fingerprint
                ):
                    raise StorageIntegrityError("Qloo identity scope mismatch")
                return saved
            day = datetime.now(timezone.utc).date()
            used = budget[1] if budget[0] == day else 0
            if used >= self.reviewer.daily_limit:
                raise CopilotQuotaReached("QLOO_DAILY_QUOTA_EXHAUSTED")
            db.execute(
                "update ofc_private.dynamic_qloo_budget "
                "set utc_day=%s,reserved=%s where id=1",
                (day, used + 1),
            )
            db.execute(
                "insert into ofc_private.copilot_search(run_id,fingerprint,status) "
                "values(%s,%s,'RESERVED')",
                (run_id, spec.fingerprint),
            )
        try:
            captured = self.reviewer.client.probe(spec)
            resolved = normalize_qloo_search(
                name, entity_type=kind, spec=spec, observation=captured,
            )
        except QlooProbeError:
            raise EntityResolutionRequired("QLOO_SEARCH_UNAVAILABLE") from None
        payload = {
            "name": resolved.name, "entity_id": resolved.entity_id,
            "entity_type": resolved.entity_type,
            "query_fingerprint": resolved.query_fingerprint,
            "source": resolved.source,
        }
        with store._connect() as db:
            updated = db.execute(
                "update ofc_private.copilot_search "
                "set status='COMPLETE',result=%s,result_digest=%s "
                "where run_id=%s and fingerprint=%s and status='RESERVED'",
                (json.dumps(payload, sort_keys=True), digest_obj(payload),
                 run_id, spec.fingerprint),
            )
            if updated.rowcount != 1:
                raise StorageIntegrityError("lost Qloo search reservation")
        return resolved
