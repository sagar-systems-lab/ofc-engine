"""Read-only qualification of a stored repair against recorded rank evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from stat import S_ISREG
from typing import Any

from pydantic import ValidationError

from .durable import SQLiteRevisionStore, StorageIntegrityError
from .intake import AgentProposal, freeze_proposal
from .qloo import brand_affinity_probe
from .qloo_affinity import AffinityCaptureError, normalize_affinity_capture
from .qloo_agent_bridge import AgentEvidenceRejected, _read_private_json
from .qloo_review import evaluate_recorded_affinity
from .revisions import digest_obj, plan_digest


def inspect_recorded_recheck(
    store: SQLiteRevisionStore,
    proposal: AgentProposal,
    original_capture: dict[str, Any],
    recheck_capture: dict[str, Any],
    *,
    decision_id: str,
    artist_id: str,
    brand_ids: tuple[str, ...],
    as_of: datetime,
    max_evidence_age: timedelta,
) -> dict[str, Any]:
    """Inspect recorded rank evidence; never clear persisted pending rechecks."""
    if as_of.utcoffset() is None or max_evidence_age <= timedelta(0):
        raise AgentEvidenceRejected("INVALID_EVIDENCE_CLOCK")
    baseline, _ = freeze_proposal(proposal)
    spec = brand_affinity_probe(artist_id, brand_ids)
    if len(baseline.decisions) != 1:
        raise AgentEvidenceRejected("UNSUPPORTED_MULTI_DECISION_RECHECK")
    original = baseline.decisions[0]
    if (
        original.decision_id != decision_id
        or original.domain not in ("partner", "brand")
        or original.locality_ref is not None
        or original.depends_on
        or len(original.candidate_pool) != len(brand_ids)
        or set(original.candidate_pool) != set(brand_ids)
    ):
        raise AgentEvidenceRejected("RECORDED_QUERY_SCOPE_MISMATCH")

    def state():
        try:
            plan = store.get(baseline.plan_id)
            journal = store.audit_history(baseline.plan_id)
            pending = store.pending_rechecks(baseline.plan_id)
        except KeyError:
            raise AgentEvidenceRejected("STORED_PLAN_MISSING") from None
        except (StorageIntegrityError, sqlite3.Error):
            raise AgentEvidenceRejected("STORED_JOURNAL_INVALID") from None
        if (
            plan.plan_id != baseline.plan_id
            or plan.revision != 1
            or plan.baseline_digest != baseline.baseline_digest
            or plan.constraints_digest != baseline.constraints_digest
            or len(plan.decisions) != 1
            or plan.decisions[0].candidate_pool != original.candidate_pool
            or plan.decisions[0].decision_id != decision_id
            or plan.decisions[0].candidate_id == original.candidate_id
            or len(journal) != 2
            or journal[0].kind != "INSERT"
            or journal[0].after_digest != plan_digest(baseline)
            or journal[1].kind != "PATCH"
            or journal[1].from_revision != 0
            or journal[1].after_digest != plan_digest(plan)
            or journal[1].invalidated_ids != (decision_id,)
            or pending != (decision_id,)
        ):
            raise AgentEvidenceRejected("STORED_REPAIR_STATE_MISMATCH")
        return plan, journal[1].event_hash

    plan, head_before = state()
    original_data = normalize_affinity_capture(original_capture, spec)
    recheck_data = normalize_affinity_capture(recheck_capture, spec)
    original_ranks = {x["entity_id"]: x["rank"] for x in original_data["ranking"]}
    if (
        original_ranks[original.candidate_id] == 1
        or original_ranks[plan.decisions[0].candidate_id] != 1
    ):
        raise AgentEvidenceRejected("ORIGINAL_RANKS_DO_NOT_EXPLAIN_REPAIR")

    review = evaluate_recorded_affinity(
        plan, decision_id=decision_id, capture=recheck_capture, spec=spec,
        as_of=as_of, max_evidence_age=max_evidence_age,
    )
    verdict = review.verdicts[0]
    same_capture = digest_obj(original_capture) == digest_obj(recheck_capture)
    first_time = datetime.fromisoformat(original_data["collected_at"])
    next_time = datetime.fromisoformat(recheck_data["collected_at"])
    after_original = next_time > first_time
    rank_pass = verdict.verdict.value == "PASS"
    if same_capture:
        status = "ORIGINAL_CAPTURE_REPLAY_ONLY"
    elif not after_original:
        status = "RECORDED_CAPTURE_NOT_LATER"
    elif not rank_pass:
        status = "RECORDED_RECHECK_UNRESOLVED"
    else:
        status = "RECORDED_LATER_CAPTURE_RANK_PASS_ONLY"

    final, head_after = state()
    if final != plan or head_after != head_before:
        raise AgentEvidenceRejected("STORED_STATE_CHANGED_DURING_RECHECK")
    return {
        "status": status,
        "plan_id": plan.plan_id,
        "stored_plan_revision": plan.revision,
        "stored_plan_digest": plan_digest(plan),
        "stored_journal_head": head_after,
        "decision_id": decision_id,
        "original_candidate_id": original.candidate_id,
        "persisted_candidate_id": plan.decisions[0].candidate_id,
        "recorded_rank_verdict": verdict.verdict.value,
        "recorded_rank_reason_codes": list(verdict.reason_codes),
        "same_capture": same_capture,
        "later_capture_observed": after_original,
        "provider_independence_established": False,
        "provider_authenticated": False,
        "fresh_provider_recheck": False,
        "independent_recheck_completed": False,
        "pending_recheck_ids": [decision_id],
        "pending_recheck_cleared": False,
        "stored_plan_mutated": False,
        "overall_plan_verified": False,
        "release_authorized": False,
        "external_action_executed": False,
        "source_mode": "RECORDED_REDACTED",
        "real_qloo_requests": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only recorded rank diagnostic for a committed repair"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--original-capture", type=Path, required=True)
    parser.add_argument("--recheck-capture", type=Path, required=True)
    parser.add_argument("--decision-id", required=True)
    parser.add_argument("--artist-id", required=True)
    parser.add_argument("--brands", required=True)
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
        result = inspect_recorded_recheck(
            SQLiteRevisionStore(args.database), proposal,
            _read_private_json(args.original_capture, max_bytes=1024 * 1024),
            _read_private_json(args.recheck_capture, max_bytes=1024 * 1024),
            decision_id=args.decision_id, artist_id=args.artist_id,
            brand_ids=tuple(args.brands.split(",")),
            as_of=datetime.now(timezone.utc),
            max_evidence_age=timedelta(hours=args.age_hours),
        )
    except (
        OSError, ValueError, TypeError, UnicodeError, ValidationError,
        AffinityCaptureError, StorageIntegrityError, sqlite3.Error,
    ):
        print(json.dumps({
            "status": "BLOCKED", "release_authorized": False,
            "pending_recheck_cleared": False,
            "real_qloo_requests": 0,
        }, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
