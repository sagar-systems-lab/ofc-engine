"""Bind one hosted Qloo observation to the existing immutable agent run."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from stat import S_ISREG

from .agent_run import commit_rank_repair, review_run
from .contracts import Verdict
from .durable import SQLiteRevisionStore
from .intake import AgentProposal, freeze_proposal
from .local_run_view import project_local_run
from .qloo import ProbeSpec
from .qloo_affinity import normalize_affinity_capture
from .qloo_flow import RecordedSource
from .revisions import RevisionConflict, plan_digest

_COMMIT_ACK = "COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE"


def load_hosted_proposal(
    path: Path, *, artist_id: str, brand_ids: tuple[str, ...],
    selected_brand_id: str,
) -> AgentProposal:
    info = path.lstat()
    if not S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe hosted proposal file")
    if info.st_size > 96 * 1024 or info.st_size == 0:
        raise ValueError("invalid hosted proposal size")
    proposal = AgentProposal.model_validate_json(path.read_bytes())
    baseline, _ = freeze_proposal(proposal)
    brand = next((d for d in baseline.decisions if d.decision_id == "brand"), None)
    if (
        brand is None or brand.domain not in ("brand", "partner")
        or brand.candidate_id != selected_brand_id
        or brand.candidate_pool != brand_ids
        or brand.mutable_fields != ("candidate_id",)
        or brand.depends_on or brand.locality_ref is not None
        or proposal.intent.taste_context.get("artist_id") != artist_id
        or proposal.intent.constraints.get("external_action") is not False
    ):
        raise ValueError("hosted agent proposal does not match frozen Qloo scope")
    if len(proposal.decisions) != 6:
        raise ValueError("hosted tour must contain six explicit decisions")
    return proposal


class HostedAgentFlow:
    def __init__(self, *, proposal: AgentProposal, database: Path) -> None:
        self.proposal = proposal
        self.store = SQLiteRevisionStore(database)
        baseline, _ = freeze_proposal(proposal)
        try:
            self.store.insert(baseline)
        except RevisionConflict:
            stored = self.store.get(baseline.plan_id)
            if (
                stored.baseline_digest != baseline.baseline_digest
                or stored.constraints_digest != baseline.constraints_digest
                or stored.revision not in (0, 1)
                or tuple(d.decision_id for d in stored.decisions)
                != tuple(d.decision_id for d in baseline.decisions)
            ):
                raise ValueError("hosted proposal state is bound to another baseline")
            self.store.audit_history(baseline.plan_id)

    def evaluate(self, capture: dict, spec: ProbeSpec) -> dict:
        source = RecordedSource(capture, spec)
        observed = normalize_affinity_capture(capture, spec)
        brand = next(
            d for d in self.proposal.decisions if d.decision_id == "brand"
        )
        now = datetime.now(timezone.utc)
        sources = {"brand": source}
        if observed["ranking"][0]["entity_id"] == brand.candidate_id:
            snapshot = review_run(
                self.store, self.proposal, sources,
                as_of=now, max_evidence_age=timedelta(hours=24),
            )
        else:
            snapshot = commit_rank_repair(
                self.store, self.proposal, sources,
                request_id="hosted-rank-" + self.proposal.intent.run_id,
                acknowledgement=_COMMIT_ACK,
                as_of=now, max_evidence_age=timedelta(hours=24),
            )
        if snapshot.release_authorized or snapshot.external_action_executed:
            raise ValueError("unexpected release from Qloo rank observation")
        report = project_local_run(
            snapshot,
            operator_constructed_hypotheses=(
                5 if self.proposal.intent.taste_context.get(
                    "supplemental_decisions"
                ) == "OPERATOR_CONSTRUCTED_UNVERIFIED" else 0
            ),
        )
        report["status"] = "HOSTED_AGENT_REVIEW"
        report["source_mode"] = "LIVE_HTTP_UNVERIFIED"
        report["real_qloo_requests"] = 1
        report["evidence_collected_at"] = observed["collected_at"]
        report["baseline_plan_digest"] = snapshot.baseline_plan_digest
        report["current_plan_digest"] = snapshot.plan_digest
        report["verification_status"] = snapshot.verification.status
        if report["release_authorized"] is not False:
            raise ValueError("hosted Qloo rank cannot authorize a handoff")
        return report
