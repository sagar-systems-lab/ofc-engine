"""Operator-controlled persistence of a recorded-only candidate repair."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from stat import S_ISREG
import sqlite3
from typing import Any

from pydantic import ValidationError

from .contracts import RepairPatch
from .durable import SQLiteRevisionStore, StorageIntegrityError
from .intake import AgentProposal, freeze_proposal
from .qloo_agent_bridge import AgentEvidenceRejected, _read_private_json, compare_agent_proposal
from .revisions import PatchRejected, RevisionConflict, apply_patch, plan_digest
from .store import IdempotencyConflict

_ACK = "COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def commit_recorded_repair(
    store: SQLiteRevisionStore,
    proposal: AgentProposal,
    capture: dict[str, Any],
    *,
    decision_id: str,
    artist_id: str,
    brand_ids: tuple[str, ...],
    request_id: str,
    acknowledgement: str,
    as_of: datetime,
    max_evidence_age: timedelta,
) -> dict[str, Any]:
    """Commit a reviewed recorded rank change; preserve its pending recheck."""
    if acknowledgement != _ACK:
        raise AgentEvidenceRejected("EXPLICIT_OPERATOR_ACK_REQUIRED")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise AgentEvidenceRejected("INVALID_REQUEST_ID")

    baseline, _ = freeze_proposal(proposal)
    comparison = compare_agent_proposal(
        proposal, capture,
        decision_id=decision_id, artist_id=artist_id, brand_ids=brand_ids,
        as_of=as_of, max_evidence_age=max_evidence_age,
    )
    arms = comparison["comparison"]
    disabled = arms["evidence_off"]
    enabled = arms["evidence_on"]
    traces = enabled["traces"]

    if (
        not arms["same_baseline"]
        or disabled["status"] != "REVIEW_REQUIRED"
        or enabled["status"] != "RECORDED_RANK_REVIEW_ONLY"
        or enabled["initial_revision"] != 0
        or enabled["current_revision"] != 1
        or enabled["repair_attempts"] != 1
        or len(traces) != 1
        or enabled["pending_recheck_ids"]
        or comparison["provider_authenticated"]
        or comparison["release_authorized"]
    ):
        raise AgentEvidenceRejected("RECORDED_REPAIR_NOT_QUALIFIED")

    trace = traces[0]
    decisions = trace["decisions"]
    if (
        trace["changed_decision_id"] != decision_id
        or trace["before_plan_digest"] != plan_digest(baseline)
        or trace["proposed_revision"] != 1
        or trace["recheck_state"] != "STRUCTURAL_PASS"
        or trace["issue_codes"]
        or len(decisions) != 1
        or len(trace["affected_decision_ids"]) != 1
        or trace["affected_decision_ids"][0] != decision_id
        or decisions[0]["decision_id"] != decision_id
        or not decisions[0]["changed"]
        or decisions[0]["before_verdict"] != "REPAIR"
        or decisions[0]["after_verdict"] != "PASS"
        or not decisions[0]["before_evidence_ids"]
        or decisions[0]["before_evidence_ids"] != decisions[0]["after_evidence_ids"]
    ):
        raise AgentEvidenceRejected("RECORDED_TRACE_NOT_QUALIFIED")

    change = decisions[0]
    patch = RepairPatch(
        patch_id=f"recorded:{baseline.plan_id}:0:{decision_id}",
        decision_id=decision_id,
        base_revision=0,
        old_candidate_id=change["before_candidate_id"],
        new_candidate_id=change["after_candidate_id"],
        precondition_digest=plan_digest(baseline),
        evidence_refs=tuple(change["before_evidence_ids"]),
    )
    expected, affected = apply_patch(baseline, patch)
    if (
        patch.old_candidate_id != next(d.candidate_id for d in baseline.decisions if d.decision_id == decision_id)
        or expected.revision != 1
        or affected != (decision_id,)
        or plan_digest(expected) != trace["after_plan_digest"]
        or enabled["selected_candidates"].get(decision_id) != patch.new_candidate_id
    ):
        raise AgentEvidenceRejected("RECORDED_PATCH_DIVERGED")

    try:
        saved = store.get(baseline.plan_id)
        history = store.audit_history(baseline.plan_id)
        pending = store.pending_rechecks(baseline.plan_id)
    except KeyError:
        raise AgentEvidenceRejected("STORED_PLAN_MISSING") from None
    except (StorageIntegrityError, sqlite3.Error):
        raise AgentEvidenceRejected("STORED_JOURNAL_INVALID") from None

    replayed = False
    if saved == baseline:
        if (
            len(history) != 1 or history[0].kind != "INSERT"
            or history[0].after_digest != plan_digest(baseline)
            or pending
        ):
            raise AgentEvidenceRejected("STORED_BASELINE_NOT_CLEAN")
    elif saved == expected:
        if (
            len(history) != 2 or history[0].kind != "INSERT"
            or history[0].after_digest != plan_digest(baseline)
            or history[1].kind != "PATCH"
            or history[1].request_id != request_id
            or history[1].after_digest != plan_digest(expected)
            or pending != affected
        ):
            raise AgentEvidenceRejected("STORED_REPAIR_REPLAY_CONFLICT")
        replayed = True
    else:
        raise AgentEvidenceRejected("STORED_BASELINE_CONFLICT")

    persisted, invalidated = store.apply_patch_once(
        baseline.plan_id, patch, client_request_id=request_id,
    )
    final_plan = store.get(baseline.plan_id)
    final_history = store.audit_history(baseline.plan_id)
    final_pending = store.pending_rechecks(baseline.plan_id)
    if (
        persisted != expected or final_plan != expected
        or invalidated != affected or final_pending != affected
        or len(final_history) != 2
        or final_history[-1].request_id != request_id
        or final_history[-1].after_digest != plan_digest(expected)
    ):
        raise AgentEvidenceRejected("STORED_REPAIR_POSTCONDITION_FAILED")

    return {
        "status": "RECORDED_REPAIR_STORED_RECHECK_PENDING",
        "plan_id": baseline.plan_id,
        "original_plan_digest": plan_digest(baseline),
        "persisted_plan_digest": plan_digest(expected),
        "persisted_plan_revision": 1,
        "stored_journal_head": final_history[-1].event_hash,
        "changed_decision_id": decision_id,
        "old_candidate_id": patch.old_candidate_id,
        "new_candidate_id": patch.new_candidate_id,
        "pending_recheck_ids": list(final_pending),
        "request_id": request_id,
        "idempotent_replay": replayed,
        "source_mode": "RECORDED_REDACTED",
        "provider_authenticated": False,
        "fresh_provider_recheck": False,
        "overall_plan_verified": False,
        "release_authorized": False,
        "external_action_executed": False,
        "real_qloo_requests": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Persist one recorded rank repair with pending recheck; no action release"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--decision-id", required=True)
    parser.add_argument("--artist-id", required=True)
    parser.add_argument("--brands", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--acknowledge", required=True)
    parser.add_argument("--age-hours", type=int, default=24)
    args = parser.parse_args(argv)

    try:
        if not 1 <= args.age_hours <= 72:
            raise AgentEvidenceRejected("INVALID_AGE_WINDOW")
        info = args.database.lstat()
        if not S_ISREG(info.st_mode) or info.st_size == 0:
            raise AgentEvidenceRejected("UNSAFE_DATABASE")
        proposal = AgentProposal.model_validate_json(
            json.dumps(_read_private_json(args.proposal, max_bytes=96 * 1024))
        )
        capture = _read_private_json(args.capture, max_bytes=1024 * 1024)
        result = commit_recorded_repair(
            SQLiteRevisionStore(args.database), proposal, capture,
            decision_id=args.decision_id, artist_id=args.artist_id,
            brand_ids=tuple(args.brands.split(",")),
            request_id=args.request_id, acknowledgement=args.acknowledge,
            as_of=datetime.now(timezone.utc),
            max_evidence_age=timedelta(hours=args.age_hours),
        )
    except (
        OSError, ValueError, TypeError, UnicodeError, ValidationError,
        StorageIntegrityError, sqlite3.Error, RevisionConflict, PatchRejected,
        IdempotencyConflict,
    ):
        print(json.dumps({
            "status": "BLOCKED", "release_authorized": False,
            "external_action_executed": False, "real_qloo_requests": 0,
        }, sort_keys=True))
        return 2

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
