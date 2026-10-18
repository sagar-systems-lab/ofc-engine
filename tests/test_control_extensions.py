import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ofc_engine.control.canonical import canonical_bytes, digest
from ofc_engine.control.engine import apply_patches, review
from ofc_engine.control.fixtures import campaign, cascading_campaign, FIXTURE_TIME
from ofc_engine.control.graph import claim_fingerprint, constraints
from ofc_engine.control.models import (
    Actor,
    Candidate,
    Edge,
    Evidence,
    Node,
    Patch,
    Plan,
    ReviewRequest,
)
from ofc_engine.control.storage import Conflict, ControlStore, NotFound
from ofc_engine.control.runtime import ControlRuntime
from ofc_engine.control.verifier import verify, ProofRejected
from ofc_engine.control.policies import Authority, POLICY_VERSION
from ofc_engine.control.ownership import Delegation
from ofc_engine.control.http import Identity


def test_two_round_cascade_revisits_dependent_and_preserves_independent():
    request = cascading_campaign()
    proof = review(request)
    assert proof["revision"] == 2
    assert proof["impact"]["changed"] == ["B01", "D01"]
    assert all(
        n.candidate_id == "planned"
        for n in Plan.parse(proof["current"]).nodes
        if n.id.startswith("I")
    )
    assert verify(proof)["valid"]
    proof["rounds"][1]["current"]["nodes"][1]["candidate_id"] = "planned"
    proof["proof_digest"] = digest(
        {k: v for k, v in proof.items() if k != "proof_digest"}
    )
    with pytest.raises(ProofRejected):
        verify(proof)


def test_max_rounds_and_unavailable_evidence_abstain():
    proof = review(cascading_campaign(max_rounds=1))
    assert proof["impact"]["changed"] == ["B01"]
    assert proof["verdicts"]["D01"]["status"] == "UNCERTAIN"
    assert proof["verdicts"]["D02"]["status"] == "UNCERTAIN"


@pytest.mark.parametrize("crash_round", [1, 2])
def test_revision_replay_preserves_committed_batches(tmp_path, crash_round):
    now = [1000]
    store = ControlStore(tmp_path / "journal.db", clock=lambda: now[0])
    req = cascading_campaign()
    receipt = store.admit(req)
    observed = []

    def crash(state, job):
        if state == "ROUND_COMMITTED":
            observed.append(state)
            if len(observed) == crash_round:
                raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        ControlRuntime(store).run_once(checkpoint=crash)
    assert (
        store.snapshot(receipt["review_id"], "sandbox", "campaign-agent")["revision"]
        == crash_round
    )
    now[0] += 31
    assert store.recover_expired() == 1
    store.resume(receipt["review_id"], "sandbox", "campaign-agent")
    ControlRuntime(store).run_once()
    assert (
        store.proof(receipt["review_id"], "sandbox", "campaign-agent")["revision"] == 2
    )
    with store._tx() as db:
        assert (
            store._run(
                db,
                f"select count(*) from {store.prefix}revisions where parent_digest<>current_digest",
            ).fetchone()[0]
            == 2
        )


def synthetic_fact(plan, node_id, claim, candidate, *, future=None, facts):
    node = next(n for n in plan.nodes if n.id == node_id)
    return Evidence(
        id=f"{node_id}-{candidate}-{claim}",
        tenant_id=plan.tenant_id,
        plan_digest=plan.digest,
        node_id=node_id,
        claim=claim,
        subject_id=candidate,
        candidate_pool=tuple(c.id for c in node.candidate_pool),
        locality=node.locality,
        input_digest=claim_fingerprint(future or plan, node_id, claim),
        source="fixture",
        mode="SYNTHETIC",
        attempt_id=f"{node_id}-{candidate}-{claim}",
        observed_at="2026-10-18T11:59:00Z",
        expires_at="2026-10-19T12:00:00Z",
        policy_version=POLICY_VERSION,
        license_scope="synthetic_public",
        facts=facts,
    )


def venue_request(*, tie=False, missing=None):
    node = Node(
        id="V",
        domain="venue",
        label="Venue",
        owner="agent",
        candidate_id="small",
        candidate_pool=tuple(
            Candidate(id=x, label=x)
            for x in (("small", "large", "other") if tie else ("small", "large"))
        ),
        mutable_fields=("candidate_id",),
        required_capacity=100,
        locality="Chicago",
        start_at="2026-10-20T10:00:00Z",
        end_at="2026-10-20T12:00:00Z",
    )
    plan = Plan(
        tenant_id="tenant",
        actor=Actor(id="agent"),
        idempotency_key="venue-repair",
        objective="Find a verified feasible venue",
        nodes=(node,),
    )
    evidence = []
    for c in node.candidate_pool:
        future = (
            apply_patches(
                plan,
                (
                    Patch(
                        node_id="V",
                        changes={"candidate_id": c.id},
                        evidence_ids=("probe",),
                    ),
                ),
            )
            if c.id != node.candidate_id
            else plan
        )
        facts = {
            "venue_capacity": {
                "capacity": 20 if c.id == "small" else 200,
                "required_capacity": 100,
                "available": True,
                "venue_id": c.id,
                "start_at": node.start_at,
                "end_at": node.end_at,
            },
            "contract_rights": {
                "signed_contract": True,
                "scope_covers_plan": True,
                "candidate_id": c.id,
                "contract_ref": "contract",
            },
            "schedule_window": {
                "start_at": node.start_at,
                "end_at": node.end_at,
                "confirmed": True,
            },
        }
        for claim, f in facts.items():
            if missing == claim:
                continue
            evidence.append(
                synthetic_fact(plan, "V", claim, c.id, future=future, facts=f)
            )
    return ReviewRequest(
        plan=plan, evidence=tuple(evidence), mode="SYNTHETIC", as_of=FIXTURE_TIME
    )


def test_capacity_licensed_candidate_repair_and_independent_verification():
    proof = review(venue_request())
    assert proof["current"]["nodes"][0]["candidate_id"] == "large"
    assert verify(proof)["valid"]


def test_equal_feasible_venues_abstain():
    result = review(venue_request(tie=True))
    assert result["optimization"]["status"] == "AMBIGUOUS_OPTIMUM"
    assert not result["patches"]


@pytest.mark.parametrize(
    "claim", ["venue_capacity", "contract_rights", "schedule_window"]
)
def test_missing_alternative_business_claim_prevents_repair(claim):
    assert not review(venue_request(missing=claim))["patches"]


@pytest.mark.parametrize(
    "actor", ["creative-agent", "operations-agent", "planning-agent"]
)
def test_named_agents_cannot_patch_foreign_nodes(actor):
    request = campaign()
    payload = request.plan.data()
    payload["actor"]["id"] = actor
    plan = Plan.parse(payload)
    with pytest.raises(PermissionError):
        apply_patches(
            plan,
            (
                Patch(
                    node_id="B01", changes={"candidate_id": "puma"}, evidence_ids=("x",)
                ),
            ),
        )


@pytest.mark.parametrize("revoked", [False, True])
def test_server_verified_delegation_changes_ownership_permission(revoked):
    payload = campaign().data()
    payload["plan"]["nodes"][0]["owner"] = "creative-agent"
    req = ReviewRequest.parse(payload)
    payload["evidence"] = [
        e | {"plan_digest": req.plan.digest} for e in payload["evidence"]
    ]
    req = ReviewRequest.parse(payload)
    grant = Delegation(
        "sandbox",
        "creative-agent",
        "campaign-agent",
        req.plan.digest,
        frozenset({"B01"}),
        frozenset({"patch"}),
        "2026-12-01T00:00:00Z",
        revoked,
    )
    proof = review(req, delegations=(grant,))
    assert bool(proof["patches"]) is not revoked
    assert verify(proof, trusted_delegations=(grant,))["valid"]
    if not revoked:
        with pytest.raises(ProofRejected):
            verify(proof)


@pytest.mark.parametrize(
    "kind",
    [
        "REQUIRES",
        "COMPATIBLE_WITH",
        "CONFLICTS_WITH",
        "CONSUMES_BUDGET",
        "SHARES_RESOURCE",
        "TEMPORAL_BEFORE",
        "APPROVAL_REQUIRES",
    ],
)
def test_every_edge_has_an_operational_result(kind):
    a = Node(
        id="a",
        owner="agent",
        domain="approval" if kind == "APPROVAL_REQUIRES" else "operations",
        label="A",
        candidate_id="x",
        candidate_pool=(
            Candidate(id="x", label="X", cost_minor=20, currency="USD", resource="r"),
        ),
        start_at="2026-10-20T10:00:00Z",
        end_at="2026-10-20T11:00:00Z",
    )
    b = Node(
        id="b",
        owner="agent",
        domain="budget" if kind == "CONSUMES_BUDGET" else "operations",
        label="B",
        candidate_id="y",
        candidate_pool=(Candidate(id="y", label="Y", resource="s"),),
        start_at="2026-10-20T12:00:00Z",
        end_at="2026-10-20T13:00:00Z",
    )
    kw = (
        {"candidate_pairs": (("x", "y"),)}
        if kind in {"COMPATIBLE_WITH", "CONFLICTS_WITH"}
        else {}
    )
    if kind == "CONSUMES_BUDGET":
        kw.update(budget_currency="USD", budget_limit_minor=10)
    plan = Plan(
        tenant_id="tenant",
        actor=Actor(id="agent"),
        idempotency_key="edge",
        objective="Check edge",
        nodes=(a, b),
        edges=(Edge(source="a", target="b", type=kind, **kw),),
    )
    result = constraints(plan)[0]
    assert result["type"] == kind
    assert result["status"] == (
        "BLOCKED"
        if kind in {"CONSUMES_BUDGET", "CONFLICTS_WITH"}
        else "UNCERTAIN"
        if kind == "APPROVAL_REQUIRES"
        else "PASS"
    )


def test_budget_and_approval_edge_types_reject_invalid_domains():
    plan = campaign().plan.data()
    plan["edges"] = [
        {
            "source": "B01",
            "target": "D01",
            "type": "CONSUMES_BUDGET",
            "budget_currency": "USD",
            "budget_limit_minor": 1,
        }
    ]
    with pytest.raises(ValueError):
        Plan.parse(plan)
    plan["edges"] = [{"source": "B01", "target": "D01", "type": "APPROVAL_REQUIRES"}]
    with pytest.raises(ValueError):
        Plan.parse(plan)


def test_retention_preserves_legal_hold_and_audit_tombstone(tmp_path):
    store = ControlStore(tmp_path / "retention.db", clock=lambda: 10000)
    req = campaign()
    rid = store.admit(req)["review_id"]
    ControlRuntime(store).run_once()
    store.set_retention(rid, "sandbox", "campaign-agent", expires_at=1, legal_hold=True)
    assert not store.prune("sandbox")["deleted_review_ids"]
    store.set_retention(rid, "sandbox", "campaign-agent", expires_at=1)
    assert not store.prune("other")["deleted_review_ids"]
    assert store.prune("sandbox")["deleted_review_ids"] == [rid]
    with pytest.raises(NotFound):
        store.snapshot(rid, "sandbox", "campaign-agent")
    with pytest.raises(Conflict):
        store.admit(req)
    with store._tx() as db:
        assert (
            store._run(
                db, f"select event_head from {store.prefix}tombstones"
            ).fetchone()[0]
            != "0" * 64
        )


def test_public_projection_excludes_restricted_fields_and_checks_signature():
    from ofc_engine.control.projections import public_projection, verify_projection

    key = Ed25519PrivateKey.generate()
    proof = review(campaign())
    out = public_projection(proof, signing_key=key)
    assert all(
        x not in json.dumps(out)
        for x in ("nike", "Chicago", "campaign-agent", "signed_contract")
    )
    assert verify_projection(out, public_key=key.public_key().public_bytes_raw())[
        "valid"
    ]
    out["projection"]["private_proof_digest"] = "0" * 64
    with pytest.raises(ValueError):
        verify_projection(out, public_key=key.public_key().public_bytes_raw())


def test_version_negotiation_and_lossless_source_ledger():
    from ofc_engine.control.contracts import (
        negotiate,
        migrate_supported,
        restore_source,
    )

    assert negotiate("plan", ["ofc.plan/1"]) == "ofc.plan/1"
    with pytest.raises(ValueError):
        negotiate("plan", ["unknown"])
    src = {"choices": [], "secret_extension": {"retain": "exact"}}
    migrated = migrate_supported(src)
    assert migrated["status"] == "REVIEW_REQUIRED"
    assert migrated["field_ledger"] == [
        {"path": "secret_extension", "status": "UNSUPPORTED_FIELD"}
    ]
    assert restore_source(migrated) == src


def test_blind_label_lock_denominators_and_index_gate():
    from ofc_engine.control.evaluation import (
        corpus_manifest,
        seal_labels,
        open_labels,
        confusion,
        experiment,
        acceptance_index,
    )

    manifest = corpus_manifest()
    assert len(manifest["holdout"]) == 150
    key = Fernet.generate_key()
    gold = {"a": True, "b": False, "c": True, "d": False}
    sealed = seal_labels(gold, manifest, key=key)
    assert (
        open_labels(
            sealed, manifest, key=key, locked_commitment=sealed["plaintext_commitment"]
        )
        == gold
    )
    with pytest.raises(ValueError):
        open_labels(sealed, manifest, key=key, locked_commitment="0" * 64)
    score = confusion(gold, {"a": True, "b": True, "c": None, "d": False})
    assert (
        score["TP"],
        score["FP"],
        score["TN"],
        score["ABSTAIN"],
        score["evaluated"],
    ) == (1, 1, 1, 1, 3)
    report = experiment(
        manifest,
        gold,
        {"controlled": gold},
        label_commitment=sealed["plaintext_commitment"],
    )
    assert report["comparators"]["llm_only"]["status"] == "NOT_MEASURED"
    assert acceptance_index(report)["index"] is None


def test_research_consent_and_stable_blind_assignment():
    from ofc_engine.control.research import assign, Feedback, study_report

    a = assign("study", "person@example.com", secret=b"x" * 32)
    assert a == assign("study", "person@example.com", secret=b"x" * 32)
    assert "person" not in json.dumps(a)
    assert study_report([], assumptions={"cost": "not measured"})["status"] == "NOT_RUN"
    with pytest.raises(ValueError):
        Feedback.model_validate({"consent": False})


def test_trace_export_is_redacted_and_latency_classes_are_separate():
    from ofc_engine.control.telemetry import Trace, summarize

    now = [0]
    t = Trace("private-person", clock=lambda: now[0], host_class="cold")
    with t.span("controller") as parent:
        with t.span("rank", parent=parent, provider="offline"):
            now[0] += 5_000_000
    out = t.export()
    assert "private-person" not in json.dumps(out)
    assert summarize([out])[0]["n"] == 1
    assert all(r["simulation"] for r in summarize([out]))


def test_typed_intake_traverses_durable_runtime_without_language_provider(tmp_path):
    from ofc_engine.control.intake import submit_typed

    store = ControlStore(tmp_path / "intake.db")
    runtime = ControlRuntime(store)
    receipt = submit_typed(
        runtime, Identity("sandbox", "campaign-agent"), cascading_campaign().data()
    )
    assert (
        len(store.request(receipt["review_id"], "sandbox", "campaign-agent").plan.nodes)
        == 30
    )
    runtime.run_once()
    proof = store.proof(receipt["review_id"], "sandbox", "campaign-agent")
    assert proof["revision"] == 2 and proof["provider_calls"] == 0


def planner_fixture(tmp_path, *, wrong_account=False, rpm=3):
    from ofc_engine.control.acquisition import Capability, EvidencePlanner

    key = Ed25519PrivateKey.generate()
    node = Node(
        id="quote",
        domain="operations",
        label="Cost",
        owner="agent",
        candidate_id="x",
        candidate_pool=(Candidate(id="x", label="X", cost_minor=100, currency="USD"),),
        claims=("budget_quote",),
    )
    plan = Plan(
        tenant_id="tenant",
        actor=Actor(id="agent"),
        idempotency_key="collect",
        objective="Collect exact quote",
        nodes=(node,),
    )
    authority = Authority(
        "issuer",
        "tenant",
        key.public_key().public_bytes_raw(),
        frozenset({"budget_quote"}),
        "operator",
        "2026-12-01T00:00:00Z",
        provider_account_scope="account",
        entitlement_id="license",
    )
    store = ControlStore(
        tmp_path / "planner.db",
        encryption_key=Fernet.generate_key(),
        clock=lambda: 1792324800,
    )
    cap = Capability(
        "tenant",
        "operator",
        "account",
        "license",
        frozenset({"budget_quote"}),
        "2026-12-01T00:00:00Z",
        rpm=rpm,
    )

    class Fake:
        offline = True
        calls = 0

        def collect(self, scope, *, deadline):
            self.calls += 1
            e = Evidence(
                id="quote-evidence",
                tenant_id="tenant",
                plan_digest=plan.digest,
                node_id="quote",
                claim="budget_quote",
                subject_id="x",
                candidate_pool=("x",),
                input_digest=scope["input_digest"],
                source="operator",
                mode="ATTESTED",
                attempt_id="fake-attempt",
                observed_at="2026-10-18T11:59:00Z",
                expires_at="2026-10-19T12:00:00Z",
                policy_version=POLICY_VERSION,
                license_scope="private_review",
                facts={"cost_minor": 100, "currency": "USD", "includes_fees": True},
                issuer="issuer",
                provider_account_scope="wrong" if wrong_account else "account",
                entitlement_id="license",
                extraction_rule_version="quote-parser-1",
                raw_payload_ref="a" * 64,
            )
            e = e.model_copy(
                update={
                    "signature": base64.b64encode(
                        key.sign(canonical_bytes(e.signing_fields()))
                    ).decode()
                }
            )
            return [e.data()]

    adapter = Fake()
    return (
        EvidencePlanner(store, cap, adapter, authorities={"issuer": authority}),
        plan,
        adapter,
    )


def test_durable_evidence_reservation_cache_and_no_blind_reissue(tmp_path):
    planner, plan, adapter = planner_fixture(tmp_path)
    first = planner.collect(plan, (), as_of=FIXTURE_TIME)
    assert len(first) == 1 and adapter.calls == 1
    assert planner.collect(plan, (), as_of=FIXTURE_TIME) == first and adapter.calls == 1
    assert planner.scopes(plan, first, as_of=FIXTURE_TIME) == []
    with planner.store._tx() as db:
        assert (
            planner.store._run(
                db, f"select count(*) from {planner.store.prefix}attempts"
            ).fetchone()[0]
            == 1
        )


def test_wrong_provider_account_is_rejected_and_reservation_not_retried(tmp_path):
    planner, plan, adapter = planner_fixture(tmp_path, wrong_account=True)
    with pytest.raises(ValueError):
        planner.collect(plan, (), as_of=FIXTURE_TIME)
    with pytest.raises(Conflict):
        planner.collect(plan, (), as_of=FIXTURE_TIME)
    assert adapter.calls == 1


def test_acquisition_quota_and_locality_fingerprints(tmp_path):
    from ofc_engine.control.storage import CapacityReached

    planner, plan, adapter = planner_fixture(tmp_path, rpm=1)
    planner.collect(plan, (), as_of=FIXTURE_TIME)
    payload = plan.data()
    payload["nodes"][0]["locality"] = "Austin"
    other = Plan.parse(payload)
    assert (
        planner.scopes(plan, (), as_of=FIXTURE_TIME)[0]["scope_id"]
        != planner.scopes(other, (), as_of=FIXTURE_TIME)[0]["scope_id"]
    )
    with pytest.raises(CapacityReached):
        planner.collect(other, (), as_of=FIXTURE_TIME)
    assert adapter.calls == 1


def test_live_transport_and_revoked_capability_cannot_run(tmp_path):
    from ofc_engine.control.acquisition import EvidencePlanner

    planner, plan, adapter = planner_fixture(tmp_path)
    with pytest.raises(PermissionError):
        EvidencePlanner(
            planner.store, replace(planner.capability, offline_only=False), adapter
        )
    planner.capability = replace(planner.capability, revoked=True)
    with pytest.raises(PermissionError):
        planner.collect(plan, (), as_of=FIXTURE_TIME)
    assert adapter.calls == 0


def test_distinct_human_delegation_and_signed_intent(tmp_path):
    from ofc_engine.control.approvals import (
        ApprovalKey,
        create_intent,
        revoke_intent,
        validate_intent,
    )
    from ofc_engine.control.models import utc_now

    store = ControlStore(tmp_path / "human.db")
    runtime = ControlRuntime(store)
    req = campaign()
    rid = store.admit(req)["review_id"]
    runtime.run_once()
    proof = store.proof(rid, "sandbox", "campaign-agent")
    human = Identity("sandbox", "reviewer-1", frozenset({"read", "approve"}), "human")
    expires = (
        (datetime.now(timezone.utc) + timedelta(minutes=10))
        .isoformat()
        .replace("+00:00", "Z")
    )
    grant = Delegation(
        "sandbox",
        "campaign-agent",
        "reviewer-1",
        proof["current_digest"],
        frozenset({"B01"}),
        frozenset({"approve"}),
        expires,
    )
    key = Ed25519PrivateKey.generate()
    trusted = ApprovalKey("sandbox", key.public_key().public_bytes_raw(), expires)
    runtime.approval_keys = {"reviewer-1": trusted}
    runtime.delegations = (grant,)
    data = {
        "request_key": "human-1",
        "node_ids": ["B01"],
        "expires_at": expires,
        "action": "REVIEW_HANDOFF",
        "maximum_spend_minor": 0,
    }
    fields = {
        "review_id": rid,
        "tenant_id": "sandbox",
        "reviewer_id": "reviewer-1",
        "plan_digest": proof["current_digest"],
        "policy_digest": proof["policy_digest"],
        "evidence_root": proof["evidence_root"],
        "revision": proof["revision"],
        "request": data,
    }
    signed = data | {
        "signature": base64.b64encode(key.sign(canonical_bytes(fields))).decode()
    }
    intent = create_intent(runtime, rid, human, signed)
    assert (
        validate_intent(intent, proof, trusted, as_of=utc_now())["release_authorized"]
        is False
    )
    runtime.delegations = ()
    with pytest.raises(NotFound):
        create_intent(runtime, rid, human, signed | {"request_key": "other"})
    runtime.delegations = (grant,)
    assert revoke_intent(runtime, rid, human, intent["intent_id"])["state"] == "REVOKED"
    with pytest.raises(ValueError):
        validate_intent(intent, proof, trusted, as_of=utc_now(), revoked=True)


def test_weighted_tenant_queue_and_key_rotation(tmp_path):
    key = Fernet.generate_key()
    store = ControlStore(tmp_path / "qos.db", encryption_key=key)
    for tenant in ("a", "a", "b"):
        req = campaign(tenant=tenant)
        data = req.data()
        data["plan"]["idempotency_key"] += (
            "-" + str(len(store.admission_estimate(tenant))) if tenant == "a" else ""
        )
        if tenant == "a" and store.admission_estimate(tenant)["pending"]:
            data["plan"]["idempotency_key"] += "-2"
        store.admit(ReviewRequest.parse(data))
    ControlRuntime(store, tenant_weights={"a": 2, "b": 1})
    first = store.claim("one")
    second = store.claim("two")
    assert first["tenant_id"] != second["tenant_id"]
    new = Fernet.generate_key()
    store.rotate_encryption(new)
    ControlStore(tmp_path / "qos.db", encryption_key=new).request(
        first["review_id"], first["tenant_id"], first["actor_id"]
    )
    with pytest.raises(Exception):
        ControlStore(tmp_path / "qos.db", encryption_key=key).request(
            first["review_id"], first["tenant_id"], first["actor_id"]
        )


def test_failed_worker_has_terminal_incident_and_no_private_diagnostic(
    tmp_path, monkeypatch
):
    import ofc_engine.control.runtime as module

    store = ControlStore(tmp_path / "failed.db")
    rid = store.admit(campaign())["review_id"]

    def fail(*args, **kwargs):
        raise ValueError("private-input-never-log")

    monkeypatch.setattr(module, "review", fail)
    with pytest.raises(ValueError):
        ControlRuntime(store).run_once(finalize_errors=True)
    assert store.snapshot(rid, "sandbox", "campaign-agent")["state"] == "FAILED"
    events = store.events(rid, "sandbox", "campaign-agent")
    assert events[-1]["kind"] == "FAILED" and "private-input" not in json.dumps(events)


def test_registered_corpus_and_retained_verifier_dispatch():
    from ofc_engine.control.evaluation import (
        corpus_manifest,
        registered_requests,
        bootstrap_interval,
    )
    from ofc_engine.control.retained import VerifierRegistry

    rows = list(registered_requests(corpus_manifest(), split="holdout"))
    assert len(rows) == 150 and len({r["case_id"] for r in rows}) == 150
    assert bootstrap_interval([True, False, True], resamples=100)["n"] == 3
    registry = VerifierRegistry()
    proof = review(campaign())
    with pytest.raises(ValueError):
        registry.verify(proof)
    registry.register(proof["policy_digest"], verify)
    assert registry.verify(proof)["valid"]


def test_budget_optimization_cannot_use_caller_invented_price():
    from ofc_engine.control.models import Budget

    payload = campaign().data()
    payload["plan"]["nodes"] = payload["plan"]["nodes"][:1]
    payload["plan"]["edges"] = []
    for candidate in payload["plan"]["nodes"][0]["candidate_pool"]:
        candidate["cost_minor"] = 1
        candidate["currency"] = "USD"
    payload["plan"]["budgets"] = [Budget(currency="USD", limit_minor=100).data()]
    frozen = ReviewRequest.parse(payload)
    for item in payload["evidence"]:
        item["plan_digest"] = frozen.plan.digest
        item["input_digest"] = claim_fingerprint(frozen.plan, "B01", item["claim"])
    proof = review(ReviewRequest.parse(payload))
    assert (
        not proof["patches"]
        and proof["constraints"][0]["input_assurance"] == "DECLARED_INPUTS"
    )


def test_cultural_place_relevance_does_not_imply_venue_permission():
    from ofc_engine.control.policies import evaluate

    req = campaign()
    payload = req.data()
    payload["plan"]["nodes"] = payload["plan"]["nodes"][:1]
    payload["plan"]["edges"] = []
    node = payload["plan"]["nodes"][0]
    node.update(
        domain="place",
        locality="Chicago",
        claims=["place_cultural_relevance", "contract_rights"],
    )
    frozen = ReviewRequest.parse(payload)
    for item in payload["evidence"]:
        item.update(
            claim="place_cultural_relevance",
            locality="Chicago",
            plan_digest=frozen.plan.digest,
            input_digest=claim_fingerprint(
                frozen.plan, "B01", "place_cultural_relevance"
            ),
        )
    request = ReviewRequest.parse(payload)
    v = evaluate(
        request.plan,
        request.evidence,
        baseline_digest=request.plan.digest,
        as_of=FIXTURE_TIME,
        synthetic=True,
    )
    assert (
        v["B01"]["claims"][0]["status"] == "QUALIFIED"
        and v["B01"]["status"] == "UNCERTAIN"
    )
    assert not review(request)["patches"]
    payload["plan"]["nodes"][0]["locality"] = "Austin"
    assert (
        review(ReviewRequest.parse(payload))["verdicts"]["B01"]["claims"][0]["status"]
        != "QUALIFIED"
    )
