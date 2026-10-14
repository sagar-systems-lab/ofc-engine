"""One-attempt Qloo recheck of a persisted recorded repair, without release authority."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from stat import S_ISREG
from typing import Any, Callable

from pydantic import ValidationError

from .durable import SQLiteRevisionStore, StorageIntegrityError
from .intake import AgentProposal
from .qloo import ProbeSpec, QlooProbeClient, QlooProbeError, brand_affinity_probe
from .qloo_affinity import AffinityCaptureError
from .qloo_agent_bridge import AgentEvidenceRejected, _read_private_json
from .qloo_operator import _reserve_once
from .qloo_probe import private_capture
from .qloo_recorded_recheck import inspect_recorded_recheck
from .revisions import digest_obj

_APPROVAL = "EXACTLY_ONE_QLOO_RECHECK_REQUEST"


class LiveRecheckFailure(RuntimeError):
    def __init__(self, code: str, *, request_attempted: bool) -> None:
        self.code = code
        self.request_attempted = request_attempted
        super().__init__(code)


def _preflight(
    store: SQLiteRevisionStore,
    proposal: AgentProposal,
    original_capture: dict[str, Any],
    *,
    decision_id: str,
    artist_id: str,
    brand_ids: tuple[str, ...],
    as_of: datetime,
    max_evidence_age: timedelta,
) -> tuple[ProbeSpec, dict[str, Any], str]:
    spec = brand_affinity_probe(artist_id, brand_ids)
    before = inspect_recorded_recheck(
        store, proposal, original_capture, original_capture,
        decision_id=decision_id, artist_id=artist_id, brand_ids=brand_ids,
        as_of=as_of, max_evidence_age=max_evidence_age,
    )
    if (
        before["status"] != "ORIGINAL_CAPTURE_REPLAY_ONLY"
        or before["stored_plan_revision"] != 1
        or before["pending_recheck_ids"] != [decision_id]
        or before["pending_recheck_cleared"]
        or before["release_authorized"]
    ):
        raise AgentEvidenceRejected("RECHECK_STATE_NOT_READY")
    reservation_key = digest_obj({
        "purpose": "qloo-live-recheck-v1",
        "plan_id": before["plan_id"],
        "plan_digest": before["stored_plan_digest"],
        "decision_id": decision_id,
        "query_fingerprint": spec.fingerprint,
    })
    return spec, before, reservation_key


def run_live_recheck(
    store: SQLiteRevisionStore,
    proposal: AgentProposal,
    original_capture: dict[str, Any],
    *,
    decision_id: str,
    artist_id: str,
    brand_ids: tuple[str, ...],
    acknowledgement: str,
    as_of: datetime,
    max_evidence_age: timedelta,
    client: QlooProbeClient,
    reserve_attempt: Callable[[str], Path] = _reserve_once,
    write_capture: Callable[[dict, dict], Path] = private_capture,
) -> dict[str, Any]:
    """Execute one authorized HTTPS probe; keep all persisted rechecks pending."""
    if acknowledgement != _APPROVAL:
        raise AgentEvidenceRejected("EXPLICIT_LIVE_RECHECK_APPROVAL_REQUIRED")
    spec, before, reservation_key = _preflight(
        store, proposal, original_capture,
        decision_id=decision_id, artist_id=artist_id, brand_ids=brand_ids,
        as_of=as_of, max_evidence_age=max_evidence_age,
    )
    if not isinstance(client, QlooProbeClient):
        raise AgentEvidenceRejected("INVALID_QLOO_CLIENT")
    reserve_attempt(reservation_key)
    try:
        observation = client.probe(spec)
    except QlooProbeError as exc:
        raise LiveRecheckFailure("QLOO_" + exc.code, request_attempted=True) from None
    except Exception:
        raise LiveRecheckFailure("QLOO_REQUEST_OUTCOME_UNKNOWN", request_attempted=True) from None

    if observation.path != spec.path or observation.query_fingerprint != spec.fingerprint:
        raise LiveRecheckFailure("QLOO_OBSERVATION_SCOPE_MISMATCH", request_attempted=True)
    summary = observation.safe_summary()
    summary.update(mode="LIVE_RESPONSE_UNQUALIFIED", real_qloo_requests=1)
    try:
        private_path = write_capture(observation.payload, summary)
    except OSError:
        raise LiveRecheckFailure("PRIVATE_CAPTURE_FAILED", request_attempted=True) from None

    try:
        result = inspect_recorded_recheck(
            store, proposal, original_capture,
            {"summary": summary, "payload": observation.payload},
            decision_id=decision_id, artist_id=artist_id, brand_ids=brand_ids,
            as_of=datetime.now(timezone.utc), max_evidence_age=max_evidence_age,
        )
    except (AgentEvidenceRejected, AffinityCaptureError, ValueError, StorageIntegrityError, sqlite3.Error):
        raise LiveRecheckFailure("FRESH_RESPONSE_NOT_QUALIFIED", request_attempted=True) from None
    if result["stored_journal_head"] != before["stored_journal_head"]:
        raise LiveRecheckFailure("STORED_STATE_CHANGED_DURING_LIVE_CALL", request_attempted=True)
    if result["same_capture"] or not result["later_capture_observed"]:
        raise LiveRecheckFailure("FRESH_RESPONSE_NOT_LATER", request_attempted=True)

    return {
        **result,
        "status": (
            "FRESH_QLOO_RANK_PASS_REVIEW_PENDING"
            if result["status"] == "RECORDED_LATER_CAPTURE_RANK_PASS_ONLY"
            else "FRESH_QLOO_RANK_UNRESOLVED"
        ),
        "fresh_provider_request_observed": True,
        "request_attempts_this_run": 1,
        "real_qloo_requests": 1,
        "private_capture_file": str(private_path),
        "retry_allowed": False,
        "provider_authenticated": False,
        "independent_recheck_completed": False,
        "pending_recheck_cleared": False,
        "release_authorized": False,
        "external_action_executed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicit one-attempt Qloo recheck of a stored recorded repair"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--original-capture", type=Path, required=True)
    parser.add_argument("--decision-id", required=True)
    parser.add_argument("--artist-id", required=True)
    parser.add_argument("--brands", required=True)
    parser.add_argument("--age-hours", type=int, default=24)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--authorize", default="")
    args = parser.parse_args(argv)

    try:
        if not 1 <= args.age_hours <= 72:
            raise AgentEvidenceRejected("INVALID_AGE_WINDOW")
        if args.authorize and (not args.live or args.authorize != _APPROVAL):
            raise AgentEvidenceRejected("UNAUTHORIZED_REQUEST")
        file_info = args.database.lstat()
        if not S_ISREG(file_info.st_mode) or file_info.st_size == 0:
            raise AgentEvidenceRejected("UNSAFE_DATABASE")
        store = SQLiteRevisionStore(args.database)
        proposal = AgentProposal.model_validate_json(
            json.dumps(_read_private_json(args.proposal, max_bytes=96 * 1024))
        )
        original = _read_private_json(args.original_capture, max_bytes=1024 * 1024)
        opts = {
            "decision_id": args.decision_id,
            "artist_id": args.artist_id,
            "brand_ids": tuple(args.brands.split(",")),
            "as_of": datetime.now(timezone.utc),
            "max_evidence_age": timedelta(hours=args.age_hours),
        }
        spec, before, reservation_key = _preflight(store, proposal, original, **opts)
        if not args.live:
            print(json.dumps({
                "status": "LIVE_RECHECK_READY_NO_NETWORK",
                "plan_id": before["plan_id"],
                "revision": before["stored_plan_revision"],
                "plan_digest": before["stored_plan_digest"],
                "query_fingerprint": spec.fingerprint,
                "reservation_key": reservation_key,
                "pending_recheck_ids": before["pending_recheck_ids"],
                "provider_authenticated": False,
                "release_authorized": False,
                "real_qloo_requests": 0,
            }, sort_keys=True))
            return 0
        if args.authorize != _APPROVAL:
            raise AgentEvidenceRejected("EXPLICIT_LIVE_RECHECK_APPROVAL_REQUIRED")
        client = QlooProbeClient.from_environment()
        result = run_live_recheck(
            store, proposal, original,
            acknowledgement=args.authorize, client=client, **opts,
        )
    except LiveRecheckFailure as exc:
        print(json.dumps({
            "status": "RECHECK_FAILED_CLOSED",
            "code": exc.code,
            "request_attempts_this_run": int(exc.request_attempted),
            "retry_allowed": False,
            "pending_recheck_cleared": False,
            "release_authorized": False,
            "real_qloo_requests": int(exc.request_attempted),
        }, sort_keys=True))
        return 3
    except (
        OSError, ValueError, TypeError, UnicodeError, ValidationError,
        StorageIntegrityError, sqlite3.Error, QlooProbeError, AffinityCaptureError,
    ):
        print(json.dumps({
            "status": "BLOCKED",
            "request_attempts_this_run": 0,
            "pending_recheck_cleared": False,
            "release_authorized": False,
            "real_qloo_requests": 0,
        }, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
