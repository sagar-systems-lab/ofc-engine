"""Offline inspection of privately captured Qloo probe responses."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


class CaptureInvalid(ValueError):
    pass


def inspect_capture(data: Any, *, expected_type: str | None = None) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise CaptureInvalid("INVALID_CAPTURE")
    summary, payload = data.get("summary"), data.get("payload")
    if not isinstance(summary, dict) or not isinstance(payload, dict):
        raise CaptureInvalid("INVALID_CAPTURE")
    if (summary.get("http_status") != 200 or summary.get("real_qloo_requests") != 1
            or summary.get("mode") != "LIVE_RESPONSE_UNQUALIFIED"
            or not isinstance(summary.get("query_fingerprint"), str)):
        raise CaptureInvalid("UNQUALIFIED_CAPTURE")
    endpoint = summary.get("endpoint")
    if endpoint == "/search":
        results = payload.get("results")
        if not isinstance(results, list) or len(results) > 100:
            raise CaptureInvalid("UNEXPECTED_SEARCH_RESULTS")
        observed = []
        for index, item in enumerate(results):
            if not isinstance(item, dict):
                raise CaptureInvalid("UNEXPECTED_SEARCH_ENTITY")
            entity_id, name, types = item.get("entity_id"), item.get("name"), item.get("types")
            if (not isinstance(entity_id, str) or not entity_id or len(entity_id) > 128
                    or not isinstance(name, str) or not name or len(name) > 256
                    or not isinstance(types, list) or any(not isinstance(x, str) for x in types)):
                raise CaptureInvalid("UNEXPECTED_SEARCH_ENTITY")
            if expected_type is None or expected_type in types:
                observed.append({"rank": index + 1, "name": name, "entity_id": entity_id, "types": types})
        return {
            "status": "SEARCH_OBSERVED", "endpoint": endpoint,
            "query_fingerprint": summary["query_fingerprint"],
            "matches": observed, "result_count": len(results),
            "provider_policy_verified": False, "real_qloo_requests": 0,
        }
    if endpoint == "/v2/insights":
        if payload.get("success") is not True:
            raise CaptureInvalid("INSIGHTS_NOT_SUCCESSFUL")
        results = payload.get("results")
        if not isinstance(results, dict):
            raise CaptureInvalid("UNEXPECTED_INSIGHTS_RESULTS")
        entities = results.get("entities")
        if not isinstance(entities, list) or len(entities) > 100:
            raise CaptureInvalid("UNEXPECTED_INSIGHTS_ENTITIES")
        if any(not isinstance(item, dict) for item in entities):
            raise CaptureInvalid("UNEXPECTED_INSIGHTS_ENTITY")
        ids = [item.get("entity_id") for item in entities]
        if any(not isinstance(entity_id, str) or not entity_id or len(entity_id) > 128 for entity_id in ids):
            raise CaptureInvalid("UNEXPECTED_INSIGHTS_ENTITY_ID")
        first = entities[0] if entities else {}
        entity_query = first.get("query")
        top_query = payload.get("query")
        if entity_query is not None and not isinstance(entity_query, dict):
            raise CaptureInvalid("UNEXPECTED_ENTITY_QUERY")
        if top_query is not None and not isinstance(top_query, dict):
            raise CaptureInvalid("UNEXPECTED_TOP_QUERY")
        return {
            "status": "INSIGHTS_SHAPE_OBSERVED" if entities else "INSIGHTS_EMPTY",
            "endpoint": endpoint,
            "query_fingerprint": summary["query_fingerprint"],
            "top_level_fields": sorted(payload),
            "results_fields": sorted(results),
            "query_fields": sorted(top_query) if top_query is not None else [],
            "query_explainability_fields": sorted(top_query.get("explainability", {}))
            if isinstance(top_query, dict) and isinstance(top_query.get("explainability"), dict) else [],
            "entity_count": len(entities),
            "entity_ids": ids,
            "duplicate_id_count": len(ids) - len(set(ids)),
            "first_entity_fields": sorted(first),
            "first_entity_query_fields": sorted(entity_query) if entity_query is not None else [],
            "first_entity_explainability_fields": sorted(entity_query.get("explainability", {}))
            if isinstance(entity_query, dict) and isinstance(entity_query.get("explainability"), dict) else [],
            "provider_policy_verified": False,
            "real_qloo_requests": 0,
        }
    raise CaptureInvalid("UNEXPECTED_ENDPOINT")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect saved Qloo response without API calls")
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--type", choices=("artist", "brand"))
    args = parser.parse_args(argv)
    try:
        if args.capture.stat().st_size > 1024 * 1024:
            raise CaptureInvalid("CAPTURE_TOO_LARGE")
        capture = json.loads(args.capture.read_text(encoding="utf-8"))
        result = inspect_capture(capture, expected_type=f"urn:entity:{args.type}" if args.type else None)
    except (OSError, UnicodeError, json.JSONDecodeError, CaptureInvalid) as exc:
        code = str(exc) if isinstance(exc, CaptureInvalid) else "CAPTURE_READ_FAILURE"
        print(json.dumps({"status": "BLOCKED", "code": code}))
        return 2
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
