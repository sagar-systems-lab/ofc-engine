"""Validate a cached Qloo affinity result without claiming a policy verdict."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from math import isfinite
from pathlib import Path
import re
from typing import Any

from .qloo import ProbeSpec, brand_affinity_probe

_HEX64 = re.compile(r"^[a-f0-9]{64}$")


class AffinityCaptureError(ValueError):
    pass


def normalize_affinity_capture(capture: Any, spec: ProbeSpec) -> dict[str, Any]:
    if spec.path != "/v2/insights":
        raise AffinityCaptureError("EXPECTED_INSIGHTS_QUERY")

    params = dict(spec.params)
    if params.get("filter.type") != "urn:entity:brand":
        raise AffinityCaptureError("EXPECTED_BRAND_QUERY")
    if "filter.location.query" in params or "signal.location.query" in params:
        raise AffinityCaptureError("UNQUALIFIED_LOCALITY")
    raw_pool = params.get("filter.results.entities", "")
    candidates = tuple(raw_pool.split(","))
    artist_id = params.get("signal.interests.entities")
    if (not 2 <= len(candidates) <= 5 or not artist_id
            or any(not candidate for candidate in candidates)
            or len(set(candidates)) != len(candidates)):
        raise AffinityCaptureError("INVALID_QUERY_POOL")

    if not isinstance(capture, dict):
        raise AffinityCaptureError("INVALID_CAPTURE")
    summary, payload = capture.get("summary"), capture.get("payload")
    if not isinstance(summary, dict) or not isinstance(payload, dict):
        raise AffinityCaptureError("INVALID_CAPTURE")
    fingerprint = summary.get("query_fingerprint")
    if (summary.get("endpoint") != spec.path
            or summary.get("http_status") != 200
            or summary.get("real_qloo_requests") != 1
            or summary.get("mode") != "LIVE_RESPONSE_UNQUALIFIED"
            or not isinstance(fingerprint, str) or not _HEX64.fullmatch(fingerprint)
            or fingerprint != spec.fingerprint):
        raise AffinityCaptureError("CAPTURE_QUERY_MISMATCH")

    collected = summary.get("utc_collected_at")
    try:
        collected_time = datetime.fromisoformat(collected)
    except (ValueError, TypeError):
        raise AffinityCaptureError("INVALID_CAPTURE_TIME") from None
    if collected_time.utcoffset() is None:
        raise AffinityCaptureError("INVALID_CAPTURE_TIME")

    if payload.get("success") is not True:
        raise AffinityCaptureError("INSIGHTS_NOT_SUCCESSFUL")
    results = payload.get("results")
    if not isinstance(results, dict):
        raise AffinityCaptureError("INVALID_INSIGHTS_RESULTS")
    entities = results.get("entities")
    if not isinstance(entities, list) or len(entities) != len(candidates):
        raise AffinityCaptureError("INCOMPLETE_CANDIDATE_RESULTS")

    observed: list[tuple[str, float, bool]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            raise AffinityCaptureError("INVALID_RESULT_ENTITY")
        entity_id = entity.get("entity_id")
        if not isinstance(entity_id, str) or entity_id not in candidates:
            raise AffinityCaptureError("UNEXPECTED_RESULT_ENTITY")
        if entity.get("type") != "urn:entity:brand":
            raise AffinityCaptureError("RESULT_TYPE_MISMATCH")
        query = entity.get("query")
        if not isinstance(query, dict):
            raise AffinityCaptureError("MISSING_ENTITY_QUERY")
        affinity = query.get("affinity")
        if isinstance(affinity, bool) or not isinstance(affinity, (float, int)) or not isfinite(affinity):
            raise AffinityCaptureError("INVALID_AFFINITY")
        explain = query.get("explainability")
        signal = explain.get("signal.interests.entities") if isinstance(explain, dict) else None
        observed.append((entity_id, float(affinity), signal is not None))

    ids = [item[0] for item in observed]
    if len(set(ids)) != len(ids) or set(ids) != set(candidates):
        raise AffinityCaptureError("INCOMPLETE_CANDIDATE_RESULTS")
    scores = [item[1] for item in observed]
    if len(set(scores)) != len(scores):
        raise AffinityCaptureError("AMBIGUOUS_AFFINITY_TIE")
    if scores != sorted(scores, reverse=True):
        raise AffinityCaptureError("UNEXPECTED_AFFINITY_ORDER")

    ranking = [{"entity_id": entity_id, "rank": rank,
                "signal_explainability_present": explained}
               for rank, (entity_id, _, explained) in enumerate(observed, start=1)]
    return {
        "status": "OFFLINE_AFFINITY_OBSERVED",
        "source": "PRIVATE_LIVE_CAPTURE",
        "endpoint": spec.path,
        "query_fingerprint": fingerprint,
        "collected_at": collected_time.isoformat(),
        "input_entity_id": artist_id,
        "candidate_count": len(ranking),
        "ranking": ranking,
        "rank_metric": "query.affinity",
        "missing_candidate_ids": [],
        "provider_policy_verified": False,
        "release_authorized": False,
        "external_action_executed": False,
        "real_qloo_requests": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate previously captured Qloo brand affinity offline")
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--artist-id", required=True)
    parser.add_argument("--brands", required=True, help="2-5 comma-separated resolved Qloo brand IDs")
    args = parser.parse_args(argv)
    try:
        spec = brand_affinity_probe(args.artist_id, tuple(args.brands.split(",")))
        stat = args.capture.lstat()
        if not args.capture.is_file() or args.capture.is_symlink() or stat.st_size > 1024 * 1024:
            raise AffinityCaptureError("UNSAFE_CAPTURE_FILE")
        capture = json.loads(args.capture.read_text(encoding="utf-8"))
        result = normalize_affinity_capture(capture, spec)
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        code = str(exc) if isinstance(exc, AffinityCaptureError) else "INVALID_CAPTURE"
        print(json.dumps({"status": "BLOCKED", "code": code,
                          "provider_policy_verified": False, "real_qloo_requests": 0}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
