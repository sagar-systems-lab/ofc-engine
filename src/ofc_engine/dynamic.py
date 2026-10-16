"""Budgeted live Qloo reviews for independent brand decisions in arbitrary agent plans."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from .contracts import RepairPatch
from .durable import SQLiteRevisionStore, StorageIntegrityError
from .intake import AgentProposal, freeze_proposal
from .qloo import QlooProbeClient, QlooProbeError, ProbeSpec, brand_affinity_probe
from .qloo_affinity import AffinityCaptureError, normalize_affinity_capture
from .revisions import RevisionConflict, digest_obj, plan_digest

_POLICY = "dynamic-brand-rank-v1"
_MAX_DECISIONS = 4
_MAX_EVIDENCE_AGE = timedelta(hours=24)


class DynamicScopeConflict(ValueError):
    pass


class DynamicQlooReview:
    """Local, operator-budgeted execution; never authorizes external actions."""

    def __init__(self, store: SQLiteRevisionStore, client: QlooProbeClient, *, daily_limit: int):
        if not isinstance(store, SQLiteRevisionStore) or not isinstance(client, QlooProbeClient):
            raise TypeError("a durable store and Qloo client are required")
        if type(daily_limit) is not int or not 1 <= daily_limit <= 64:
            raise ValueError("Qloo request budget must be between 1 and 64")
        self.store = store
        self.client = client
        self.daily_limit = daily_limit
        db = self.store._connect()
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS dynamic_qloo_daily (
                    utc_day TEXT PRIMARY KEY, reserved INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dynamic_qloo_attempts (
                    plan_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    pass_no INTEGER NOT NULL CHECK (pass_no IN (1,2)),
                    baseline_digest TEXT NOT NULL,
                    query_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('RESERVED','COMPLETE')),
                    result TEXT,
                    result_digest TEXT,
                    PRIMARY KEY (plan_id,decision_id,pass_no),
                    FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
                );
            """)
        finally:
            db.close()

    def _observation(self, plan_id: str, decision_id: str, pass_no: int,
                     baseline_digest: str, spec: ProbeSpec) -> tuple[dict | None, int, str]:
        """Reserve before network I/O. A crash or failure cannot silently retry."""
        db = self.store._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT baseline_digest,query_fingerprint,state,result,result_digest "
                "FROM dynamic_qloo_attempts WHERE plan_id=? AND decision_id=? AND pass_no=?",
                (plan_id, decision_id, pass_no),
            ).fetchone()
            if row is not None:
                if row[0] != baseline_digest or row[1] != spec.fingerprint:
                    raise StorageIntegrityError("Qloo attempt belongs to another frozen scope")
                if row[2] == "COMPLETE":
                    try:
                        saved = json.loads(row[3])
                    except (ValueError, TypeError) as exc:
                        raise StorageIntegrityError("invalid saved Qloo rank") from exc
                    if (
                        not isinstance(saved, dict)
                        or digest_obj(saved) != row[4]
                        or saved.get("query_fingerprint") != spec.fingerprint
                        or not isinstance(saved.get("ranking"), list)
                        or set(saved["ranking"]) !=
                            set(dict(spec.params)["filter.results.entities"].split(","))
                        or len(saved["ranking"]) != len(set(saved["ranking"]))
                    ):
                        raise StorageIntegrityError("saved rank scope or digest mismatch")
                    db.execute("COMMIT")
                    return saved, 0, ""
                db.execute("COMMIT")
                return None, 0, "ATTEMPT_ALREADY_RESERVED"

            day = datetime.now(timezone.utc).date().isoformat()
            used_row = db.execute(
                "SELECT reserved FROM dynamic_qloo_daily WHERE utc_day=?", (day,)
            ).fetchone()
            used = used_row[0] if used_row else 0
            if used >= self.daily_limit:
                db.execute("COMMIT")
                return None, 0, "DAILY_QLOO_BUDGET_EXHAUSTED"
            db.execute(
                "INSERT INTO dynamic_qloo_daily(utc_day,reserved) VALUES(?,1) "
                "ON CONFLICT(utc_day) DO UPDATE SET reserved=reserved+1", (day,)
            )
            db.execute(
                "INSERT INTO dynamic_qloo_attempts "
                "(plan_id,decision_id,pass_no,baseline_digest,query_fingerprint,state) "
                "VALUES(?,?,?,?,?,'RESERVED')",
                (plan_id, decision_id, pass_no, baseline_digest, spec.fingerprint),
            )
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

        # No retries: even a failed network request consumes one reservation.
        try:
            raw = self.client.probe(spec)
            capture = {
                "summary": {
                    "endpoint": raw.path,
                    "query_fingerprint": raw.query_fingerprint,
                    "utc_collected_at": raw.utc_collected_at,
                    "http_status": raw.http_status,
                    "real_qloo_requests": 1,
                    "mode": "LIVE_RESPONSE_UNQUALIFIED",
                },
                "payload": raw.payload,
            }
            rank = normalize_affinity_capture(capture, spec)
            result = {
                "query_fingerprint": spec.fingerprint,
                "collected_at": rank["collected_at"],
                "ranking": [item["entity_id"] for item in rank["ranking"]],
                "capture_digest": digest_obj(raw.payload),
            }
        except (QlooProbeError, AffinityCaptureError, ValueError, TypeError):
            return None, 1, "QLOO_EVIDENCE_UNAVAILABLE"

        db = self.store._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE dynamic_qloo_attempts SET state='COMPLETE',result=?,result_digest=? "
                "WHERE plan_id=? AND decision_id=? AND pass_no=? AND state='RESERVED' "
                "AND baseline_digest=? AND query_fingerprint=?",
                (json.dumps(result, sort_keys=True), digest_obj(result),
                 plan_id, decision_id, pass_no, baseline_digest, spec.fingerprint),
            )
            if changed.rowcount != 1:
                raise StorageIntegrityError("Qloo reservation lost during completion")
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()
        return result, 1, ""

    @staticmethod
    def _scope(proposal: AgentProposal, decision) -> tuple[ProbeSpec | None, str]:
        if decision.domain.lower() not in ("brand", "partner", "brand_partner"):
            return None, "UNSUPPORTED_QLOO_DECISION_DOMAIN"
        if decision.depends_on:
            return None, "DEPENDENCY_SCOPE_NOT_QUALIFIED"
        if decision.locality_ref is not None:
            return None, "LOCALITY_SCOPE_NOT_QUALIFIED"
        artist_map = proposal.intent.taste_context.get("qloo_artist_ids", {})
        if not isinstance(artist_map, dict):
            return None, "INVALID_QLOO_SUBJECT_MAP"
        artist = artist_map.get(
            decision.decision_id, proposal.intent.taste_context.get("artist_id")
        )
        if not isinstance(artist, str):
            return None, "QLOO_ARTIST_ID_REQUIRED"
        try:
            return brand_affinity_probe(artist, decision.candidate_pool), ""
        except ValueError:
            return None, "UNQUALIFIED_QLOO_CANDIDATE_SCOPE"

    @staticmethod
    def _fresh(first: dict, second: dict | None = None) -> bool:
        now = datetime.now(timezone.utc)
        try:
            dates = [
                datetime.fromisoformat(item["collected_at"])
                for item in (first,) if item is not None
            ]
            if second is not None:
                dates.append(datetime.fromisoformat(second["collected_at"]))
        except (ValueError, TypeError, KeyError):
            return False
        return all(
            stamp.utcoffset() is not None
            and timedelta(0) <= now - stamp < _MAX_EVIDENCE_AGE
            for stamp in dates
        ) and (second is None or dates[1] > dates[0])

    def evaluate(self, proposal: AgentProposal) -> dict:
        proposal = AgentProposal.model_validate(proposal.model_dump())
        if proposal.intent.constraints.get("external_action") is not False:
            raise DynamicScopeConflict("dynamic Qloo review requires external_action=false")
        baseline, _ = freeze_proposal(proposal)
        baseline_digest = plan_digest(baseline)
        try:
            self.store.insert(baseline)
        except RevisionConflict:
            current = self.store.get(baseline.plan_id)
            journal = self.store.audit_history(baseline.plan_id)
            if (
                journal[0].after_digest != baseline_digest
                or current.baseline_digest != baseline.baseline_digest
                or current.constraints_digest != baseline.constraints_digest
            ):
                raise DynamicScopeConflict("run ID belongs to a different baseline")

        results = []
        spent = 0
        eligible = 0
        saw_observation = False
        for decision in baseline.decisions:
            report = {
                "decision_id": decision.decision_id,
                "domain": decision.domain,
                "verdict": "UNCERTAIN",
                "reason_code": "EVIDENCE_UNAVAILABLE",
                "rank_recheck_completed": False,
            }
            spec, reason = self._scope(proposal, decision)
            if spec is None:
                report["reason_code"] = reason
                results.append(report)
                continue
            if eligible >= _MAX_DECISIONS:
                report["reason_code"] = "PER_RUN_QLOO_SCOPE_LIMIT"
                results.append(report)
                continue
            eligible += 1
            first, used, reason = self._observation(
                baseline.plan_id, decision.decision_id, 1, baseline_digest, spec
            )
            spent += used
            if first is None or not self._fresh(first):
                report["reason_code"] = reason or "FIRST_RANK_STALE"
                results.append(report)
                continue
            saw_observation = True
            report["qloo_rank_evidence"] = {
                "source_mode": "LIVE_HTTP_UNVERIFIED",
                "query_fingerprint": spec.fingerprint,
                "capture_digest": first["capture_digest"],
                "collected_at": first["collected_at"],
                "ranked_candidate_ids": list(first["ranking"]),
            }
            top = first["ranking"][0]
            current = self.store.get(baseline.plan_id)
            selected = next(
                x.candidate_id for x in current.decisions
                if x.decision_id == decision.decision_id
            )
            if selected != top:
                if selected != decision.candidate_id:
                    report["reason_code"] = "STORED_SELECTION_CONFLICT"
                    results.append(report)
                    continue
                if "candidate_id" not in decision.mutable_fields:
                    report["reason_code"] = "REPAIR_NOT_ALLOWED"
                    results.append(report)
                    continue

            # A repair must never be committed using only one observation.
            second, used, reason = self._observation(
                baseline.plan_id, decision.decision_id, 2, baseline_digest, spec
            )
            spent += used
            if second is None or not self._fresh(first, second):
                report["reason_code"] = reason or "RANK_RECHECK_STALE"
                results.append(report)
                continue
            if second["ranking"] != first["ranking"]:
                report["reason_code"] = "FRESH_RANK_RECHECK_DISAGREES"
                results.append(report)
                continue

            if selected != top:
                patch = RepairPatch(
                    patch_id="dynamic:" + baseline.plan_id + ":" + decision.decision_id,
                    decision_id=decision.decision_id,
                    base_revision=current.revision,
                    old_candidate_id=selected,
                    new_candidate_id=top,
                    evidence_refs=(
                        "qloo:" + spec.fingerprint + ":" + first["capture_digest"],
                        "qloo-recheck:" + spec.fingerprint + ":" + second["capture_digest"],
                    ),
                    precondition_digest=plan_digest(current),
                )
                try:
                    self.store.apply_patch_once(
                        baseline.plan_id, patch,
                        client_request_id="dynamic:" + decision.decision_id,
                    )
                except RevisionConflict:
                    report["reason_code"] = "CONCURRENT_REPAIR_CONFLICT"
                    results.append(report)
                    continue
            report.update({
                "verdict": "PASS",
                "reason_code": "QLOO_RANK_RECHECK_MATCHED",
                "rank_recheck_completed": True,
                "evidence_fingerprints": [spec.fingerprint],
                "original_capture_digest": first["capture_digest"],
                "recheck_capture_digest": second["capture_digest"],
            })
            results.append(report)

        current = self.store.get(baseline.plan_id)
        journal = self.store.audit_history(baseline.plan_id)
        if journal[0].after_digest != baseline_digest:
            raise StorageIntegrityError("stored plan is no longer scope-bound")
        changed = [
            old.decision_id for old, new in zip(baseline.decisions, current.decisions)
            if old.candidate_id != new.candidate_id
        ]
        selected = {decision.decision_id: decision.candidate_id
                    for decision in current.decisions}
        for item in results:
            item["selected_candidate_id"] = selected[item["decision_id"]]
        return {
            "run_id": baseline.plan_id,
            "policy_version": _POLICY,
            "source_mode": "LIVE_HTTP_UNVERIFIED" if saw_observation else "EVIDENCE_UNAVAILABLE",
            "baseline_plan_digest": baseline_digest,
            "plan_digest": plan_digest(current),
            "revision": current.revision,
            "journal_head": journal[-1].event_hash,
            "changed_ids": changed,
            "pending_rechecks": list(self.store.pending_rechecks(baseline.plan_id)),
            "decisions": results,
            "new_qloo_requests": spent,
            "verification_status": "REVIEW_REQUIRED",
            "rank_evidence_rechecked": sum(
                item["rank_recheck_completed"] for item in results
            ),
            "full_plan_independently_verified": False,
            "provider_authenticated": False,
            "release_authorized": False,
            "external_action_executed": False,
        }
