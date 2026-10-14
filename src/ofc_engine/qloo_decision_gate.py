"""Recorded Qloo decision gate for a frozen, durably stored agent plan."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from stat import S_ISREG
import sqlite3
from typing import Any, Mapping

from pydantic import ValidationError

from .contracts import RepairPatch, Verdict
from .durable import SQLiteRevisionStore, StorageIntegrityError
from .intake import AgentProposal, freeze_proposal
from .proof import check_review_integrity
from .qloo import brand_affinity_probe
from .qloo_affinity import AffinityCaptureError, normalize_affinity_capture
from .qloo_agent_bridge import AgentEvidenceRejected, _read_private_json
from .qloo_flow import RecordedSource, review_recorded_plan
from .repair import RepairReviewRejected, begin_repair_ledger, preview_repair
from .revisions import PatchRejected, RevisionConflict, affected_dependents, digest_obj, plan_digest
from .store import IdempotencyConflict

_ACK = "COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE"
_REQUEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _state(store: SQLiteRevisionStore, plan_id: str):
    plan = store.get(plan_id)
    history = store.audit_history(plan_id)
    pending = store.pending_rechecks(plan_id)
    return plan, history, pending


def _sources(plan, sources: Mapping[str, RecordedSource]) -> None:
    ids = {d.decision_id for d in plan.decisions}
    if not isinstance(sources, Mapping) or not set(sources).issubset(ids):
        raise AgentEvidenceRejected("UNKNOWN_EVIDENCE_SOURCE")
    fingerprints = set()
    for decision in plan.decisions:
        if decision.decision_id not in sources:
            continue
        source = sources[decision.decision_id]
        if not isinstance(source, RecordedSource):
            raise AgentEvidenceRejected("INVALID_EVIDENCE_SOURCE")
        if (
            decision.domain not in ("brand", "partner")
            or decision.locality_ref is not None
            or decision.depends_on
        ):
            raise AgentEvidenceRejected("UNSUPPORTED_DEPENDENT_OR_LOCAL_DECISION")
        params = dict(source.spec.params)
        if (
            source.spec.path != "/v2/insights"
            or params.get("filter.type") != "urn:entity:brand"
            or set(params.get("filter.results.entities", "").split(",")) != set(decision.candidate_pool)
            or len(params.get("filter.results.entities", "").split(",")) != len(decision.candidate_pool)
            or source.spec.fingerprint in fingerprints
        ):
            raise AgentEvidenceRejected("EVIDENCE_SCOPE_MISMATCH")
        fingerprints.add(source.spec.fingerprint)


def _rank_top(source: RecordedSource) -> str:
    observed = normalize_affinity_capture(source.capture, source.spec)
    return observed["ranking"][0]["entity_id"]


def _result(plan, review, pending, *, status, reasons=(), changed=None,
            journal_head="", historical=False) -> dict[str, Any]:
    return {
        "status": status,
        "plan_id": plan.plan_id,
        "revision": plan.revision,
        "plan_digest": plan_digest(plan),
        "journal_head": journal_head,
        "source_mode": "RECORDED_REDACTED",
        "historical_replay": historical,
        "decision_reviews": [
            {
                "decision_id": v.decision_id,
                "verdict": v.verdict.value,
                "reason_codes": list(v.reason_codes),
                "evidence_count": len(v.evidence_refs),
            } for v in review.verdicts
        ],
        "changed_decision_id": changed,
        "reason_codes": list(reasons),
        "pending_recheck_ids": list(pending),
        "recorded_rank_recheck_pass": status == "RECORDED_RANK_RECHECK_PASS_NO_RELEASE",
        "provider_authenticated": False,
        "independent_recheck_completed": False,
        "overall_plan_verified": False,
        "release_authorized": False,
        "external_action_executed": False,
        "real_qloo_requests": 0,
    }


def run_integrated_gate(
    store: SQLiteRevisionStore,
    proposal: AgentProposal,
    sources: Mapping[str, RecordedSource],
    *,
    as_of: datetime,
    max_evidence_age: timedelta,
    commit: bool = False,
    request_id: str | None = None,
    acknowledgement: str | None = None,
    recheck_sources: Mapping[str, RecordedSource] | None = None,
    historical: bool = False,
) -> dict[str, Any]:
    """Evaluate, optionally persist one bounded repair and inspect recorded rechecks.

    Captures are operator-supplied; recorded ranks never grant release authority.
    """
    if as_of.utcoffset() is None or max_evidence_age <= timedelta(0):
        raise AgentEvidenceRejected("INVALID_EVIDENCE_WINDOW")
    if commit and (acknowledgement != _ACK or not request_id or not _REQUEST.fullmatch(request_id)):
        raise AgentEvidenceRejected("EXPLICIT_COMMIT_ACK_REQUIRED")
    if recheck_sources is not None and commit:
        raise AgentEvidenceRejected("COMMIT_AND_RECHECK_MUST_BE_SEPARATE")
    baseline, _ = freeze_proposal(proposal)
    _sources(baseline, sources)
    if recheck_sources is not None:
        _sources(baseline, recheck_sources)
        if commit:
            raise AgentEvidenceRejected("RECHECK_MUST_BE_READ_ONLY")
    current, history, pending = _state(store, baseline.plan_id)
    if (
        current.baseline_digest != baseline.baseline_digest
        or current.constraints_digest != baseline.constraints_digest
        or len(history) != current.revision + 1
        or history[0].after_digest != plan_digest(baseline)
    ):
        raise AgentEvidenceRejected("STORED_BASELINE_CONFLICT")

    review = review_recorded_plan(
        current, sources, as_of=as_of, max_evidence_age=max_evidence_age,
    )
    if current == baseline:
        if current.revision != 0 or len(history) != 1 or pending:
            raise AgentEvidenceRejected("STORED_BASELINE_NOT_CLEAN")
        integrity = check_review_integrity(
            baseline, review, as_of=as_of, max_evidence_age=max_evidence_age,
        )
        repairs = [v for v in review.verdicts if v.verdict == Verdict.REPAIR]
        others = [v for v in review.verdicts if v.verdict != Verdict.PASS and v.verdict != Verdict.REPAIR]
        if others or len(repairs) != 1 or {
            issue.code for issue in integrity.issues
        } != {"REPAIR_REQUIRED"}:
            if commit:
                raise AgentEvidenceRejected("RECORDED_PLAN_NOT_REPAIR_QUALIFIED")
            return _result(
                current, review, pending, status="REVIEW_REQUIRED",
                reasons=tuple(sorted({i.code for i in integrity.issues})),
                journal_head=history[-1].event_hash, historical=historical,
            )
        target = repairs[0].decision_id
        decision = next(d for d in current.decisions if d.decision_id == target)
        if decision.depends_on or affected_dependents(current, target) != (target,) or (
            "candidate_id" not in decision.mutable_fields
        ):
            raise AgentEvidenceRejected("DEPENDENT_OR_IMMUTABLE_REPAIR_UNQUALIFIED")
        source = sources[target]
        alternative = _rank_top(source)
        if alternative == decision.candidate_id:
            raise AgentEvidenceRejected("REPAIR_RANK_MISMATCH")
        patch = RepairPatch(
            patch_id=f"recorded:{baseline.plan_id}:0:{target}",
            decision_id=target, base_revision=0,
            old_candidate_id=decision.candidate_id,
            new_candidate_id=alternative,
            evidence_refs=repairs[0].evidence_refs,
            precondition_digest=plan_digest(baseline),
        )
        preview = preview_repair(
            current, begin_repair_ledger(current), review, patch,
            as_of=as_of, max_evidence_age=max_evidence_age,
        )
        if preview.affected_decision_ids != (target,):
            raise AgentEvidenceRejected("REPAIR_INVALIDATION_MISMATCH")
        if not commit:
            return _result(
                current, review, pending,
                status="RECORDED_REPAIR_PROPOSED",
                reasons=("REQUIRES_EXPLICIT_COMMIT_AND_RECHECK",),
                changed=target, journal_head=history[-1].event_hash,
                historical=historical,
            )
        saved, invalidated = store.apply_patch_once(
            current.plan_id, patch, client_request_id=request_id,
        )
        after, events, pending_after = _state(store, current.plan_id)
        if (
            after != preview.proposed_plan or saved != after
            or invalidated != (target,) or pending_after != (target,)
            or len(events) != 2 or events[-1].request_id != request_id
        ):
            raise AgentEvidenceRejected("RECORDED_COMMIT_POSTCONDITION_FAILED")
        next_review = review_recorded_plan(
            after, sources, as_of=as_of, max_evidence_age=max_evidence_age,
        )
        return _result(
            after, next_review, pending_after,
            status="RECORDED_REPAIR_STORED_RECHECK_PENDING",
            reasons=("NO_INDEPENDENT_RECHECK",), changed=target,
            journal_head=events[-1].event_hash, historical=historical,
        )

    if current.revision != 1 or len(history) != 2 or history[-1].kind != "PATCH":
        raise AgentEvidenceRejected("UNSUPPORTED_STORED_REVISION")
    changed = [
        d.decision_id for original, d in zip(baseline.decisions, current.decisions)
        if original.candidate_id != d.candidate_id
    ]
    if len(changed) != 1:
        raise AgentEvidenceRejected("STORED_REPAIR_NOT_SINGLE")
    changed_id = changed[0]
    expected = baseline.model_copy(update={
        "revision": 1,
        "decisions": tuple(
            d.model_copy(update={"candidate_id": next(x.candidate_id for x in current.decisions if x.decision_id == d.decision_id)})
            if d.decision_id == changed_id else d
            for d in baseline.decisions
        ),
    })
    if (
        current != expected
        or history[-1].invalidated_ids != (changed_id,)
        or pending != (changed_id,)
        or changed_id not in sources
    ):
        raise AgentEvidenceRejected("STORED_REPAIR_STATE_CONFLICT")
    original = sources[changed_id]
    if _rank_top(original) != next(d.candidate_id for d in current.decisions if d.decision_id == changed_id):
        raise AgentEvidenceRejected("ORIGINAL_RANK_DOES_NOT_EXPLAIN_REPAIR")
    if recheck_sources is None:
        return _result(
            current, review, pending, status="FRESH_RECHECK_REQUIRED",
            reasons=("RECHECK_NOT_SUPPLIED",), changed=changed_id,
            journal_head=history[-1].event_hash, historical=historical,
        )
    if set(recheck_sources) != {changed_id} or recheck_sources[changed_id].spec != original.spec:
        raise AgentEvidenceRejected("RECHECK_SCOPE_MISMATCH")
    next_source = recheck_sources[changed_id]
    before = normalize_affinity_capture(original.capture, original.spec)
    after_observed = normalize_affinity_capture(next_source.capture, next_source.spec)
    first_collected = datetime.fromisoformat(before["collected_at"])
    next_collected = datetime.fromisoformat(after_observed["collected_at"])
    if (
        digest_obj(original.capture) == digest_obj(next_source.capture)
        or next_collected <= first_collected
    ):
        reason = "RECHECK_CAPTURE_REPLAYED_OR_NOT_LATER"
    else:
        updated_sources = dict(sources)
        updated_sources[changed_id] = next_source
        updated_review = review_recorded_plan(
            current, updated_sources, as_of=as_of,
            max_evidence_age=max_evidence_age,
        )
        issues = check_review_integrity(
            current, updated_review, as_of=as_of,
            max_evidence_age=max_evidence_age,
        ).issues
        reason = "RECORDED_RANK_RECHECK_PASS_NO_RELEASE" if (
            not issues and all(v.verdict == Verdict.PASS for v in updated_review.verdicts)
        ) else "RECHECK_REVIEW_REQUIRED"
        review = updated_review
    after_state, after_history, after_pending = _state(store, baseline.plan_id)
    if (
        after_state != current
        or after_history[-1].event_hash != history[-1].event_hash
        or after_pending != pending
    ):
        raise AgentEvidenceRejected("CONCURRENT_STORED_STATE_CHANGE")
    return _result(
        current, review, pending, status=reason,
        reasons=("PROVIDER_INDEPENDENCE_NOT_ESTABLISHED",),
        changed=changed_id, journal_head=history[-1].event_hash,
        historical=historical,
    )


def _load_sources(index: Path, proposal: AgentProposal) -> dict[str, RecordedSource]:
    document = _read_private_json(index, max_bytes=48 * 1024)
    if not isinstance(document, dict) or set(document) != {"decisions"}:
        raise AgentEvidenceRejected("INVALID_PRIVATE_EVIDENCE_INDEX")
    entries = document["decisions"]
    if not isinstance(entries, dict) or len(entries) > 64:
        raise AgentEvidenceRejected("INVALID_PRIVATE_EVIDENCE_INDEX")
    known = {d.decision_id for d in proposal.decisions}
    if not set(entries).issubset(known):
        raise AgentEvidenceRejected("UNKNOWN_EVIDENCE_SOURCE")
    result = {}
    for decision_id, item in entries.items():
        if not isinstance(item, dict) or set(item) != {"artist_id", "brand_ids", "capture_file"}:
            raise AgentEvidenceRejected("INVALID_EVIDENCE_SOURCE")
        if (
            not isinstance(item["artist_id"], str)
            or not isinstance(item["brand_ids"], list)
            or not all(isinstance(x, str) for x in item["brand_ids"])
            or not isinstance(item["capture_file"], str)
        ):
            raise AgentEvidenceRejected("INVALID_EVIDENCE_SOURCE")
        source_path = Path(item["capture_file"])
        if not source_path.is_absolute():
            raise AgentEvidenceRejected("CAPTURE_PATH_MUST_BE_ABSOLUTE")
        spec = brand_affinity_probe(item["artist_id"], tuple(item["brand_ids"]))
        result[decision_id] = RecordedSource(
            capture=_read_private_json(source_path, max_bytes=1024 * 1024),
            spec=spec,
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review, repair and recheck one stored plan with private recorded Qloo evidence")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--evidence-index", type=Path, required=True)
    parser.add_argument("--recheck-index", type=Path)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--request-id")
    parser.add_argument("--acknowledge")
    parser.add_argument("--historical", action="store_true")
    parser.add_argument("--age-hours", type=int, default=24)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.age_hours <= 72:
            raise AgentEvidenceRejected("INVALID_EVIDENCE_WINDOW")
        stat = args.database.lstat()
        if not S_ISREG(stat.st_mode) or stat.st_size == 0:
            raise AgentEvidenceRejected("UNSAFE_DATABASE")
        proposal = AgentProposal.model_validate_json(
            json.dumps(_read_private_json(args.proposal, max_bytes=96 * 1024))
        )
        sources = _load_sources(args.evidence_index, proposal)
        recheck = _load_sources(args.recheck_index, proposal) if args.recheck_index else None
        as_of = datetime.now(timezone.utc)
        if args.historical and sources:
            collected = [
                datetime.fromisoformat(normalize_affinity_capture(s.capture, s.spec)["collected_at"])
                for s in [*sources.values(), *(recheck or {}).values()]
            ]
            if max(collected) > as_of:
                raise AgentEvidenceRejected("CAPTURE_FROM_FUTURE")
            as_of = max(collected) + timedelta(minutes=1)
        result = run_integrated_gate(
            SQLiteRevisionStore(args.database), proposal, sources,
            as_of=as_of, max_evidence_age=timedelta(hours=args.age_hours),
            commit=args.commit, request_id=args.request_id,
            acknowledgement=args.acknowledge, recheck_sources=recheck,
            historical=args.historical,
        )
    except (
        OSError, ValueError, TypeError, UnicodeError, ValidationError,
        StorageIntegrityError, sqlite3.Error, RevisionConflict,
        PatchRejected, IdempotencyConflict, RepairReviewRejected,
        AffinityCaptureError,
    ):
        print(json.dumps({
            "status": "BLOCKED", "source_mode": "RECORDED_REDACTED",
            "release_authorized": False, "real_qloo_requests": 0,
            "external_action_executed": False,
        }, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
