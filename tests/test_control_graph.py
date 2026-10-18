import random

import pytest
from pydantic import ValidationError

from ofc_engine.control.canonical import canonical_bytes, digest, strict_json
from ofc_engine.control.engine import apply_patches, review
from ofc_engine.control.fixtures import campaign
from ofc_engine.control.graph import constraints, impact, input_fingerprints
from ofc_engine.control.models import Patch, Plan, ReviewRequest


@pytest.mark.parametrize("case,changed", [("repair", ["B01"]), ("preserve", []), ("abstain", []), ("stale", []), ("conflict", [])])
def test_campaign_outcomes(case, changed):
    request = campaign(case)
    result = review(request)
    assert result["impact"]["changed"] == changed
    assert result["external_action_executed"] is False
    assert result["release_authorized"] is False
    assert result["provider_calls"] == 0
    assert review(request) == result
    if changed:
        assert result["impact"]["invalidated"] == [f"D{i:02}" for i in range(1, 10)]
        assert result["impact"]["preserved"] == [f"I{i:02}" for i in range(1, 21)]
        assert [n for n in result["current"]["nodes"] if n["id"].startswith("I")] == [n for n in result["baseline"]["nodes"] if n["id"].startswith("I")]
        assert result["current"]["nodes"][0]["candidate_id"] == "adidas"
        assert result["verdicts"]["B01"]["claims"][0]["status"] == "QUALIFIED"
        assert all(result["verdicts"][f"D{i:02}"]["status"] != "QUALIFIED" for i in range(1, 10))


@pytest.mark.parametrize("seed", range(80))
def test_random_graph_closure_matches_fixed_point_oracle(seed):
    rng = random.Random(seed)
    size = rng.randint(2, 40)
    payload = campaign().plan.data()
    template = payload["nodes"][-1]
    payload["nodes"] = [dict(template, id=f"n{i}") for i in range(size)]
    pairs = [(i, j) for i in range(size) for j in range(i + 1, size) if rng.random() < .08]
    payload["edges"] = [{"source": f"n{i}", "target": f"n{j}", "type": "REQUIRES"} for i, j in pairs]
    plan = Plan.parse(payload)
    root = rng.randrange(size)
    expected = {root}
    while True:
        expanded = expected | {j for i, j in pairs if i in expected}
        if expanded == expected:
            break
        expected = expanded
    assert set(impact(plan, {f"n{root}": {"candidate_id"}})["affected"]) == {f"n{i}" for i in expected}


def test_typed_constraints_do_not_create_false_cycles():
    payload = campaign().plan.data()
    payload["edges"] += [dict(source="D01", target="B01", type="CONFLICTS_WITH", candidate_pairs=[["planned", "puma"]])]
    assert Plan.parse(payload)
    payload["edges"][-1] = dict(source="D01", target="B01", type="REQUIRES")
    with pytest.raises(ValidationError):
        Plan.parse(payload)


def test_read_set_excludes_unrelated_field_changes():
    plan = campaign().plan
    assert impact(plan, {"B01": {"start_at"}})["affected"] == ["B01"]
    fingerprints = input_fingerprints(plan)
    patched = apply_patches(plan, (Patch(node_id="B01", changes={"candidate_id": "adidas"}, evidence_ids=("e1",)),))
    new = input_fingerprints(patched)
    assert all(fingerprints[f"I{i:02}"] == new[f"I{i:02}"] for i in range(1, 21))
    assert all(fingerprints[f"D{i:02}"] != new[f"D{i:02}"] for i in range(1, 10))


@pytest.mark.parametrize("mutation", ["pool", "mask", "unknown", "noop"])
def test_patch_boundary(mutation):
    plan = campaign().plan
    key, changes = "B01", {"candidate_id": "adidas"}
    if mutation == "pool": changes["candidate_id"] = "outside"
    if mutation == "mask": key = "D01"
    if mutation == "unknown": key = "missing"
    if mutation == "noop": changes["candidate_id"] = "nike"
    with pytest.raises(ValueError):
        apply_patches(plan, (Patch(node_id=key, changes=changes, evidence_ids=("e1",)),))


def test_unknown_cost_is_not_zero_and_currency_is_separate():
    payload = campaign().plan.data()
    payload["budgets"] = [{"currency": "USD", "limit_minor": 100}]
    result = constraints(Plan.parse(payload))
    assert result[0]["status"] == "UNCERTAIN"
    assert len(result[0]["unknown_nodes"]) == 30


@pytest.mark.parametrize("value", [float("nan"), .3, 2**53, {"非": 1}, {"bad": b"data"}])
def test_canonical_rejects_nonportable_values(value):
    with pytest.raises((ValueError, UnicodeError)):
        canonical_bytes(value)


def test_strict_input_rejects_duplicate_keys_and_extra_fields():
    with pytest.raises(ValueError): strict_json('{"a":1,"a":2}')
    payload = campaign().plan.data()
    payload["approve_everything"] = True
    with pytest.raises(ValidationError): Plan.parse(payload)
    assert digest({"z": 1, "a": "é🧭"}) == digest({"a": "é🧭", "z": 1})


def test_optimizer_bound_has_no_partial_commit():
    payload = campaign().data()
    payload["plan"]["limits"]["max_expansions"] = 1
    old = ReviewRequest.parse(payload)
    # Rebind a new fixture scope after changing declared limits.
    for item in payload["evidence"]: item["plan_digest"] = old.plan.digest
    result = review(ReviewRequest.parse(payload))
    assert result["optimization"]["status"] == "OPTIMIZATION_UNDECIDED"
    assert result["patches"] == []
    assert result["current_digest"] == result["baseline_digest"]
