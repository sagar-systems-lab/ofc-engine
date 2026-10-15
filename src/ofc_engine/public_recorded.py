"""Validate an explicitly reviewable public excerpt of an offline Qloo run."""
from __future__ import annotations

from typing import Any
import re

from .revisions import digest_obj

_IDS = ("brand", "opener", "chicago", "austin", "seattle", "afterparty")
_ALLOWED_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_PUBLIC_FIELDS = {
    "status", "source_mode", "historical_replay", "revision", "decision_count",
    "operator_constructed_unverified_decisions", "decisions", "changed_ids",
    "preserved_ids", "invalidated_ids", "pending_rechecks",
    "recorded_rank_recheck_pass", "off_on", "blockers", "release_state",
    "provider_authenticated", "independent_recheck_completed",
    "release_authorized", "external_action_executed", "real_qloo_requests",
}


def _codes(values: Any) -> bool:
    return (
        isinstance(values, list) and len(values) <= 64
        and all(isinstance(x, str) and _ALLOWED_CODE.fullmatch(x) for x in values)
    )


def seal_public_recorded_excerpt(payload: dict) -> dict:
    validate_public_recorded_excerpt(payload)
    return {
        "schema": "ofc-public-recorded-excerpt-v1",
        "payload": payload,
        "sha256": digest_obj(payload),
    }


def validate_public_recorded_excerpt(payload: Any) -> None:
    if not isinstance(payload, dict) or set(payload) != _PUBLIC_FIELDS:
        raise ValueError("INVALID_PUBLIC_EXCERPT")
    if (
        payload["status"] != "RECORDED_AGENT_REVIEW"
        or payload["source_mode"] != "RECORDED_REDACTED"
        or payload["historical_replay"] is not True
        or type(payload["revision"]) is not int or payload["revision"] != 1
        or payload["decision_count"] != 6
        or payload["operator_constructed_unverified_decisions"] != 5
        or payload["release_state"] != "REVIEW_REQUIRED"
        or payload["provider_authenticated"] is not False
        or payload["independent_recheck_completed"] is not False
        or payload["release_authorized"] is not False
        or payload["external_action_executed"] is not False
        or type(payload["real_qloo_requests"]) is not int
        or payload["real_qloo_requests"] != 0
    ):
        raise ValueError("UNQUALIFIED_PUBLIC_EXCERPT")
    if (
        payload["changed_ids"] != ["brand"]
        or payload["preserved_ids"] != list(_IDS[1:])
        or payload["invalidated_ids"] != ["brand", "afterparty"]
        or payload["pending_rechecks"] != ["brand", "afterparty"]
        or type(payload["recorded_rank_recheck_pass"]) is not bool
        or not _codes(payload["blockers"])
        or "HISTORICAL_REPLAY_ONLY" not in payload["blockers"]
        or "PENDING_RECHECKS" not in payload["blockers"]
        or not isinstance(payload["decisions"], list)
        or len(payload["decisions"]) != 6
    ):
        raise ValueError("INVALID_EXCERPT_LINEAGE")
    for expected, item in zip(_IDS, payload["decisions"]):
        if not isinstance(item, dict) or set(item) != {
            "decision_id", "domain", "verdict", "changed", "preserved",
            "pending_recheck", "depends_on", "reason_codes",
        }:
            raise ValueError("INVALID_EXCERPT_DECISION")
        if (
            item["decision_id"] != expected or item["domain"] not in (
                "partner", "brand", "artist", "venue", "experience"
            )
            or item["verdict"] not in ("PASS", "REPAIR", "UNCERTAIN")
            or item["changed"] is not (expected == "brand")
            or item["preserved"] is not (expected != "brand")
            or item["pending_recheck"] is not (expected in ("brand", "afterparty"))
            or item["depends_on"] != (["brand"] if expected == "afterparty" else [])
            or not _codes(item["reason_codes"])
        ):
            raise ValueError("INVALID_EXCERPT_DECISION_STATE")
    comparison = payload["off_on"]
    if not isinstance(comparison, dict) or set(comparison) != {
        "same_baseline", "changed_ids", "off_verdicts", "on_verdicts",
        "real_causal_proof",
    }:
        raise ValueError("INVALID_EXCERPT_COMPARISON")
    if (
        comparison["same_baseline"] is not True
        or comparison["real_causal_proof"] is not False
        or comparison["changed_ids"] != ["brand"]
        or not isinstance(comparison["off_verdicts"], dict)
        or not isinstance(comparison["on_verdicts"], dict)
        or set(comparison["off_verdicts"]) != set(_IDS)
        or set(comparison["on_verdicts"]) != set(_IDS)
        or comparison["off_verdicts"]["brand"] != "UNCERTAIN"
        or comparison["on_verdicts"]["brand"] != "REPAIR"
        or any(x not in ("PASS", "REPAIR", "UNCERTAIN")
               for x in (*comparison["off_verdicts"].values(), *comparison["on_verdicts"].values()))
    ):
        raise ValueError("UNQUALIFIED_EXCERPT_COMPARISON")


def read_public_recorded_excerpt(document: Any) -> dict:
    if (
        not isinstance(document, dict)
        or set(document) != {"schema", "payload", "sha256"}
        or document["schema"] != "ofc-public-recorded-excerpt-v1"
    ):
        raise ValueError("INVALID_PUBLIC_DOCUMENT")
    payload = document["payload"]
    validate_public_recorded_excerpt(payload)
    if document["sha256"] != digest_obj(payload):
        raise ValueError("PUBLIC_EXCERPT_CHECKSUM_MISMATCH")
    return payload
