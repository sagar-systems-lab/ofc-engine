"""Read-only operator evidence for the authenticated local agent API."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Mapping

from .durable import SQLiteRevisionStore
from .intake import AgentProposal, freeze_proposal
from .qloo_agent_bridge import _read_private_json
from .qloo_decision_gate import _load_sources, _sources, run_integrated_gate
from .qloo import brand_affinity_probe
from .qloo_affinity import normalize_affinity_capture
from .qloo_flow import RecordedSource


@dataclass(frozen=True)
class OperatorEvidenceSession:
    proposal: AgentProposal
    original: Mapping[str, RecordedSource]
    later: Mapping[str, RecordedSource] | None
    max_evidence_age: timedelta
    historical: bool = False

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
        plan, _ = freeze_proposal(proposal)
        _sources(plan, original)
        if later is not None:
            _sources(plan, later)
        return cls(proposal, original, later, timedelta(hours=age_hours))

    @classmethod
    def from_capture_files(
        cls,
        *,
        proposal_path: Path,
        original_capture: Path,
        later_capture: Path,
        decision_id: str,
        artist_id: str,
        brand_ids: tuple[str, ...],
        age_hours: int = 24,
        historical: bool = False,
    ) -> OperatorEvidenceSession:
        if type(age_hours) is not int or not 1 <= age_hours <= 72:
            raise ValueError("invalid evidence age window")
        proposal = AgentProposal.model_validate_json(
            json.dumps(_read_private_json(proposal_path, max_bytes=96 * 1024))
        )
        spec = brand_affinity_probe(artist_id, brand_ids)
        original = {decision_id: RecordedSource(
            _read_private_json(original_capture, max_bytes=1024 * 1024), spec
        )}
        later = {decision_id: RecordedSource(
            _read_private_json(later_capture, max_bytes=1024 * 1024), spec
        )}
        plan, _ = freeze_proposal(proposal)
        _sources(plan, original)
        _sources(plan, later)
        return cls(proposal, original, later, timedelta(hours=age_hours), historical)

    def snapshot(self, store: SQLiteRevisionStore, run_id: str) -> dict:
        if run_id != self.proposal.intent.run_id:
            raise KeyError("unknown evidence session")
        from .agent_run import review_run
        return review_run(
            store, self.proposal, self.original,
            as_of=datetime.now(timezone.utc),
            max_evidence_age=self.max_evidence_age,
            later_sources=self.later,
        ).model_dump(mode="json")

    def review(self, store: SQLiteRevisionStore, run_id: str) -> dict:
        if run_id != self.proposal.intent.run_id:
            raise KeyError("unknown evidence session")
        now = datetime.now(timezone.utc)
        as_of = now
        if self.historical:
            captures = [
                normalize_affinity_capture(source.capture, source.spec)
                for source in (*self.original.values(), *((self.later or {}).values()))
            ]
            if not captures:
                raise ValueError("historical replay requires evidence")
            collected = [
                datetime.fromisoformat(item["collected_at"]) for item in captures
            ]
            if max(collected) > now:
                raise ValueError("capture from the future")
            as_of = min(now, max(collected) + timedelta(minutes=1))
        return run_integrated_gate(
            store,
            self.proposal,
            self.original,
            as_of=as_of,
            max_evidence_age=self.max_evidence_age,
            recheck_sources=self.later,
            historical=self.historical,
        )
