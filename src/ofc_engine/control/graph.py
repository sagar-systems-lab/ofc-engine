from __future__ import annotations

from collections import deque
from datetime import timedelta

from .canonical import digest
from .models import CAUSAL_EDGES, Plan, instant


def topological(plan: Plan) -> list[str]:
    children = {n.id: [] for n in plan.nodes}
    incoming = dict.fromkeys(children, 0)
    for edge in plan.edges:
        if edge.type in CAUSAL_EDGES:
            children[edge.source].append(edge.target)
            incoming[edge.target] += 1
    ready = deque(sorted(k for k, count in incoming.items() if count == 0))
    result = []
    while ready:
        key = ready.popleft()
        result.append(key)
        for child in sorted(children[key]):
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)
    return result


def _inputs(plan: Plan):
    nodes = {n.id: n for n in plan.nodes}
    incoming = {key: [] for key in nodes}
    for edge in plan.edges:
        if edge.type in CAUSAL_EDGES:
            incoming[edge.target].append(edge)
    result, dependency_inputs = {}, {}
    for key in topological(plan):
        node = nodes[key]
        dependencies = []
        for edge in sorted(incoming[key], key=lambda x: (x.source, x.type)):
            source = nodes[edge.source]
            reads = edge.read_set or (f"{source.id}.candidate_id",)
            dependencies.append(
                {
                    "type": edge.type,
                    "values": {
                        field: getattr(source, field.split(".")[1]) for field in reads
                    },
                    "upstream": digest(dependency_inputs[source.id])
                    if incoming[source.id]
                    else None,
                }
            )
        dependency_inputs[key] = dependencies
        result[key] = digest(
            {
                "candidate_id": node.candidate_id,
                "locality": node.locality,
                "start_at": node.start_at,
                "end_at": node.end_at,
                "required_capacity": node.required_capacity,
                "candidate_pool": [c.data() for c in node.candidate_pool],
                "claims": list(node.claims),
                "dependencies": dependencies,
            }
        )
    return result, dependency_inputs


def input_fingerprints(plan: Plan) -> dict[str, str]:
    return _inputs(plan)[0]


def impact(plan: Plan, changed: dict[str, set[str]]) -> dict:
    nodes = {n.id for n in plan.nodes}
    if not set(changed) <= nodes:
        raise ValueError("unknown changed node")
    children = {key: [] for key in nodes}
    constraints = []
    for edge in plan.edges:
        if edge.type in CAUSAL_EDGES:
            children[edge.source].append(edge)
        else:
            constraints.append(edge)
    affected, derived, paths = set(changed), set(), {key: [key] for key in changed}
    for source in topological(plan):
        if source not in affected:
            continue
        for edge in children[source]:
            reads = edge.read_set or (f"{source}.candidate_id",)
            fields = {value.split(".")[1] for value in reads}
            if source not in derived and not fields.intersection(
                changed.get(source, set())
            ):
                continue
            derived.add(edge.target)
            if edge.target not in affected:
                affected.add(edge.target)
                paths[edge.target] = paths[source] + [edge.target]
    constraint_nodes = set()
    for edge in constraints:
        if edge.source in changed or edge.target in changed:
            constraint_nodes.update((edge.source, edge.target))
    if plan.budgets and changed:
        constraint_nodes.update(nodes)
    return {
        "changed": sorted(changed),
        "affected": sorted(affected),
        "invalidated": sorted(affected - set(changed)),
        "preserved": sorted(nodes - affected),
        "constraint_rechecks": sorted(constraint_nodes),
        "paths": paths,
        "recheck_order": [key for key in topological(plan) if key in affected],
    }


def claim_fingerprint(plan: Plan, node_id: str, claim: str) -> str:
    node = next(n for n in plan.nodes if n.id == node_id)
    if claim in (
        "cultural_brand_rank",
        "place_cultural_observation",
        "place_cultural_relevance",
    ):
        parents = _inputs(plan)[1][node_id]
        return digest(
            {
                "claim": claim,
                "subject_id": node.subject_id,
                "pool": [c.data() for c in node.candidate_pool],
                "locality": node.locality,
                "parents": parents,
            }
        )
    return digest({"claim": claim, "inputs": input_fingerprints(plan)[node_id]})


def constraints(plan: Plan) -> list[dict]:
    nodes = {n.id: n for n in plan.nodes}
    chosen = {
        n.id: next(c for c in n.candidate_pool if c.id == n.candidate_id)
        for n in plan.nodes
    }
    results = []
    for budget in plan.budgets:
        unknown = [
            key for key, candidate in chosen.items() if candidate.cost_minor is None
        ]
        total = sum(
            c.cost_minor for c in chosen.values() if c.currency == budget.currency
        )
        status = (
            "BLOCKED"
            if total > budget.limit_minor
            else "UNCERTAIN"
            if unknown
            else "PASS"
        )
        results.append(
            {
                "type": "BUDGET",
                "status": status,
                "currency": budget.currency,
                "total_minor": total,
                "limit_minor": budget.limit_minor,
                "unknown_nodes": unknown,
                "input_assurance": "DECLARED_INPUTS",
                "feasibility": "ARITHMETIC_VALID_ON_DECLARED_INPUTS"
                if status == "PASS"
                else "UNRESOLVED",
            }
        )
    if plan.budgets:
        unbounded = sorted(
            {c.currency for c in chosen.values() if c.currency is not None}
            - {b.currency for b in plan.budgets}
        )
        for currency in unbounded:
            results.append(
                {
                    "type": "BUDGET",
                    "status": "UNCERTAIN",
                    "currency": currency,
                    "reason": "CURRENCY_LIMIT_MISSING",
                }
            )
    for edge in plan.edges:
        left, right = nodes[edge.source], nodes[edge.target]
        status, reason = "PASS", "CONSTRAINT_SATISFIED"
        pair = (left.candidate_id, right.candidate_id)
        if edge.type == "CONFLICTS_WITH" and pair in edge.candidate_pairs:
            status, reason = "BLOCKED", "CANDIDATE_CONFLICT"
        elif edge.type == "COMPATIBLE_WITH" and pair not in edge.candidate_pairs:
            status, reason = "BLOCKED", "CANDIDATES_INCOMPATIBLE"
        elif edge.type == "TEMPORAL_BEFORE":
            if left.end_at is None or right.start_at is None:
                status, reason = "UNCERTAIN", "SCHEDULE_INPUT_MISSING"
            elif instant(left.end_at) + timedelta(
                seconds=edge.buffer_seconds
            ) > instant(right.start_at):
                status, reason = "BLOCKED", "TEMPORAL_CONFLICT"
        elif edge.type == "SHARES_RESOURCE":
            a, b = chosen[left.id], chosen[right.id]
            if a.resource is None or b.resource is None:
                status, reason = "UNCERTAIN", "RESOURCE_INPUT_MISSING"
            elif a.resource == b.resource:
                if left.start_at is None or right.start_at is None:
                    status, reason = "UNCERTAIN", "SCHEDULE_INPUT_MISSING"
                elif max(instant(left.start_at), instant(right.start_at)) < min(
                    instant(left.end_at), instant(right.end_at)
                ):
                    status, reason = "BLOCKED", "EXCLUSIVE_RESOURCE_OVERLAP"
        elif edge.type == "CONSUMES_BUDGET":
            consumers = [
                nodes[e.source]
                for e in plan.edges
                if e.type == "CONSUMES_BUDGET"
                and e.target == edge.target
                and e.budget_currency == edge.budget_currency
            ]
            costs = [chosen[n.id] for n in consumers]
            total = sum(
                c.cost_minor
                for c in costs
                if c.cost_minor is not None and c.currency == edge.budget_currency
            )
            if any(
                c.cost_minor is None or c.currency != edge.budget_currency
                for c in costs
            ):
                status, reason = "UNCERTAIN", "BUDGET_EDGE_INPUT_MISSING"
            elif total > edge.budget_limit_minor:
                status, reason = "BLOCKED", "BUDGET_EDGE_EXCEEDED"
        elif edge.type == "APPROVAL_REQUIRES":
            status, reason = "UNCERTAIN", "SCOPED_HUMAN_APPROVAL_REQUIRED"
        elif edge.type == "REQUIRES":
            reason = "DEPENDENCY_EVALUATED_BY_CLAIM_POLICY"
        elif edge.type not in (
            "CONFLICTS_WITH",
            "COMPATIBLE_WITH",
            "SHARES_RESOURCE",
            "TEMPORAL_BEFORE",
        ):
            continue
        results.append(
            {
                "type": edge.type,
                "source": edge.source,
                "target": edge.target,
                "status": status,
                "reason": reason,
            }
        )
    return results


def recheck_state(status, invalidated_claims):
    return (
        "QUALIFIED"
        if status == "QUALIFIED"
        else "STALE_PENDING_RECHECK"
        if invalidated_claims
        else "UNCERTAIN"
    )
