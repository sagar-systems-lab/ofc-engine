"""Run a bounded Qloo evidence comparison using one local capture."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from .contracts import Decision, PlanSnapshot
from .qloo import brand_affinity_probe
from .qloo_flow import RecordedSource, compare_recorded_arms
from .revisions import digest_obj


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline recorded Qloo rank review; zero API calls")
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--artist-id", required=True)
    parser.add_argument("--brands", required=True)
    parser.add_argument("--selected", required=True, help="initial candidate Qloo brand ID")
    parser.add_argument("--age-hours", type=int, default=24, help="maximum recorded evidence age")
    args = parser.parse_args(argv)

    try:
        if not 1 <= args.age_hours <= 72:
            raise ValueError("INVALID_EVIDENCE_AGE")
        brands = tuple(args.brands.split(","))
        spec = brand_affinity_probe(args.artist_id, brands)
        if args.selected not in brands:
            raise ValueError("SELECTED_OUTSIDE_FIXED_POOL")
        file_stat = args.capture.lstat()
        if (args.capture.is_symlink() or not args.capture.is_file()
                or file_stat.st_size > 1024 * 1024):
            raise ValueError("UNSAFE_CAPTURE")
        capture = json.loads(args.capture.read_text(encoding="utf-8"))

        objective = {
            "input_entity_id": args.artist_id,
            "candidate_pool": brands,
            "initial_candidate_id": args.selected,
        }
        baseline = PlanSnapshot(
            plan_id=f"recorded-brand-{spec.fingerprint[:16]}",
            revision=0,
            baseline_digest=digest_obj(objective),
            constraints_digest=digest_obj({"locality": None, "external_action": False}),
            decisions=(Decision(
                decision_id="brand", domain="partner",
                candidate_id=args.selected, candidate_pool=brands,
                mutable_fields=("candidate_id",),
            ),),
        )
        comparison = compare_recorded_arms(
            baseline, {"brand": RecordedSource(capture, spec)},
            as_of=datetime.now(timezone.utc),
            max_evidence_age=timedelta(hours=args.age_hours),
            max_attempts=2,
        )
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        print(json.dumps({
            "status": "BLOCKED", "reason": "INVALID_RECORDED_RUN",
            "release_authorized": False, "real_qloo_requests": 0,
        }))
        return 2
    print(json.dumps(comparison.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
