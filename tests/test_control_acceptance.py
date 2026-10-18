"""Registered offline oracles. Holdout labels are supplied and locked separately."""

from dataclasses import replace

import pytest

from ofc_engine.control.canonical import digest
from ofc_engine.control.engine import review
from ofc_engine.control.fixtures import campaign
from ofc_engine.control.models import Candidate, ReviewRequest, Plan, Actor, Node, Edge
from ofc_engine.control.graph import constraints
from ofc_engine.control.runtime import ControlRuntime
from ofc_engine.control.storage import ControlStore, NotFound
from ofc_engine.control.verifier import verify, ProofRejected


@pytest.mark.parametrize("case", range(60))
def test_registered_contract(case):
    data = campaign().data()
    node = data["plan"]["nodes"][case % 30]
    kind = case // 10
    if kind == 0:
        node["candidate_id"] = "outside"
    elif kind == 1:
        node["candidate_pool"] = []
    elif kind == 2:
        node["mutable_fields"] = ["owner"]
    elif kind == 3:
        data["plan"]["external_action"] = True
    elif kind == 4:
        data["plan"]["limits"]["spend_minor"] = case + 1
    else:
        node["start_at"] = "2026-10-18T10:00:00Z"
        node["end_at"] = None
    with pytest.raises(ValueError):
        ReviewRequest.parse(data)


@pytest.mark.parametrize("case", range(80))
def test_registered_graph(case):
    from test_control_graph import test_random_graph_closure_matches_fixed_point_oracle

    test_random_graph_closure_matches_fixed_point_oracle(case + 2000)


@pytest.mark.parametrize("case", range(60))
def test_registered_evidence(case):
    from test_control_evidence import (
        test_mismatched_mandatory_evidence_never_passes,
        test_valid_signature_cannot_validate_false_attested_facts,
        attested_request,
    )

    mutations = [
        "city",
        "subject",
        "tenant",
        "digest",
        "fingerprint",
        "policy",
        "source",
        "mode",
        "license",
        "expiry",
        "future",
        "signature",
        "pool",
    ]
    if case < 52:
        test_mismatched_mandatory_evidence_never_passes(mutations[case // 4], case % 4)
    elif case < 56:
        test_valid_signature_cannot_validate_false_attested_facts(case - 52)
    else:
        req, authorities, _ = attested_request()
        issuer = next(iter(authorities))
        authorities[issuer] = replace(authorities[issuer], revoked=True)
        assert (
            review(req, authorities=authorities)["verdicts"]["venue"]["status"]
            != "QUALIFIED"
        )


@pytest.mark.parametrize("case", range(60))
def test_registered_repair(case):
    from test_control_oracles import (
        test_bounded_optimizer_matches_exhaustive_constraint_oracle,
    )

    test_bounded_optimizer_matches_exhaustive_constraint_oracle(case + 4000)


@pytest.mark.parametrize("case", range(60))
def test_registered_attacker(case):
    proof = review(campaign())
    kind = case % 10
    index = case // 10 % len(proof["events"])
    if kind == 0:
        proof["current"]["nodes"][0]["owner"] = "intruder"
    elif kind == 1:
        proof["events"][index]["payload"]["forged"] = case
    elif kind == 2:
        proof["policy_digest"] = "0" * 64
    elif kind == 3:
        proof["evidence"][case % 2]["tenant_id"] = "intruder"
    elif kind == 4:
        proof["evidence"][case % 2]["ranks"].reverse()
    elif kind == 5:
        proof["external_action_executed"] = True
    elif kind == 6:
        proof["release_authorized"] = True
    elif kind == 7:
        proof["patches"][0]["evidence_ids"] = ["forged"]
    elif kind == 8:
        proof["current"]["nodes"][0]["candidate_id"] = "puma"
    else:
        proof["revision"] = case + 10
    proof["proof_digest"] = digest(
        {k: v for k, v in proof.items() if k != "proof_digest"}
    )
    with pytest.raises(ProofRejected):
        verify(proof)


@pytest.mark.parametrize("case", range(30))
def test_registered_temporal_budget(case):
    currency = ("USD", "EUR", "INR")[case % 3]
    cost = case + 1
    cap = cost * 2 + (1 if case % 2 else -1)
    nodes = tuple(
        Node(
            id=f"n{i}",
            domain="operations",
            owner="a",
            label=f"Node {i}",
            candidate_id="x",
            candidate_pool=(
                Candidate(id="x", label="X", cost_minor=cost, currency=currency),
            ),
        )
        for i in range(2)
    )
    target = Node(
        id="bucket",
        domain="budget",
        owner="a",
        label="Bucket",
        candidate_id="x",
        candidate_pool=(Candidate(id="x", label="X"),),
    )
    edges = tuple(
        Edge(
            source=n.id,
            target="bucket",
            type="CONSUMES_BUDGET",
            budget_currency=currency,
            budget_limit_minor=cap,
        )
        for n in nodes
    )
    plan = Plan(
        tenant_id="t",
        actor=Actor(id="a"),
        idempotency_key=f"time-{case}",
        objective="Check aggregate consumption",
        nodes=nodes + (target,),
        edges=edges,
    )
    assert all(
        c["status"] == ("PASS" if cost * 2 <= cap else "BLOCKED")
        for c in constraints(plan)
    )


@pytest.mark.parametrize("case", range(30))
def test_registered_workflow(case, tmp_path):
    phases = ("FROZEN", "EVALUATING", "REPAIR_PROPOSED", "RECHECKING", "HANDOFF_READY")
    phase = phases[case % 5]
    now = [1000 + case]
    store = ControlStore(tmp_path / f"job-{case}.db", clock=lambda: now[0])
    req = campaign(tenant=f"tenant-{case}")
    rid = store.admit(req)["review_id"]

    def crash(stage, job):
        if stage == phase:
            raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError):
        ControlRuntime(store).run_once(checkpoint=crash)
    now[0] += 31
    assert store.recover_expired() == 1
    store.resume(rid, req.plan.tenant_id, req.plan.actor.id)
    ControlRuntime(store).run_once()
    assert store.proof(rid, req.plan.tenant_id, req.plan.actor.id)["impact"][
        "changed"
    ] == ["B01"]
    with pytest.raises(NotFound):
        store.snapshot(rid, "wrong", req.plan.actor.id)


@pytest.mark.parametrize("case", range(30))
def test_registered_agent(case):
    from ofc_engine.control.agents import AgentTools, fault
    from urllib.error import HTTPError

    req = campaign()
    proof = review(req)

    class Client:
        def submit(self, value):
            return {"plan_digest": value.plan.digest}

        def status(self, rid):
            return {"state": "FINISHED", "review_id": rid}

        def proof(self, rid):
            return proof

    actor = ("marketing-agent", "venue-agent", "budget-agent")[case % 3]
    tools = AgentTools(Client(), tenant_id="sandbox", actor_id=actor)
    with pytest.raises(PermissionError):
        tools.submit(req.data())
    owner = AgentTools(Client(), tenant_id="sandbox", actor_id="campaign-agent")
    assert owner.submit(req.data())["plan_digest"] == req.plan.digest
    assert (
        owner.invoke("review_evidence", {"review_id": str(case)})["result"][
            "proof_digest"
        ]
        == proof["proof_digest"]
    )
    error = HTTPError(
        "https://example.invalid",
        (403, 404, 409, 422, 429, 503)[case % 6],
        "redacted",
        {},
        None,
    )
    result = fault(error).data()
    assert result["write_retry_allowed"] is False
    assert result["retryable_read"] is (error.code in {429, 503})
