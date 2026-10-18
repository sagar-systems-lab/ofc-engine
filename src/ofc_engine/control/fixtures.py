from __future__ import annotations

from datetime import timedelta

from .graph import claim_fingerprint
from .models import Actor, Candidate, Edge, Evidence, Node, Plan, ReviewRequest, instant

FIXTURE_TIME = "2026-10-18T12:00:00Z"


def campaign(case: str = "repair", *, tenant: str = "sandbox", original: str = "nike") -> ReviewRequest:
    if case not in {"repair", "preserve", "abstain", "conflict", "stale"}:
        raise ValueError("unknown scenario")
    root = Node(
        id="B01", domain="brand", label="Campaign partner", candidate_id=original,
        candidate_pool=tuple(Candidate(id=x, label=x.title()) for x in ("nike", "adidas", "puma")),
        mutable_fields=("candidate_id",), owner="campaign-agent", claims=("cultural_brand_rank", "contract_rights"),
        subject_id="synthetic-artist",
    )
    descriptions = (
        ("Chicago", "experience", "Branded pop-up", "B01"),
        ("Austin", "experience", "Fan activation", "B01"),
        ("Seattle", "experience", "Listening room", "B01"),
        ("Chicago", "channel", "Creative placement", "D01"),
        ("Austin", "channel", "Channel placement", "D02"),
        ("Seattle", "channel", "Partnership placement", "D03"),
        ("Chicago", "budget", "Sponsor allocation", "D01"),
        ("Austin", "collaborator", "Sponsor collaborator", "D02"),
        ("Seattle", "schedule", "Launch timeline", "D03"),
    )
    nodes, edges = [root], []
    for index, (city, domain, label, parent) in enumerate(descriptions, 1):
        key = f"D{index:02}"
        nodes.append(Node(id=key, domain=domain, label=label, locality=city,
                          candidate_id="planned", candidate_pool=(Candidate(id="planned", label=label),),
                          owner="campaign-agent", claims=("contract_rights",)))
        edges.append(Edge(source=parent, target=key, read_set=(f"{parent}.candidate_id",)))
    independent = ("City date", "Event window", "Artist window", "Neutral venue", "Neutral venue", "Neutral venue",
                   "Travel leg", "Travel leg", "Travel leg", "Local arts", "Local arts", "Local arts",
                   "Staffing", "Accessibility", "Site operations", "Public transport", "Public transport",
                   "Public transport", "Reporting", "Artist account control")
    for index, label in enumerate(independent, 1):
        nodes.append(Node(id=f"I{index:02}", domain="operations", label=label,
                          locality=("Chicago", "Austin", "Seattle")[(index - 1) % 3],
                          candidate_id="planned", candidate_pool=(Candidate(id="planned", label=label),),
                          owner="operations-agent", claims=("contract_rights",)))
    if case == "conflict":
        edges.append(Edge(source="B01", target="I01", type="CONFLICTS_WITH",
                          candidate_pairs=(("adidas", "planned"),)))
    plan = Plan(tenant_id=tenant, actor=Actor(id="campaign-agent"), idempotency_key=f"campaign-{case}-{original}",
                objective="Review a hypothetical three-city campaign before any commitment.",
                nodes=tuple(nodes), edges=tuple(edges))
    ranks = ("nike", "adidas", "puma") if case == "preserve" else ("adidas", "puma", "nike")
    evidence = []
    for index in (1, 2):
        order = ("puma", "adidas", "nike") if case == "abstain" and index == 2 else ranks
        expiry = "2026-10-18T11:00:00Z" if case == "stale" else "2026-10-19T12:00:00Z"
        observed = "2026-10-18T10:00:00Z" if case == "stale" else (instant(FIXTURE_TIME) - timedelta(seconds=3 - index)).isoformat().replace("+00:00", "Z")
        evidence.append(Evidence(
            id=f"brand-observation-{index}", tenant_id=tenant, plan_digest=plan.digest,
            node_id="B01", claim="cultural_brand_rank", subject_id="synthetic-artist",
            candidate_pool=("nike", "adidas", "puma"), input_digest=claim_fingerprint(plan, "B01", "cultural_brand_rank"),
            source="fixture", mode="SYNTHETIC", attempt_id=f"attempt-{index}",
            observed_at=observed, expires_at=expiry, policy_version="control-policy/1",
            license_scope="synthetic_public", ranks=order,
        ))
    return ReviewRequest(plan=plan, evidence=tuple(evidence), mode="SYNTHETIC", as_of=FIXTURE_TIME)
