"""Budgeted Qloo name resolution using the same durable quota as rank evaluation."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Literal

from .copilot_intake import EntityResolutionRequired, ResolvedEntity
from .copilot_resolver import normalize_qloo_search
from .dynamic import DynamicQlooReview
from .durable import StorageIntegrityError
from .qloo import QlooProbeClient, QlooProbeError, search_probe
from .revisions import digest_obj

Kind = Literal["artist", "brand"]


class BudgetedQlooResolver:
    """Reserve every Qloo /search request before I/O; never retry an uncertain attempt."""

    def __init__(self, engine: DynamicQlooReview):
        if not isinstance(engine, DynamicQlooReview):
            raise TypeError("a configured dynamic Qloo reviewer is required")
        self.engine = engine
        with self.engine.store._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS copilot_qloo_search (
                    run_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('RESERVED','COMPLETE')),
                    result TEXT,
                    result_digest TEXT,
                    PRIMARY KEY(run_id, fingerprint)
                )
            """)

    def resolve(self, *, run_id: str, name: str, kind: Kind) -> ResolvedEntity:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run id required")
        if kind not in ("artist", "brand"):
            raise EntityResolutionRequired("UNSUPPORTED_QLOO_SEARCH_CATEGORY")
        spec = search_probe(name, entity_type="urn:entity:" + kind)
        db = self.engine.store._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,result,result_digest FROM copilot_qloo_search "
                "WHERE run_id=? AND fingerprint=?",
                (run_id, spec.fingerprint),
            ).fetchone()
            if row is not None:
                if row[0] != "COMPLETE":
                    db.execute("COMMIT")
                    raise EntityResolutionRequired("QLOO_SEARCH_ALREADY_RESERVED")
                try:
                    saved = json.loads(row[1])
                    if not isinstance(saved, dict) or digest_obj(saved) != row[2]:
                        raise StorageIntegrityError("Qloo search result digest mismatch")
                    result = ResolvedEntity(**saved)
                except (TypeError, ValueError, KeyError) as exc:
                    raise StorageIntegrityError("invalid saved Qloo search result") from exc
                if (
                    result.name != name or result.entity_type != "urn:entity:" + kind
                    or result.query_fingerprint != spec.fingerprint
                ):
                    raise StorageIntegrityError("saved Qloo identity scope mismatch")
                db.execute("COMMIT")
                return result

            today = datetime.now(timezone.utc).date().isoformat()
            usage = db.execute(
                "SELECT reserved FROM dynamic_qloo_daily WHERE utc_day=?", (today,)
            ).fetchone()
            if usage is not None and usage[0] >= self.engine.daily_limit:
                db.execute("COMMIT")
                raise EntityResolutionRequired("DAILY_QLOO_BUDGET_EXHAUSTED")
            db.execute(
                "INSERT INTO dynamic_qloo_daily(utc_day,reserved) VALUES(?,1) "
                "ON CONFLICT(utc_day) DO UPDATE SET reserved=reserved+1",
                (today,),
            )
            db.execute(
                "INSERT INTO copilot_qloo_search(run_id,fingerprint,state) "
                "VALUES(?,?,'RESERVED')",
                (run_id, spec.fingerprint),
            )
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

        try:
            observation = self.engine.client.probe(spec)
            result = normalize_qloo_search(
                name, entity_type=kind, spec=spec, observation=observation,
            )
        except QlooProbeError:
            raise EntityResolutionRequired("QLOO_SEARCH_UNAVAILABLE") from None
        # Ambiguity or a malformed response consumes its reservation as well.
        payload = {
            "name": result.name, "entity_id": result.entity_id,
            "entity_type": result.entity_type,
            "query_fingerprint": result.query_fingerprint,
            "source": result.source,
        }
        db = self.engine.store._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                "UPDATE copilot_qloo_search SET state='COMPLETE',result=?,"
                "result_digest=? WHERE run_id=? AND fingerprint=? AND state='RESERVED'",
                (json.dumps(payload, sort_keys=True), digest_obj(payload),
                 run_id, spec.fingerprint),
            )
            if updated.rowcount != 1:
                raise StorageIntegrityError("Qloo search reservation lost")
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()
        return result
