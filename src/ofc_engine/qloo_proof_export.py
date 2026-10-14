"""Export a compact, offline Qloo replay proof without provider payloads."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
from typing import Any

from .contracts import Decision, PlanSnapshot
from .qloo import brand_affinity_probe
from .qloo_affinity import normalize_affinity_capture
from .qloo_flow import RecordedSource, RecordedState, compare_recorded_arms
from .revisions import digest_obj


class ProofError(ValueError):
    pass


def build_proof(
    capture: dict[str, Any], *,
    artist_id: str, brand_ids: tuple[str, ...], selected_id: str,
    now: datetime, historical: bool = False,
) -> dict[str, Any]:
    if now.utcoffset() is None:
        raise ProofError("CLOCK_NOT_AWARE")
    spec = brand_affinity_probe(artist_id, brand_ids)
    if selected_id not in brand_ids:
        raise ProofError("SELECTED_OUTSIDE_POOL")
    observation = normalize_affinity_capture(capture, spec)
    collected_at = datetime.fromisoformat(observation["collected_at"])
    if collected_at > now:
        raise ProofError("CAPTURE_FROM_FUTURE")
    max_age = timedelta(hours=24)
    if now - collected_at >= max_age and not historical:
        raise ProofError("STALE_REQUIRES_HISTORICAL_MODE")
    as_of = collected_at + timedelta(minutes=1) if historical else now
    baseline = PlanSnapshot(
        plan_id=f"recorded-brand-{spec.fingerprint[:16]}",
        revision=0,
        baseline_digest=digest_obj({
            "artist": artist_id, "candidate_ids": brand_ids, "selected": selected_id,
        }),
        constraints_digest=digest_obj({"locality": None, "action": False}),
        decisions=(Decision(
            decision_id="brand", domain="partner", candidate_id=selected_id,
            candidate_pool=brand_ids, mutable_fields=("candidate_id",),
        ),),
    )
    comparison = compare_recorded_arms(
        baseline, {"brand": RecordedSource(capture, spec)},
        as_of=as_of, max_evidence_age=max_age, max_attempts=2,
    )
    off, on = comparison.disabled, comparison.enabled
    if (not comparison.same_baseline
            or off.status != RecordedState.REVIEW_REQUIRED
            or off.plan.revision != 0
            or on.status != RecordedState.RANK_REVIEW_COMPLETE
            or on.ledger.pending_recheck_ids
            or on.ledger.attempts_used > 2):
        raise ProofError("REPLAY_INTEGRITY_FAILURE")
    if any(trace.recheck_state.value != "STRUCTURAL_PASS" for trace in on.traces):
        raise ProofError("RECHECK_NOT_STRUCTURALLY_VALID")

    return {
        "schema": "ofc-recorded-proof-v1",
        "source_mode": "RECORDED_REDACTED",
        "qualification": "OFFLINE_RANK_REPLAY_ONLY",
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "captured_at": collected_at.astimezone(timezone.utc).isoformat(),
        "historical_replay": historical,
        "query_fingerprint": spec.fingerprint,
        "plan_baseline_digest": comparison.baseline_digest,
        "same_frozen_baseline": True,
        "evidence_off": {
            "verdict": off.reviews[0].verdicts[0].verdict.value,
            "revision": off.plan.revision,
        },
        "evidence_on": {
            "initial_verdict": on.reviews[0].verdicts[0].verdict.value,
            "final_verdict": on.reviews[-1].verdicts[0].verdict.value,
            "initial_candidate_id": selected_id,
            "final_candidate_id": on.plan.decisions[0].candidate_id,
            "final_revision": on.plan.revision,
            "repair_attempts": on.ledger.attempts_used,
            "recheck_states": [trace.recheck_state.value for trace in on.traces],
            "pending_recheck_ids": list(on.ledger.pending_recheck_ids),
        },
        "rank_order": [item["entity_id"] for item in observation["ranking"]],
        "request_attempts_this_run": 0,
        "real_qloo_requests": 0,
        "fresh_provider_recheck": False,
        "provider_authenticated": False,
        "real_causal_proof": False,
        "release_authorized": False,
        "external_action_executed": False,
    }


def _read_json(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        raise ProofError("UNSAFE_INPUT")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProofError("INVALID_DOCUMENT")
    return data


def _private_directory() -> Path:
    directory = Path.home() / ".local/share/ofc-engine/private-qloo-proofs"
    if directory.is_symlink():
        raise ProofError("UNSAFE_PROOF_DIRECTORY")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise ProofError("UNSAFE_PROOF_DIRECTORY")
    directory.chmod(0o700)
    return directory


def _save_proof(payload: dict[str, Any]) -> Path:
    directory = _private_directory()
    checksum = digest_obj(payload)
    path = directory / f"recorded-{checksum[:24]}.json"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"payload": payload, "sha256": checksum}, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def verify_proof(document: dict[str, Any]) -> dict[str, Any]:
    payload = document.get("payload")
    if not isinstance(payload, dict) or set(document) != {"payload", "sha256"}:
        raise ProofError("INVALID_PROOF_FORMAT")
    expected = document["sha256"]
    if not isinstance(expected, str) or digest_obj(payload) != expected:
        raise ProofError("PROOF_DIGEST_MISMATCH")
    if (payload.get("schema") != "ofc-recorded-proof-v1"
            or payload.get("source_mode") != "RECORDED_REDACTED"
            or payload.get("qualification") != "OFFLINE_RANK_REPLAY_ONLY"
            or payload.get("same_frozen_baseline") is not True
            or payload.get("provider_authenticated") is not False
            or payload.get("fresh_provider_recheck") is not False
            or payload.get("real_causal_proof") is not False
            or payload.get("release_authorized") is not False
            or payload.get("external_action_executed") is not False
            or payload.get("real_qloo_requests") != 0):
        raise ProofError("INVALID_PROOF_BOUNDARY")
    return {
        "status": "LOCAL_DIGEST_VALID",
        "sha256": expected,
        "historical_replay": payload.get("historical_replay"),
        "provider_authenticated": False,
        "release_authorized": False,
        "real_qloo_requests": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline recorded Qloo proof export and digest check")
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--artist-id")
    parser.add_argument("--brands")
    parser.add_argument("--selected")
    parser.add_argument("--historical", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.verify:
            if args.capture or args.artist_id or args.brands or args.selected or args.historical:
                raise ProofError("VERIFY_ARGUMENT_CONFLICT")
            result = verify_proof(_read_json(args.verify))
        else:
            if not all((args.capture, args.artist_id, args.brands, args.selected)):
                raise ProofError("MISSING_INPUT")
            proof = build_proof(
                _read_json(args.capture),
                artist_id=args.artist_id, brand_ids=tuple(args.brands.split(",")),
                selected_id=args.selected, now=datetime.now(timezone.utc),
                historical=args.historical,
            )
            destination = _save_proof(proof)
            result = verify_proof({"payload": proof, "sha256": digest_obj(proof)})
            result.update({"status": "PRIVATE_PROOF_EXPORTED", "path": str(destination)})
    except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeError):
        print(json.dumps({"status": "BLOCKED", "real_qloo_requests": 0,
                          "release_authorized": False}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
