"""Read-only operator evidence for the authenticated local agent API."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Mapping

from .durable import SQLiteRevisionStore
from .intake import AgentProposal
from .qloo_agent_bridge import AgentEvidenceRejected, _read_private_json
from .qloo_decision_gate import _load_sources, _sources, run_integrated_gate
from .qloo_flow import RecordedSource


@dataclass(frozen=True)
class OperatorEvidenceSession:
    proposal: AgentProposal
    original: Mapping[str, RecordedSource]
    later: Mapping[str, RecordedSource] | None
    max_evidence_age: timedelta

    @classmethod
    def load(
        cls,
        *,
        proposal_path: Path,
        evidence_index: Path,
        recheck_index: Path | None = None,
        age_hours: int = 24,
    ) -> OperatorEvidenceSession:
        if type(age_hours) is not int or not 1 <= age_hours <= 72:
            raise ValueError("invalid evidence age window")
        proposal = AgentProposal.model_validate_json(
            json.dumps(_read_private_json(proposal_path, max_bytes=96 * 1024))
        )
        original = _load_sources(evidence_index, proposal)
        later = _load_sources(recheck_index, proposal) if recheck_index else None
        by_plan = tuple(proposal.decisions)
        from .intake import freeze_proposal
        plan, _ = freeze_proposal(proposal)
        _sources(plan, original)
        if later is not None:
            _sources(plan, later)
        return cls(proposal, original, later, timedelta(hours=age_hours))

    def review(self, store: SQLiteRevisionStore, run_id: str) -> dict:
        if run_id != self.proposal.intent.run_id:
            raise KeyError("unknown evidence session")
        return run_integrated_gate(
            store,
            self.proposal,
            self.original,
            as_of=datetime.now(timezone.utc),
            max_evidence_age=self.max_evidence_age,
            recheck_sources=self.later,
        )
