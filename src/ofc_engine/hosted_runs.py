"""Bounded, ticket-scoped durable preflight for a public browser run."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import hmac
import secrets

from .durable import SQLiteRevisionStore, StorageIntegrityError
from .intake import AgentProposal, freeze_proposal
from .postgres_store import PostgresRevisionStore
from .revisions import plan_digest


class RunCapacityReached(RuntimeError):
    pass


class HostedRunRegistry:
    max_daily_runs = 80

    def __init__(self, store: PostgresRevisionStore):
        self.store = store
        if not self.ready():
            raise RuntimeError("browser quota configuration missing")

    def ready(self) -> bool:
        with self.store._connect() as db:
            return db.execute(
                "select id from ofc_private.browser_budget where id=1"
            ).fetchone() == (1,)

    def create(self, submitted: dict) -> dict:
        proposal = AgentProposal.model_validate_json(
            json.dumps(submitted, allow_nan=False, separators=(",", ":"))
        )
        if proposal.intent.constraints.get("external_action") is True:
            raise ValueError("external actions cannot be requested")
        run_id = "web-" + secrets.token_hex(16)
        ticket = secrets.token_urlsafe(32)
        intent = proposal.intent.model_copy(update={
            "run_id": run_id,
            "client_request_id": run_id,
            "constraints": {
                **proposal.intent.constraints,
                "external_action": False,
            },
        })
        proposal = AgentProposal.model_validate(
            proposal.model_copy(update={"intent": intent}).model_dump()
        )
        baseline, _ = freeze_proposal(proposal)
        with self.store._connect() as db:
            quota = db.execute(
                "select day_utc,writes from ofc_private.browser_budget "
                "where id=1 for update"
            ).fetchone()
            if quota is None:
                raise StorageIntegrityError("browser quota configuration missing")
            today = datetime.now(timezone.utc).date()
            count = quota[1] if quota[0] == today else 0
            if count >= self.max_daily_runs:
                raise RunCapacityReached("daily review quota exhausted")
            db.execute(
                "update ofc_private.browser_budget set day_utc=%s,writes=%s where id=1",
                (today, count + 1),
            )
            self.store.insert_in_transaction(db, baseline)
            db.execute(
                "insert into ofc_private.browser_runs "
                "(plan_id,ticket_digest,expires_at,proposal_snapshot) "
                "values (%s,%s,now()+interval '24 hours',%s)",
                (run_id, sha256(ticket.encode()).hexdigest(),
                 proposal.model_dump_json()),
            )
        return {
            "run_id": run_id,
            "ticket": ticket,
            "expires_in_hours": 24,
            "review": self.review(run_id, ticket),
        }

    def operator_proposal(self, run_id: str) -> AgentProposal:
        if (
            not isinstance(run_id, str)
            or len(run_id) != 36
            or not run_id.startswith("web-")
            or any(c not in "0123456789abcdef" for c in run_id[4:])
        ):
            raise ValueError("invalid browser run ID")
        with self.store._connect() as db:
            row = db.execute(
                "select r.proposal_snapshot,p.snapshot,p.revision,p.digest "
                "from ofc_private.browser_runs r "
                "join ofc_private.plans p on p.plan_id=r.plan_id "
                "where r.plan_id=%s and r.expires_at>now()",
                (run_id,),
            ).fetchone()
            if row is None or row[0] is None:
                raise ValueError("original browser proposal unavailable")
            proposal = AgentProposal.model_validate_json(row[0])
            baseline, _ = freeze_proposal(proposal)
            current = SQLiteRevisionStore._decode(row[1], row[2], row[3])
            history = self.store._history(db, run_id)
            if (
                baseline.plan_id != run_id
                or baseline.baseline_digest != current.baseline_digest
                or baseline.constraints_digest != current.constraints_digest
                or history[0].after_digest != plan_digest(baseline)
                or current.revision not in (0, 1)
            ):
                raise StorageIntegrityError("browser proposal baseline conflict")
        return proposal

    def review(self, run_id: str, ticket: str) -> dict:
        if (
            not isinstance(run_id, str) or not run_id.startswith("web-")
            or len(run_id) != 36 or
            not isinstance(ticket, str) or not 32 <= len(ticket) <= 128
        ):
            raise KeyError("unknown run")
        fingerprint = sha256(ticket.encode()).hexdigest()
        with self.store._connect() as db:
            row = db.execute(
                "select p.snapshot,p.revision,p.digest,p.pending_rechecks,"
                "r.ticket_digest from ofc_private.browser_runs r "
                "join ofc_private.plans p on p.plan_id=r.plan_id "
                "where r.plan_id=%s and r.expires_at>now()",
                (run_id,),
            ).fetchone()
            if row is None or not hmac.compare_digest(fingerprint, row[4]):
                raise KeyError("unknown run")
            snapshot, revision, digest, pending_json, _ = row
            plan = SQLiteRevisionStore._decode(snapshot, revision, digest)
            pending = SQLiteRevisionStore._decode_pending(pending_json, plan)
            history = self.store._history(db, run_id)
        return {
            "run_id": run_id,
            "revision": revision,
            "plan_digest": digest,
            "journal_head": history[-1].event_hash,
            "source_mode": "EVIDENCE_UNAVAILABLE",
            "verification_status": "REVIEW_REQUIRED",
            "provider_authenticated": False,
            "independent_recheck_completed": False,
            "release_authorized": False,
            "external_action_executed": False,
            "real_qloo_requests": 0,
            "pending_rechecks": list(pending),
            "decisions": [
                {
                    "decision_id": item.decision_id,
                    "verdict": "UNCERTAIN",
                    "reason_codes": ["EVIDENCE_UNAVAILABLE"],
                    "required": item.required,
                    "depends_on": list(item.depends_on),
                }
                for item in plan.decisions
            ],
        }
