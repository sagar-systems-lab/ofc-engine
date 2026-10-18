from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
import psycopg

from .canonical import canonical_bytes, digest, strict_json
from .models import ReviewRequest, utc_now
from .policies import POLICY_DIGEST
from .verifier import verify

TERMINAL = frozenset({"FINISHED", "FAILED", "CANCELLED_SAFE", "RECOVERY_REQUIRED"})
TRANSITIONS = {
    "RESERVED": {"FROZEN"}, "FROZEN": {"EVALUATING"},
    "EVALUATING": {"REPAIR_PROPOSED", "HANDOFF_READY"},
    "REPAIR_PROPOSED": {"RECHECKING"}, "RECHECKING": {"HANDOFF_READY"},
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
    def __init__(self, location: str | Path, *, encryption_key: str | bytes | None = None,
                 initialize: bool = False, clock=time.time):
        self.location, self.clock = str(location), clock
        self.postgres = self.location.startswith(("postgresql://", "postgres://"))
        self.prefix = "ofc_control." if self.postgres else "control_"
        self.cipher = Fernet(encryption_key) if encryption_key else None
        if self.postgres:
            if initialize:
                self.migrate()
            with self._tx() as db:
                db.execute(f"select review_id,event_count,event_head,policy_digest from {self.prefix}reviews limit 0")
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
            with psycopg.connect(self.location, connect_timeout=5, sslmode="disable" if local else "require") as conn:
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
                event_head text not null default '{'0' * 64}',
                unique(tenant_id,actor_id,request_key))""")
            db.execute(f"""create table if not exists {self.prefix}events (
                review_id text not null references {self.prefix}reviews(review_id),
                sequence integer not null, kind text not null, payload text not null,
                previous_hash text not null, event_hash text not null,
                primary key(review_id,sequence))""")
            db.execute(f"""create table if not exists {self.prefix}capacity (
                id integer primary key check(id=1))""")
            db.execute(f"insert into {self.prefix}capacity(id) values(1) on conflict(id) do nothing")
            db.execute(f"create index if not exists control_review_queue on {self.prefix}reviews(state,created_at)")
            db.execute(f"""create table if not exists {self.prefix}intents (
                intent_id text primary key, review_id text not null references {self.prefix}reviews(review_id),
                request_key text not null, tenant_id text not null, actor_id text not null,
                state text not null, payload text not null, digest text not null,
                unique(review_id,request_key))""")
            if self.postgres:
                for table in ("reviews", "events", "intents", "capacity"):
                    db.execute(f"alter table {self.prefix}{table} enable row level security")
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
        anchor = self._run(db, f"select event_count,event_head from {self.prefix}reviews where review_id=?" + suffix, (review_id,)).fetchone()
        last = self._run(db, f"select sequence,event_hash from {self.prefix}events where review_id=? order by sequence desc limit 1", (review_id,)).fetchone()
        sequence, previous = (last[0] + 1, last[1]) if last else (1, "0" * 64)
        if anchor is None or anchor[0] != sequence - 1 or anchor[1] != previous:
            raise IntegrityError("journal anchor mismatch")
        fields = {"review_id": review_id, "sequence": sequence, "kind": kind,
                  "payload": payload, "previous": previous}
        self._run(db, f"insert into {self.prefix}events values(?,?,?,?,?,?)",
                  (review_id, sequence, kind, canonical_bytes(payload).decode(), previous, digest(fields)))
        self._run(db, f"update {self.prefix}reviews set event_count=?,event_head=? where review_id=?",
                  (sequence, digest(fields), review_id))

    def admit(self, request: ReviewRequest, *, max_pending=128, max_total=4096) -> dict:
        original = request.data()
        original_digest = digest(original)
        plan = request.plan
        private = request.mode == "PRIVATE"
        if private and request.as_of is not None:
            raise ValueError("private review clock is server controlled")
        frozen = request.data()
        frozen["as_of"] = utc_now() if private or request.as_of is None else request.as_of
        packed = {"input": original, "frozen": frozen}
        encoded = self._encode(packed, private=private)
        with self._tx(write=True) as db:
            if self.postgres:
                db.execute(f"select id from {self.prefix}capacity where id=1 for update")
            row = self._run(db, f"select review_id,input_digest from {self.prefix}reviews where tenant_id=? and actor_id=? and request_key=?",
                            (plan.tenant_id, plan.actor.id, plan.idempotency_key)).fetchone()
            if row:
                if row[1] != original_digest:
                    raise Conflict("idempotency key already binds a different input")
                return self._receipt(row[0], plan.digest)
            total = self._run(db, f"select count(*) from {self.prefix}reviews").fetchone()[0]
            pending = self._run(db, f"select count(*) from {self.prefix}reviews where tenant_id=? and state not in ('FINISHED','FAILED','CANCELLED_SAFE')", (plan.tenant_id,)).fetchone()[0]
            if total >= max_total or pending >= max_pending:
                raise CapacityReached("review capacity exhausted")
            review_id = "pr-" + secrets.token_hex(16)
            self._run(db, f"insert into {self.prefix}reviews (review_id,tenant_id,actor_id,request_key,input_digest,request_digest,request_payload,state,version,revision,current_digest,policy_digest,created_at) values(?,?,?,?,?,?,?,'ACCEPTED',0,0,?,?,?)",
                      (review_id, plan.tenant_id, plan.actor.id, plan.idempotency_key, original_digest,
                       digest(packed), encoded, plan.digest, POLICY_DIGEST, int(self.clock())))
            self._append(db, review_id, "ACCEPTED", {"plan_digest": plan.digest, "policy_digest": POLICY_DIGEST,
                                                   "reserved_provider_calls": 0, "reserved_spend_minor": 0})
        return self._receipt(review_id, plan.digest)

    @staticmethod
    def _receipt(review_id, plan_digest):
        return {"review_id": review_id, "correlation_id": review_id, "plan_digest": plan_digest,
                "status_url": f"/api/control/reviews/{review_id}",
                "events_url": f"/api/control/reviews/{review_id}/events",
                "proof_url": f"/api/control/reviews/{review_id}/proof",
                "provider_calls": 0, "spend_minor": 0, "external_action_executed": False}

    def snapshot(self, review_id, tenant_id, actor_id) -> dict:
        with self._tx() as db:
            row = self._run(db, f"select state,version,revision,current_digest,policy_digest,result_digest,result_payload from {self.prefix}reviews where review_id=? and tenant_id=? and actor_id=?",
                            (review_id, tenant_id, actor_id)).fetchone()
        if row is None:
            raise NotFound("review unavailable")
        proof_digest = self._decode(row[6], row[5])["proof_digest"] if row[6] else None
        return {"review_id": review_id, "state": row[0], "version": row[1], "revision": row[2],
                "current_digest": row[3], "policy_digest": row[4], "proof_digest": proof_digest,
                "action_status": "WITHHELD", "external_action_executed": False,
                "provider_calls": 0, "spend_minor": 0}

    def events(self, review_id, tenant_id, actor_id, *, after=0):
        with self._tx() as db:
            suffix = " for share" if self.postgres else ""
            anchor = self._run(db, f"select event_count,event_head from {self.prefix}reviews where review_id=? and tenant_id=? and actor_id=?" + suffix,
                               (review_id, tenant_id, actor_id)).fetchone()
            if anchor is None:
                raise NotFound("review unavailable")
            rows = self._run(db, f"select sequence,kind,payload,previous_hash,event_hash from {self.prefix}events where review_id=? order by sequence", (review_id,)).fetchall()
        previous, events = "0" * 64, []
        for sequence, kind, raw, parent, event_hash in rows:
            item = {"review_id": review_id, "sequence": sequence, "kind": kind,
                    "payload": strict_json(raw), "previous": parent}
            if sequence != len(events) + 1 or parent != previous or digest(item) != event_hash:
                raise IntegrityError("review event chain mismatch")
            events.append(item | {"hash": event_hash})
            previous = event_hash
        if len(events) != anchor[0] or previous != anchor[1]:
            raise IntegrityError("review journal anchor mismatch")
        return [e for e in events if e["sequence"] > after]

    def request(self, review_id, tenant_id, actor_id):
        with self._tx() as db:
            row = self._run(db, f"select request_payload,request_digest from {self.prefix}reviews where review_id=? and tenant_id=? and actor_id=?",
                            (review_id, tenant_id, actor_id)).fetchone()
        if row is None:
            raise NotFound("review unavailable")
        return ReviewRequest.parse(self._decode(row[0], row[1])["frozen"])

    def proof(self, review_id, tenant_id, actor_id, *, authorities=None):
        self.events(review_id, tenant_id, actor_id)
        with self._tx() as db:
            row = self._run(db, f"select result_payload,result_digest,current_digest,revision from {self.prefix}reviews where review_id=? and tenant_id=? and actor_id=?",
                            (review_id, tenant_id, actor_id)).fetchone()
        if row is None:
            raise NotFound("review unavailable")
        if row[0] is None:
            raise Conflict("proof is not committed")
        value = self._decode(row[0], row[1])
        if value["current_digest"] != row[2] or value["revision"] != row[3]:
            raise IntegrityError("review revision mismatch")
        verify(value, trusted_authorities=authorities)
        return value

    def claim(self, owner: str, *, lease_seconds=30):
        if not owner or not 1 <= lease_seconds <= 300:
            raise ValueError("invalid worker lease")
        with self._tx(write=True) as db:
            suffix = " for update skip locked" if self.postgres else ""
            row = self._run(db, f"select review_id,tenant_id,actor_id,version from {self.prefix}reviews where state='ACCEPTED' order by created_at,review_id limit 1" + suffix).fetchone()
            if row is None:
                return None
            self._run(db, f"update {self.prefix}reviews set state='RESERVED',version=version+1,lease_owner=?,lease_until=? where review_id=? and version=?",
                      (owner, int(self.clock()) + lease_seconds, row[0], row[3]))
            self._append(db, row[0], "RESERVED", {"provider_calls": 0})
        return {"review_id": row[0], "tenant_id": row[1], "actor_id": row[2], "version": row[3] + 1, "owner": owner}

    def advance(self, job: dict, state: str, *, elapsed_ms: int = 0) -> dict:
        with self._tx(write=True) as db:
            row = self._run(db, f"select state from {self.prefix}reviews where review_id=?", (job["review_id"],)).fetchone()
            if row is None or state not in TRANSITIONS.get(row[0], set()):
                raise Conflict("invalid review transition")
            changed = self._run(db, f"update {self.prefix}reviews set state=?,version=version+1 where review_id=? and version=? and lease_owner=? and lease_until>?",
                                (state, job["review_id"], job["version"], job["owner"], int(self.clock()))).rowcount
            if changed != 1:
                raise Conflict("worker lease or version lost")
            self._append(db, job["review_id"], state, {"provider_calls": 0, "controller_elapsed_ms": max(0, elapsed_ms)})
        return job | {"version": job["version"] + 1}

    def finish(self, job, proof, *, authorities=None):
        verify(proof, trusted_authorities=authorities)
        encoded = self._encode(proof, private=proof["mode"] == "PRIVATE")
        with self._tx(write=True) as db:
            changed = self._run(db, f"update {self.prefix}reviews set state='FINISHED',version=version+1,revision=?,current_digest=?,result_digest=?,result_payload=?,lease_owner=null,lease_until=null where review_id=? and tenant_id=? and actor_id=? and version=? and lease_owner=? and lease_until>? and state='HANDOFF_READY' and revision=0 and current_digest=? and policy_digest=?",
                                (proof["revision"], proof["current_digest"], digest(proof), encoded,
                                 job["review_id"], job["tenant_id"], job["actor_id"], job["version"], job["owner"],
                                 int(self.clock()), proof["baseline_digest"], proof["policy_digest"])).rowcount
            if changed != 1:
                raise Conflict("revision compare-and-swap failed")
            self._append(db, job["review_id"], "REVIEW_COMMITTED", {"proof_digest": proof["proof_digest"],
                         "current_digest": proof["current_digest"], "revision": proof["revision"], "action_status": "WITHHELD"})

    def cancel(self, review_id, tenant_id, actor_id):
        with self._tx(write=True) as db:
            changed = self._run(db, f"update {self.prefix}reviews set state='CANCELLED_SAFE',version=version+1,lease_owner=null,lease_until=null where review_id=? and tenant_id=? and actor_id=? and state not in ('FINISHED','FAILED','CANCELLED_SAFE')",
                                (review_id, tenant_id, actor_id)).rowcount
            if changed:
                self._append(db, review_id, "CANCELLED_SAFE", {"external_action_executed": False})
        return self.snapshot(review_id, tenant_id, actor_id)

    def recover_expired(self):
        recovered = 0
        with self._tx(write=True) as db:
            rows = self._run(db, f"select review_id,version from {self.prefix}reviews where lease_until<=? and lease_owner is not null", (int(self.clock()),)).fetchall()
            for review_id, version in rows:
                count = self._run(db, f"update {self.prefix}reviews set state='RECOVERY_REQUIRED',version=version+1,lease_owner=null,lease_until=null where review_id=? and version=?", (review_id, version)).rowcount
                if count:
                    self._append(db, review_id, "RECOVERY_REQUIRED", {"reason": "LEASE_EXPIRED_NO_AUTOMATIC_REISSUE"})
                    recovered += 1
        return recovered

    def resume(self, review_id, tenant_id, actor_id):
        with self._tx(write=True) as db:
            count = self._run(db, f"update {self.prefix}reviews set state='ACCEPTED',version=version+1 where review_id=? and tenant_id=? and actor_id=? and state='RECOVERY_REQUIRED'", (review_id, tenant_id, actor_id)).rowcount
            if count != 1:
                raise Conflict("review does not require recovery")
            self._append(db, review_id, "RESUMED", {"explicit_recovery": True, "provider_calls": 0})
        return self.snapshot(review_id, tenant_id, actor_id)
