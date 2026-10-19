from __future__ import annotations

from datetime import timedelta

from .graph import claim_fingerprint
from .models import Actor, Candidate, Edge, Evidence, Limits, Node, Plan, ReviewRequest, instant

FIXTURE_TIME = "2026-10-18T12:00:00Z"


def campaign(
    case: str = "repair", *, tenant: str = "sandbox", original: str = "nike"
) -> ReviewRequest:
    if case not in {"repair", "preserve", "abstain", "conflict", "stale"}:
        raise ValueError("unknown scenario")
    root = Node(
        id="B01",
        domain="brand",
        label="Campaign partner",
        candidate_id=original,
        candidate_pool=tuple(
            Candidate(id=x, label=x.title()) for x in ("nike", "adidas", "puma")
        ),
        mutable_fields=("candidate_id",),
        owner="campaign-agent",
        claims=("cultural_brand_rank", "contract_rights"),
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
        nodes.append(
            Node(
                id=key,
                domain=domain,
                label=label,
                locality=city,
                candidate_id="planned",
                candidate_pool=(Candidate(id="planned", label=label),),
                owner="campaign-agent",
                claims=("contract_rights",),
            )
        )
        edges.append(
            Edge(source=parent, target=key, read_set=(f"{parent}.candidate_id",))
        )
    independent = (
        "City date",
        "Event window",
        "Artist window",
        "Neutral venue",
        "Neutral venue",
        "Neutral venue",
        "Travel leg",
        "Travel leg",
        "Travel leg",
        "Local arts",
        "Local arts",
        "Local arts",
        "Staffing",
        "Accessibility",
        "Site operations",
        "Public transport",
        "Public transport",
        "Public transport",
        "Reporting",
        "Artist account control",
    )
    for index, label in enumerate(independent, 1):
        nodes.append(
            Node(
                id=f"I{index:02}",
                domain="operations",
                label=label,
                locality=("Chicago", "Austin", "Seattle")[(index - 1) % 3],
                candidate_id="planned",
                candidate_pool=(Candidate(id="planned", label=label),),
                owner="operations-agent",
                claims=("contract_rights",),
            )
        )
    if case == "conflict":
        edges.append(
            Edge(
                source="B01",
                target="I01",
                type="CONFLICTS_WITH",
                candidate_pairs=(("adidas", "planned"),),
            )
        )
    plan = Plan(
        tenant_id=tenant,
        actor=Actor(id="campaign-agent"),
        idempotency_key=f"campaign-{case}-{original}",
        objective="Review a hypothetical three-city campaign before any commitment.",
        nodes=tuple(nodes),
        edges=tuple(edges),
        limits=Limits(deadline_ms=2000),
    )
    ranks = (
        ("nike", "adidas", "puma") if case == "preserve" else ("adidas", "puma", "nike")
    )
    evidence = []
    for index in (1, 2):
        order = (
            ("puma", "adidas", "nike") if case == "abstain" and index == 2 else ranks
        )
        expiry = "2026-10-18T11:00:00Z" if case == "stale" else "2026-10-19T12:00:00Z"
        observed = (
            "2026-10-18T10:00:00Z"
            if case == "stale"
            else (instant(FIXTURE_TIME) - timedelta(seconds=3 - index))
            .isoformat()
            .replace("+00:00", "Z")
        )
        evidence.append(
            Evidence(
                id=f"brand-observation-{index}",
                tenant_id=tenant,
                plan_digest=plan.digest,
                node_id="B01",
                claim="cultural_brand_rank",
                subject_id="synthetic-artist",
                candidate_pool=("nike", "adidas", "puma"),
                input_digest=claim_fingerprint(plan, "B01", "cultural_brand_rank"),
                source="fixture",
                mode="SYNTHETIC",
                attempt_id=f"attempt-{index}",
                observed_at=observed,
                expires_at=expiry,
                policy_version="control-policy/1",
                license_scope="synthetic_public",
                ranks=order,
            )
        )
    return ReviewRequest(
        plan=plan, evidence=tuple(evidence), mode="SYNTHETIC", as_of=FIXTURE_TIME
    )


def cascading_campaign(*, max_rounds=3):
    """A separately scoped creative authorization becomes usable after the brand correction."""
    request = campaign()
    payload = request.plan.data()
    dependent = payload["nodes"][1]
    dependent["candidate_pool"].append(
        {"id": "refreshed", "label": "Revised partner creative"}
    )
    dependent["mutable_fields"] = ["candidate_id"]
    payload["limits"]["max_rounds"] = max_rounds
    payload["limits"]["deadline_ms"] = 2000
    plan = Plan.parse(payload)
    records = [
        e.model_copy(update={"plan_digest": plan.digest}) for e in request.evidence
    ]
    from .engine import apply_patches
    from .models import Patch

    future = apply_patches(
        plan,
        (
            Patch(
                node_id="B01",
                changes={"candidate_id": "adidas"},
                evidence_ids=("fixture-license",),
            ),
            Patch(
                node_id="D01",
                changes={"candidate_id": "refreshed"},
                evidence_ids=("fixture-license",),
            ),
        ),
    )
    records.append(
        Evidence(
            id="corrected-partner-rights",
            tenant_id=plan.tenant_id,
            plan_digest=plan.digest,
            node_id="B01",
            claim="contract_rights",
            subject_id="adidas",
            candidate_pool=("nike", "adidas", "puma"),
            input_digest=claim_fingerprint(future, "B01", "contract_rights"),
            source="fixture",
            mode="SYNTHETIC",
            attempt_id="corrected-partner-rights",
            observed_at="2026-10-18T11:59:00Z",
            expires_at="2026-10-19T12:00:00Z",
            policy_version="control-policy/1",
            license_scope="synthetic_public",
            facts={
                "signed_contract": True,
                "scope_covers_plan": True,
                "candidate_id": "adidas",
                "contract_ref": "synthetic-partner-contract",
            },
        )
    )
    records.append(
        Evidence(
            id="dependent-creative-rights",
            tenant_id=plan.tenant_id,
            plan_digest=plan.digest,
            node_id="D01",
            claim="contract_rights",
            subject_id="refreshed",
            candidate_pool=("planned", "refreshed"),
            locality=dependent["locality"],
            input_digest=claim_fingerprint(future, "D01", "contract_rights"),
            source="fixture",
            mode="SYNTHETIC",
            attempt_id="creative-rights-attempt",
            observed_at="2026-10-18T11:59:00Z",
            expires_at="2026-10-19T12:00:00Z",
            policy_version="control-policy/1",
            license_scope="synthetic_public",
            facts={
                "signed_contract": True,
                "scope_covers_plan": True,
                "candidate_id": "refreshed",
                "contract_ref": "synthetic-creative-authorization",
            },
        )
    )
    return ReviewRequest(
        plan=plan, evidence=tuple(records), mode="SYNTHETIC", as_of=FIXTURE_TIME
    )
