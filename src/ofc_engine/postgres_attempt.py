"""Distributed-safe provider-attempt reservation in the OFC Postgres database."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import psycopg


class PostgresAttemptLedger:
    def __init__(
        self, dsn: str, *, scope: str, insecure_local_test: bool = False,
    ): 
        if len(scope) != 64 or any(c not in "0123456789abcdef" for c in scope):
            raise ValueError("invalid frozen Qloo scope")
        if not isinstance(dsn, str) or not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("Postgres connection required")
        parsed = urlsplit(dsn)
        if insecure_local_test and parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("insecure test transport requires loopback")
        self._sslmode = "disable" if insecure_local_test else "require"
        self._dsn = dsn
        self.scope = scope
        with self._connect() as db:
            db.execute(
                "insert into ofc_private.provider_scope (id,scope) values(1,%s) "
                "on conflict (id) do nothing", (scope,)
            )
            row = db.execute(
                "select scope from ofc_private.provider_scope where id=1"
            ).fetchone()
            if row is None or row[0] != scope:
                raise ValueError("provider budget belongs to another plan")
            row = db.execute(
                "select scope from ofc_private.provider_attempt where id=1"
            ).fetchone()
            if row is not None and row[0] != scope:
                raise ValueError("provider reservation scope conflict")

    def _connect(self):
        return psycopg.connect(self._dsn, connect_timeout=5, sslmode=self._sslmode)

    def get(self) -> tuple[str, dict | None]:
        with self._connect() as db:
            row = db.execute(
                "select scope,state,result from ofc_private.provider_attempt where id=1"
            ).fetchone()
        if row is None:
            return "READY", None
        if row[0] != self.scope:
            raise ValueError("reservation scope mismatch")
        if row[1] == "RESERVED":
            return "SPENT", None
        try:
            record = json.loads(row[2])
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid stored provider outcome") from exc
        if (
            not isinstance(record, dict)
            or record.get("status") not in ("LIVE_RANK_OBSERVED", "BLOCKED")
            or record.get("release_authorized") is not False
            or record.get("external_action_executed") is not False
        ):
            raise ValueError("unsafe persisted provider outcome")
        return record["status"], record

    def reserve(self) -> bool:
        with self._connect() as db:
            scope = db.execute(
                "select scope from ofc_private.provider_scope where id=1 for update"
            ).fetchone()
            if scope is None or scope[0] != self.scope:
                raise ValueError("provider budget mismatch")
            row = db.execute(
                "insert into ofc_private.provider_attempt "
                "(id,scope,state,result) values(1,%s,'RESERVED',null) "
                "on conflict (id) do nothing returning id", (self.scope,)
            ).fetchone()
            return row is not None

    def complete(self, record: dict) -> None:
        if (
            not isinstance(record, dict)
            or record.get("status") not in ("LIVE_RANK_OBSERVED", "BLOCKED")
            or record.get("release_authorized") is not False
            or record.get("external_action_executed") is not False
        ):
            raise ValueError("unsafe provider outcome")
        payload = json.dumps(
            record, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        with self._connect() as db:
            result = db.execute(
                "update ofc_private.provider_attempt set state='COMPLETE',result=%s "
                "where id=1 and scope=%s and state='RESERVED'",
                (payload, self.scope),
            )
            if result.rowcount != 1:
                raise ValueError("provider reservation already finalized")
