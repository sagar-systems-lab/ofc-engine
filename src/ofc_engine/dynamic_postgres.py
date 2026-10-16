"""PostgreSQL reservations for budgeted, per-plan live-Qloo evaluation."""
from __future__ import annotations

from datetime import datetime, timezone
import json

from .dynamic import DynamicQlooReview
from .durable import StorageIntegrityError
from .postgres_store import PostgresRevisionStore
from .qloo import QlooProbeClient, QlooProbeError, ProbeSpec
from .qloo_affinity import AffinityCaptureError, normalize_affinity_capture
from .revisions import digest_obj


class PostgresDynamicQlooReview(DynamicQlooReview):
    def __init__(self, store: PostgresRevisionStore, client: QlooProbeClient, *, daily_limit: int):
        if not isinstance(store, PostgresRevisionStore) or not isinstance(client, QlooProbeClient):
            raise TypeError("Postgres store and Qloo client required")
        if type(daily_limit) is not int or not 1 <= daily_limit <= 64:
            raise ValueError("Qloo request budget must be between 1 and 64")
        self.store = store
        self.client = client
        self.daily_limit = daily_limit
        with store._connect() as db:
            if db.execute(
                "select id from ofc_private.dynamic_qloo_budget where id=1"
            ).fetchone() != (1,):
                raise RuntimeError("dynamic Qloo budget migration missing")
            db.execute("select plan_id from ofc_private.dynamic_qloo_attempts limit 0")

    def _observation(self, plan_id: str, decision_id: str, pass_no: int,
                     baseline_digest: str, spec: ProbeSpec) -> tuple[dict | None, int, str]:
        with self.store._connect() as db:
            # Serialize admissions through one budget row, including first-use scopes.
            budget = db.execute(
                "select utc_day,reserved from ofc_private.dynamic_qloo_budget "
                "where id=1 for update"
            ).fetchone()
            if budget is None:
                raise StorageIntegrityError("missing Qloo budget gate")
            row = db.execute(
                "select baseline_digest,query_fingerprint,state,result,result_digest "
                "from ofc_private.dynamic_qloo_attempts "
                "where plan_id=%s and decision_id=%s and pass_no=%s for update",
                (plan_id, decision_id, pass_no),
            ).fetchone()
            if row is not None:
                if row[0] != baseline_digest or row[1] != spec.fingerprint:
                    raise StorageIntegrityError("Qloo attempt scope mismatch")
                if row[2] != "COMPLETE":
                    return None, 0, "ATTEMPT_ALREADY_RESERVED"
                try:
                    saved = json.loads(row[3])
                except (ValueError, TypeError) as exc:
                    raise StorageIntegrityError("invalid stored Qloo rank") from exc
                candidates = dict(spec.params)["filter.results.entities"].split(",")
                if (
                    not isinstance(saved, dict)
                    or digest_obj(saved) != row[4]
                    or saved.get("query_fingerprint") != spec.fingerprint
                    or not isinstance(saved.get("ranking"), list)
                    or len(saved["ranking"]) != len(set(saved["ranking"]))
                    or set(saved["ranking"]) != set(candidates)
                ):
                    raise StorageIntegrityError("Qloo rank scope or digest mismatch")
                return saved, 0, ""

            today = datetime.now(timezone.utc).date()
            used = budget[1] if budget[0] == today else 0
            if used >= self.daily_limit:
                return None, 0, "DAILY_QLOO_BUDGET_EXHAUSTED"
            db.execute(
                "update ofc_private.dynamic_qloo_budget "
                "set utc_day=%s,reserved=%s where id=1",
                (today, used + 1),
            )
            db.execute(
                "insert into ofc_private.dynamic_qloo_attempts "
                "(plan_id,decision_id,pass_no,baseline_digest,query_fingerprint,state) "
                "values (%s,%s,%s,%s,%s,'RESERVED')",
                (plan_id, decision_id, pass_no, baseline_digest, spec.fingerprint),
            )
        # The reservation is committed before the external request.
        try:
            observed = self.client.probe(spec)
            capture = {
                "summary": {
                    "endpoint": observed.path,
                    "query_fingerprint": observed.query_fingerprint,
                    "utc_collected_at": observed.utc_collected_at,
                    "http_status": observed.http_status,
                    "real_qloo_requests": 1,
                    "mode": "LIVE_RESPONSE_UNQUALIFIED",
                },
                "payload": observed.payload,
            }
            ranking = normalize_affinity_capture(capture, spec)
            result = {
                "query_fingerprint": spec.fingerprint,
                "collected_at": ranking["collected_at"],
                "ranking": [item["entity_id"] for item in ranking["ranking"]],
                "capture_digest": digest_obj(observed.payload),
            }
        except (QlooProbeError, AffinityCaptureError, ValueError, TypeError):
            return None, 1, "QLOO_EVIDENCE_UNAVAILABLE"

        with self.store._connect() as db:
            changed = db.execute(
                "update ofc_private.dynamic_qloo_attempts "
                "set state='COMPLETE',result=%s,result_digest=%s "
                "where plan_id=%s and decision_id=%s and pass_no=%s "
                "and state='RESERVED' and baseline_digest=%s and query_fingerprint=%s",
                (json.dumps(result, sort_keys=True), digest_obj(result),
                 plan_id, decision_id, pass_no, baseline_digest, spec.fingerprint),
            )
            if changed.rowcount != 1:
                raise StorageIntegrityError("Qloo completion without reservation")
        return result, 1, ""
