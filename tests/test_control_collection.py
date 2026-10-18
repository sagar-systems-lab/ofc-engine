import base64
from datetime import timedelta
import json
import time
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from ofc_engine.control.canonical import canonical_bytes
from ofc_engine.control.collection import GraphCollector, SourceBinding, timestamp
from ofc_engine.control.collection_contracts import CollectionGrant
from ofc_engine.control.feeds import QlooGraphSource, SignedBusinessSource
from ofc_engine.control.fixtures import cascading_campaign
from ofc_engine.control.models import Evidence, ReviewRequest, instant
from ofc_engine.control.policies import Authority, POLICY_VERSION
from ofc_engine.control.runtime import ControlRuntime
from ofc_engine.control.storage import ControlStore
from ofc_engine.control.verifier import verify, ProofRejected


def collection_runtime(tmp_path, *, live=False, calls=64, malformed=None):
    now = [time.time()]
    clock = lambda: now[0]
    expiry = (
        (instant(timestamp(clock)) + timedelta(days=2))
        .isoformat()
        .replace("+00:00", "Z")
    )
    qkey, bkey, jkey = (Ed25519PrivateKey.generate() for _ in range(3))
    request = cascading_campaign()
    plan = request.plan
    p = plan.data()
    p["limits"]["provider_calls"] = calls if live else 0
    plan = plan.parse(p)
    authorities = {
        "qloo-adapter": Authority(
            "qloo-adapter",
            "sandbox",
            qkey.public_key().public_bytes_raw(),
            frozenset(QlooGraphSource.claims),
            "qloo",
            expiry,
            provider_account_scope="q-account",
            entitlement_id="q-license",
        ),
        "venue-source": Authority(
            "venue-source",
            "sandbox",
            bkey.public_key().public_bytes_raw(),
            frozenset(SignedBusinessSource.claims),
            "operator",
            expiry,
            provider_account_scope="business-account",
            entitlement_id="business-license",
        ),
    }
    observed = []

    class QlooTransport:
        offline = not live

        def request(self, method, path, payload, *, timeout):
            now[0] += 0.001
            observed.append(("qloo", method, path))
            params = parse_qs(urlsplit(path).query)
            pool = params["filter.results.entities"][0].split(",")
            order = sorted(
                pool, key=lambda s: ({"adidas": 0, "puma": 1, "nike": 2}.get(s, 0), s)
            )
            return 200, json.dumps(
                {
                    "success": True,
                    "results": {
                        "entities": [
                            {
                                "entity_id": x,
                                "type": "urn:entity",
                                "query": {"affinity": len(order) - i},
                            }
                            for i, x in enumerate(order)
                        ]
                    },
                }
            ).encode()

    class BusinessTransport:
        offline = not live

        def request(self, method, path, scope, *, timeout):
            now[0] += 0.001
            observed.append(("operator", method, path))
            candidate = scope["candidate_id"]
            claim = scope["claim"]
            node = scope["node_id"]
            facts = {
                "signed_contract": candidate not in {"nike", "puma"}
                and not (node == "D01" and candidate == "planned"),
                "scope_covers_plan": True,
                "candidate_id": candidate,
                "contract_ref": "official-contract",
            }
            e = Evidence(
                id="b-" + scope["input_digest"][:32],
                tenant_id=scope["tenant_id"],
                plan_digest=scope["anchor_digest"],
                node_id=node,
                claim=claim,
                subject_id=scope["subject_id"],
                candidate_pool=tuple(scope["pool"]),
                locality=scope["locality"],
                input_digest=scope["input_digest"],
                source="operator",
                mode="ATTESTED",
                attempt_id="b-" + scope["input_digest"][:32],
                observed_at=timestamp(clock),
                expires_at=expiry,
                policy_version=POLICY_VERSION,
                license_scope="private_review",
                facts=facts,
                issuer="venue-source",
                provider_account_scope="business-account",
                entitlement_id="business-license",
                raw_payload_ref="a" * 64,
                extraction_rule_version="business-feed-1",
            )
            if malformed == "account":
                e = e.model_copy(update={"provider_account_scope": "wrong"})
            if malformed == "signature":
                signature = base64.b64encode(b"x" * 64).decode()
            else:
                signature = base64.b64encode(
                    bkey.sign(canonical_bytes(e.signing_fields()))
                ).decode()
            e = e.model_copy(update={"signature": signature})
            return 200, json.dumps(
                {
                    "schema": "ofc.source-evidence/1",
                    "account_scope": "business-account",
                    "entitlement_id": "business-license",
                    "evidence": [e.data()],
                }
            ).encode()

    store = ControlStore(
        tmp_path / "collection.db", clock=clock, encryption_key=Fernet.generate_key()
    )
    bindings = []
    for id, provider, account, license, claims, adapter in (
        (
            "q-grant",
            "qloo",
            "q-account",
            "q-license",
            tuple(QlooGraphSource.claims),
            QlooGraphSource(QlooTransport(), issuer="qloo-adapter", signing_key=qkey),
        ),
        (
            "b-grant",
            "operator",
            "business-account",
            "business-license",
            tuple(SignedBusinessSource.claims),
            SignedBusinessSource(BusinessTransport()),
        ),
    ):
        grant = CollectionGrant(
            id=id,
            tenant_id="sandbox",
            provider=provider,
            account_scope=account,
            entitlement_id=license,
            claims=claims,
            expires_at=expiry,
            rpm=128,
            rpd=256,
            total=256,
            per_review=128,
            allow_live=live,
        )
        bindings.append(SourceBinding(grant, adapter))
    collector = GraphCollector(
        store, bindings, authorities=authorities, issuer="journal", signing_key=jkey
    )
    runtime = ControlRuntime(store, authorities=authorities, acquisition=collector)
    return runtime, ReviewRequest(plan=plan, mode="PRIVATE"), observed, now


def test_graph_native_collection_requalifies_new_dependent_inputs(tmp_path):
    runtime, request, observed, _ = collection_runtime(tmp_path)
    rid = runtime.store.admit(request)["review_id"]
    assert runtime.run_once()
    proof = runtime.store.proof(
        rid, "sandbox", "campaign-agent", authorities=runtime.authorities
    )
    assert proof["schema"] == "ofc.collected-review/1"
    assert proof["impact"]["changed"] == ["B01", "D01"] and proof["revision"] == 2
    assert len(proof["impact"]["preserved"]) == 20
    assert proof["verdicts"]["D01"]["status"] == "QUALIFIED"
    assert proof["provider_calls"] == 0 and len(observed) > 2
    assert verify(
        proof,
        trusted_authorities=runtime.authorities,
        trusted_collection_keys=runtime.acquisition.public_keys,
    )["valid"]
    with pytest.raises(ProofRejected):
        verify(proof, trusted_authorities=runtime.authorities)
    assert runtime.store.trace(rid, "sandbox", "campaign-agent")["spans"]


@pytest.mark.parametrize("malformed", ["account", "signature"])
def test_business_feed_cannot_self_assert_authority(tmp_path, malformed):
    runtime, request, _, _ = collection_runtime(tmp_path, malformed=malformed)
    rid = runtime.store.admit(request)["review_id"]
    runtime.run_once()
    proof = runtime.store.proof(
        rid, "sandbox", "campaign-agent", authorities=runtime.authorities
    )
    assert proof["verdicts"]["B01"]["status"] != "QUALIFIED"
    assert proof["impact"]["changed"] == []


def test_explicit_live_branch_is_budget_bounded_with_fake_transport(tmp_path):
    runtime, request, observed, _ = collection_runtime(tmp_path, live=True, calls=2)
    rid = runtime.store.admit(request)["review_id"]
    runtime.run_once()
    proof = runtime.store.proof(
        rid, "sandbox", "campaign-agent", authorities=runtime.authorities
    )
    assert proof["provider_calls"] == 2 and len(observed) == 2
    assert not proof["patches"] and proof["spend_minor"] == 0
    assert verify(
        proof,
        trusted_authorities=runtime.authorities,
        trusted_collection_keys=runtime.acquisition.public_keys,
    )["valid"]


def test_source_crash_preserves_tickets_without_automatic_reissue(tmp_path):
    runtime, request, observed, now = collection_runtime(tmp_path)
    rid = runtime.store.admit(request)["review_id"]

    def crash(stage, job):
        if stage == "SOURCE_SENT":
            raise SystemExit("simulated process exit")

    with pytest.raises(SystemExit):
        runtime.run_once(checkpoint=crash)
    assert not observed
    now[0] += 31
    runtime.store.recover_expired()
    runtime.store.resume(rid, "sandbox", "campaign-agent")
    runtime.run_once()
    with runtime.store._tx() as db:
        rows = runtime.store._run(
            db,
            f"select state from {runtime.store.prefix}provider_io where provider='qloo' order by call_id",
        ).fetchall()
    assert len(rows) == 2 and any(row[0] == "UNKNOWN_OUTCOME" for row in rows)
    assert not any(p == "qloo" for p, _, _ in observed)


def test_collected_proof_rejects_tampered_call_budget(tmp_path):
    from ofc_engine.control.canonical import digest

    runtime, request, _, _ = collection_runtime(tmp_path)
    rid = runtime.store.admit(request)["review_id"]
    runtime.run_once()
    proof = runtime.store.proof(
        rid, "sandbox", "campaign-agent", authorities=runtime.authorities
    )
    proof["collection"]["provider_calls"] = 100
    proof["proof_digest"] = digest(
        {k: v for k, v in proof.items() if k != "proof_digest"}
    )
    with pytest.raises(ProofRejected):
        verify(
            proof,
            trusted_authorities=runtime.authorities,
            trusted_collection_keys=runtime.acquisition.public_keys,
        )


def test_collected_signature_failure_is_a_typed_rejection(tmp_path):
    from ofc_engine.control.canonical import digest

    runtime, request, _, _ = collection_runtime(tmp_path)
    rid = runtime.store.admit(request)["review_id"]
    runtime.run_once()
    proof = runtime.store.proof(
        rid, "sandbox", "campaign-agent", authorities=runtime.authorities
    )
    proof["collection"]["signature"] = base64.b64encode(b"x" * 64).decode()
    proof["proof_digest"] = digest(
        {k: v for k, v in proof.items() if k != "proof_digest"}
    )
    with pytest.raises(ProofRejected):
        verify(
            proof,
            trusted_authorities=runtime.authorities,
            trusted_collection_keys=runtime.acquisition.public_keys,
        )


def test_encrypted_collector_payloads_survive_key_rotation(tmp_path):
    runtime, request, observed, _ = collection_runtime(tmp_path)
    rid = runtime.store.admit(request)["review_id"]
    runtime.run_once()
    original = runtime.store.proof(
        rid, "sandbox", "campaign-agent", authorities=runtime.authorities
    )
    runtime.store.rotate_encryption(Fernet.generate_key())
    assert (
        runtime.store.proof(
            rid, "sandbox", "campaign-agent", authorities=runtime.authorities
        )
        == original
    )
    before = sum(source == "qloo" for source, _, _ in observed)
    payload = request.data()
    payload["plan"]["idempotency_key"] = "rotated-cache"
    runtime.store.admit(ReviewRequest.parse(payload))
    runtime.run_once()
    assert sum(source == "qloo" for source, _, _ in observed) == before


def test_async_collection_exposes_progress_before_delayed_source_completes(tmp_path):
    from threading import Event, Thread
    from urllib.request import Request, urlopen
    from ofc_engine.control.http import ControlAccess, Identity
    from ofc_engine.control.sdk import ControlClient
    from ofc_engine.hosted_preview import HostedFixtureServer

    runtime, review_request, _, _ = collection_runtime(tmp_path)
    entered, release = Event(), Event()
    transport = runtime.acquisition.bindings[0].adapter.transport
    original = transport.request

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release the delayed source"
        return original(*args, **kwargs)

    transport.request = delayed
    token = "collection-contract-" + "a" * 40
    server = HostedFixtureServer(
        ("127.0.0.1", 0),
        control=runtime,
        control_enabled=True,
        control_access=ControlAccess({token: Identity("sandbox", "campaign-agent")}),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = "http://127.0.0.1:" + str(server.server_port)
        client = ControlClient(origin, token)
        message = Request(
            origin + "/api/control/plans",
            data=canonical_bytes(review_request.data()),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        with urlopen(message, timeout=3) as response:
            assert response.status == 202
            receipt = json.load(response)
        assert entered.wait(3)
        assert client.status(receipt["review_id"])["state"] != "FINISHED"
        events = client.events(receipt["review_id"])
        assert any(event["kind"] == "SOURCE_SENT" for event in events["events"])
        cursor = max(event["sequence"] for event in events["events"])
        assert client.events(receipt["review_id"], after=cursor)["events"] == []
        release.set()
        deadline = time.monotonic() + 20
        while client.status(receipt["review_id"])["state"] != "FINISHED":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert client.submit(review_request)["review_id"] == receipt["review_id"]
        assert all(
            event["sequence"] > cursor
            for event in client.events(receipt["review_id"], after=cursor)["events"]
        )
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_provider_trace_contains_each_actual_stage_and_no_private_bodies(tmp_path):
    runtime, request, _, _ = collection_runtime(tmp_path)
    rid = runtime.store.admit(request)["review_id"]
    runtime.run_once()
    trace = runtime.store.trace(rid, "sandbox", "campaign-agent")
    stages = {s["stage"] for s in trace["spans"]}
    assert {
        "queue",
        "controller",
        "rank",
        "attestation",
        "policy",
        "optimize",
        "cas",
        "recheck",
        "proof",
    } <= stages
    assert trace["host_class"] == "cold" and trace["simulation"] is True
    assert trace["otlp"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    raw = json.dumps(trace)
    assert all(
        value not in raw
        for value in ("nike", "official-contract", "business-account", "Authorization")
    )


def test_entity_resolution_is_admitted_cached_and_city_scoped(tmp_path):
    runtime, request, _, now = collection_runtime(tmp_path)
    collector = runtime.acquisition
    original = collector.bindings[0]
    grant = original.grant.model_copy(update={"resolve_entities": True})
    searches = []

    def search(method, path, payload, *, timeout):
        searches.append(path)
        now[0] += 0.001
        return 200, json.dumps(
            {
                "results": [
                    {
                        "entity_id": "550e8400-e29b-41d4-a716-446655440000",
                        "name": "Artist",
                    }
                ]
            }
        ).encode()

    original.adapter.transport.request = search
    collector.bindings = (SourceBinding(grant, original.adapter),) + collector.bindings[
        1:
    ]
    rid = runtime.store.admit(request)["review_id"]
    first = collector.resolve(rid, request.plan, "Artist", "artist", locality="Chicago")
    assert (
        collector.resolve(rid, request.plan, "Artist", "artist", locality="Chicago")
        == first
        and len(searches) == 1
    )
    collector.resolve(rid, request.plan, "Artist", "artist", locality="Austin")
    assert len(searches) == 2
    payload = request.plan.data()
    payload["tenant_id"] = "another"
    foreign = request.plan.parse(payload)
    with pytest.raises(PermissionError):
        collector.resolve(rid, foreign, "Artist", "artist")


def test_qualified_qloo_cache_keeps_observation_time_and_avoids_new_calls(tmp_path):
    runtime, request, observed, now = collection_runtime(tmp_path)
    rid = runtime.store.admit(request)["review_id"]
    runtime.run_once()
    first = runtime.store.proof(
        rid, "sandbox", "campaign-agent", authorities=runtime.authorities
    )
    timestamps = [
        e["observed_at"]
        for e in first["evidence"]
        if e["claim"] == "cultural_brand_rank"
    ]
    calls = sum(source == "qloo" for source, _, _ in observed)
    payload = request.data()
    payload["plan"]["idempotency_key"] = "fresh-review"
    next_request = ReviewRequest.parse(payload)
    second_id = runtime.store.admit(next_request)["review_id"]
    runtime.run_once()
    second = runtime.store.proof(
        second_id, "sandbox", "campaign-agent", authorities=runtime.authorities
    )
    assert sum(source == "qloo" for source, _, _ in observed) == calls
    assert [
        e["observed_at"]
        for e in second["evidence"]
        if e["claim"] == "cultural_brand_rank"
    ] == timestamps
    assert all(
        e["facts"].get("cache_replay")
        for e in second["evidence"]
        if e["claim"] == "cultural_brand_rank"
    )


def test_expired_rank_cache_refreshes_with_new_durable_calls(tmp_path):
    runtime, request, observed, now = collection_runtime(tmp_path)
    rid = runtime.store.admit(request)["review_id"]
    runtime.run_once()
    assert sum(s == "qloo" for s, _, _ in observed) == 2
    now[0] += 86401
    payload = request.data()
    payload["plan"]["idempotency_key"] = "expired-refresh"
    rid = runtime.store.admit(ReviewRequest.parse(payload))["review_id"]
    runtime.run_once()
    proof = runtime.store.proof(
        rid, "sandbox", "campaign-agent", authorities=runtime.authorities
    )
    assert sum(s == "qloo" for s, _, _ in observed) == 4
    assert proof["verdicts"]["B01"]["claims"][0]["status"] == "QUALIFIED"


def test_revoked_source_entitlement_cannot_reuse_cached_rank(tmp_path):
    runtime, request, observed, _ = collection_runtime(tmp_path)
    rid = runtime.store.admit(request)["review_id"]
    runtime.run_once()
    before = sum(s == "qloo" for s, _, _ in observed)
    runtime.acquisition.revoke_entitlement("sandbox", "q-license")
    payload = request.data()
    payload["plan"]["idempotency_key"] = "after-revocation"
    rid = runtime.store.admit(ReviewRequest.parse(payload))["review_id"]
    runtime.run_once()
    proof = runtime.store.proof(
        rid, "sandbox", "campaign-agent", authorities=runtime.authorities
    )
    assert proof["verdicts"]["B01"]["claims"][0]["status"] != "QUALIFIED"
    assert sum(s == "qloo" for s, _, _ in observed) == before


@pytest.mark.parametrize("collected", [False, True])
def test_pinned_retained_bundle_is_selected_without_reinterpreting_policy(
    tmp_path, collected
):
    from hashlib import sha256
    from ofc_engine.control.release import build_bundle
    from ofc_engine.control.retained import VerifierRegistry
    from ofc_engine.control.policies import POLICY_DIGEST

    anchors = {}
    if collected:
        runtime, request, _, _ = collection_runtime(tmp_path)
        rid = runtime.store.admit(request)["review_id"]
        runtime.run_once()
        proof = runtime.store.proof(
            rid, "sandbox", "campaign-agent", authorities=runtime.authorities
        )
        anchors = {
            "trusted_authorities": runtime.authorities,
            "trusted_collection_keys": runtime.acquisition.public_keys,
        }
    else:
        from ofc_engine.control.engine import review
        from ofc_engine.control.fixtures import campaign

        proof = review(campaign())
    raw = build_bundle("a" * 40)
    bundle = tmp_path / "verifier.zip"
    bundle.write_bytes(raw)
    registry = VerifierRegistry()
    registry.register_bundle(
        POLICY_DIGEST, bundle, expected_sha256=sha256(raw).hexdigest()
    )
    assert registry.verify(proof, **anchors)["valid"]
    with pytest.raises(ValueError):
        VerifierRegistry().register_bundle(
            POLICY_DIGEST, bundle, expected_sha256="0" * 64
        )


def attestation_service_runtime(tmp_path, request, records, *, discover_window=False):
    key, journal = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    now = [instant(request.as_of).timestamp()]
    expiry = (
        (instant(request.as_of) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    )

    class Feed:
        offline = True

        def request(self, method, path, scope, *, timeout):
            now[0] += 0.001
            matches = [
                e
                for e in records
                if e.node_id == scope["node_id"]
                and e.claim == scope["claim"]
                and e.input_digest == scope["input_digest"]
            ]
            if (
                discover_window
                and scope["node_id"] == "child"
                and scope["claim"] == "schedule_window"
            ):
                matches = [
                    e
                    for e in records
                    if e.node_id == "child"
                    and e.claim == "schedule_window"
                    and e.facts.get("start_at") == "2026-10-20T13:00:00Z"
                ]
            output = []
            for e in matches:
                item = e.model_copy(
                    update={
                        "source": "operator",
                        "mode": "ATTESTED",
                        "license_scope": "private_review",
                        "plan_digest": scope["anchor_digest"],
                        "issuer": "official-feed",
                        "provider_account_scope": "official-account",
                        "entitlement_id": "official-license",
                        "raw_payload_ref": "b" * 64,
                        "extraction_rule_version": "feed-parser-1",
                        "observed_at": timestamp(lambda: now[0]),
                        "expires_at": expiry,
                        "signature": None,
                    }
                )
                item = item.model_copy(
                    update={
                        "signature": base64.b64encode(
                            key.sign(canonical_bytes(item.signing_fields()))
                        ).decode()
                    }
                )
                output.append(item.data())
            return 200, json.dumps(
                {
                    "schema": "ofc.source-evidence/1",
                    "account_scope": "official-account",
                    "entitlement_id": "official-license",
                    "evidence": output,
                }
            ).encode()

    authority = Authority(
        "official-feed",
        request.plan.tenant_id,
        key.public_key().public_bytes_raw(),
        frozenset(SignedBusinessSource.claims),
        "operator",
        expiry,
        provider_account_scope="official-account",
        entitlement_id="official-license",
    )
    store = ControlStore(
        tmp_path / "service.db",
        clock=lambda: now[0],
        encryption_key=Fernet.generate_key(),
    )
    grant = CollectionGrant(
        id="official-grant",
        tenant_id=request.plan.tenant_id,
        provider="operator",
        account_scope="official-account",
        entitlement_id="official-license",
        claims=tuple(SignedBusinessSource.claims),
        expires_at=expiry,
        rpm=100,
        rpd=100,
        total=100,
        per_review=100,
    )
    collector = GraphCollector(
        store,
        (SourceBinding(grant, SignedBusinessSource(Feed())),),
        authorities={authority.issuer: authority},
        issuer="journal",
        signing_key=journal,
    )
    return ControlRuntime(
        store, authorities=collector.authorities, acquisition=collector
    ), ReviewRequest(plan=request.plan, mode="PRIVATE")


def test_signed_capacity_service_forces_a_feasible_venue(tmp_path):
    from test_control_extensions import venue_request

    source = venue_request()
    runtime, request = attestation_service_runtime(tmp_path, source, source.evidence)
    rid = runtime.store.admit(request)["review_id"]
    runtime.run_once()
    proof = runtime.store.proof(rid, "tenant", "agent", authorities=runtime.authorities)
    assert proof["current"]["nodes"][0]["candidate_id"] == "large"
    assert (
        proof["verdicts"]["V"]["status"] == "QUALIFIED" and proof["provider_calls"] == 0
    )


def test_signed_schedule_service_licenses_a_multi_field_temporal_repair(tmp_path):
    from test_control_extensions import synthetic_fact
    from ofc_engine.control.models import Node, Candidate, Plan, Actor, Edge, Patch
    from ofc_engine.control.engine import apply_patches

    nodes = (
        Node(
            id="parent",
            domain="operations",
            owner="agent",
            label="First event",
            candidate_id="x",
            candidate_pool=(Candidate(id="x", label="X"),),
            claims=("contract_rights", "schedule_window"),
            start_at="2026-10-20T08:00:00Z",
            end_at="2026-10-20T12:00:00Z",
        ),
        Node(
            id="child",
            domain="schedule",
            owner="agent",
            label="Second event",
            candidate_id="y",
            candidate_pool=(Candidate(id="y", label="Y"),),
            claims=("contract_rights", "schedule_window"),
            mutable_fields=("start_at", "end_at"),
            start_at="2026-10-20T11:00:00Z",
            end_at="2026-10-20T12:00:00Z",
        ),
    )
    plan = Plan(
        tenant_id="tenant",
        actor=Actor(id="agent"),
        idempotency_key="schedule-feed",
        objective="Resolve a sourced schedule conflict",
        nodes=nodes,
        edges=(Edge(source="parent", target="child", type="TEMPORAL_BEFORE"),),
    )
    future = apply_patches(
        plan,
        (
            Patch(
                node_id="child",
                changes={
                    "start_at": "2026-10-20T13:00:00Z",
                    "end_at": "2026-10-20T14:00:00Z",
                },
                evidence_ids=("probe",),
            ),
        ),
    )
    evidence = []
    for variant in (plan, future):
        for node in variant.nodes:
            for claim in ("contract_rights", "schedule_window"):
                facts = (
                    {
                        "signed_contract": True,
                        "scope_covers_plan": True,
                        "candidate_id": node.candidate_id,
                        "contract_ref": "signed-ref",
                    }
                    if claim == "contract_rights"
                    else {
                        "start_at": node.start_at,
                        "end_at": node.end_at,
                        "confirmed": True,
                    }
                )
                e = synthetic_fact(
                    plan, node.id, claim, node.candidate_id, future=variant, facts=facts
                )
                e = e.model_copy(
                    update={
                        "id": e.id + "-" + e.input_digest[:8],
                        "attempt_id": e.id + "-" + e.input_digest[:8],
                    }
                )
                if e.id not in {v.id for v in evidence}:
                    evidence.append(e)
    source = ReviewRequest(
        plan=plan,
        evidence=tuple(evidence),
        mode="SYNTHETIC",
        as_of="2026-10-18T12:00:00Z",
    )
    runtime, request = attestation_service_runtime(
        tmp_path, source, source.evidence, discover_window=True
    )
    rid = runtime.store.admit(request)["review_id"]
    runtime.run_once()
    proof = runtime.store.proof(rid, "tenant", "agent", authorities=runtime.authorities)
    assert proof["patches"][0]["changes"] == {
        "start_at": "2026-10-20T13:00:00Z",
        "end_at": "2026-10-20T14:00:00Z",
    }
    assert all(c["status"] == "PASS" for c in proof["constraints"])


def test_candidate_oscillation_stops_at_three_scoped_rounds_and_replays():
    from ofc_engine.control.fixtures import campaign, FIXTURE_TIME
    from ofc_engine.control.engine import review
    from ofc_engine.control.graph import claim_fingerprint

    request = campaign()
    request = request.model_copy(update={"evidence": ()})
    origin = instant(FIXTURE_TIME)

    def collect(plan, old, index, as_of):
        order = (
            ("adidas", "puma", "nike"),
            ("puma", "nike", "adidas"),
            ("nike", "adidas", "puma"),
        )[index - 1]
        records = []
        for i in range(2):
            records.append(
                Evidence(
                    id=f"round-{index}-{i}",
                    tenant_id="sandbox",
                    plan_digest=request.plan.digest,
                    node_id="B01",
                    claim="cultural_brand_rank",
                    subject_id="synthetic-artist",
                    candidate_pool=("nike", "adidas", "puma"),
                    input_digest=claim_fingerprint(plan, "B01", "cultural_brand_rank"),
                    source="fixture",
                    mode="SYNTHETIC",
                    attempt_id=f"attempt-{index}-{i}",
                    observed_at=(
                        origin + timedelta(seconds=index * 2 - 1, microseconds=i)
                    )
                    .isoformat()
                    .replace("+00:00", "Z"),
                    expires_at=(origin + timedelta(seconds=index * 2 + 1))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    policy_version=POLICY_VERSION,
                    license_scope="synthetic_public",
                    ranks=order,
                )
            )
        return old + tuple(records), (
            origin + timedelta(seconds=index * 2)
        ).isoformat().replace("+00:00", "Z")

    proof = review(request, reconcile=collect)
    assert (
        proof["revision"] == 3
        and len(proof["rounds"]) == 3
        and proof["termination"] == "BOUND_REACHED"
    )
    assert verify(proof)["valid"] and proof["external_action_executed"] is False
