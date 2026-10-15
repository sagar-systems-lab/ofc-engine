"""Inspect or persist one rank-backed agent repair without provider traffic."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

from pydantic import ValidationError

from .agent_run import commit_rank_repair, review_run
from .durable import SQLiteRevisionStore, StorageIntegrityError
from .intake import AgentProposal
from .qloo import brand_affinity_probe
from .qloo_affinity import AffinityCaptureError, normalize_affinity_capture
from .qloo_agent_bridge import AgentEvidenceRejected, _read_private_json
from .qloo_flow import RecordedSource
from .revisions import PatchRejected, RevisionConflict
from .store import IdempotencyConflict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recorded Qloo agent run, revision-bound and fail-closed")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--original-capture", type=Path, required=True)
    parser.add_argument("--later-capture", type=Path)
    parser.add_argument("--decision-id", default="brand")
    parser.add_argument("--artist-id", required=True)
    parser.add_argument("--brands", required=True)
    parser.add_argument("--age-hours", type=int, default=24)
    parser.add_argument("--historical", action="store_true")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--request-id")
    parser.add_argument("--acknowledge")
    args = parser.parse_args(argv)

    try:
        if not 1 <= args.age_hours <= 72:
            raise ValueError("invalid age window")
        if args.commit and (args.historical or args.later_capture):
            raise ValueError("historical and later recheck cannot accompany commit")
        proposal = AgentProposal.model_validate_json(json.dumps(
            _read_private_json(args.proposal, max_bytes=96 * 1024)
        ))
        original = _read_private_json(args.original_capture, max_bytes=1024 * 1024)
        spec = brand_affinity_probe(args.artist_id, tuple(args.brands.split(",")))
        sources = {args.decision_id: RecordedSource(original, spec)}
        later = None
        if args.later_capture is not None:
            later = {args.decision_id: RecordedSource(
                _read_private_json(args.later_capture, max_bytes=1024 * 1024), spec
            )}

        as_of = datetime.now(timezone.utc)
        if args.historical:
            observed = [normalize_affinity_capture(x.capture, x.spec)
                        for x in (*sources.values(), *(later or {}).values())]
            collected = [datetime.fromisoformat(item["collected_at"]) for item in observed]
            if max(collected) > as_of:
                raise ValueError("future capture")
            as_of = min(as_of, max(collected) + timedelta(minutes=1))
        store = SQLiteRevisionStore(args.database)
        if args.commit:
            result = commit_rank_repair(
                store, proposal, sources, request_id=args.request_id,
                acknowledgement=args.acknowledge, as_of=as_of,
                max_evidence_age=timedelta(hours=args.age_hours),
            )
        else:
            result = review_run(
                store, proposal, sources, later_sources=later,
                as_of=as_of, max_evidence_age=timedelta(hours=args.age_hours),
                historical=args.historical,
            )
    except (
        OSError, ValueError, TypeError, ValidationError,
        StorageIntegrityError, sqlite3.Error, PatchRejected, RevisionConflict,
        IdempotencyConflict, AffinityCaptureError,
    ):
        print(json.dumps({
            "status": "BLOCKED", "release_authorized": False,
            "real_qloo_requests": 0, "external_action_executed": False,
        }, sort_keys=True))
        return 2

    print(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
