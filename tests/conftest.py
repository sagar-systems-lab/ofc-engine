import pytest
from ofc_engine.contracts import Decision, PlanSnapshot
from ofc_engine.revisions import digest_obj

@pytest.fixture
def plan():
    return PlanSnapshot(
        plan_id="tour-001", revision=0,
        baseline_digest=digest_obj({"frozen":"baseline"}),
        constraints_digest=digest_obj({"budget_usd":60000}),
        decisions=(
            Decision(decision_id="brand",domain="partner",candidate_id="brand-a",candidate_pool=("brand-a","brand-b"),mutable_fields=("candidate_id",)),
            Decision(decision_id="afterparty",domain="experience",candidate_id="exp-a",candidate_pool=("exp-a",),depends_on=("brand",)),
            Decision(decision_id="venue",domain="venue",candidate_id="venue-a",candidate_pool=("venue-a",)),
        ),
    )
