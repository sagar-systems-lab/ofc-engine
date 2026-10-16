"""Bounded Qloo-native discovery; observed taste never implies business approval."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from uuid import UUID

from .qloo import ProbeObservation, ProbeSpec
from .qloo_catalog import category_for

class TasteEvidenceError(ValueError):
    pass

@dataclass(frozen=True)
class TasteContext:
    seed_name: str
    seed_category: str
    target_category: str
    location: str | None = None

    def __post_init__(self):
        if (
            not isinstance(self.seed_name, str)
            or not 1 <= len(self.seed_name.strip()) <= 120
            or any(ord(c) < 32 for c in self.seed_name)
        ):
            raise TasteEvidenceError("INVALID_TASTE_SEED")
        category_for(self.seed_category)
        category_for(self.target_category)
        if self.location is not None and (
            not isinstance(self.location, str)
            or not 2 <= len(self.location.strip()) <= 100
            or any(ord(c) < 32 for c in self.location)
            or self.target_category != "place"
        ):
            raise TasteEvidenceError("UNQUALIFIED_LOCATION_SCOPE")


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise TasteEvidenceError("INVALID_QLOO_DISCOVERY_ID") from None


def discovery_spec(context: TasteContext, seed_id: str, *, take: int = 5) -> ProbeSpec:
    if not isinstance(context, TasteContext):
        raise TypeError("typed taste context required")
    if type(take) is not int or not 2 <= take <= 5:
        raise TasteEvidenceError("INVALID_DISCOVERY_LIMIT")
    params = [
        ("filter.type", "urn:entity:" + context.target_category),
        ("signal.interests.entities", _uuid(seed_id)),
        ("take", str(take)),
        ("feature.explainability", "true"),
    ]
    if context.location is not None:
        params.append(("filter.location.query", context.location.strip()))
    return ProbeSpec("/v2/insights", tuple(params))


def normalize_discovery(observation: ProbeObservation, spec: ProbeSpec) -> dict:
    if (
        not isinstance(observation, ProbeObservation) or not isinstance(spec, ProbeSpec)
        or observation.path != "/v2/insights"
        or observation.http_status != 200
        or observation.query_fingerprint != spec.fingerprint
    ):
        raise TasteEvidenceError("QLOO_DISCOVERY_PROVENANCE_MISMATCH")
    body = observation.payload
    entities = (
        body.get("results", {}).get("entities")
        if isinstance(body, dict) and body.get("success") is True
        and isinstance(body.get("results"), dict) else None
    )
    if not isinstance(entities, list) or not 2 <= len(entities) <= 5:
        raise TasteEvidenceError("QLOO_DISCOVERY_INSUFFICIENT_RESULTS")
    pool = []
    for entity in entities:
        if not isinstance(entity, dict):
            raise TasteEvidenceError("QLOO_DISCOVERY_ENTITY_MALFORMED")
        name = entity.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
            raise TasteEvidenceError("QLOO_DISCOVERY_NAME_MISSING")
        entity_id = _uuid(entity.get("entity_id"))
        score = entity.get("query", {}).get("affinity") if isinstance(entity.get("query"), dict) else None
        if type(score) not in (float, int) or not isfinite(score):
            raise TasteEvidenceError("QLOO_DISCOVERY_AFFINITY_MISSING")
        explain = entity.get("query", {}).get("explainability") or {}
        reasons = []
        if isinstance(explain, dict):
            for signal, impact in sorted(explain.items()):
                if not isinstance(signal, str) or len(signal) > 100:
                    continue
                if isinstance(impact, dict):
                    for key, strength in list(sorted(impact.items()))[:5]:
                        if (
                            isinstance(key, str) and len(key) <= 120
                            and type(strength) in (int, float)
                            and isfinite(strength) and 0 <= strength <= 1
                        ):
                            reasons.append({"signal": signal, "source": key, "weight": round(float(strength), 4)})
        pool.append({"name": name.strip(), "entity_id": entity_id,
                     "affinity": float(score), "explainability": reasons[:5]})
    if (
        len({x["entity_id"] for x in pool}) != len(pool)
        or len({x["name"].casefold() for x in pool}) != len(pool)
        or len({x["affinity"] for x in pool}) != len(pool)
        or [x["affinity"] for x in pool] != sorted(
            (x["affinity"] for x in pool), reverse=True
        )
    ):
        raise TasteEvidenceError("QLOO_DISCOVERY_RANK_AMBIGUOUS")
    return {
        "status": "CULTURAL_CANDIDATES_OBSERVED",
        "source_mode": "LIVE_HTTP_UNVERIFIED",
        "query_fingerprint": spec.fingerprint,
        "collected_at": observation.utc_collected_at,
        "candidates": pool,
        "policy_qualified": False,
        "business_verified": False,
        "external_action_authorized": False,
    }


def policy_for(category: str) -> dict:
    canonical = category_for(category)
    return {
        "category": canonical,
        "rank_observation": "SCHEMA_SUPPORTED",
        "automatic_repair": (
            "ARTIST_BRAND_TWO_MATCHING_RANKS_ONLY"
            if canonical == "brand" else "NOT_QUALIFIED"
        ),
        "business_proof_required": True,
        "release_authorized": False,
    }
