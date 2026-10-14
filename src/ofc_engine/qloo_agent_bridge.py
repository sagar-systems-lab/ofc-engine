"""Offline Qloo evidence review of an immutable agent proposal."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from stat import S_ISREG
from typing import Any

from pydantic import ValidationError

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
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.age_hours <= 72:
            raise AgentEvidenceRejected("INVALID_AGE_WINDOW")
        raw_proposal = _read_private_json(args.proposal, max_bytes=96 * 1024)
        proposal = AgentProposal.model_validate_json(json.dumps(raw_proposal, ensure_ascii=False))
        capture = _read_private_json(args.capture, max_bytes=1024 * 1024)
        result = compare_agent_proposal(
            proposal, capture,
            decision_id=args.decision_id,
            artist_id=args.artist_id,
            brand_ids=tuple(args.brands.split(",")),
            as_of=datetime.now(timezone.utc),
            max_evidence_age=timedelta(hours=args.age_hours),
        )
    except (OSError, ValueError, TypeError, UnicodeError, ValidationError):
        print(json.dumps({
            "status": "BLOCKED", "release_authorized": False,
            "external_action_executed": False, "real_qloo_requests": 0,
        }, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
