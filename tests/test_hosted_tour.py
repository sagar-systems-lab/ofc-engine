"""Bounded six-decision server-owned tour intake."""
import pytest

from ofc_engine.hosted_tour import configured_tour
from ofc_engine.intake import freeze_proposal


def scope():
    return {
        "OFC_TOUR_ARTIST_ID": "artist-ci",
        "OFC_TOUR_BRAND_IDS": "brand-a,brand-b",
        "OFC_TOUR_SELECTED_ID": "brand-a",
    }


def test_server_builds_exact_six_decision_tour_without_qloo_request():
    tour = configured_tour(scope())
    assert tour is not None
    assert len(tour.decisions) == 6
    assert tour.intent.taste_context["artist_id"] == "artist-ci"
    assert tour.intent.taste_context["supplemental_decisions"] == (
        "OPERATOR_CONSTRUCTED_UNVERIFIED"
    )
    assert tour.intent.constraints["external_action"] is False
    assert tour.intent.constraints["cities"] == ["Chicago", "Austin", "Seattle"]
    brand = tour.decisions[0]
    assert brand.decision_id == "brand"
    assert brand.domain == "partner"
    assert brand.candidate_id == "brand-a"
    assert brand.candidate_pool == ("brand-a", "brand-b")
    assert brand.mutable_fields == ("candidate_id",)
    assert tour.decisions[-1].depends_on == ("brand",)
    baseline, _ = freeze_proposal(tour)
    assert baseline.revision == 0
    assert baseline.plan_id == tour.intent.run_id


@pytest.mark.parametrize("overrides", [
    {"OFC_TOUR_ARTIST_ID": ""},
    {"OFC_TOUR_BRAND_IDS": ""},
    {"OFC_TOUR_SELECTED_ID": ""},
    {"OFC_TOUR_SELECTED_ID": "out-of-pool"},
    {"OFC_TOUR_BRAND_IDS": "brand-a,brand-a"},
])
def test_partial_or_conflicting_scope_refused(overrides):
    with pytest.raises(ValueError):
        configured_tour({**scope(), **overrides})


def test_disabled_scope_never_enables_server_owned_tour():
    assert configured_tour({}) is None
    assert configured_tour({"QLOO_API_KEY": "fake-ci-only"}) is None
