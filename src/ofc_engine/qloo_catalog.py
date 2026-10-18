"""Qloo category contracts and evidence-only cross-domain insight normalization."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from difflib import SequenceMatcher
from math import isfinite
import re
import unicodedata
from uuid import UUID

from .qloo import ProbeObservation, ProbeSpec, qloo_search_entities

CATEGORIES = (
    "artist", "book", "brand", "destination", "movie", "person",
    "place", "podcast", "tv_show", "videogame",
)
ALIASES = {
    "restaurant": "place", "restaurants": "place",
    "venue": "place", "venues": "place", "hotel": "place",
    "cinema": "movie", "film": "movie", "films": "movie",
    "series": "tv_show", "tv": "tv_show",
    "travel": "destination", "city": "destination",
    "sponsor": "brand", "partner": "brand", "brand_partner": "brand",
    "game": "videogame", "games": "videogame",
}
SCHEMA_REFERENCE = "https://github.com/qloo/docs-public/blob/main/reference/insights-api-deep-dive.md"


class CatalogEvidenceError(ValueError):
    pass


def category_for(domain: str) -> str:
    if not isinstance(domain, str):
        raise ValueError("invalid domain")
    canonical = ALIASES.get(domain.strip().lower(), domain.strip().lower())
    if canonical not in CATEGORIES:
        raise ValueError("QLOO_CATEGORY_NOT_SUPPORTED")
    return canonical


def _uuid(value: str) -> str:
    if not isinstance(value, str):
        raise CatalogEvidenceError("INVALID_QLOO_ENTITY_ID")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise CatalogEvidenceError("INVALID_QLOO_ENTITY_ID") from None


def _fold(name: str) -> str:
    value = unicodedata.normalize("NFKD", name).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)


@dataclass(frozen=True)
class EntitySuggestion:
    name: str
    entity_id: str
    entity_type: str
    request_fingerprint: str
    exact_name: bool
    similarity: float

    def safe_label(self) -> dict:
        return {
            "name": self.name,
            "category": self.entity_type.removeprefix("urn:entity:"),
            "is_exact": self.exact_name,
            "option_ref": sha256(
                (self.request_fingerprint + ":" + self.entity_id).encode()
            ).hexdigest()[:16],
        }


def qloo_suggestions(
    query: str, category: str, observation: ProbeObservation,
    *, limit: int = 5,
) -> tuple[EntitySuggestion, ...]:
    """Suggestions never become verified identities without explicit selection."""
    canonical = category_for(category)
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120:
        raise CatalogEvidenceError("INVALID_SEARCH_QUERY")
    if type(limit) is not int or not 1 <= limit <= 10:
        raise CatalogEvidenceError("INVALID_SEARCH_LIMIT")
    if (
        not isinstance(observation, ProbeObservation)
        or observation.path != "/search" or observation.http_status != 200
        or not isinstance(observation.payload, dict)
        or qloo_search_entities(observation.payload) is None
    ):
        raise CatalogEvidenceError("SEARCH_OBSERVATION_INVALID")
    spec = ProbeSpec(
        "/search", (("query", query.strip()), ("types", "urn:entity:" + canonical), ("take", "8")),
    )
    if observation.query_fingerprint != spec.fingerprint:
        raise CatalogEvidenceError("SEARCH_FINGERPRINT_MISMATCH")
    entities = qloo_search_entities(observation.payload)
    if not isinstance(entities, list):
        raise CatalogEvidenceError("SEARCH_RESULTS_INVALID")
    looked_up = _fold(query.strip())
    matches = []
    for entity in entities:
        if not isinstance(entity, dict):
            raise CatalogEvidenceError("SEARCH_ENTITY_INVALID")
        name = entity.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise CatalogEvidenceError("SEARCH_NAME_INVALID")
        expected_type = "urn:entity:" + canonical
        types = entity.get("types")
        category_matches = (
            isinstance(types, list) and expected_type in types
            and all(isinstance(item, str) for item in types)
        ) or (
            entity.get("type") == expected_type
            or entity.get("type") == "urn:entity"
            and entity.get("subtype") == expected_type
        )
        if not category_matches:
            continue
        entity_id = _uuid(entity.get("entity_id"))
        exact = _fold(name) == looked_up
        similarity = SequenceMatcher(None, looked_up, _fold(name)).ratio()
        if exact or similarity >= 0.55:
            matches.append(EntitySuggestion(
                name=name, entity_id=entity_id, entity_type=expected_type,
                request_fingerprint=spec.fingerprint, exact_name=exact,
                similarity=round(similarity, 4),
            ))
    unique = {}
    for item in matches:
        unique[item.entity_id] = item
    return tuple(sorted(
        unique.values(), key=lambda item: (
            not item.exact_name, -item.similarity, item.name.casefold(),
        ),
    )[:limit])


def domain_affinity_probe(
    subject_id: str, category: str, candidate_ids: tuple[str, ...],
    *, location: str | None = None,
) -> ProbeSpec:
    """Strict candidate/locality request; place evidence remains observation-only."""
    canonical = category_for(category)
    subject = _uuid(subject_id)
    if (
        not isinstance(candidate_ids, tuple) or not 2 <= len(candidate_ids) <= 5
        or len(set(candidate_ids)) != len(candidate_ids)
    ):
        raise CatalogEvidenceError("INVALID_CANDIDATE_SCOPE")
    pool = tuple(_uuid(x) for x in candidate_ids)
    if len(set(pool)) != len(pool):
        raise CatalogEvidenceError("DUPLICATE_CANDIDATE_SCOPE")
    params = [
        ("filter.type", "urn:entity:" + canonical),
        ("signal.interests.entities", subject),
        ("filter.results.entities", ",".join(pool)),
        ("take", str(len(pool))),
        ("feature.explainability", "true"),
    ]
    if location is not None:
        if (canonical != "place" or not isinstance(location, str)
                or not 2 <= len(location.strip()) <= 100
                or any(ord(c) < 32 for c in location)):
            raise CatalogEvidenceError("INVALID_PLACE_LOCALITY_SCOPE")
        params.extend((
            ("filter.location.query", location.strip()),
            ("filter.location.radius", "0"),
        ))
    return ProbeSpec("/v2/insights", tuple(params))


def inspect_domain_rank(observation: ProbeObservation, spec: ProbeSpec) -> dict:
    """Safely inspect observed relative affinity, never grant PASS or REPAIR."""
    if (
        not isinstance(observation, ProbeObservation)
        or observation.path != spec.path
        or spec.path != "/v2/insights"
        or observation.http_status != 200
        or observation.query_fingerprint != spec.fingerprint
    ):
        raise CatalogEvidenceError("INSIGHTS_PROVENANCE_INVALID")
    params = dict(spec.params)
    kind = params.get("filter.type", "")
    if not kind.startswith("urn:entity:") or kind[11:] not in CATEGORIES:
        raise CatalogEvidenceError("INVALID_INSIGHTS_CATEGORY")
    ids = params.get("filter.results.entities", "").split(",")
    if not 2 <= len(ids) <= 5 or len(set(ids)) != len(ids):
        raise CatalogEvidenceError("INVALID_INSIGHTS_POOL")
    body = observation.payload
    if not isinstance(body, dict) or body.get("success") is not True:
        raise CatalogEvidenceError("INSIGHTS_FAILURE")
    results = body.get("results")
    entities = results.get("entities") if isinstance(results, dict) else None
    if not isinstance(entities, list) or len(entities) != len(ids):
        raise CatalogEvidenceError("INCOMPLETE_INSIGHTS")
    ranking = []
    for value in entities:
        if not isinstance(value, dict):
            raise CatalogEvidenceError("INVALID_INSIGHTS_ENTITY")
        entity_id = value.get("entity_id")
        q = value.get("query")
        affinity = q.get("affinity") if isinstance(q, dict) else None
        if (
            entity_id not in ids or value.get("type") != "urn:entity"
            or type(affinity) not in (float, int) or not isfinite(affinity)
        ):
            raise CatalogEvidenceError("INSIGHTS_ENTITY_UNQUALIFIED")
        ranking.append((entity_id, float(affinity)))
    if (
        len({x[0] for x in ranking}) != len(ids)
        or len({x[1] for x in ranking}) != len(ids)
        or [v for _, v in ranking] != sorted(
            [v for _, v in ranking], reverse=True
        )
    ):
        raise CatalogEvidenceError("INSIGHTS_RANKING_AMBIGUOUS")
    return {
        "category": kind[11:],
        "query_fingerprint": spec.fingerprint,
        "collected_at": observation.utc_collected_at,
        "ranking": [entity_id for entity_id, _ in ranking],
        "source_mode": "LIVE_HTTP_UNVERIFIED",
        "policy_qualified": False,
        "external_action_authorized": False,
    }


def capability_registry() -> dict:
    """Availability in Qloo's published schema != OFC production qualification."""
    return {
        category: {
            "search_adapter": "SCHEMA_SUPPORTED",
            "insights_adapter": "SCHEMA_SUPPORTED",
            "policy_scope": (
                "SCOPED_BRAND_RANK_REPEATABILITY"
                if category == "brand" else "OBSERVATION_ONLY_UNQUALIFIED"
            ),
            "independent_business_verification": False,
        }
        for category in CATEGORIES
    }
