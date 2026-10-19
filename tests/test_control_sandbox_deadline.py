from ofc_engine.control.engine import optimize, review
from ofc_engine.control.fixtures import campaign
from ofc_engine.control.models import Limits
from ofc_engine.control.policies import evaluate


class SteppedClock:
    def __init__(self):
        self.time_ns = 0

    def __call__(self):
        result = self.time_ns
        self.time_ns += 120_000_000
        return result


def test_sandbox_campaign_has_bounded_hosted_budget():
    request = campaign("repair")
    assert Limits().deadline_ms == 200
    assert request.plan.limits.deadline_ms == 2000
    assert request.plan.limits.max_expansions == Limits().max_expansions
    assert request.plan.limits.provider_calls == 0
    assert request.plan.limits.spend_minor == 0


def test_rank_repair_completes_with_bounded_optimizer_clock():
    request = campaign("repair")
    evaluation = {
        "baseline_digest": request.plan.digest,
        "as_of": request.as_of,
        "synthetic": True,
        "authorities": {},
    }
    verdicts = evaluate(request.plan, request.evidence, **evaluation)
    result = optimize(
        request.plan,
        verdicts,
        evidence=request.evidence,
        evaluation=evaluation,
        clock=SteppedClock(),
    )
    assert result["status"] == "REPAIR_PROPOSED"
    assert result["expansions"] == 3
    assert [(p["node_id"], p["changes"]) for p in result["patches"]] == [
        ("B01", {"candidate_id": "adidas"})
    ]


def test_hosted_sandbox_repair_reopens_only_dependent_decisions():
    record = review(campaign("repair"))
    assert record["impact"]["changed"] == ["B01"]
    assert record["impact"]["invalidated"] == [f"D{i:02}" for i in range(1, 10)]
    assert record["impact"]["preserved"] == [f"I{i:02}" for i in range(1, 21)]
    assert record["comparison"]["original"]["B01"] == "nike"
    assert record["comparison"]["controlled"]["B01"] == "adidas"
    assert record["action_status"] == "WITHHELD"
    assert record["external_action_executed"] is False
    assert record["provider_calls"] == 0
    assert record["spend_minor"] == 0


def test_other_sandbox_scenarios_remain_fail_closed():
    for case in ("preserve", "abstain", "stale", "conflict"):
        record = review(campaign(case))
        assert record["patches"] == []
        assert record["impact"]["changed"] == []
        assert record["action_status"] == "WITHHELD"
        assert record["external_action_executed"] is False
        assert record["provider_calls"] == 0
        assert record["spend_minor"] == 0
