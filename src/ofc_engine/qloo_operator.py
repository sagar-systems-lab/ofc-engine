"""Operator-controlled Qloo qualification with a one-attempt local budget gate."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat

from .contracts import Decision, PlanSnapshot
from .qloo import QlooProbeClient, QlooProbeError, brand_affinity_probe
from .qloo_affinity import AffinityCaptureError, normalize_affinity_capture
from .qloo_flow import RecordedSource, compare_recorded_arms
from .qloo_probe import find_private_cached_probe, private_capture
from .revisions import digest_obj

_APPROVAL = "EXACTLY_ONE_QLOO_REQUEST"


def _output(status: str, *, attempts: int = 0, **fields: object) -> dict:
    return {
        "status": status,
        "request_attempts_this_run": attempts,
        "real_qloo_requests": attempts,
        "provider_authenticated": False,
        "release_authorized": False,
        "external_action_executed": False,
        **fields,
    }


def _reserve_once(fingerprint: str) -> Path:
    directory = Path.home() / ".local/share/ofc-engine/private-qloo-attempts"
    if directory.is_symlink():
        raise ValueError("UNSAFE_ATTEMPT_DIRECTORY")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("UNSAFE_ATTEMPT_DIRECTORY")
    status = directory.stat()
    if status.st_uid != os.getuid() or stat.S_IMODE(status.st_mode) & 0o077:
        raise ValueError("UNSAFE_ATTEMPT_DIRECTORY")
    filename = directory / (fingerprint + ".json")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(filename, flags, 0o600)
    except FileExistsError:
        raise ValueError("REQUEST_ALREADY_RESERVED") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({
                "query_fingerprint": fingerprint,
                "reserved_at": datetime.now(timezone.utc).isoformat(),
                "status": "ATTEMPT_RESERVED_OUTCOME_MAY_BE_UNKNOWN",
            }, stream)
    except BaseException:
        # Preserve the marker: the request may have started in another process.
        raise
    return filename


def _baseline(spec, selected: str) -> PlanSnapshot:
    params = dict(spec.params)
    candidates = tuple(params["filter.results.entities"].split(","))
    artist = params["signal.interests.entities"]
    return PlanSnapshot(
        plan_id=f"operator-brand-{spec.fingerprint[:16]}",
        revision=0,
        baseline_digest=digest_obj({
            "artist": artist, "candidate_ids": candidates, "selected": selected,
        }),
        constraints_digest=digest_obj({"locality": None, "action": False}),
        decisions=(Decision(
            decision_id="brand", domain="partner",
            candidate_id=selected, candidate_pool=candidates,
            mutable_fields=("candidate_id",),
        ),),
    )


def _replay(capture: dict, spec, selected: str, age_hours: int) -> dict:
    observed = normalize_affinity_capture(capture, spec)
    now = datetime.now(timezone.utc)
    collected = datetime.fromisoformat(observed["collected_at"])
    if collected > now:
        raise AffinityCaptureError("EVIDENCE_FROM_FUTURE")
    if now - collected >= timedelta(hours=age_hours):
        return _output(
            "CACHED_EVIDENCE_STALE",
            query_fingerprint=spec.fingerprint,
            collected_at=observed["collected_at"],
            source_mode="RECORDED_REDACTED",
            real_causal_proof=False,
        )
    comparison = compare_recorded_arms(
        _baseline(spec, selected),
        {"brand": RecordedSource(capture, spec)},
        as_of=now, max_evidence_age=timedelta(hours=age_hours),
        max_attempts=2,
    )
    result = comparison.as_dict()
    return _output(
        "RECORDED_REPLAY_QUALIFIED",
        query_fingerprint=spec.fingerprint,
        source_mode="RECORDED_REDACTED",
        comparison=result,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit, quota-gated operator Qloo qualification")
    parser.add_argument("--artist-id", required=True)
    parser.add_argument("--brands", required=True)
    parser.add_argument("--selected", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--authorize", default="", help="exact operator consent for one request")
    parser.add_argument("--age-hours", type=int, default=24)
    args = parser.parse_args(argv)
    try:
        if type(args.age_hours) is not int or not 1 <= args.age_hours <= 72:
            raise ValueError("INVALID_EVIDENCE_AGE")
        ids = tuple(args.brands.split(","))
        spec = brand_affinity_probe(args.artist_id, ids)
        if args.selected not in ids:
            raise ValueError("SELECTED_OUTSIDE_POOL")
        if args.authorize and (not args.live or args.authorize != _APPROVAL):
            raise ValueError("UNAUTHORIZED_REQUEST")
        cached = find_private_cached_probe(spec.fingerprint)
        if cached is not None:
            path, _ = cached
            if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                raise ValueError("UNSAFE_CAPTURE")
            capture = json.loads(path.read_text(encoding="utf-8"))
            result = _replay(capture, spec, args.selected, args.age_hours)
            result["mode"] = "LOCAL_PRIVATE_CACHE"
            result["private_capture_file"] = str(path)
            print(json.dumps(result, sort_keys=True))
            return 0
        if not args.live:
            print(json.dumps(_output(
                "DRY_RUN_NO_NETWORK", query_fingerprint=spec.fingerprint,
                endpoint=spec.path, cache_present=False,
            ), sort_keys=True))
            return 0
        if args.authorize != _APPROVAL:
            raise ValueError("EXPLICIT_OPERATOR_APPROVAL_REQUIRED")
        client = QlooProbeClient.from_environment()
        _reserve_once(spec.fingerprint)
    except (OSError, ValueError, QlooProbeError, TypeError, json.JSONDecodeError) as exc:
        code = str(exc) if isinstance(exc, ValueError) else (
            exc.code if isinstance(exc, QlooProbeError) else "PREFLIGHT_BLOCKED"
        )
        print(json.dumps(_output("BLOCKED", code=code), sort_keys=True))
        return 2

    try:
        observation = client.probe(spec)
    except QlooProbeError as exc:
        print(json.dumps(_output(
            "REQUEST_OUTCOME_UNKNOWN", attempts=1, code=exc.code,
            retry_allowed=False, query_fingerprint=spec.fingerprint,
        ), sort_keys=True))
        return 3

    summary = observation.safe_summary()
    summary["mode"] = "LIVE_RESPONSE_UNQUALIFIED"
    summary["real_qloo_requests"] = 1
    try:
        path = private_capture(observation.payload, summary)
    except OSError:
        print(json.dumps(_output(
            "PRIVATE_CAPTURE_FAILED", attempts=1, retry_allowed=False,
            query_fingerprint=spec.fingerprint,
        ), sort_keys=True))
        return 4

    capture = {"summary": summary, "payload": observation.payload}
    try:
        output = _replay(capture, spec, args.selected, args.age_hours)
    except (AffinityCaptureError, ValueError, TypeError):
        print(json.dumps(_output(
            "RESPONSE_NOT_QUALIFIED", attempts=1,
            private_capture_file=str(path), query_fingerprint=spec.fingerprint,
            retry_allowed=False,
        ), sort_keys=True))
        return 5
    output["status"] = "ONE_LIVE_RESPONSE_CAPTURED"
    output["real_qloo_requests"] = 1
    output["request_attempts_this_run"] = 1
    output["private_capture_file"] = str(path)
    output["fresh_provider_call_observed"] = True
    output["provider_authenticated"] = False
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
