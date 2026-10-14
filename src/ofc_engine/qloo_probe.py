"""Command-line inspection of Qloo API capabilities with explicit live opt-in."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from .qloo import QlooProbeClient, QlooProbeError, brand_affinity_probe, search_probe


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bounded Qloo capability probe (no automatic retries)")
    p.add_argument("--live", action="store_true", help="perform one real API call; consumes event quota")
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--search", help="entity name to look up")
    action.add_argument("--artist-id", help="resolved Qloo artist entity ID")
    p.add_argument("--type", choices=("artist", "brand"), default="artist")
    p.add_argument("--brands", help="2–5 comma-separated resolved brand entity IDs")
    p.add_argument("--locality", help="named locality to probe")
    p.add_argument("--locality-mode", choices=("filter", "signal"))
    p.add_argument("--capture", action="store_true", help="save raw response locally, outside repo, mode 0600")
    return p.parse_args(argv)


def private_capture(payload: dict, summary: dict) -> Path:
    """Store opt-in raw response only under the user's home, never in repo."""
    directory = Path.home() / ".local" / "share" / "ofc-engine" / "private-qloo-probes"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    filename = directory / (uuid4().hex + ".json")
    mode = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        mode |= os.O_NOFOLLOW
    fd = os.open(filename, mode, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"summary": summary, "payload": payload}, stream, ensure_ascii=False, indent=2)
    except BaseException:
        filename.unlink(missing_ok=True)
        raise
    return filename


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    if args.search:
        if args.brands or args.locality or args.locality_mode:
            raise SystemExit("Search accepts no brand pool or locality options")
        spec = search_probe(args.search, entity_type=f"urn:entity:{args.type}")
    else:
        if not args.brands:
            raise SystemExit("Brand probe requires --brands")
        spec = brand_affinity_probe(
            args.artist_id, tuple(s.strip() for s in args.brands.split(",")),
            locality=args.locality, locality_mode=args.locality_mode,
        )
    if args.capture and not args.live:
        raise SystemExit("--capture requires --live")
    if args.live and args.locality:
        raise SystemExit("Brand locality parameters are not qualified for this entity type; live request blocked")
    if not args.live:
        print(json.dumps({
            "mode": "DRY_RUN_NO_NETWORK", "endpoint": spec.path,
            "parameter_names": sorted(key for key, _ in spec.params),
            "query_fingerprint": spec.fingerprint, "real_qloo_requests": 0,
        }, sort_keys=True))
        return 0
    try:
        result = QlooProbeClient.from_environment().probe(spec)
    except QlooProbeError as exc:
        print(json.dumps({"status": "UNAVAILABLE", "code": exc.code}), file=sys.stderr)
        return 2
    summary = result.safe_summary()
    summary["mode"] = "LIVE_RESPONSE_UNQUALIFIED"
    summary["real_qloo_requests"] = 1
    if args.capture:
        try:
            summary["private_capture_file"] = str(private_capture(result.payload, summary))
        except OSError:
            print(json.dumps({"status":"CAPTURE_FAILED"}), file=sys.stderr)
            return 3
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
