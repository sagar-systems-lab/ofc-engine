"""Qloo-native discovery is a source observation, never a business approval."""
from datetime import datetime, timezone

import pytest
from ofc_engine.qloo import ProbeObservation
from ofc_engine.taste_intelligence import (
    TasteContext, TasteEvidenceError, discovery_spec, normalize_discovery, policy_for,
)

ART = "c70ce2b8-0ad1-4150-bf18-5a6347f2e860"
ONE = "43a36e73-9bc7-4b09-a116-78e51554c3e3"
TWO = "e26bc58d-18d2-405b-bc7b-7c556ade3829"


def captured(spec, values):
    return ProbeObservation(
        path=spec.path, query_fingerprint=spec.fingerprint,
        utc_collected_at=datetime.now(timezone.utc).isoformat(),
        duration_ms=1, http_status=200,
        top_level_fields=("results", "success"),
        payload={"success": True, "results": {"entities": values}},
    )


def candidates():
    return [
        {"name": "Brand B", "entity_id": TWO, "type": "urn:entity",
         "query": {"affinity": 0.85, "explainability": {
             "signal.interests.entities": {ART: 0.62},
         }}},
        {"name": "Brand A", "entity_id": ONE, "type": "urn:entity",
         "query": {"affinity": 0.57}},
    ]


def test_native_recommendation_without_llm_candidates():
    context = TasteContext("Artist Example", "artist", "brand")
    spec = discovery_spec(context, ART)
    assert "filter.results.entities" not in dict(spec.params)
    assert dict(spec.params)["filter.type"] == "urn:entity:brand"
    result = normalize_discovery(captured(spec, candidates()), spec)
    assert result["status"] == "CULTURAL_CANDIDATES_OBSERVED"
    assert [x["name"] for x in result["candidates"]] == ["Brand B", "Brand A"]
    assert result["candidates"][0]["explainability"][0]["weight"] == 0.62
    assert result["policy_qualified"] is False
    assert result["business_verified"] is False


def test_nonaligned_ranks_duplicate_entity_and_wrong_fingerprint_blocked():
    spec = discovery_spec(TasteContext("Artist Example", "artist", "movie"), ART)
    for values in (
        candidates()[::-1],
        [{**candidates()[0], "name": "Other"}, candidates()[0]],
        [candidates()[0]],
    ):
        with pytest.raises(TasteEvidenceError):
            normalize_discovery(captured(spec, values), spec)
    incorrect = discovery_spec(TasteContext("Artist Example", "artist", "brand"), ART)
    with pytest.raises(TasteEvidenceError, match="PROVENANCE"):
        normalize_discovery(captured(incorrect, candidates()), spec)


def test_context_location_typed_and_unsupported_policy_constrained():
    contextual = TasteContext("Movie Example", "movie", "place", "New York")
    spec = discovery_spec(contextual, ART)
    assert dict(spec.params)["filter.location.query"] == "New York"
    with pytest.raises(TasteEvidenceError):
        TasteContext("Artist Example", "artist", "brand", "New York")
    with pytest.raises(ValueError):
        TasteContext("Artist Example", "artist", "contract")
    assert policy_for("brand")["automatic_repair"] == "ARTIST_BRAND_TWO_MATCHING_RANKS_ONLY"
    assert policy_for("movie")["automatic_repair"] == "NOT_QUALIFIED"
    assert all(policy_for(k)["release_authorized"] is False for k in ("brand", "movie", "place"))



def test_discovery_error_codes_distinguish_provider_from_evidence():
    from ofc_engine.copilot_domain_observations import (
        _discovery_provider_reason,
        _discovery_evidence_reason,
    )
    assert _discovery_provider_reason("HTTP_429") == "QLOO_DISCOVERY_PROVIDER_RATE_LIMITED"
    assert _discovery_provider_reason("HTTP_401") == "QLOO_DISCOVERY_PROVIDER_AUTH_REJECTED"
    assert _discovery_provider_reason("HTTP_503") == "QLOO_DISCOVERY_PROVIDER_UNAVAILABLE"
    assert _discovery_provider_reason("NETWORK_ERROR") == "QLOO_DISCOVERY_PROVIDER_NETWORK_UNAVAILABLE"
    assert _discovery_provider_reason("HTTP_400") == "QLOO_DISCOVERY_PROVIDER_REJECTED"
    assert _discovery_evidence_reason(
        "QLOO_DISCOVERY_INSUFFICIENT_RESULTS"
    ) == "QLOO_DISCOVERY_INSUFFICIENT_RESULTS"
    assert _discovery_evidence_reason(
        "QLOO_DISCOVERY_RANK_AMBIGUOUS"
    ) == "QLOO_DISCOVERY_RANK_AMBIGUOUS"
    assert _discovery_evidence_reason(
        "QLOO_DISCOVERY_PROVENANCE_MISMATCH"
    ) == "QLOO_DISCOVERY_EVIDENCE_UNQUALIFIED"
