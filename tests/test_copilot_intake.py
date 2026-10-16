"""Natural-language plans cannot impersonate Qloo identities."""
import json
import pytest

from ofc_engine.copilot_intake import (
    EntityResolutionRequired, ResolvedEntity, draft_plan,
)
from ofc_engine.copilot_llm import CopilotDraft


def example():
    return CopilotDraft.model_validate_json(json.dumps({
        "objective": "Find an artist's brand and afterparty",
        "artist_name": "Artist Example",
        "budget_usd": 60000,
        "cities": ["Austin"],
        "baseline_answer": "I would propose Brand A with the afterparty.",
        "clarification_question": None,
        "choices": [
            {
                "decision_id": "brand", "domain": "brand",
                "selected_name": "Brand A",
                "candidate_names": ["Brand A", "Brand B"],
                "mutable": True, "depends_on": [],
            },
            {
                "decision_id": "party", "domain": "event",
                "selected_name": "Event A",
                "candidate_names": ["Event A", "Event B"],
                "mutable": True, "depends_on": ["brand"],
            },
        ],
    }))


def resolved(name, value, entity_type):
    return ResolvedEntity(
        name=name, entity_id=value, entity_type="urn:entity:" + entity_type,
        query_fingerprint="a" * 64,
    )


def test_resolved_brand_scope_and_unsupported_domain_marking():
    draft = example()
    proposal = draft_plan(
        draft, run_id="case-1", request_id="request-1",
        resolved_artist=resolved("Artist Example", "artist-real-id", "artist"),
        resolved_brands={
            "Brand A": resolved("Brand A", "brand-real-a", "brand"),
            "Brand B": resolved("Brand B", "brand-real-b", "brand"),
        },
    )
    assert proposal.intent.constraints["external_action"] is False
    assert proposal.intent.constraints["budget_usd"] == 60000
    assert proposal.intent.taste_context["artist_id"] == "artist-real-id"
    assert proposal.decisions[0].candidate_pool == ("brand-real-a", "brand-real-b")
    assert proposal.decisions[0].mutable_fields == ("candidate_id",)
    assert proposal.decisions[1].depends_on == ("brand",)
    assert proposal.decisions[1].mutable_fields == ()
    assert all(x.startswith("unverified:") for x in proposal.decisions[1].candidate_pool)
    assert proposal.intent.taste_context["unverified_decision_domains"] == ["event"]


def test_missing_or_cross_type_entities_fail_closed():
    draft = example()
    artist = resolved("Artist Example", "artist", "artist")
    brands = {"Brand A": resolved("Brand A", "a", "brand")}
    with pytest.raises(EntityResolutionRequired, match="BRAND_RESOLUTION"):
        draft_plan(
            draft, run_id="case-2", request_id="req-2",
            resolved_artist=artist, resolved_brands=brands,
        )
    with pytest.raises(EntityResolutionRequired, match="ARTIST_RESOLUTION"):
        draft_plan(
            draft, run_id="case-2", request_id="req-2",
            resolved_artist=resolved("Artist Example", "artist", "brand"),
            resolved_brands=brands,
        )
    with pytest.raises(EntityResolutionRequired, match="BRAND_RESOLUTION"):
        draft_plan(
            draft, run_id="case-3", request_id="req-3",
            resolved_artist=artist,
            resolved_brands={
                "Brand A": resolved("Brand A", "a", "brand"),
                "Brand B": resolved("Brand B", "b", "artist"),
            },
        )


def test_collision_and_forged_resolution_label_rejected():
    draft = example()
    artist = resolved("Artist Example", "artist", "artist")
    with pytest.raises(EntityResolutionRequired, match="AMBIGUOUS_BRAND"):
        draft_plan(
            draft, run_id="case-4", request_id="req-4",
            resolved_artist=artist,
            resolved_brands={
                "Brand A": resolved("Brand A", "duplicate", "brand"),
                "Brand B": resolved("Brand B", "duplicate", "brand"),
            },
        )
    with pytest.raises(ValueError, match="unqualified"):
        ResolvedEntity(
            name="Brand A", entity_id="qloo-a",
            entity_type="urn:entity:brand",
            query_fingerprint="a" * 64, source="MODEL_GUESSED",
        )
