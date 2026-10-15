"""Revision-bound agent decision review with recorded Qloo evidence."""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Mapping

from .contracts import (
    ReleaseArtifact, RepairPatch, SourceMode, StrictModel,
    VerificationResult, Verdict,
)
from .durable import SQLiteRevisionStore
from .intake import AgentProposal, freeze_proposal
from .qloo_affinity import AffinityCaptureError, normalize_affinity_capture
from .qloo_agent_bridge import AgentEvidenceRejected
from .qloo_decision_gate import _sources
from .qloo_flow import RecordedSource, review_recorded_plan
from .qloo_review import RECORDED_RANK_POLICY
from .revisions import (
    affected_dependents, apply_patch,
    digest_obj, plan_digest,
)

_ACK = "COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE"


class DecisionState(StrictModel):
    decision_id: str
    domain: str
    original_candidate_id: str
    candidate_id: str
    verdict: Verdict
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    required: bool
    depends_on: tuple[str, ...]
    pending_recheck: bool


class RunEvent(StrictModel):
    sequence: int
    kind: str
    revision: int
    plan_digest: str
    journal_hash: str
    invalidated_ids: tuple[str, ...]


class Ablation(StrictModel):
    same_baseline: bool
    baseline_plan_digest: str
    evidence_off_verdicts: dict[str, Verdict]
    evidence_on_verdicts: dict[str, Verdict]
    evidence_caused_change_ids: tuple[str, ...]
    business_outcomes_measured: bool = False
    real_causal_proof: bool = False


class RunSnapshot(StrictModel):
    run_id: str
    baseline_digest: str
    baseline_plan_digest: str
    plan_revision: int
    plan_digest: str
    policy_version: str = RECORDED_RANK_POLICY
    source_mode: SourceMode = SourceMode.RECORDED_REDACTED
    historical_replay: bool
    decision_verdicts: tuple[DecisionState, ...]
    evidence_refs: tuple[str, ...]
    changed_ids: tuple[str, ...]
    preserved_ids: tuple[str, ...]
    invalidated_ids: tuple[str, ...]
    pending_rechecks: tuple[str, ...]
    events: tuple[RunEvent, ...]
    recorded_rank_recheck_pass: bool
    release_state: str = "REVIEW_REQUIRED"
    blockers: tuple[str, ...]
    verification: VerificationResult
    handoff: ReleaseArtifact
    comparison: Ablation
    provider_authenticated: bool = False
    independent_recheck_completed: bool = False
    release_authorized: bool = False
    external_action_executed: bool = False
    real_qloo_requests: int = 0


def _stored(
    store: SQLiteRevisionStore, proposal: AgentProposal,
    sources: Mapping[str, RecordedSource],
):
    baseline, _ = freeze_proposal(proposal)
    _sources(baseline, sources)
    artist = proposal.intent.taste_context.get("artist_id")
    if artist is not None and (
        not isinstance(artist, str) or not artist
        or any(
            dict(source.spec.params).get("signal.interests.entities") != artist
            for source in sources.values()
        )
    ):
        raise AgentEvidenceRejected("ARTIST_EVIDENCE_SCOPE_MISMATCH")
    plan = store.get(baseline.plan_id)
    history = store.audit_history(baseline.plan_id)
    pending = store.pending_rechecks(baseline.plan_id)
    if (
        history[0].after_digest != plan_digest(baseline)
        or plan.baseline_digest != baseline.baseline_digest
        or plan.constraints_digest != baseline.constraints_digest
        or len(history) != plan.revision + 1
        or plan.revision not in (0, 1)
    ):
        raise AgentEvidenceRejected("STORED_BASELINE_CONFLICT")

    changed = tuple(
        old.decision_id for old, new in zip(baseline.decisions, plan.decisions)
        if old.candidate_id != new.candidate_id
    )
    if plan.revision == 0:
        if plan != baseline or pending or changed:
            raise AgentEvidenceRejected("STORED_BASELINE_CONFLICT")
    else:
        if len(changed) != 1:
            raise AgentEvidenceRejected("STORED_REPAIR_SCOPE_MISMATCH")
        target = changed[0]
        affected = affected_dependents(baseline, target)
        expected = baseline.model_copy(update={
            "revision": 1,
            "decisions": tuple(
                d.model_copy(update={
                    "candidate_id": next(
                        x.candidate_id for x in plan.decisions
                        if x.decision_id == d.decision_id
                    )
                }) if d.decision_id == target else d
                for d in baseline.decisions
            ),
        })
        if (
            plan != expected or history[1].kind != "PATCH"
            or history[1].invalidated_ids != affected
            or pending != affected or target not in sources
        ):
            raise AgentEvidenceRejected("STORED_REPAIR_STATE_CONFLICT")
    return baseline, plan, history, pending, changed


def commit_rank_repair(
    store: SQLiteRevisionStore,
    proposal: AgentProposal,
    sources: Mapping[str, RecordedSource],
    *,
    request_id: str,
    acknowledgement: str,
    as_of: datetime,
    max_evidence_age: timedelta,
) -> RunSnapshot:
    if (
        acknowledgement != _ACK or not isinstance(request_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", request_id)
    ):
        raise AgentEvidenceRejected("EXPLICIT_COMMIT_ACK_REQUIRED")
    baseline, plan, history, pending, changed = _stored(store, proposal, sources)
    review = review_recorded_plan(
        baseline, sources, as_of=as_of, max_evidence_age=max_evidence_age
    )
    repairs = [v for v in review.verdicts if v.verdict == Verdict.REPAIR]
    if len(repairs) != 1:
        raise AgentEvidenceRejected("SINGLE_EVIDENCED_REPAIR_REQUIRED")
    target = repairs[0]
    decision = next(d for d in baseline.decisions if d.decision_id == target.decision_id)
    if (
        decision.depends_on or "candidate_id" not in decision.mutable_fields
        or target.decision_id not in sources
    ):
        raise AgentEvidenceRejected("UNQUALIFIED_REPAIR_TARGET")
    source = sources[target.decision_id]
    try:
        observed = normalize_affinity_capture(source.capture, source.spec)
    except (AffinityCaptureError, ValueError, TypeError) as exc:
        raise AgentEvidenceRejected("ORIGINAL_RANK_UNQUALIFIED") from exc
    top = observed["ranking"][0]["entity_id"]
    if top == decision.candidate_id or top not in decision.candidate_pool:
        raise AgentEvidenceRejected("REPAIR_NOT_SUPPORTED_BY_RANK")
    patch = RepairPatch(
        patch_id=f"recorded:{baseline.plan_id}:0:{target.decision_id}",
        decision_id=target.decision_id,
        base_revision=0,
        old_candidate_id=decision.candidate_id,
        new_candidate_id=top,
        evidence_refs=target.evidence_refs,
        precondition_digest=plan_digest(baseline),
    )
    expected, invalidated = apply_patch(baseline, patch)
    if plan.revision == 1 and (
        plan != expected or history[1].request_id != request_id
        or pending != invalidated or changed != (target.decision_id,)
    ):
        raise AgentEvidenceRejected("REPAIR_REPLAY_CONFLICT")
    result, affected = store.apply_patch_once(
        baseline.plan_id, patch, client_request_id=request_id
    )
    if result != expected or affected != invalidated:
        raise AgentEvidenceRejected("COMMIT_POSTCONDITION_FAILED")
    return review_run(
        store, proposal, sources, as_of=as_of,
        max_evidence_age=max_evidence_age,
    )


def review_run(
    store: SQLiteRevisionStore,
    proposal: AgentProposal,
    sources: Mapping[str, RecordedSource],
    *,
    as_of: datetime,
    max_evidence_age: timedelta,
    later_sources: Mapping[str, RecordedSource] | None = None,
    historical: bool = False,
) -> RunSnapshot:
    if as_of.utcoffset() is None or max_evidence_age <= timedelta(0):
        raise ValueError("invalid evidence clock")
    baseline, plan, history, pending, changed = _stored(store, proposal, sources)
    if later_sources is not None:
        _sources(baseline, later_sources)
    off = review_recorded_plan(
        baseline, sources, as_of=as_of, max_evidence_age=max_evidence_age,
        evidence_enabled=False,
    )
    on = review_recorded_plan(
        baseline, sources, as_of=as_of, max_evidence_age=max_evidence_age,
    )
    current = review_recorded_plan(
        plan, sources, as_of=as_of, max_evidence_age=max_evidence_age,
    )
    current_verdicts = {v.decision_id: v for v in current.verdicts}
    status = False
    recheck_blocker = "RECHECK_NOT_SUPPLIED" if changed else "REPAIR_NOT_COMMITTED"
    if changed:
        target = changed[0]
        original = sources[target]
        original_rank = normalize_affinity_capture(original.capture, original.spec)
        if original_rank["ranking"][0]["entity_id"] != next(
            d.candidate_id for d in plan.decisions if d.decision_id == target
        ):
            raise AgentEvidenceRejected("STORED_REPAIR_NOT_EVIDENCE_SUPPORTED")
        if later_sources is not None and set(later_sources) == {target}:
            later = later_sources[target]
            if later.spec != original.spec:
                raise AgentEvidenceRejected("RECHECK_SCOPE_MISMATCH")
            try:
                ranked = normalize_affinity_capture(later.capture, later.spec)
            except (AffinityCaptureError, ValueError, TypeError):
                recheck_blocker = "RECORDED_RECHECK_UNQUALIFIED"
            else:
                captured_at = datetime.fromisoformat(ranked["collected_at"])
                original_at = datetime.fromisoformat(original_rank["collected_at"])
                if (
                    captured_at <= original_at
                    or digest_obj(later.capture) == digest_obj(original.capture)
                ):
                    recheck_blocker = "RECHECK_NOT_DISTINCT"
                elif captured_at > as_of or as_of - captured_at >= max_evidence_age:
                    recheck_blocker = "RECHECK_STALE_OR_FUTURE"
                elif on.verdicts[
                    next(i for i, v in enumerate(on.verdicts) if v.decision_id == target)
                ].verdict != Verdict.REPAIR or current_verdicts[target].verdict != Verdict.PASS:
                    recheck_blocker = "ORIGINAL_EVIDENCE_UNQUALIFIED"
                elif ranked["ranking"][0]["entity_id"] != next(
                    d.candidate_id for d in plan.decisions if d.decision_id == target
                ):
                    recheck_blocker = "RANK_RECHECK_UNRESOLVED"
                else:
                    status = True
                    recheck_blocker = "PROVIDER_INDEPENDENCE_NOT_ESTABLISHED"
        elif later_sources is not None:
            raise AgentEvidenceRejected("RECHECK_TARGET_MISMATCH")

    uncertain = tuple(
        f"UNCERTAIN:{v.decision_id}" for v in current.verdicts
        if v.verdict == Verdict.UNCERTAIN and
        next(d.required for d in plan.decisions if d.decision_id == v.decision_id)
    )
    blockers = tuple(dict.fromkeys((
        *uncertain, *(("PENDING_RECHECKS",) if pending else ()),
        recheck_blocker, "PROVIDER_AUTHENTICATION_NOT_ESTABLISHED",
        *(("HISTORICAL_REPLAY_ONLY",) if historical else ()),
    )))
    recorded = tuple(
        DecisionState(
            decision_id=d.decision_id, domain=d.domain,
            original_candidate_id=baseline.decisions[i].candidate_id,
            candidate_id=d.candidate_id,
            verdict=current_verdicts[d.decision_id].verdict,
            reason_codes=(
                ("DEPENDENCY_INVALIDATED",)
                if d.decision_id in pending and d.decision_id not in changed
                else current_verdicts[d.decision_id].reason_codes
            ),
            evidence_refs=current_verdicts[d.decision_id].evidence_refs,
            required=d.required, depends_on=d.depends_on,
            pending_recheck=d.decision_id in pending,
        ) for i, d in enumerate(plan.decisions)
    )
    events = tuple(RunEvent(
        sequence=e.sequence, kind=e.kind, revision=e.to_revision,
        plan_digest=e.after_digest, journal_hash=e.event_hash,
        invalidated_ids=e.invalidated_ids,
    ) for e in history)
    verification = VerificationResult(
        verified_revision=plan.revision, verified_plan_digest=plan_digest(plan),
        checked_decision_ids=changed if status else (),
        evidence_ids=tuple(sorted({
            ref for item in current.verdicts for ref in item.evidence_refs
        })),
        policy_version=RECORDED_RANK_POLICY, status="REVIEW_REQUIRED",
    )
    handoff = ReleaseArtifact(
        plan_id=plan.plan_id, revision=plan.revision,
        verified_plan_digest=plan_digest(plan),
        verification_status="REVIEW_REQUIRED",
        source_mode=SourceMode.RECORDED_REDACTED,
    )
    return RunSnapshot(
        run_id=plan.plan_id, baseline_digest=baseline.baseline_digest,
        baseline_plan_digest=plan_digest(baseline),
        plan_revision=plan.revision, plan_digest=plan_digest(plan),
        historical_replay=historical,
        decision_verdicts=recorded,
        evidence_refs=tuple(sorted({
            ref for item in on.verdicts for ref in item.evidence_refs
        })),
        changed_ids=changed,
        preserved_ids=tuple(d.decision_id for d in plan.decisions if d.decision_id not in changed),
        invalidated_ids=history[-1].invalidated_ids,
        pending_rechecks=pending,
        events=events, recorded_rank_recheck_pass=status,
        blockers=blockers, verification=verification,
        handoff=handoff,
        comparison=Ablation(
            same_baseline=True,
            baseline_plan_digest=plan_digest(baseline),
            evidence_off_verdicts={v.decision_id: v.verdict for v in off.verdicts},
            evidence_on_verdicts={v.decision_id: v.verdict for v in on.verdicts},
            evidence_caused_change_ids=tuple(
                v.decision_id for v in on.verdicts
                if v.decision_id in changed and v.verdict == Verdict.REPAIR
                and v.evidence_refs
            ),
        ),
    )
