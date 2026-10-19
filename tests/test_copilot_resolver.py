"""Strict search normalization: no arbitrary names or category confusion."""
from datetime import datetime, timezone
import pytest

from ofc_engine.copilot_intake import EntityResolutionRequired
from ofc_engine.copilot_resolver import normalize_qloo_search
from ofc_engine.qloo import ProbeObservation, search_probe

_A = "c70ce2b8-0ad1-4150-bf18-5a6347f2e860"
_B = "43a36e73-9bc7-4b09-a116-78e51554c3e3"


def observation(name="Example Artist", *, records=None, success=True):
    spec = search_probe(name, entity_type="urn:entity:artist")
    return spec, ProbeObservation(
        path="/search",
        query_fingerprint=spec.fingerprint,
        utc_collected_at=datetime.now(timezone.utc).isoformat(),
        duration_ms=0.5,
        http_status=200,
        top_level_fields=("success", "results"),
        payload={"success": success, "results": {
            "entities": records if records is not None else [{
                "name": "Example Artist", "entity_id": _A,
                "type": "urn:entity", "subtype": "urn:entity:artist",
            }],
        }},
    )


def test_real_search_envelope_without_success_is_accepted():
    from dataclasses import replace

    spec, source = observation()
    payload = {"results": [{
        "name": "Example Artist", "entity_id": _A,
        "types": ["urn:entity:artist"],
    }]}
    source = replace(source, payload=payload, top_level_fields=("results",))
    resolved = normalize_qloo_search(
        "Example Artist", entity_type="artist", spec=spec,
        observation=source,
    )
    assert resolved.entity_id == _A


def test_explicit_qloo_search_failure_still_fails_closed():
    spec, source = observation(success=False)
    with pytest.raises(EntityResolutionRequired, match="SEARCH_RESPONSE_INVALID"):
        normalize_qloo_search(
            "Example Artist", entity_type="artist", spec=spec,
            observation=source,
        )


def test_exact_normalized_name_has_scoped_qloo_identity():
    spec, source = observation()
    resolved = normalize_qloo_search(
        "Example Artist", entity_type="artist", spec=spec,
        observation=source,
    )
    assert resolved.name == "Example Artist"
    assert resolved.entity_id == _A
    assert resolved.query_fingerprint == spec.fingerprint


def test_no_ambiguous_fuzzy_or_cross_category_claims():
    spec, source = observation(records=[
        {"name": "Example Artist", "entity_id": _A,
         "type": "urn:entity", "subtype": "urn:entity:artist"},
        {"name": "Example Artist", "entity_id": _B,
         "type": "urn:entity", "subtype": "urn:entity:artist"},
    ])
    with pytest.raises(EntityResolutionRequired, match="AMBIGUOUS"):
        normalize_qloo_search(
            "Example Artist", entity_type="artist",
            spec=spec, observation=source,
        )
    spec, source = observation(records=[
        {"name": "Example Artist Remix", "entity_id": _A,
         "type": "urn:entity", "subtype": "urn:entity:artist"},
    ])
    with pytest.raises(EntityResolutionRequired, match="SELECTION_REQUIRED"):
        normalize_qloo_search(
            "Example Artist", entity_type="artist",
            spec=spec, observation=source,
        )
    spec, source = observation(records=[
        {"name": "Example Artist", "entity_id": _A,
         "type": "urn:entity", "subtype": "urn:entity:brand"},
    ])
    with pytest.raises(EntityResolutionRequired, match="EXACT_NAME"):
        normalize_qloo_search(
            "Example Artist", entity_type="artist",
            spec=spec, observation=source,
        )


def test_wrong_query_fingerprint_and_malformed_entity_fail_closed():
    spec, source = observation()
    with pytest.raises(EntityResolutionRequired, match="PROVENANCE"):
        normalize_qloo_search(
            "Another Artist", entity_type="artist",
            spec=spec, observation=source,
        )
    spec, source = observation(records=[{
        "name": "Example Artist", "entity_id": "not-qloo-uuid",
        "type": "urn:entity", "subtype": "urn:entity:artist",
    }])
    with pytest.raises(EntityResolutionRequired, match="ID_UNQUALIFIED"):
        normalize_qloo_search(
            "Example Artist", entity_type="artist",
            spec=spec, observation=source,
        )


def test_accent_folded_identity_accepts_only_unambiguous_exact_name():
    spec, source = observation(name="Beyonce", records=[
        {"name": "Beyoncé", "entity_id": _A,
         "type": "urn:entity", "subtype": "urn:entity:artist"},
    ])
    resolved = normalize_qloo_search(
        "Beyonce", entity_type="artist", spec=spec, observation=source,
    )
    assert resolved.entity_id == _A
    assert resolved.entity_type == "urn:entity:artist"


def test_fuzzy_mismatch_provides_selection_hints_not_provider_guesses():
    from ofc_engine.copilot_intake import QlooSelectionRequired
    spec, source = observation(name="Taylor Swft", records=[
        {"name": "Taylor Swift", "entity_id": _A,
         "type": "urn:entity", "subtype": "urn:entity:artist"},
    ])
    with pytest.raises(QlooSelectionRequired, match="SELECTION_REQUIRED") as err:
        normalize_qloo_search(
            "Taylor Swft", entity_type="artist", spec=spec,
            observation=source,
        )
    hints = err.value.suggestions
    assert hints[0]["name"] == "Taylor Swift"
    assert len(hints[0]["option_ref"]) == 16
    assert "entity_id" not in hints[0]


def test_non_exact_artist_brand_search_retains_specific_user_query():
    from ofc_engine.copilot_intake import QlooSelectionRequired
    spec = search_probe("Levi's", entity_type="urn:entity:brand")
    result = ProbeObservation(
        path="/search", query_fingerprint=spec.fingerprint,
        utc_collected_at=datetime.now(timezone.utc).isoformat(), duration_ms=1.0,
        http_status=200, top_level_fields=("results",),
        payload={"results": {"entities": [{
            "name": "Levi Strauss & Co.", "entity_id": _A,
            "type": "urn:entity", "subtype": "urn:entity:brand",
        }]}},
    )
    with pytest.raises(QlooSelectionRequired, match="SEARCH_SELECTION_REQUIRED") as error:
        normalize_qloo_search("Levi's", entity_type="brand", spec=spec, observation=result)
    assert error.value.requested_name == "Levi's"
    assert error.value.category == "brand"
    assert error.value.suggestions and "entity_id" not in error.value.suggestions[0]
