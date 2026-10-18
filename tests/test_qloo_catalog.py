"""Document-backed cross-domain Qloo adapters without provider network spend."""
from datetime import datetime, timezone

import pytest

from ofc_engine.qloo import ProbeObservation, search_probe
from ofc_engine.qloo_catalog import (
    CATEGORIES, CatalogEvidenceError, capability_registry, category_for,
    domain_affinity_probe, inspect_domain_rank, qloo_suggestions,
)

ART = "c70ce2b8-0ad1-4150-bf18-5a6347f2e860"
ONE = "43a36e73-9bc7-4b09-a116-78e51554c3e3"
TWO = "e26bc58d-18d2-405b-bc7b-7c556ade3829"


def observed(spec, payload):
    return ProbeObservation(
        path=spec.path, query_fingerprint=spec.fingerprint,
        utc_collected_at=datetime.now(timezone.utc).isoformat(),
        duration_ms=1, http_status=200,
        top_level_fields=("results", "success"),
        payload=payload,
    )


@pytest.mark.parametrize("category", CATEGORIES)
def test_every_qloo_category_has_a_bounded_observation_adapter(category):
    spec = domain_affinity_probe(ART, category, (ONE, TWO))
    assert dict(spec.params)["filter.type"] == "urn:entity:" + category
    assert dict(spec.params)["signal.interests.entities"] == ART
    assert dict(spec.params)["filter.results.entities"] == ONE + "," + TWO
    rank = inspect_domain_rank(observed(spec, {
        "success": True, "results": {"entities": [
            {"entity_id": ONE, "type": "urn:entity",
             "query": {"affinity": 0.9}},
            {"entity_id": TWO, "type": "urn:entity",
             "query": {"affinity": 0.5}},
        ]},
    }), spec)
    assert rank["ranking"] == [ONE, TWO]
    assert rank["category"] == category
    assert rank["policy_qualified"] is False
    assert rank["external_action_authorized"] is False
    assert rank["source_mode"] == "LIVE_HTTP_UNVERIFIED"


@pytest.mark.parametrize("bad", [
    {"success": False, "results": {"entities": []}},
    {"success": True, "results": {"entities": []}},
    {"success": True, "results": {"entities": [
        {"entity_id": ONE, "type": "urn:entity", "query": {"affinity": 0.5}},
        {"entity_id": TWO, "type": "urn:entity", "query": {"affinity": 0.5}},
    ]}},
    {"success": True, "results": {"entities": [
        {"entity_id": ONE, "type": "urn:entity", "query": {"affinity": 0.8}},
        {"entity_id": ONE, "type": "urn:entity", "query": {"affinity": 0.5}},
    ]}},
    {"success": True, "results": {"entities": [
        {"entity_id": TWO, "type": "urn:entity", "query": {"affinity": 0.5}},
        {"entity_id": ONE, "type": "urn:entity", "query": {"affinity": 0.9}},
    ]}},
])
def test_catalog_rank_fails_closed_on_wrong_or_tied_scope(bad):
    spec = domain_affinity_probe(ART, "movie", (ONE, TWO))
    with pytest.raises(CatalogEvidenceError):
        inspect_domain_rank(observed(spec, bad), spec)


def test_semantic_domain_mapping_does_not_claim_business_authority():
    assert category_for("restaurant") == "place"
    assert category_for("venue") == "place"
    assert category_for("film") == "movie"
    assert category_for("city") == "destination"
    with pytest.raises(ValueError):
        category_for("contract")
    with pytest.raises(CatalogEvidenceError):
        domain_affinity_probe(ART, "place", (ONE, ONE))
    registry = capability_registry()
    assert set(registry) == set(CATEGORIES)
    assert registry["brand"]["policy_scope"] == "SCOPED_BRAND_RANK_REPEATABILITY"
    assert registry["place"]["policy_scope"] == "OBSERVATION_ONLY_UNQUALIFIED"
    assert all(
        category["independent_business_verification"] is False
        for category in registry.values()
    )


def test_taste_search_returns_fuzzy_choice_hints_but_never_auto_verifies():
    spec = search_probe("Beyonce", entity_type="urn:entity:artist")
    matches = qloo_suggestions("Beyonce", "artist", observed(spec, {
        "success": True, "results": {"entities": [
            {"name": "Beyoncé", "entity_id": ART,
             "type": "urn:entity", "subtype": "urn:entity:artist"},
            {"name": "Beyoncé Tribute Band", "entity_id": ONE,
             "type": "urn:entity", "subtype": "urn:entity:artist"},
            {"name": "Beyonce", "entity_id": TWO,
             "type": "urn:entity", "subtype": "urn:entity:brand"},
        ]},
    }))
    assert matches[0].name == "Beyoncé"
    assert matches[0].exact_name is True
    assert matches[0].entity_id == ART
    assert all(item.entity_type == "urn:entity:artist" for item in matches)
    assert all("entity_id" not in item.safe_label() for item in matches)


def test_qloo_search_results_only_envelope_accepts_scoped_entities():
    from dataclasses import replace

    spec = search_probe("Beyonce", entity_type="urn:entity:artist")
    source = observed(spec, {
        "results": [
            {"name": "Beyoncé", "entity_id": ART,
             "types": ["urn:entity:artist"]},
        ],
    })
    source = replace(source, top_level_fields=("results",))
    hints = qloo_suggestions("Beyonce", "artist", source)
    assert len(hints) == 1 and hints[0].entity_id == ART


def test_mismatched_fingerprint_never_produces_suggestions():
    spec = search_probe("Beyonce", entity_type="urn:entity:brand")
    with pytest.raises(CatalogEvidenceError, match="FINGERPRINT"):
        qloo_suggestions("Beyonce", "artist", observed(spec, {
            "success": True, "results": {"entities": []},
        }))


def test_place_locality_scopes_are_fingerprinted_and_never_policy_qualified():
    chicago = domain_affinity_probe(ART, "place", (ONE, TWO), location="Chicago")
    austin = domain_affinity_probe(ART, "place", (ONE, TWO), location="Austin")
    params = dict(chicago.params)
    assert params["filter.location.query"] == "Chicago"
    assert params["filter.location.radius"] == "0"
    assert chicago.fingerprint != austin.fingerprint
    assert chicago.fingerprint != domain_affinity_probe(
        ART, "place", (ONE, TWO)).fingerprint
    observation = observed(chicago, {
        "success": True,
        "results": {"entities": [
            {"entity_id": ONE, "type": "urn:entity", "query": {"affinity": 0.9}},
            {"entity_id": TWO, "type": "urn:entity", "query": {"affinity": 0.5}},
        ]},
    })
    assert inspect_domain_rank(observation, chicago)["policy_qualified"] is False
    with pytest.raises(CatalogEvidenceError, match="INVALID_PLACE_LOCALITY_SCOPE"):
        domain_affinity_probe(ART, "brand", (ONE, TWO), location="Chicago")
    with pytest.raises(CatalogEvidenceError, match="INVALID_PLACE_LOCALITY_SCOPE"):
        domain_affinity_probe(ART, "place", (ONE, TWO), location="Chicago\nAustin")
    with pytest.raises(CatalogEvidenceError, match="INSIGHTS_PROVENANCE_INVALID"):
        inspect_domain_rank(observation, austin)
