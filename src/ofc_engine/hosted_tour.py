"""Server-owned tour scope for browser review without provider calls."""
from __future__ import annotations

from collections.abc import Mapping

from .contracts import Decision
from .hosted_agent import validate_hosted_proposal
from .intake import AgentProposal
from .qloo import brand_affinity_probe
from .webdemo import canonical_proposal

_FIELDS = ("OFC_TOUR_ARTIST_ID", "OFC_TOUR_BRAND_IDS", "OFC_TOUR_SELECTED_ID")


def configured_tour(environment: Mapping[str, str]) -> AgentProposal | None:
    raw = tuple(environment.get(name, "").strip() for name in _FIELDS)
    if not any(raw):
        return None
    if not all(raw):
        raise ValueError("incomplete hosted tour scope")
    artist, brand_list, selected = raw
    brands = tuple(value.strip() for value in brand_list.split(","))
    brand_affinity_probe(artist, brands)
    if selected not in brands:
        raise ValueError("tour selected brand outside approved scope")

    example = canonical_proposal()
    brand = next(d for d in example.decisions if d.decision_id == "brand")
    scoped = brand.model_copy(update={
        "candidate_id": selected,
        "candidate_pool": brands,
    })
    intent = example.intent.model_copy(update={
        "constraints": {**example.intent.constraints, "external_action": False},
        "taste_context": {
            **example.intent.taste_context,
            "artist_id": artist,
            "supplemental_decisions": "OPERATOR_CONSTRUCTED_UNVERIFIED",
        },
    })
    proposal = AgentProposal.model_validate({
        "intent": intent.model_dump(),
        "decisions": [
            (scoped if decision.decision_id == "brand" else decision).model_dump()
            for decision in example.decisions
        ],
    })
    return validate_hosted_proposal(
        proposal.model_dump_json().encode("utf-8"),
        artist_id=artist, brand_ids=brands, selected_brand_id=selected,
    )
