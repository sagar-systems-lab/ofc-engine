"""Bind proposed names to scoped, resolved Qloo identities without guessing IDs."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from .contracts import ActionIntent, Decision
from .copilot_llm import CopilotDraft
from .intake import AgentProposal

_SUPPORTED = frozenset({"brand", "partner", "brand_partner"})


class EntityResolutionRequired(ValueError):
    pass


class QlooSelectionRequired(EntityResolutionRequired):
    def __init__(self, reason_code: str, suggestions: list[dict], *, requested_name=None, category=None):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.suggestions = suggestions
        self.requested_name = requested_name
        self.category = category


@dataclass(frozen=True)
class ResolvedEntity:
    name: str
    entity_id: str
    entity_type: str
    query_fingerprint: str
    source: str = "QLOO_SEARCH"

    def __post_init__(self):
        if (
            self.source not in ("QLOO_SEARCH", "QLOO_DISCOVERY")
            or not self.name.strip() or not self.entity_id.strip()
            or len(self.entity_id) > 120
            or len(self.query_fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in self.query_fingerprint)
        ):
            raise ValueError("unqualified entity resolution")


def draft_plan(
    draft: CopilotDraft, *,
    run_id: str, request_id: str,
    resolved_artist: ResolvedEntity | None,
    resolved_brands: dict[str, ResolvedEntity],
) -> AgentProposal:
    """Leave unsupported domains explicitly unverified; Qloo IDs only after lookup."""
    if not isinstance(draft, CopilotDraft):
        raise TypeError("typed model extraction required")
    if len(draft.choices) == 0:
        raise EntityResolutionRequired("CLARIFICATION_REQUIRED")
    if (
        resolved_artist is None or resolved_artist.name != (
            draft.taste_seed_name or draft.artist_name
        )
        or (
            resolved_artist.entity_type != "urn:entity:artist"
            and draft.taste_seed_category != resolved_artist.entity_type.removeprefix(
                "urn:entity:"
            )
        )
    ):
        raise EntityResolutionRequired("QLOO_ARTIST_RESOLUTION_REQUIRED")

    decisions = []
    for choice in draft.choices:
        supported = (
            choice.domain.lower() in _SUPPORTED
            and resolved_artist.entity_type == "urn:entity:artist"
        )
        if supported:
            identities = []
            for name in choice.candidate_names:
                resolved = resolved_brands.get(name)
                if (
                    resolved is None or resolved.name != name
                    or resolved.entity_type != "urn:entity:brand"
                ):
                    raise EntityResolutionRequired("QLOO_BRAND_RESOLUTION_REQUIRED")
                identities.append(resolved.entity_id)
            if len(set(identities)) != len(identities):
                raise EntityResolutionRequired("AMBIGUOUS_BRAND_IDENTITY")
            index = choice.candidate_names.index(choice.selected_name)
            selected, pool = identities[index], tuple(identities)
        else:
            # Scoped, private placeholder IDs only; never claim they came from Qloo.
            pool = tuple(
                "unverified:" + sha256(
                    (choice.decision_id + ":" + name).encode()
                ).hexdigest()[:32]
                for name in choice.candidate_names
            )
            selected = pool[choice.candidate_names.index(choice.selected_name)]
        decisions.append(Decision(
            decision_id=choice.decision_id,
            domain=choice.domain,
            candidate_id=selected,
            candidate_pool=pool,
            mutable_fields=("candidate_id",) if supported and choice.mutable else (),
            depends_on=choice.depends_on,
            required=True,
        ))
    proposal = AgentProposal(
        intent=ActionIntent(
            run_id=run_id,
            client_request_id=request_id,
            objective=draft.objective,
            constraints={
                "external_action": False,
                "budget_usd": draft.budget_usd,
                "cities": list(draft.cities),
            },
            taste_context={
                "artist_id": (
                    resolved_artist.entity_id
                    if resolved_artist.entity_type == "urn:entity:artist" else None
                ),
                "taste_seed_id": resolved_artist.entity_id,
                "taste_seed_type": resolved_artist.entity_type,
                "qloo_resolution": "SCOPED_LOOKUP",
                "unverified_decision_domains": sorted({
                    choice.domain for choice in draft.choices
                    if choice.domain.lower() not in _SUPPORTED
                }),
            },
        ),
        decisions=tuple(decisions),
    )
    return proposal
