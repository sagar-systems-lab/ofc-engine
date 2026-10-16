"""Qloo name resolution: exact normalized identity or user-visible suggestions."""
from __future__ import annotations

from .copilot_intake import (
    EntityResolutionRequired, QlooSelectionRequired, ResolvedEntity,
)
from .qloo import ProbeObservation, ProbeSpec, search_probe
from .qloo_catalog import (
    CatalogEvidenceError, category_for, qloo_suggestions,
)


def normalize_qloo_search(
    requested_name: str, *, entity_type: str, spec: ProbeSpec,
    observation: ProbeObservation,
) -> ResolvedEntity:
    """A fuzzy hit is a suggestion, never an implicitly verified Qloo ID."""
    try:
        category = category_for(entity_type)
    except ValueError:
        raise EntityResolutionRequired("UNQUALIFIED_SEARCH_CATEGORY") from None
    expected = search_probe(
        requested_name, entity_type="urn:entity:" + category,
    )
    if (
        not isinstance(observation, ProbeObservation)
        or spec != expected or observation.path != "/search"
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
    if not isinstance(results.get("entities"), list) or len(
        results["entities"]
    ) > 20:
        raise EntityResolutionRequired("SEARCH_ENTITIES_UNQUALIFIED")
    try:
        matches = qloo_suggestions(requested_name, category, observation, limit=8)
    except CatalogEvidenceError as exc:
        if str(exc) == "INVALID_QLOO_ENTITY_ID":
            raise EntityResolutionRequired("SEARCH_ENTITY_ID_UNQUALIFIED") from None
        raise EntityResolutionRequired("SEARCH_ENTITY_MALFORMED") from None
    exact = [item for item in matches if item.exact_name]
    if len(exact) > 1:
        raise QlooSelectionRequired(
            "SEARCH_AMBIGUOUS_ENTITY",
            [item.safe_label() for item in matches],
        )
    if not exact:
        if matches:
            raise QlooSelectionRequired(
                "SEARCH_SELECTION_REQUIRED",
                [item.safe_label() for item in matches],
            )
        raise EntityResolutionRequired("SEARCH_EXACT_NAME_NOT_FOUND")
    found = exact[0]
    return ResolvedEntity(
        name=requested_name,
        entity_id=found.entity_id,
        entity_type="urn:entity:" + category,
        query_fingerprint=spec.fingerprint,
    )
