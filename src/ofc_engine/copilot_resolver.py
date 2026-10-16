"""Strict name-to-Qloo ID normalization after a budgeted /search observation."""
from __future__ import annotations

from uuid import UUID

from .copilot_intake import EntityResolutionRequired, ResolvedEntity
from .qloo import ProbeObservation, ProbeSpec, search_probe


def normalize_qloo_search(
    requested_name: str, *, entity_type: str, spec: ProbeSpec,
    observation: ProbeObservation,
) -> ResolvedEntity:
    """Never silently choose the first fuzzy search result."""
    if entity_type not in ("artist", "brand"):
        raise EntityResolutionRequired("UNQUALIFIED_SEARCH_CATEGORY")
    expected = search_probe(requested_name, entity_type="urn:entity:" + entity_type)
    if (
        not isinstance(observation, ProbeObservation)
        or spec != expected
        or observation.path != "/search"
        or observation.query_fingerprint != spec.fingerprint
        or observation.http_status != 200
    ):
        raise EntityResolutionRequired("SEARCH_PROVENANCE_MISMATCH")

    payload = observation.payload
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise EntityResolutionRequired("SEARCH_RESPONSE_INVALID")
    results = payload.get("results")
    if not isinstance(results, dict):
        raise EntityResolutionRequired("SEARCH_RESULTS_MISSING")
    entities = results.get("entities")
    if not isinstance(entities, list) or len(entities) > 20:
        raise EntityResolutionRequired("SEARCH_ENTITIES_UNQUALIFIED")

    normalized = requested_name.strip().casefold()
    matched = []
    for entity in entities:
        if not isinstance(entity, dict):
            raise EntityResolutionRequired("SEARCH_ENTITY_MALFORMED")
        name, entity_id = entity.get("name"), entity.get("entity_id")
        if not isinstance(name, str) or not isinstance(entity_id, str):
            raise EntityResolutionRequired("SEARCH_ENTITY_MALFORMED")
        if name.strip().casefold() != normalized:
            continue
        # /search queries specify types. Generic taxonomy is accepted only
        # when its subtype explicitly resolves the requested Qloo category.
        expected_type = "urn:entity:" + entity_type
        kind = entity.get("type")
        subtype = entity.get("subtype")
        if (
            kind != expected_type and
            not (kind == "urn:entity" and subtype == expected_type)
        ):
            continue
        try:
            entity_uuid = str(UUID(entity_id))
        except (ValueError, AttributeError):
            raise EntityResolutionRequired("SEARCH_ENTITY_ID_UNQUALIFIED") from None
        matched.append((name, entity_uuid))

    if not matched:
        raise EntityResolutionRequired("SEARCH_EXACT_NAME_NOT_FOUND")
    if len(matched) != 1:
        raise EntityResolutionRequired("SEARCH_AMBIGUOUS_ENTITY")
    name, entity_id = matched[0]
    return ResolvedEntity(
        name=requested_name, entity_id=entity_id,
        entity_type="urn:entity:" + entity_type,
        query_fingerprint=spec.fingerprint,
    )
