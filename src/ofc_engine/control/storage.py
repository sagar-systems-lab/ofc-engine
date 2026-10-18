from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import secrets
from datetime import datetime, timezone
import sqlite3
import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
import psycopg

from .canonical import canonical_bytes, digest, strict_json
from .models import ReviewRequest
from .policies import POLICY_DIGEST
from .verifier import verify

TERMINAL = frozenset({"FINISHED", "FAILED", "CANCELLED_SAFE", "RECOVERY_REQUIRED"})
TRANSITIONS = {
    "RESERVED": {"FROZEN"},
    "FROZEN": {"EVALUATING"},
    "EVALUATING": {"SEARCHING", "REPAIR_PROPOSED", "HANDOFF_READY"},
    "SEARCHING": {"REPAIR_PROPOSED", "HANDOFF_READY"},
    "REPAIR_PROPOSED": {"RECHECKING"},
    "RECHECKING": {"HANDOFF_READY"},
}


class Conflict(ValueError):
    pass


class NotFound(ValueError):
    pass


class CapacityReached(ValueError):
    pass


class IntegrityError(ValueError):
    pass


class ControlStore:
    def __init__(
        self,
        location: str | Path,
        *,
        encryption_key: str | bytes | None = None,
        initialize: bool = False,
        clock=time.time,
    ):
        self.collection_keys = {}
        self.delegations = ()
        self.tenant_weights = {}
        self.location, self.clock = str(location), clock
        self.postgres = self.location.startswith(("postgresql://", "postgres://"))
        self.prefix = "ofc_control." if self.postgres else "control_"
        self.cipher = Fernet(encryption_key) if encryption_key else None
        if self.postgres:
            if initialize:
                self.migrate()
            with self._tx() as db:
                db.execute(
                    f"select review_id,event_count,event_head,policy_digest from {self.prefix}reviews limit 0"
                )
                db.execute(f"select intent_id from {self.prefix}intents limit 0")
        else:
            if self.location == ":memory:":
                raise ValueError("durable SQLite path required")
            Path(self.location).parent.mkdir(parents=True, exist_ok=True)
            self.migrate()

    @contextmanager
    def _tx(self, *, write=False):
        if self.postgres:
            local = urlsplit(self.location).hostname in {"127.0.0.1", "localhost"}
            with psycopg.connect(
                self.location,
                connect_timeout=5,
                sslmode="disable" if local else "require",
            ) as conn:
                yield conn
        else:
            conn = sqlite3.connect(self.location, timeout=5)
            try:
                conn.execute("pragma foreign_keys=on")
                conn.execute("begin immediate" if write else "begin")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _sql(self, query):
        return query.replace("?", "%s") if self.postgres else query

    def _run(self, db, query, args=()):
        return db.execute(self._sql(query), args)

    def migrate(self):
        with self._tx(write=True) as db:
            if self.postgres:
                db.execute("create schema if not exists ofc_control")
            db.execute(f"""create table if not exists {self.prefix}reviews (
                review_id text primary key, tenant_id text not null, actor_id text not null,
                request_key text not null, input_digest text not null,
                request_digest text not null, request_payload text not null,
                state text not null, version integer not null, revision integer not null,
                current_digest text not null, policy_digest text not null,
                lease_owner text, lease_until bigint, created_at bigint not null,
                result_digest text, result_payload text,
                event_count integer not null default 0,
                event_head text not null default '{"0" * 64}',
                unique(tenant_id,actor_id,request_key))""")
            db.execute(f"""create table if not exists {self.prefix}events (
                review_id text not null references {self.prefix}reviews(review_id),
                sequence integer not null, kind text not null, payload text not null,
                previous_hash text not null, event_hash text not null,
                primary key(review_id,sequence))""")
            db.execute(f"""create table if not exists {self.prefix}capacity (
                id integer primary key check(id=1))""")
            db.execute(
                f"insert into {self.prefix}capacity(id) values(1) on conflict(id) do nothing"
            )
            db.execute(
                f"create index if not exists control_review_queue on {self.prefix}reviews(state,created_at)"
            )
            db.execute(f"""create table if not exists {self.prefix}intents (
                intent_id text primary key, review_id text not null references {self.prefix}reviews(review_id),
                request_key text not null, tenant_id text not null, actor_id text not null,
                state text not null, payload text not null, digest text not null,
                unique(review_id,request_key))""")
            db.execute(f"""create table if not exists {self.prefix}schema_versions (
                version integer primary key, installed_at bigint not null)""")
            db.execute(
                f"insert into {self.prefix}schema_versions values(2,?) on conflict(version) do nothing".replace(
                    "?", "%s"
                )
                if self.postgres
                else f"insert into {self.prefix}schema_versions values(2,?) on conflict(version) do nothing",
                (int(self.clock()),),
            )
            db.execute(f"""create table if not exists {self.prefix}revisions (
                review_id text not null references {self.prefix}reviews(review_id), round integer not null,
                parent_digest text not null, current_digest text not null, evidence_root text not null,
                policy_digest text not null, payload text not null, payload_digest text not null,
                primary key(review_id,round))""")
            db.execute(f"""create table if not exists {self.prefix}attempts (
                scope_id text primary key, tenant_id text not null, provider text not null,
                account_scope text not null, input_digest text not null, state text not null,
                created_at bigint not null, payload text, payload_digest text, expires_at bigint not null)""")
            db.execute(f"""create table if not exists {self.prefix}retention (
                review_id text primary key references {self.prefix}reviews(review_id),
                tenant_id text not null, expires_at bigint not null, legal_hold integer not null default 0)""")
            db.execute(f"""create table if not exists {self.prefix}tombstones (
                review_id text primary key, tenant_id text not null, actor_id text not null,
                request_key text not null, input_digest text not null, event_head text not null,
                deleted_at bigint not null, policy_version text not null)""")
            db.execute(
                f"""create table if not exists {self.prefix}qos (tenant_id text primary key, served integer not null)"""
            )
            db.execute(
                f"""create table if not exists {self.prefix}traces (review_id text primary key references {self.prefix}reviews(review_id), payload text not null, payload_digest text not null)"""
            )
            db.execute(
                f"create index if not exists control_retention_expiry on {self.prefix}retention(tenant_id,expires_at)"
            )
            from .collection import migrate_tables

            migrate_tables(self, db)
            self._run(
                db,
                f"insert into {self.prefix}schema_versions values(3,?) on conflict(version) do nothing",
                (int(self.clock()),),
            )
            if self.postgres:
                for table in (
                    "reviews",
                    "events",
                    "intents",
                    "capacity",
                    "schema_versions",
                    "revisions",
                    "attempts",
                    "retention",
                    "tombstones",
                    "qos",
                    "traces",
                ):
                    db.execute(
                        f"alter table {self.prefix}{table} enable row level security"
                    )
                db.execute("revoke all on schema ofc_control from public")
                db.execute("revoke all on all tables in schema ofc_control from public")

    def _encode(self, value, *, private):
        raw = canonical_bytes(value)
        if self.cipher:
            return "fernet:" + self.cipher.encrypt(raw).decode()
        if private:
            raise ValueError("private storage requires an encryption key")
        return "json:" + raw.decode()

    def _decode(self, raw, expected_digest):
        try:
            if raw.startswith("fernet:") and self.cipher:
                value = strict_json(self.cipher.decrypt(raw[7:].encode()))
            elif raw.startswith("json:"):
                value = strict_json(raw[5:])
            else:
                raise IntegrityError("storage codec unavailable")
            if digest(value) != expected_digest:
                raise IntegrityError("stored payload digest mismatch")
            return value
        except (InvalidToken, ValueError, TypeError) as exc:
            raise IntegrityError("stored payload integrity failure") from exc

    def _append(self, db, review_id, kind, payload):
        suffix = " for update" if self.postgres else ""
        anchor = self._run(
            db,
            f"select event_count,event_head from {self.prefix}reviews where review_id=?"
            + suffix,
            (review_id,),
        ).fetchone()
        last = self._run(
            db,
            f"select sequence,event_hash from {self.prefix}events where review_id=? order by sequence desc limit 1",
            (review_id,),
        ).fetchone()
        sequence, previous = (last[0] + 1, last[1]) if last else (1, "0" * 64)
        if anchor is None or anchor[0] != sequence - 1 or anchor[1] != previous:
            raise IntegrityError("journal anchor mismatch")
        fields = {
            "review_id": review_id,
            "sequence": sequence,
            "kind": kind,
            "payload": payload,
            "previous": previous,
        }
        self._run(
            db,
            f"insert into {self.prefix}events values(?,?,?,?,?,?)",
            (
                review_id,
                sequence,
                kind,
                canonical_bytes(payload).decode(),
                previous,
                digest(fields),
            ),
        )
        self._run(
            db,
            f"update {self.prefix}reviews set event_count=?,event_head=? where review_id=?",
            (sequence, digest(fields), review_id),
        )

    def admit(self, request: ReviewRequest, *, max_pending=128, max_total=4096) -> dict:
        original = request.data()
        original_digest = digest(original)
        plan = request.plan
        private = request.mode == "PRIVATE"
        if private and request.as_of is not None:
            raise ValueError("private review clock is server controlled")
        frozen = request.data()
        frozen["as_of"] = (
            datetime.fromtimestamp(self.clock(), timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
            if private or request.as_of is None
            else request.as_of
        )
        packed = {"input": original, "frozen": frozen}
        encoded = self._encode(packed, private=private)
        with self._tx(write=True) as db:
            if self.postgres:
                db.execute(
                    f"select id from {self.prefix}capacity where id=1 for update"
                )
            row = self._run(
                db,
                f"select review_id,input_digest from {self.prefix}reviews where tenant_id=? and actor_id=? and request_key=?",
                (plan.tenant_id, plan.actor.id, plan.idempotency_key),
            ).fetchone()
            if row:
                if row[1] != original_digest:
                    raise Conflict("idempotency key already binds a different input")
                return self._receipt(row[0], plan.digest)
            erased = self._run(
                db,
                f"select input_digest from {self.prefix}tombstones where tenant_id=? and actor_id=? and request_key=?",
                (plan.tenant_id, plan.actor.id, plan.idempotency_key),
            ).fetchone()
            if erased:
                raise Conflict("request retained as an audit tombstone")
            total = self._run(
                db, f"select count(*) from {self.prefix}reviews"
            ).fetchone()[0]
            pending = self._run(
                db,
                f"select count(*) from {self.prefix}reviews where tenant_id=? and state not in ('FINISHED','FAILED','CANCELLED_SAFE')",
                (plan.tenant_id,),
            ).fetchone()[0]
            if total >= max_total or pending >= max_pending:
                raise CapacityReached("review capacity exhausted")
            review_id = "pr-" + secrets.token_hex(16)
            self._run(
                db,
                f"insert into {self.prefix}reviews (review_id,tenant_id,actor_id,request_key,input_digest,request_digest,request_payload,state,version,revision,current_digest,policy_digest,created_at) values(?,?,?,?,?,?,?,'ACCEPTED',0,0,?,?,?)",
                (
                    review_id,
                    plan.tenant_id,
                    plan.actor.id,
                    plan.idempotency_key,
                    original_digest,
                    digest(packed),
                    encoded,
                    plan.digest,
                    POLICY_DIGEST,
                    int(self.clock()),
                ),
            )
            self._run(
                db,
                f"insert into {self.prefix}retention values(?,?,?,0)",
                (review_id, plan.tenant_id, int(self.clock()) + 30 * 86400),
            )
            self._append(
                db,
                review_id,
                "ACCEPTED",
                {
                    "plan_digest": plan.digest,
                    "policy_digest": POLICY_DIGEST,
                    "reserved_provider_calls": 0,
                    "reserved_spend_minor": 0,
                },
            )
        return self._receipt(review_id, plan.digest)

    @staticmethod
    def _receipt(review_id, plan_digest):
        return {
            "review_id": review_id,
            "correlation_id": review_id,
            "plan_digest": plan_digest,
            "status_url": f"/api/control/reviews/{review_id}",
            "events_url": f"/api/control/reviews/{review_id}/events",
            "proof_url": f"/api/control/reviews/{review_id}/proof",
            "provider_calls": 0,
            "spend_minor": 0,
            "external_action_executed": False,
        }

    def snapshot(self, review_id, tenant_id, actor_id) -> dict:
        with self._tx() as db:
            row = self._run(
                db,
                f"select state,version,revision,current_digest,policy_digest,result_digest,result_payload from {self.prefix}reviews where review_id=? and tenant_id=? and actor_id=?",
                (review_id, tenant_id, actor_id),
            ).fetchone()
        if row is None:
            raise NotFound("review unavailable")
        proof_digest = self._decode(row[6], row[5])["proof_digest"] if row[6] else None
        return {
            "review_id": review_id,
            "state": row[0],
            "version": row[1],
            "revision": row[2],
            "current_digest": row[3],
            "policy_digest": row[4],
            "proof_digest": proof_digest,
            "action_status": "WITHHELD",
            "external_action_executed": False,
            "provider_calls": 0,
            "spend_minor": 0,
        }

    def events(self, review_id, tenant_id, actor_id, *, after=0):
        with self._tx() as db:
            suffix = " for share" if self.postgres else ""
            anchor = self._run(
                db,
                f"select event_count,event_head from {self.prefix}reviews where review_id=? and tenant_id=? and actor_id=?"
                + suffix,
                (review_id, tenant_id, actor_id),
            ).fetchone()
            if anchor is None:
                raise NotFound("review unavailable")
            rows = self._run(
                db,
                f"select sequence,kind,payload,previous_hash,event_hash from {self.prefix}events where review_id=? order by sequence",
                (review_id,),
            ).fetchall()
        previous, events = "0" * 64, []
        for sequence, kind, raw, parent, event_hash in rows:
            item = {
                "review_id": review_id,
                "sequence": sequence,
                "kind": kind,
                "payload": strict_json(raw),
                "previous": parent,
            }
            if (
                sequence != len(events) + 1
                or parent != previous
                or digest(item) != event_hash
            ):
                raise IntegrityError("review event chain mismatch")
            events.append(item | {"hash": event_hash})
            previous = event_hash
        if len(events) != anchor[0] or previous != anchor[1]:
            raise IntegrityError("review journal anchor mismatch")
        return [e for e in events if e["sequence"] > after]

    def request(self, review_id, tenant_id, actor_id):
        with self._tx() as db:
            row = self._run(
                db,
                f"select request_payload,request_digest from {self.prefix}reviews where review_id=? and tenant_id=? and actor_id=?",
                (review_id, tenant_id, actor_id),
            ).fetchone()
        if row is None:
            raise NotFound("review unavailable")
        return ReviewRequest.parse(self._decode(row[0], row[1])["frozen"])

    def proof(self, review_id, tenant_id, actor_id, *, authorities=None):
        self.events(review_id, tenant_id, actor_id)
        with self._tx() as db:
            row = self._run(
                db,
                f"select result_payload,result_digest,current_digest,revision from {self.prefix}reviews where review_id=? and tenant_id=? and actor_id=?",
                (review_id, tenant_id, actor_id),
            ).fetchone()
        if row is None:
            raise NotFound("review unavailable")
        if row[0] is None:
            raise Conflict("proof is not committed")
        value = self._decode(row[0], row[1])
        if value["current_digest"] != row[2] or value["revision"] != row[3]:
            raise IntegrityError("review revision mismatch")
        verify(
            value,
            trusted_authorities=authorities,
            trusted_delegations=self.delegations,
            trusted_collection_keys=self.collection_keys,
        )
        return value

    def claim(self, owner: str, *, lease_seconds=30):
        if not owner or not 1 <= lease_seconds <= 300:
            raise ValueError("invalid worker lease")
        with self._tx(write=True) as db:
            suffix = " for update skip locked" if self.postgres else ""
            if self.postgres:
                db.execute(
                    f"select id from {self.prefix}capacity where id=1 for update"
                )
            tenants = self._run(
                db,
                f"select distinct r.tenant_id,coalesce(q.served,0) from {self.prefix}reviews r left join {self.prefix}qos q on q.tenant_id=r.tenant_id where r.state='ACCEPTED'",
            ).fetchall()
            if not tenants:
                return None
            tenant = min(
                tenants, key=lambda t: (t[1] / self.tenant_weights.get(t[0], 1), t[0])
            )[0]
            row = self._run(
                db,
                f"select review_id,tenant_id,actor_id,version from {self.prefix}reviews where state='ACCEPTED' and tenant_id=? order by created_at,review_id limit 1"
                + suffix,
                (tenant,),
            ).fetchone()
            if row is None:
                return None
            self._run(
                db,
                f"update {self.prefix}reviews set state='RESERVED',version=version+1,lease_owner=?,lease_until=? where review_id=? and version=?",
                (owner, int(self.clock()) + lease_seconds, row[0], row[3]),
            )
            self._run(
                db,
                f"insert into {self.prefix}qos values(?,1) on conflict(tenant_id) do update set served={self.prefix}qos.served+1"
                if self.postgres
                else f"insert into {self.prefix}qos values(?,1) on conflict(tenant_id) do update set served=served+1",
                (tenant,),
            )
            self._append(db, row[0], "RESERVED", {"provider_calls": 0})
        return {
            "review_id": row[0],
            "tenant_id": row[1],
            "actor_id": row[2],
            "version": row[3] + 1,
            "owner": owner,
        }

    def advance(self, job: dict, state: str, *, elapsed_ms: int = 0) -> dict:
        with self._tx(write=True) as db:
            row = self._run(
                db,
                f"select state from {self.prefix}reviews where review_id=?",
                (job["review_id"],),
            ).fetchone()
            if row is None or state not in TRANSITIONS.get(row[0], set()):
                raise Conflict("invalid review transition")
            changed = self._run(
                db,
                f"update {self.prefix}reviews set state=?,version=version+1 where review_id=? and version=? and lease_owner=? and lease_until>?",
                (
                    state,
                    job["review_id"],
                    job["version"],
                    job["owner"],
                    int(self.clock()),
                ),
            ).rowcount
            if changed != 1:
                raise Conflict("worker lease or version lost")
            self._append(
                db,
                job["review_id"],
                state,
                {"provider_calls": 0, "controller_elapsed_ms": max(0, elapsed_ms)},
            )
        return job | {"version": job["version"] + 1}

    def finish(self, job, proof, *, authorities=None):
        verify(
            proof,
            trusted_authorities=authorities,
            trusted_delegations=self.delegations,
            trusted_collection_keys=self.collection_keys,
        )
        encoded = self._encode(proof, private=proof["mode"] == "PRIVATE")
        with self._tx(write=True) as db:
            changed = self._run(
                db,
                f"update {self.prefix}reviews set state='FINISHED',version=version+1,revision=?,current_digest=?,result_digest=?,result_payload=?,lease_owner=null,lease_until=null where review_id=? and tenant_id=? and actor_id=? and version=? and lease_owner=? and lease_until>? and state='HANDOFF_READY' and current_digest=? and policy_digest=?",
                (
                    proof["revision"],
                    proof["current_digest"],
                    digest(proof),
                    encoded,
                    job["review_id"],
                    job["tenant_id"],
                    job["actor_id"],
                    job["version"],
                    job["owner"],
                    int(self.clock()),
                    job.get("current_digest", proof["baseline_digest"]),
                    proof["policy_digest"],
                ),
            ).rowcount
            if changed != 1:
                raise Conflict("revision compare-and-swap failed")
            self._append(
                db,
                job["review_id"],
                "REVIEW_COMMITTED",
                {
                    "proof_digest": proof["proof_digest"],
                    "current_digest": proof["current_digest"],
                    "revision": proof["revision"],
                    "action_status": "WITHHELD",
                },
            )

    def cancel(self, review_id, tenant_id, actor_id):
        with self._tx(write=True) as db:
            changed = self._run(
                db,
                f"update {self.prefix}reviews set state='CANCELLED_SAFE',version=version+1,lease_owner=null,lease_until=null where review_id=? and tenant_id=? and actor_id=? and state not in ('FINISHED','FAILED','CANCELLED_SAFE')",
                (review_id, tenant_id, actor_id),
            ).rowcount
            if changed:
                self._append(
                    db, review_id, "CANCELLED_SAFE", {"external_action_executed": False}
                )
        return self.snapshot(review_id, tenant_id, actor_id)

    def recover_expired(self):
        recovered = 0
        with self._tx(write=True) as db:
            rows = self._run(
                db,
                f"select review_id,version from {self.prefix}reviews where lease_until<=? and lease_owner is not null",
                (int(self.clock()),),
            ).fetchall()
            for review_id, version in rows:
                count = self._run(
                    db,
                    f"update {self.prefix}reviews set state='RECOVERY_REQUIRED',version=version+1,lease_owner=null,lease_until=null where review_id=? and version=?",
                    (review_id, version),
                ).rowcount
                if count:
                    self._run(
                        db,
                        f"update {self.prefix}provider_io set state='UNKNOWN_OUTCOME' where review_id=? and state='SENT'",
                        (review_id,),
                    )
                    self._append(
                        db,
                        review_id,
                        "RECOVERY_REQUIRED",
                        {"reason": "LEASE_EXPIRED_NO_AUTOMATIC_REISSUE"},
                    )
                    recovered += 1
        return recovered

    def resume(self, review_id, tenant_id, actor_id):
        with self._tx(write=True) as db:
            count = self._run(
                db,
                f"update {self.prefix}reviews set state='ACCEPTED',version=version+1 where review_id=? and tenant_id=? and actor_id=? and state='RECOVERY_REQUIRED'",
                (review_id, tenant_id, actor_id),
            ).rowcount
            if count != 1:
                raise Conflict("review does not require recovery")
            self._append(
                db,
                review_id,
                "RESUMED",
                {"explicit_recovery": True, "provider_calls": 0},
            )
        return self.snapshot(review_id, tenant_id, actor_id)

    def commit_round(self, job, number, proof, *, authorities=None):
        verify(
            proof,
            trusted_authorities=authorities,
            trusted_delegations=self.delegations,
            trusted_collection_keys=self.collection_keys,
        )
        packed = self._encode(proof, private=proof["mode"] == "PRIVATE")
        with self._tx(write=True) as db:
            existing = self._run(
                db,
                f"select payload_digest,current_digest from {self.prefix}revisions where review_id=? and round=?",
                (job["review_id"], number),
            ).fetchone()
            if existing:
                if existing[0] != digest(proof):
                    raise Conflict("replayed round differs from committed revision")
                return job | {"current_digest": existing[1]}
            changed = self._run(
                db,
                f"update {self.prefix}reviews set current_digest=?,revision=revision+?,version=version+1 where review_id=? and version=? and lease_owner=? and lease_until>? and current_digest=? and policy_digest=?",
                (
                    proof["current_digest"],
                    int(bool(proof["patches"])),
                    job["review_id"],
                    job["version"],
                    job["owner"],
                    int(self.clock()),
                    proof["baseline_digest"],
                    proof["policy_digest"],
                ),
            ).rowcount
            if changed != 1:
                raise Conflict("round revision compare-and-swap failed")
            self._run(
                db,
                f"insert into {self.prefix}revisions values(?,?,?,?,?,?,?,?)",
                (
                    job["review_id"],
                    number,
                    proof["baseline_digest"],
                    proof["current_digest"],
                    proof["evidence_root"],
                    proof["policy_digest"],
                    packed,
                    digest(proof),
                ),
            )
            self._append(
                db,
                job["review_id"],
                "ROUND_COMMITTED",
                {
                    "round": number,
                    "current_digest": proof["current_digest"],
                    "evidence_root": proof["evidence_root"],
                    "pending_claims": proof["unresolved"],
                    "rechecks": proof["rechecks"],
                },
            )
        return job | {
            "version": job["version"] + 1,
            "current_digest": proof["current_digest"],
        }

    def fail(self, job, reason_code="CONTROLLER_FAILURE"):
        allowed = {"CONTROLLER_FAILURE", "INVALID_EVIDENCE", "POLICY_REJECTED"}
        if reason_code not in allowed:
            raise ValueError("invalid incident reason")
        with self._tx(write=True) as db:
            changed = self._run(
                db,
                f"update {self.prefix}reviews set state='FAILED',version=version+1,lease_owner=null,lease_until=null where review_id=? and version=? and lease_owner=? and lease_until>?",
                (job["review_id"], job["version"], job["owner"], int(self.clock())),
            ).rowcount
            if changed != 1:
                raise Conflict("failure lease lost")
            self._append(
                db,
                job["review_id"],
                "FAILED",
                {"reason_code": reason_code, "external_action_executed": False},
            )

    def set_retention(
        self, review_id, tenant_id, actor_id, *, expires_at, legal_hold=False
    ):
        self.snapshot(review_id, tenant_id, actor_id)
        with self._tx(write=True) as db:
            self._run(
                db,
                f"update {self.prefix}retention set expires_at=?,legal_hold=? where review_id=? and tenant_id=?",
                (int(expires_at), int(legal_hold), review_id, tenant_id),
            )
            self._append(
                db,
                review_id,
                "RETENTION_UPDATED",
                {
                    "policy": "retention/1",
                    "expires_at": int(expires_at),
                    "legal_hold": bool(legal_hold),
                },
            )

    def prune(self, tenant_id, *, limit=100):
        if not 1 <= limit <= 100:
            raise ValueError("bounded cleanup required")
        removed = []
        with self._tx(write=True) as db:
            if self.postgres:
                db.execute(
                    f"select id from {self.prefix}capacity where id=1 for update"
                )
            rows = self._run(
                db,
                f"select r.review_id,r.actor_id,r.request_key,r.input_digest,r.event_head from {self.prefix}reviews r join {self.prefix}retention t on t.review_id=r.review_id where r.tenant_id=? and t.expires_at<=? and t.legal_hold=0 and r.state in ('FINISHED','FAILED','CANCELLED_SAFE') order by t.expires_at limit ?",
                (tenant_id, int(self.clock()), limit),
            ).fetchall()
            for rid, actor, key, input_hash, head in rows:
                self._run(
                    db,
                    f"insert into {self.prefix}tombstones values(?,?,?,?,?,?,?,'retention/1')",
                    (rid, tenant_id, actor, key, input_hash, head, int(self.clock())),
                )
                source_rows = self._run(
                    db,
                    f"select call_id,payload,payload_digest from {self.prefix}provider_io where review_id=? and tenant_id=?",
                    (rid, tenant_id),
                ).fetchall()
                for call_id, payload, payload_hash in source_rows:
                    original = self._decode(payload, payload_hash)
                    cache_key = original.get("scope", {}).get("cache_key")
                    if cache_key:
                        self._run(
                            db,
                            f"delete from {self.prefix}claim_cache where cache_key=? and tenant_id=?",
                            (cache_key, tenant_id),
                        )
                    erased = {
                        "retention": "retention/1",
                        "original_commitment": payload_hash,
                    }
                    self._run(
                        db,
                        f"update {self.prefix}provider_io set payload=?,payload_digest=?,response=null where call_id=?",
                        (
                            self._encode(erased, private=self.cipher is not None),
                            digest(erased),
                            call_id,
                        ),
                    )
                for table in (
                    "intents",
                    "events",
                    "revisions",
                    "retention",
                    "traces",
                    "collection_rounds",
                ):
                    self._run(
                        db,
                        f"delete from {self.prefix}{table} where review_id=?",
                        (rid,),
                    )
                self._run(
                    db,
                    f"delete from {self.prefix}reviews where review_id=? and tenant_id=?",
                    (rid, tenant_id),
                )
                removed.append(rid)
            self._run(
                db,
                f"update {self.prefix}attempts set state='EVICTED',payload=null,payload_digest=null where tenant_id=? and state='COMPLETE' and expires_at<=?",
                (tenant_id, int(self.clock())),
            )
        return {
            "deleted_review_ids": removed,
            "policy": "retention/1",
            "audit_tombstones_preserved": True,
        }

    def admission_estimate(self, tenant_id, *, workers=2):
        with self._tx() as db:
            pending = self._run(
                db,
                f"select count(*) from {self.prefix}reviews where tenant_id=? and state not in ('FINISHED','FAILED','CANCELLED_SAFE')",
                (tenant_id,),
            ).fetchone()[0]
        return {
            "pending": pending,
            "available_slots": max(0, 128 - pending),
            "estimated_controller_wait_ms": pending * 2000 // max(1, workers),
            "estimate_basis": "CONFIGURED_DEADLINE_UPPER_BOUND_EXCLUDING_HOST_QUEUE",
            "provider_calls_reserved": 0,
        }

    def save_trace(self, review_id, trace):
        # Trace has a strict allowlist of dimensions and no request bodies or credentials.
        with self._tx(write=True) as db:
            self._run(
                db,
                f"insert into {self.prefix}traces values(?,?,?) on conflict(review_id) do update set payload=excluded.payload,payload_digest=excluded.payload_digest",
                (
                    review_id,
                    self._encode(trace, private=self.cipher is not None),
                    digest(trace),
                ),
            )

    def trace(self, review_id, tenant_id, actor_id):
        self.snapshot(review_id, tenant_id, actor_id)
        with self._tx() as db:
            row = self._run(
                db,
                f"select payload,payload_digest from {self.prefix}traces where review_id=?",
                (review_id,),
            ).fetchone()
        if not row:
            raise Conflict("trace not finalized")
        return self._decode(row[0], row[1])

    def rotate_encryption(self, new_key):
        if self.cipher is None:
            raise ValueError("encrypted store required")
        replacement = Fernet(new_key)
        tables = {
            "reviews": (
                ("request_payload", "request_digest"),
                ("result_payload", "result_digest"),
            ),
            "intents": (("payload", "digest"),),
            "revisions": (("payload", "payload_digest"),),
            "attempts": (("payload", "payload_digest"),),
            "traces": (("payload", "payload_digest"),),
            "provider_io": (
                ("payload", "payload_digest"),
                ("response", "response_digest"),
            ),
            "claim_cache": (("payload", "payload_digest"),),
            "entity_cache": (("payload", "payload_digest"),),
            "collection_rounds": (("payload", "payload_digest"),),
        }
        with self._tx(write=True) as db:
            for table, columns in tables.items():
                primary = (
                    "call_id"
                    if table == "provider_io"
                    else "cache_key"
                    if table in {"claim_cache", "entity_cache"}
                    else "intent_id"
                    if table == "intents"
                    else "scope_id"
                    if table == "attempts"
                    else "review_id"
                )
                for payload_col, hash_col in columns:
                    extra = (
                        ",round" if table in {"revisions", "collection_rounds"} else ""
                    )
                    rows = self._run(
                        db,
                        f"select {primary},{payload_col},{hash_col}{extra} from {self.prefix}{table} where {payload_col} is not null",
                    ).fetchall()
                    for row in rows:
                        value = self._decode(row[1], row[2])
                        encoded = (
                            "fernet:"
                            + replacement.encrypt(canonical_bytes(value)).decode()
                        )
                        where = f"{primary}=?" + (" and round=?" if extra else "")
                        args = (encoded, row[0], row[3]) if extra else (encoded, row[0])
                        self._run(
                            db,
                            f"update {self.prefix}{table} set {payload_col}=? where {where}",
                            args,
                        )
        self.cipher = replacement

    def note(self, job, kind, payload):
        if kind not in {
            "SOURCE_SENT",
            "SOURCE_OBSERVED",
            "EVIDENCE_RECONCILED",
            "RECHECK_QUEUED",
            "NOT_EVALUATED",
        }:
            raise ValueError("unregistered collection event")
        with self._tx(write=True) as db:
            changed = self._run(
                db,
                f"update {self.prefix}reviews set version=version+1 where review_id=? and version=? and lease_owner=? and lease_until>?",
                (job["review_id"], job["version"], job["owner"], int(self.clock())),
            ).rowcount
            if changed != 1:
                raise Conflict("collection coordinator lease lost")
            self._append(db, job["review_id"], kind, payload)
        return job | {"version": job["version"] + 1}

    def renew(self, job, *, lease_seconds=30):
        if not 1 <= lease_seconds <= 300:
            raise ValueError("invalid lease extension")
        with self._tx(write=True) as db:
            changed = self._run(
                db,
                f"update {self.prefix}reviews set lease_until=? where review_id=? and tenant_id=? and actor_id=? and lease_owner=? and lease_until>?",
                (
                    int(self.clock()) + lease_seconds,
                    job["review_id"],
                    job["tenant_id"],
                    job["actor_id"],
                    job["owner"],
                    int(self.clock()),
                ),
            ).rowcount
        return changed == 1
