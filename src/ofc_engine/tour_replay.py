"""Build a private six-decision tour replay from one saved agent brand choice."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
from stat import S_ISDIR

from .agent_run import commit_rank_repair
from .agent_workspace import prepare_workspace
from .contracts import Decision
from .durable import SQLiteRevisionStore
from .intake import AgentProposal, accept_proposal
from .local_run_view import project_local_run
from .public_recorded import seal_public_recorded_excerpt
from .qloo import brand_affinity_probe
from .qloo_affinity import normalize_affinity_capture
from .qloo_agent_bridge import _read_private_json
from .qloo_flow import RecordedSource


class TourReplayRejected(ValueError):
    pass


def build_tour_proposal(
    original: AgentProposal, *, artist_id: str, brand_ids: tuple[str, ...],
    run_id: str,
) -> AgentProposal:
    if len(original.decisions) != 1:
        raise TourReplayRejected("EXPECTED_SINGLE_BRAND_PROPOSAL")
    brand = original.decisions[0]
    declared_artist = original.intent.taste_context.get("artist_id")
    if declared_artist is not None and declared_artist != artist_id:
        raise TourReplayRejected("ORIGINAL_ARTIST_SCOPE_MISMATCH")
    if (
        brand.decision_id != "brand" or brand.domain not in ("brand", "partner")
        or brand.locality_ref is not None or brand.depends_on
        or brand.candidate_pool != brand_ids
        or brand.mutable_fields != ("candidate_id",)
    ):
        raise TourReplayRejected("ORIGINAL_BRAND_SCOPE_MISMATCH")

    supplemental = (
        Decision(
            decision_id="opener", domain="artist",
            candidate_id="unverified-opener", candidate_pool=("unverified-opener",),
        ),
        Decision(
            decision_id="chicago", domain="venue", locality_ref="Chicago",
            candidate_id="unverified-chicago-venue",
            candidate_pool=("unverified-chicago-venue",),
        ),
        Decision(
            decision_id="austin", domain="venue", locality_ref="Austin",
            candidate_id="unverified-austin-venue",
            candidate_pool=("unverified-austin-venue",),
        ),
        Decision(
            decision_id="seattle", domain="venue", locality_ref="Seattle",
            candidate_id="unverified-seattle-venue",
            candidate_pool=("unverified-seattle-venue",),
        ),
        Decision(
            decision_id="afterparty", domain="experience",
            candidate_id="unverified-afterparty",
            candidate_pool=("unverified-afterparty",),
            depends_on=("brand",),
        ),
    )
    intent = original.intent.model_copy(update={
        "run_id": run_id,
        "client_request_id": run_id,
        "objective": "Three-city tour replay with unverified supplementary decisions",
        "constraints": {
            **original.intent.constraints, "cities": ["Chicago", "Austin", "Seattle"],
            "external_action": False,
        },
        "taste_context": {
            **original.intent.taste_context,
            "artist_id": artist_id,
            "supplemental_decisions": "OPERATOR_CONSTRUCTED_UNVERIFIED",
        },
    })
    return AgentProposal(intent=intent, decisions=(brand, *supplemental))


def _private_directory() -> Path:
    parent = Path.home() / ".local/share/ofc-engine"
    if parent.is_symlink():
        raise TourReplayRejected("UNSAFE_SESSION_PARENT")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = parent / "private-tour-runs"
    if path.is_symlink():
        raise TourReplayRejected("UNSAFE_SESSION_DIRECTORY")
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise TourReplayRejected("UNSAFE_SESSION_DIRECTORY")
    return path


def prepare_tour_replay(
    *,
    original_proposal: Path,
    original_capture: Path,
    later_capture: Path,
    artist_id: str,
    brand_ids: tuple[str, ...],
    port: int = 18080,
):
    if not 0 <= port <= 65535:
        raise TourReplayRejected("INVALID_PORT")
    source_proposal = AgentProposal.model_validate_json(json.dumps(
        _read_private_json(original_proposal, max_bytes=96 * 1024)
    ))
    first = _read_private_json(original_capture, max_bytes=1024 * 1024)
    later = _read_private_json(later_capture, max_bytes=1024 * 1024)
    spec = brand_affinity_probe(artist_id, brand_ids)
    first_rank = normalize_affinity_capture(first, spec)
    later_rank = normalize_affinity_capture(later, spec)
    original_at = datetime.fromisoformat(first_rank["collected_at"])
    later_at = datetime.fromisoformat(later_rank["collected_at"])
    now = datetime.now(timezone.utc)
    if original_at > now or later_at > now or later_at <= original_at:
        raise TourReplayRejected("CAPTURE_SEQUENCE_INVALID")
    if first == later:
        raise TourReplayRejected("CAPTURE_REPLAYED")

    run_id = "tour-replay-" + secrets.token_hex(10)
    proposal = build_tour_proposal(
        source_proposal, artist_id=artist_id, brand_ids=brand_ids, run_id=run_id,
    )
    if first_rank["ranking"][0]["entity_id"] == proposal.decisions[0].candidate_id:
        raise TourReplayRejected("ORIGINAL_CHOICE_ALREADY_TOP_RANK")

    directory = _private_directory() / run_id
    directory.mkdir(mode=0o700)
    if directory.stat().st_mode & 0o077:
        raise TourReplayRejected("UNSAFE_SESSION_PERMISSIONS")
    database = directory / "plan.sqlite3"
    proposal_file = directory / "proposal.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(proposal_file, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(proposal.model_dump_json())

    store = SQLiteRevisionStore(database)
    accept_proposal(store, proposal)
    record = commit_rank_repair(
        store, proposal,
        {"brand": RecordedSource(first, spec)},
        acknowledgement="COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE",
        request_id="tour-replay-commit",
        as_of=original_at + timedelta(seconds=1),
        max_evidence_age=timedelta(hours=72),
    )
    if (
        record.plan_revision != 1 or record.changed_ids != ("brand",)
        or record.invalidated_ids != ("brand", "afterparty")
        or record.release_authorized or record.external_action_executed
    ):
        raise TourReplayRejected("REPLAY_STATE_NOT_QUALIFIED")

    workspace = prepare_workspace(
        database=database, proposal=proposal_file,
        original_capture=original_capture, later_capture=later_capture,
        artist_id=artist_id, brand_ids=brand_ids, historical=True,
        age_hours=72, port=port,
    )
    snapshot = workspace.server.evidence_session.snapshot_model(store, run_id)
    excerpt = project_local_run(snapshot, operator_constructed_hypotheses=5)
    try:
        draft = seal_public_recorded_excerpt(excerpt)
    except ValueError:
        pass
    else:
        outfile = directory / "public-excerpt-draft.json"
        file = os.open(outfile, flags, 0o600)
        with os.fdopen(file, "w", encoding="utf-8") as output:
            json.dump(draft, output, sort_keys=True, separators=(",", ":"))
    return workspace, directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Historical, private six-decision tour replay; never requests Qloo"
    )
    parser.add_argument("--original-proposal", type=Path, required=True)
    parser.add_argument("--original-capture", type=Path, required=True)
    parser.add_argument("--later-capture", type=Path, required=True)
    parser.add_argument("--artist-id", required=True)
    parser.add_argument("--brands", required=True)
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args(argv)
    try:
        workspace, directory = prepare_tour_replay(
            original_proposal=args.original_proposal,
            original_capture=args.original_capture,
            later_capture=args.later_capture, artist_id=args.artist_id,
            brand_ids=tuple(args.brands.split(",")), port=args.port,
        )
    except (OSError, ValueError, TypeError):
        print(json.dumps({
            "status": "BLOCKED", "release_authorized": False,
            "real_qloo_requests": 0, "external_action_executed": False,
        }))
        return 2

    try:
        print(json.dumps({
            "status": "HISTORICAL_TOUR_STUDIO_READY",
            "product_url": f"http://127.0.0.1:{workspace.server.server_port}/",
            "private_session_dir": str(directory),
            "redacted_excerpt_reviewable": (directory / "public-excerpt-draft.json").exists(),
            "operator_constructed_unverified_decisions": 5,
            "historical_replay": True,
            "recorded_rank_recheck_pass": (
                workspace.rank_status == "RECORDED_RANK_RECHECK_PASS_NO_RELEASE"
            ),
            "release_authorized": False,
            "real_qloo_requests": 0,
            "external_action_executed": False,
        }, sort_keys=True), flush=True)
        workspace.server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        workspace.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
