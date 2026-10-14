"""Offline Qloo evidence review of an immutable agent proposal."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from stat import S_ISREG
import sqlite3
from typing import Any

from pydantic import ValidationError

from .durable import SQLiteRevisionStore, StorageIntegrityError
from .intake import AgentProposal, freeze_proposal
from .qloo import brand_affinity_probe
from .qloo_flow import RecordedSource, compare_recorded_arms
from .revisions import plan_digest


class AgentEvidenceRejected(ValueError):
    pass


def compare_agent_proposal(
    proposal: AgentProposal,
    capture: dict[str, Any],
    *,
    decision_id: str,
    artist_id: str,
    brand_ids: tuple[str, ...],
    as_of: datetime,
    max_evidence_age: timedelta,
) -> dict[str, Any]:
    """Compare exact frozen agent choices, not a fabricated demonstration plan."""
    if not isinstance(proposal, AgentProposal):
        raise AgentEvidenceRejected("INVALID_AGENT_PROPOSAL")
    if as_of.utcoffset() is None or max_evidence_age <= timedelta(0):
        raise AgentEvidenceRejected("INVALID_EVIDENCE_CLOCK")
    baseline, candidate_pool_digest = freeze_proposal(proposal)
    found = [d for d in baseline.decisions if d.decision_id == decision_id]
    if len(found) != 1:
        raise AgentEvidenceRejected("DECISION_NOT_FOUND")
    decision = found[0]
    if decision.domain not in ("brand", "partner"):
        raise AgentEvidenceRejected("UNSUPPORTED_DECISION_DOMAIN")
    spec = brand_affinity_probe(artist_id, brand_ids)
    if (len(decision.candidate_pool) != len(brand_ids)
            or set(decision.candidate_pool) != set(brand_ids)):
        raise AgentEvidenceRejected("CANDIDATE_POOL_MISMATCH")

    comparison = compare_recorded_arms(
        baseline, {decision_id: RecordedSource(capture, spec)},
        as_of=as_of, max_evidence_age=max_evidence_age, max_attempts=2,
    )
    if not comparison.same_baseline or comparison.baseline_digest != plan_digest(baseline):
        raise AgentEvidenceRejected("BASELINE_MISMATCH")
    if not (
        comparison.disabled.source_mode.value == "RECORDED_REDACTED"
        and comparison.enabled.source_mode.value == "RECORDED_REDACTED"
    ):
        raise AgentEvidenceRejected("SOURCE_MODE_MISMATCH")
    return {
        "status": "RECORDED_AGENT_PLAN_REVIEW",
        "plan_id": baseline.plan_id,
        "plan_revision": baseline.revision,
        "baseline_plan_digest": plan_digest(baseline),
        "baseline_digest": baseline.baseline_digest,
        "constraints_digest": baseline.constraints_digest,
        "candidate_pool_digest": candidate_pool_digest,
        "decision_id": decision_id,
        "query_fingerprint": spec.fingerprint,
        "mapping_basis": "OPERATOR_SUPPLIED_ARTIST_ID_NOT_IDENTITY_ATTESTED",
        "review_scope": "FIXED_POOL_RECORDED_BRAND_AFFINITY_ONLY",
        "comparison": comparison.as_dict(),
        "overall_plan_verified": False,
        "fresh_provider_recheck": False,
        "provider_authenticated": False,
        "release_authorized": False,
        "external_action_executed": False,
        "real_qloo_requests": 0,
    }




def compare_stored_agent_proposal(
    store: SQLiteRevisionStore,
    proposal: AgentProposal,
    capture: dict[str, Any],
    *,
    decision_id: str,
    artist_id: str,
    brand_ids: tuple[str, ...],
    as_of: datetime,
    max_evidence_age: timedelta,
) -> dict[str, Any]:
    """Review an existing revision-zero intake without applying the preview."""
    baseline, _ = freeze_proposal(proposal)

    def checked_state() -> tuple[str, str]:
        try:
            saved = store.get(baseline.plan_id)
            history = store.audit_history(baseline.plan_id)
            pending = store.pending_rechecks(baseline.plan_id)
        except KeyError:
            raise AgentEvidenceRejected("STORED_PLAN_MISSING") from None
        except (StorageIntegrityError, sqlite3.Error):
            raise AgentEvidenceRejected("STORED_JOURNAL_INVALID") from None
        expected = plan_digest(baseline)
        if saved != baseline or saved.revision != 0 or plan_digest(saved) != expected:
            raise AgentEvidenceRejected("STORED_BASELINE_CONFLICT")
        if (len(history) != 1 or history[0].kind != "INSERT"
                or history[0].after_digest != expected or pending):
            raise AgentEvidenceRejected("STORED_PLAN_NOT_CLEAN_BASELINE")
        return expected, history[0].event_hash

    digest_before, journal_before = checked_state()
    result = compare_agent_proposal(
        proposal, capture, decision_id=decision_id,
        artist_id=artist_id, brand_ids=brand_ids,
        as_of=as_of, max_evidence_age=max_evidence_age,
    )
    digest_after, journal_after = checked_state()
    if (digest_before != digest_after or journal_before != journal_after
            or result["baseline_plan_digest"] != digest_before):
        raise AgentEvidenceRejected("STORED_STATE_CHANGED_DURING_REVIEW")
    return {
        **result,
        "stored_baseline_matched": True,
        "stored_plan_revision": 0,
        "stored_journal_event_count": 1,
        "stored_journal_head": journal_after,
        "persisted_repair_committed": False,
        "stored_plan_mutated": False,
    }

def _read_private_json(path: Path, *, max_bytes: int) -> dict[str, Any]:
    info = path.lstat()
    if not S_ISREG(info.st_mode) or not 0 < info.st_size <= max_bytes:
        raise AgentEvidenceRejected("UNSAFE_INPUT_FILE")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AgentEvidenceRejected("INVALID_INPUT_DOCUMENT")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate recorded Qloo ranking against a frozen local agent proposal; zero provider requests"
    )
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--decision-id", required=True)
    parser.add_argument("--artist-id", required=True)
    parser.add_argument("--brands", required=True)
    parser.add_argument("--age-hours", type=int, default=24)
    parser.add_argument("--database", type=Path, help="existing SQLite proposal-intake database")
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.age_hours <= 72:
            raise AgentEvidenceRejected("INVALID_AGE_WINDOW")
        raw_proposal = _read_private_json(args.proposal, max_bytes=96 * 1024)
        proposal = AgentProposal.model_validate_json(json.dumps(raw_proposal, ensure_ascii=False))
        capture = _read_private_json(args.capture, max_bytes=1024 * 1024)
        options = {
            "decision_id": args.decision_id,
            "artist_id": args.artist_id,
            "brand_ids": tuple(args.brands.split(",")),
            "as_of": datetime.now(timezone.utc),
            "max_evidence_age": timedelta(hours=args.age_hours),
        }
        if args.database is None:
            result = compare_agent_proposal(proposal, capture, **options)
        else:
            info = args.database.lstat()
            if not S_ISREG(info.st_mode) or info.st_size == 0:
                raise AgentEvidenceRejected("UNSAFE_DATABASE")
            store = SQLiteRevisionStore(args.database)
            result = compare_stored_agent_proposal(
                store, proposal, capture, **options
            )
    except (OSError, ValueError, TypeError, UnicodeError, ValidationError, StorageIntegrityError, sqlite3.Error):
        print(json.dumps({
            "status": "BLOCKED", "release_authorized": False,
            "external_action_executed": False, "real_qloo_requests": 0,
        }, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
