"""SQLite revision persistence with transactional CAS and idempotent replay."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import sqlite3
from typing import Iterable

from .contracts import PlanSnapshot, RepairPatch
from .revisions import RevisionConflict, apply_patch, digest_obj, plan_digest
from .store import IdempotencyConflict

_ZERO = "0" * 64


class StorageIntegrityError(RuntimeError):
    """Stored state or journal failed internal consistency validation."""


@dataclass(frozen=True)
class RevisionEvent:
    sequence: int
    plan_id: str
    request_id: str | None
    kind: str
    from_revision: int | None
    to_revision: int
    before_digest: str | None
    after_digest: str
    invalidated_ids: tuple[str, ...]
    previous_hash: str
    event_hash: str


class SQLiteRevisionStore:
    """Local SQLite store with cross-connection, transactional writes.

    Keep the database and WAL on persistent local storage. This store does not
    provide distributed consensus or authorize actions.
    """

    def __init__(self, db_path: str | Path, *, timeout_seconds: float = 10.0) -> None:
        raw = str(db_path)
        if not raw or raw == ":memory:" or raw.startswith("file:"):
            raise ValueError("a filesystem SQLite database path is required")
        if not isinstance(timeout_seconds, (float, int)) or isinstance(timeout_seconds, bool) or not (0 < timeout_seconds <= 120):
            raise ValueError("timeout_seconds must be between 0 and 120")
        path = Path(raw).expanduser()
        if not path.parent.is_dir():
            raise FileNotFoundError("database parent directory must already exist")
        self._path = path
        self._timeout = float(timeout_seconds)
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    journal_head TEXT NOT NULL,
                    pending_rechecks TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    plan_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    patch_digest TEXT NOT NULL,
                    response_snapshot TEXT NOT NULL,
                    invalidated TEXT NOT NULL,
                    PRIMARY KEY(plan_id, request_id),
                    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    plan_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    PRIMARY KEY(plan_id, sequence),
                    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
                );
            """)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=self._timeout, isolation_level=None)
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)}")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA journal_mode = WAL")
            return conn
        except BaseException:
            conn.close()
            raise

    @staticmethod
    def _decode(snapshot: str, revision: int, stored_digest: str) -> PlanSnapshot:
        try:
            plan = PlanSnapshot.model_validate_json(snapshot)
        except (ValueError, TypeError) as exc:
            raise StorageIntegrityError("invalid stored plan snapshot") from exc
        if plan.revision != revision or plan_digest(plan) != stored_digest:
            raise StorageIntegrityError("stored plan revision or digest mismatch")
        return plan

    @staticmethod
    def _event_payload(
        plan_id: str, sequence: int, kind: str, request_id: str | None,
        from_revision: int | None, to_revision: int,
        before_digest: str | None, after_digest: str,
        invalidated_ids: Iterable[str],
    ) -> dict:
        return {
            "plan_id": plan_id, "sequence": sequence, "kind": kind,
            "request_id": request_id, "from_revision": from_revision,
            "to_revision": to_revision, "before_digest": before_digest,
            "after_digest": after_digest, "invalidated_ids": list(invalidated_ids),
        }

    @staticmethod
    def _append_event(conn: sqlite3.Connection, payload: dict, previous_hash: str) -> str:
        next_hash = digest_obj({"previous_hash": previous_hash, "event": payload})
        conn.execute(
            "INSERT INTO events(plan_id,sequence,event_payload,previous_hash,event_hash) VALUES (?,?,?,?,?)",
            (payload["plan_id"], payload["sequence"], json.dumps(payload, sort_keys=True), previous_hash, next_hash),
        )
        return next_hash

    def insert(self, plan: PlanSnapshot) -> None:
        """Insert a validated plan; reject duplicate plan IDs."""
        plan = PlanSnapshot.model_validate(plan.model_dump())
        if plan.revision != 0:
            raise ValueError("initial stored plan must be revision zero")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM plans WHERE plan_id=?", (plan.plan_id,)).fetchone():
                raise RevisionConflict("plan already exists")
            digest = plan_digest(plan)
            payload = self._event_payload(plan.plan_id, 0, "INSERT", None, None, 0, None, digest, ())
            conn.execute(
                "INSERT INTO plans(plan_id,revision,digest,snapshot,journal_head,pending_rechecks) VALUES (?,?,?,?,?,?)",
                (plan.plan_id, 0, digest, plan.model_dump_json(), _ZERO, "[]"),
            )
            head = self._append_event(conn, payload, _ZERO)
            conn.execute("UPDATE plans SET journal_head=? WHERE plan_id=?", (head, plan.plan_id))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get(self, plan_id: str) -> PlanSnapshot:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT snapshot,revision,digest FROM plans WHERE plan_id=?", (plan_id,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown plan")
            return self._decode(*row)
        finally:
            conn.close()

    def apply_patch_once(
        self, plan_id: str, patch: RepairPatch, *, client_request_id: str,
    ) -> tuple[PlanSnapshot, tuple[str, ...]]:
        """Apply a patch once per request ID; identical retries return the saved result."""
        if not isinstance(client_request_id, str) or not client_request_id.strip():
            raise ValueError("client_request_id is required")
        patch = RepairPatch.model_validate(patch.model_dump())
        fingerprint = digest_obj(patch.model_dump(mode="json"))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            saved = conn.execute(
                "SELECT patch_digest,response_snapshot,invalidated FROM requests WHERE plan_id=? AND request_id=?",
                (plan_id, client_request_id),
            ).fetchone()
            if saved is not None:
                if saved[0] != fingerprint:
                    raise IdempotencyConflict("same request key with different patch")
                replay = PlanSnapshot.model_validate_json(saved[1])
                # Validate replay against its recorded journal event.
                if replay.plan_id != plan_id or replay.revision != patch.base_revision + 1:
                    raise StorageIntegrityError("invalid idempotent response snapshot")
                invalidated = tuple(json.loads(saved[2]))
                event_row = conn.execute(
                    "SELECT event_payload,previous_hash,event_hash FROM events WHERE plan_id=? AND sequence=?",
                    (plan_id, replay.revision),
                ).fetchone()
                if event_row is None:
                    raise StorageIntegrityError("idempotent replay has no journal event")
                raw_event, prior, event_hash = event_row
                try:
                    event = json.loads(raw_event)
                except (TypeError, ValueError) as exc:
                    raise StorageIntegrityError("invalid idempotent replay journal") from exc
                if (
                    event_hash != digest_obj({"previous_hash": prior, "event": event})
                    or event.get("request_id") != client_request_id
                    or event.get("after_digest") != plan_digest(replay)
                    or tuple(event.get("invalidated_ids", ())) != invalidated
                ):
                    raise StorageIntegrityError("idempotent replay journal mismatch")
                conn.execute("COMMIT")
                return replay, invalidated
            row = conn.execute(
                "SELECT snapshot,revision,digest,journal_head,pending_rechecks FROM plans WHERE plan_id=?", (plan_id,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown plan")
            snapshot, revision, digest, head, pending_raw = row
            try:
                pending_prior = tuple(json.loads(pending_raw))
            except (TypeError, ValueError) as exc:
                raise StorageIntegrityError("invalid pending recheck list") from exc
            current = self._decode(snapshot, revision, digest)
            updated, invalidated = apply_patch(current, patch)
            next_digest = plan_digest(updated)
            seq = updated.revision
            payload = self._event_payload(
                plan_id, seq, "PATCH", client_request_id, current.revision,
                updated.revision, digest, next_digest, invalidated,
            )
            next_hash = self._append_event(conn, payload, head)
            outstanding = set(pending_prior) | set(invalidated)
            pending_next = tuple(d.decision_id for d in updated.decisions if d.decision_id in outstanding)
            result = conn.execute(
                "UPDATE plans SET snapshot=?,revision=?,digest=?,journal_head=?,pending_rechecks=? WHERE plan_id=? AND revision=? AND digest=?",
                (updated.model_dump_json(), updated.revision, next_digest, next_hash, json.dumps(pending_next),
                 plan_id, current.revision, digest),
            )
            if result.rowcount != 1:
                raise RevisionConflict("concurrent plan modification")
            conn.execute(
                "INSERT INTO requests(plan_id,request_id,patch_digest,response_snapshot,invalidated) VALUES (?,?,?,?,?)",
                (plan_id, client_request_id, fingerprint, updated.model_dump_json(), json.dumps(invalidated)),
            )
            conn.execute("COMMIT")
            return updated, invalidated
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def pending_rechecks(self, plan_id: str) -> tuple[str, ...]:
        """Return unresolved rechecks; this store cannot approve or clear them."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT pending_rechecks,snapshot,revision,digest FROM plans WHERE plan_id=?", (plan_id,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown plan")
            pending_json, snapshot, revision, digest = row
            plan = self._decode(snapshot, revision, digest)
            try:
                values = json.loads(pending_json)
            except (TypeError, ValueError) as exc:
                raise StorageIntegrityError("invalid pending recheck list") from exc
            if (
                not isinstance(values, list)
                or any(not isinstance(x, str) for x in values)
                or len(values) != len(set(values))
                or not set(values).issubset(d.decision_id for d in plan.decisions)
            ):
                raise StorageIntegrityError("invalid pending recheck IDs")
            return tuple(values)
        finally:
            conn.close()

    def audit_history(self, plan_id: str) -> tuple[RevisionEvent, ...]:
        """Verify stored snapshot consistency and the hash-linked event journal."""
        conn = self._connect()
        try:
            state = conn.execute(
                "SELECT snapshot,revision,digest,journal_head FROM plans WHERE plan_id=?", (plan_id,),
            ).fetchone()
            if state is None:
                raise KeyError("unknown plan")
            snapshot, revision, digest, latest_hash = state
            self._decode(snapshot, revision, digest)
            rows = conn.execute(
                "SELECT sequence,event_payload,previous_hash,event_hash FROM events WHERE plan_id=? ORDER BY sequence",
                (plan_id,),
            ).fetchall()
            previous_hash = _ZERO
            events: list[RevisionEvent] = []
            for expected_seq, (seq, raw, prior, event_hash) in enumerate(rows):
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError) as exc:
                    raise StorageIntegrityError("invalid journal entry") from exc
                if (
                    seq != expected_seq or payload.get("sequence") != seq or
                    payload.get("plan_id") != plan_id or prior != previous_hash or
                    event_hash != digest_obj({"previous_hash": prior, "event": payload})
                ):
                    raise StorageIntegrityError("journal chain mismatch")
                if (seq == 0 and (payload.get("kind") != "INSERT" or payload.get("to_revision") != 0)) or (
                    seq > 0 and (
                        payload.get("kind") != "PATCH" or
                        payload.get("from_revision") != seq - 1 or
                        payload.get("to_revision") != seq or
                        payload.get("before_digest") != events[-1].after_digest
                    )
                ):
                    raise StorageIntegrityError("journal revision linkage mismatch")
                events.append(RevisionEvent(
                    sequence=seq, plan_id=plan_id, request_id=payload.get("request_id"),
                    kind=payload["kind"], from_revision=payload.get("from_revision"),
                    to_revision=payload["to_revision"], before_digest=payload.get("before_digest"),
                    after_digest=payload["after_digest"],
                    invalidated_ids=tuple(payload.get("invalidated_ids", ())),
                    previous_hash=prior, event_hash=event_hash,
                ))
                previous_hash = event_hash
            if (
                not events or len(events) != revision + 1 or
                events[-1].after_digest != digest or latest_hash != previous_hash
            ):
                raise StorageIntegrityError("journal head does not match current plan")
            return tuple(events)
        finally:
            conn.close()
