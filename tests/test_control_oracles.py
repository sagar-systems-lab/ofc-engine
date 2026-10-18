from itertools import product
import random

import pytest

from ofc_engine.control.engine import optimize
from ofc_engine.control.fixtures import campaign
from ofc_engine.control.graph import constraints
from ofc_engine.control.models import Plan


@pytest.mark.parametrize("seed", range(60))
def test_bounded_optimizer_matches_exhaustive_constraint_oracle(seed):
    rng = random.Random(seed)
    size = rng.randint(2, 5)
    payload = campaign().plan.data()
    template = payload["nodes"][0]
    payload["nodes"] = [dict(template, id=f"n{i}", candidate_id=rng.choice(["nike", "adidas"]),
                              candidate_pool=template["candidate_pool"][:2]) for i in range(size)]
    conflicts = [(i, j) for i in range(size) for j in range(i + 1, size) if rng.random() < .35]
    payload["edges"] = [{"source": f"n{i}", "target": f"n{j}", "type": "CONFLICTS_WITH", "candidate_pairs": [["adidas", "adidas"]]} for i, j in conflicts]
    payload["limits"]["deadline_ms"] = 2000
    plan = Plan.parse(payload)
    verdicts = {f"n{i}": {"claims": [{"claim": "cultural_brand_rank", "status": "QUALIFIED",
                                       "ranked_candidates": ["adidas", "nike"], "evidence_ids": ["e1", "e2"]}]} for i in range(size)}
    original = tuple(n.candidate_id for n in plan.nodes)

    def score(selection):
        failures = sum(selection[i] == selection[j] == "adidas" for i, j in conflicts)
        return (failures, sum(value != "adidas" for value in selection),
                sum(a != b for a, b in zip(original, selection)), 0)

    feasible = [x for x in product(["nike", "adidas"], repeat=size) if score(x)[0] == 0]
    best = min(score(x) for x in feasible)
    optima = [x for x in feasible if score(x) == best]
    result = optimize(plan, verdicts)
    if best >= score(original):
        assert result["patches"] == []
    elif len(optima) != 1:
        assert result["status"] == "AMBIGUOUS_OPTIMUM" and result["patches"] == []
    else:
        selected = dict(zip([n.id for n in plan.nodes], original))
        for patch in result["patches"]: selected[patch["node_id"]] = patch["changes"]["candidate_id"]
        assert tuple(selected[n.id] for n in plan.nodes) == optima[0]


def test_financial_units_and_exclusive_time_constraints_remain_separate():
    payload = campaign().plan.data()
    payload["nodes"] = payload["nodes"][:2]
    payload["edges"] = []
    for node in payload["nodes"]:
        node["candidate_pool"] = [{"id": "hall", "label": "Hall", "cost_minor": 100, "currency": "EUR", "resource": "hall"}]
        node["candidate_id"] = "hall"
        node["start_at"], node["end_at"] = "2026-10-18T10:00:00Z", "2026-10-18T12:00:00Z"
    payload["budgets"] = [{"currency": "USD", "limit_minor": 1000}]
    payload["edges"] = [{"source": "B01", "target": "D01", "type": "SHARES_RESOURCE"}]
    result = constraints(Plan.parse(payload))
    assert any(c.get("reason") == "CURRENCY_LIMIT_MISSING" for c in result)
    assert any(c.get("reason") == "EXCLUSIVE_RESOURCE_OVERLAP" for c in result)


def test_large_plan_bound_is_checked_before_any_review():
    payload = campaign().plan.data()
    template = payload["nodes"][-1]
    payload["nodes"] = [dict(template, id=f"n{i}") for i in range(64)]
    payload["edges"] = []
    assert len(Plan.parse(payload).nodes) == 64
    payload["nodes"].append(dict(template, id="n64"))
    with pytest.raises(ValueError): Plan.parse(payload)
