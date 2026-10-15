"""Durable PostgreSQL implementation of the revision journal contract."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import psycopg

from .contracts import PlanSnapshot, RepairPatch
from .durable import SQLiteRevisionStore, RevisionEvent, StorageIntegrityError, _ZERO
from .revisions import RevisionConflict, apply_patch, digest_obj, plan_digest
from .store import IdempotencyConflict


class PostgresRevisionStore:
    def __init__(self, dsn: str, *, insecure_local_test: bool = False):
        if not isinstance(dsn, str) or not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("a PostgreSQL connection URL is required")
        parsed = urlsplit(dsn)
        if insecure_local_test and parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("insecure test transport requires loopback")
        self._sslmode = "disable" if insecure_local_test else "require"
        self._dsn = dsn
        with self._connect() as conn:
            conn.execute("select 1 from ofc_private.plans limit 1")

    def _connect(self):
        return psycopg.connect(self._dsn, connect_timeout=5, sslmode=self._sslmode)

    def _history(self, db, plan_id: str) -> tuple[RevisionEvent, ...]:
        row = db.execute(
            "select snapshot, revision, digest, journal_head from ofc_private.plans "
            "where plan_id = %s", (plan_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown plan")
        snapshot, revision, digest, head = row
        SQLiteRevisionStore._decode(snapshot, revision, digest)
        rows = db.execute(
            "select sequence,event_payload,previous_hash,event_hash "
            "from ofc_private.events where plan_id=%s order by sequence",
            (plan_id,),
        ).fetchall()
        prev = _ZERO
        history = []
        for seq, (position, raw, preceding, event_hash) in enumerate(rows):
            try:
                event = json.loads(raw)
            except (ValueError, TypeError) as exc:
                raise StorageIntegrityError("invalid journal event") from exc
            if (
                position != seq or event.get("plan_id") != plan_id
                or event.get("sequence") != seq or preceding != prev
                or digest_obj({"previous_hash": prev, "event": event}) != event_hash
                or event.get("after_digest") is None
            ):
                raise StorageIntegrityError("journal chain mismatch")
            if seq == 0:
                if (
                    event.get("kind") != "INSERT" or event.get("to_revision") != 0
                    or event.get("before_digest") is not None
                ):
                    raise StorageIntegrityError("invalid initial event")
            elif (
                event.get("kind") != "PATCH"
                or event.get("from_revision") != seq - 1
                or event.get("to_revision") != seq
                or event.get("before_digest") != history[-1].after_digest
            ):
                raise StorageIntegrityError("journal revision linkage mismatch")
            history.append(RevisionEvent(
                sequence=seq, plan_id=plan_id,
                request_id=event.get("request_id"), kind=event["kind"],
                from_revision=event.get("from_revision"),
                to_revision=event["to_revision"],
                before_digest=event.get("before_digest"),
                after_digest=event["after_digest"],
                invalidated_ids=tuple(event.get("invalidated_ids", ())),
                previous_hash=preceding, event_hash=event_hash,
            ))
            prev = event_hash
        if (
            len(history) != revision + 1 or not history
            or history[-1].after_digest != digest or prev != head
        ):
            raise StorageIntegrityError("journal head mismatch")
        return tuple(history)

    @staticmethod
    def _append(db, payload: dict, previous: str) -> str:
        latest = digest_obj({"previous_hash": previous, "event": payload})
        db.execute(
            "insert into ofc_private.events "
            "(plan_id,sequence,event_payload,previous_hash,event_hash) "
            "values (%s,%s,%s,%s,%s)",
            (payload["plan_id"], payload["sequence"],
             json.dumps(payload, sort_keys=True), previous, latest),
        )
        return latest

    def insert(self, plan: PlanSnapshot) -> None:
        with self._connect() as db:
            self.insert_in_transaction(db, plan)

    def insert_in_transaction(self, db, plan: PlanSnapshot) -> None:
        plan = PlanSnapshot.model_validate(plan.model_dump())
        if plan.revision != 0:
            raise ValueError("initial plan must be revision zero")
        digest = plan_digest(plan)

        row = db.execute(
            "insert into ofc_private.plans "
            "(plan_id,revision,digest,snapshot,journal_head,pending_rechecks) "
            "values (%s,0,%s,%s,%s,'[]') "
            "on conflict (plan_id) do nothing returning plan_id",
            (plan.plan_id, digest, plan.model_dump_json(), _ZERO),
        ).fetchone()
        if row is None:
            raise RevisionConflict("plan already exists")
        entry = SQLiteRevisionStore._event_payload(
            plan.plan_id, 0, "INSERT", None, None, 0, None, digest, ()
        )
        head = self._append(db, entry, _ZERO)
        db.execute(
            "update ofc_private.plans set journal_head=%s where plan_id=%s",
            (head, plan.plan_id),
        )

    def get(self, plan_id: str) -> PlanSnapshot:
        with self._connect() as db:
            row = db.execute(
                "select snapshot,revision,digest from ofc_private.plans "
                "where plan_id=%s", (plan_id,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown plan")
            return SQLiteRevisionStore._decode(*row)

    def pending_rechecks(self, plan_id: str) -> tuple[str, ...]:
        with self._connect() as db:
            row = db.execute(
                "select pending_rechecks,snapshot,revision,digest "
                "from ofc_private.plans where plan_id=%s", (plan_id,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown plan")
            pending, snapshot, revision, digest = row
            return SQLiteRevisionStore._decode_pending(
                pending, SQLiteRevisionStore._decode(snapshot, revision, digest)
            )

    def audit_history(self, plan_id: str) -> tuple[RevisionEvent, ...]:
        with self._connect() as db:
            return self._history(db, plan_id)

    def apply_patch_once(
        self, plan_id: str, patch: RepairPatch, *, client_request_id: str
    ) -> tuple[PlanSnapshot, tuple[str, ...]]:
        if not isinstance(client_request_id, str) or not client_request_id.strip():
            raise ValueError("client_request_id required")
        patch = RepairPatch.model_validate(patch.model_dump())
        fingerprint = digest_obj(patch.model_dump(mode="json"))
        with self._connect() as db:
            # Row lock spans idempotency lookup and revision CAS.
            row = db.execute(
                "select snapshot,revision,digest,journal_head,pending_rechecks "
                "from ofc_private.plans where plan_id=%s for update", (plan_id,)
            ).fetchone()
            if row is None:
                raise KeyError("unknown plan")
            history = self._history(db, plan_id)
            saved = db.execute(
                "select patch_digest,response_snapshot,invalidated from "
                "ofc_private.requests where plan_id=%s and request_id=%s",
                (plan_id, client_request_id),
            ).fetchone()
            if saved is not None:
                if saved[0] != fingerprint:
                    raise IdempotencyConflict("request ID reused for another patch")
                replay = PlanSnapshot.model_validate_json(saved[1])
                invalidated = tuple(json.loads(saved[2]))
                event = next((e for e in history if e.sequence == replay.revision), None)
                if (
                    replay.plan_id != plan_id
                    or replay.revision != patch.base_revision + 1
                    or event is None or event.request_id != client_request_id
                    or event.after_digest != plan_digest(replay)
                    or event.invalidated_ids != invalidated
                ):
                    raise StorageIntegrityError("idempotent replay journal mismatch")
                return replay, invalidated

            snapshot, revision, digest, head, pending = row
            current = SQLiteRevisionStore._decode(snapshot, revision, digest)
            previous_pending = SQLiteRevisionStore._decode_pending(pending, current)
            updated, invalidated = apply_patch(current, patch)
            next_digest = plan_digest(updated)
            entry = SQLiteRevisionStore._event_payload(
                plan_id, updated.revision, "PATCH", client_request_id,
                current.revision, updated.revision, digest, next_digest, invalidated,
            )
            next_head = self._append(db, entry, head)
            outstanding = set(previous_pending) | set(invalidated)
            new_pending = tuple(
                d.decision_id for d in updated.decisions
                if d.decision_id in outstanding
            )
            result = db.execute(
                "update ofc_private.plans set snapshot=%s,revision=%s,digest=%s,"
                "journal_head=%s,pending_rechecks=%s where plan_id=%s "
                "and revision=%s and digest=%s",
                (updated.model_dump_json(), updated.revision, next_digest,
                 next_head, json.dumps(new_pending),
                 plan_id, current.revision, digest),
            )
            if result.rowcount != 1:
                raise RevisionConflict("concurrent revision")
            db.execute(
                "insert into ofc_private.requests "
                "(plan_id,request_id,patch_digest,response_snapshot,invalidated) "
                "values (%s,%s,%s,%s,%s)",
                (plan_id, client_request_id, fingerprint,
                 updated.model_dump_json(), json.dumps(invalidated)),
            )
            return updated, invalidated
