"""Postgres durable run and provider-attempt qualification on an isolated CI database."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from ofc_engine.contracts import RepairPatch
from ofc_engine.durable import StorageIntegrityError
from ofc_engine.hosted_agent import HostedAgentFlow
from ofc_engine.hosted_live import HostedLiveReview
from ofc_engine.intake import freeze_proposal
from ofc_engine.postgres_attempt import PostgresAttemptLedger
from ofc_engine.postgres_store import PostgresRevisionStore
from ofc_engine.qloo import QlooProbeClient, TransportReply
from ofc_engine.revisions import RevisionConflict, plan_digest
from ofc_engine.webdemo import canonical_proposal


@pytest.fixture(scope="module")
def pg_url():
    dsn = os.environ.get("OFC_TEST_POSTGRES_URL")
    if not dsn:
        pytest.skip("isolated Postgres CI service not configured")
    with psycopg.connect(dsn, sslmode="disable") as db:
        for role in ("anon", "authenticated"):
            db.execute(f"do $$ begin if not exists (select from pg_roles where rolname = '{role}') then create role {role}; end if; end $$")
        sql = (Path(__file__).resolve().parents[1] / "sql/001_private_runtime.sql").read_text()
        db.execute(sql)
        db.execute((Path(__file__).resolve().parents[1] / "sql/002_browser_proposal_scope.sql").read_text())
        allowed = db.execute(
            "select has_schema_privilege('anon','ofc_private','USAGE'),"
            "has_schema_privilege('authenticated','ofc_private','USAGE')"
        ).fetchone()
        assert allowed == (False, False)
    return dsn


def plan_for_test():
    proposal = canonical_proposal()
    slug = uuid4().hex
    intent = proposal.intent.model_copy(update={
        "run_id": "durable-" + slug,
        "client_request_id": "durable-" + slug,
        "constraints": {**proposal.intent.constraints, "external_action": False},
        "taste_context": {**proposal.intent.taste_context, "artist_id": "artist-ci"},
    })
    return proposal.model_copy(update={"intent": intent})


def test_revision_cas_restart_idempotency_and_dependency_lock(pg_url):
    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    proposal = plan_for_test()
    baseline, _ = freeze_proposal(proposal)
    store.insert(baseline)
    assert store.get(baseline.plan_id) == baseline
    with pytest.raises(RevisionConflict):
        store.insert(baseline)
    patch = RepairPatch(
        patch_id="ci-repair", decision_id="brand", base_revision=0,
        old_candidate_id="brand-a", new_candidate_id="brand-b",
        evidence_refs=("ci-evidence",), precondition_digest=plan_digest(baseline),
    )
    updated, invalidated = store.apply_patch_once(
        baseline.plan_id, patch, client_request_id="ci-commit"
    )
    assert updated.revision == 1
    assert invalidated == ("brand", "afterparty")
    other = PostgresRevisionStore(pg_url, insecure_local_test=True)
    assert other.get(baseline.plan_id) == updated
    assert other.pending_rechecks(baseline.plan_id) == invalidated
    assert len(other.audit_history(baseline.plan_id)) == 2
    assert other.apply_patch_once(
        baseline.plan_id, patch, client_request_id="ci-commit"
    ) == (updated, invalidated)
    assert len(other.audit_history(baseline.plan_id)) == 2
    with pytest.raises(RevisionConflict):
        other.apply_patch_once(baseline.plan_id, patch, client_request_id="another")


def test_journal_tampering_fails_closed(pg_url):
    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    proposal = plan_for_test()
    baseline, _ = freeze_proposal(proposal)
    store.insert(baseline)
    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute(
            "update ofc_private.events set event_hash = %s where plan_id=%s",
            ("f" * 64, baseline.plan_id)
        )
    with pytest.raises(StorageIntegrityError):
        store.audit_history(baseline.plan_id)


def test_provider_reservation_is_one_time_across_independent_connections(pg_url):
    scope = uuid4().hex * 2
    first = PostgresAttemptLedger(
        pg_url, scope=scope, insecure_local_test=True
    )
    duplicate = PostgresAttemptLedger(
        pg_url, scope=scope, insecure_local_test=True
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        reserved = [
            pool.submit(ledger.reserve)
            for ledger in (first, duplicate)
        ]
        assert sorted(task.result() for task in reserved) == [False, True]
    assert duplicate.get() == ("SPENT", None)
    first.complete({
        "status": "BLOCKED",
        "reason": "PROVIDER_RESULT_UNAVAILABLE",
        "release_authorized": False,
        "external_action_executed": False,
    })
    saved = duplicate.get()
    assert saved[0] == "BLOCKED"
    assert saved[1]["release_authorized"] is False
    assert duplicate.reserve() is False
    with pytest.raises(ValueError, match="budget belongs"):
        PostgresAttemptLedger(
            pg_url, scope="0" * 64, insecure_local_test=True
        )


class FixedTransport:
    def __init__(self):
        self.calls = 0

    def get(self, url, headers, *, timeout, max_bytes):
        self.calls += 1
        return TransportReply(200, json.dumps({
            "success": True,
            "results": {"entities": [
                {
                    "entity_id": name, "type": "urn:entity",
                    "query": {
                        "affinity": score,
                        "explainability": {"signal.interests.entities": {}},
                    },
                }
                for name, score in (("brand-b", 0.93), ("brand-a", 0.51))
            ]}
        }).encode())


def test_full_hosted_provider_to_postgres_revision_and_readonly_review(pg_url):
    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    proposal = plan_for_test()
    flow = HostedAgentFlow(proposal=proposal, store=store)
    transport = FixedTransport()
    # A new CI database/container starts with its one configured Qloo scope.
    with psycopg.connect(pg_url, sslmode="disable") as db:
        scope = db.execute(
            "select scope from ofc_private.provider_scope where id=1"
        ).fetchone()[0]
        db.execute("delete from ofc_private.provider_attempt")
    ledger = PostgresAttemptLedger(pg_url, scope=scope, insecure_local_test=True)
    live = HostedLiveReview(
        api_key="fake-ci-only",
        artist_id="artist-ci", brand_ids=("brand-a", "brand-b"),
        selected_brand_id="brand-a", ledger=ledger,
        operator_token="operator-" + "x" * 40,
        client=QlooProbeClient(api_key="fake-ci-only", transport=transport),
        agent_evaluator=flow.evaluate,
    )
    code, reply = live.observe()
    assert code == 200 and transport.calls == 1
    report = reply["agent_run"]
    assert report["revision"] == 1
    assert report["changed_ids"] == ["brand"]
    assert report["pending_rechecks"] == ["brand", "afterparty"]
    assert report["source_mode"] == "LIVE_HTTP_UNVERIFIED"
    assert report["release_authorized"] is False
    assert report["verification_status"] == "REVIEW_REQUIRED"
    assert report["real_qloo_requests"] == 1
    stored = PostgresRevisionStore(pg_url, insecure_local_test=True)
    assert stored.get(proposal.intent.run_id).revision == 1
    assert len(stored.audit_history(proposal.intent.run_id)) == 2
    assert live.public_result()[1]["agent_run"] == report
    assert live.observe()[1]["requests_this_call"] == 0
    assert transport.calls == 1
    for secret in ("fake-ci-only", "artist-ci", "brand-a", "brand-b"):
        assert secret not in json.dumps(live.public_result()[1])


def test_hosted_browser_run_save_and_reload_with_private_ticket(pg_url):
    from threading import Thread
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from ofc_engine.hosted_preview import HostedFixtureServer
    from ofc_engine.hosted_runs import HostedRunRegistry

    registry = HostedRunRegistry(
        PostgresRevisionStore(pg_url, insecure_local_test=True)
    )
    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute("update ofc_private.browser_budget set writes=0 where id=1")
    registry.max_daily_runs = 1
    with HostedFixtureServer(
        ("127.0.0.1", 0), run_registry=registry
    ) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)

        def submit(*, origin=None, method="POST"):
            payload = canonical_proposal().model_dump(mode="json")
            request = Request(
                base + "/api/runs",
                method=method,
                headers={
                    "Origin": origin or base,
                    "Content-Type": "application/json",
                },
                data=json.dumps(payload).encode(),
            )
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except HTTPError as error:
                return error.code, json.loads(error.read())

        try:
            assert submit(origin="https://attacker.example")[0] == 403
            assert submit(method="PUT")[0] == 405
            status, receipt = submit()
            assert status == 201
            assert receipt["expires_in_hours"] == 24
            assert receipt["run_id"].startswith("web-")
            assert receipt["review"]["verification_status"] == "REVIEW_REQUIRED"
            assert receipt["review"]["release_authorized"] is False
            assert len(receipt["review"]["decisions"]) == 6
            assert all(v["verdict"] == "UNCERTAIN"
                       for v in receipt["review"]["decisions"])
            review_path = "/api/runs/" + receipt["run_id"] + "/review"
            with pytest.raises(HTTPError) as error:
                urlopen(base + review_path, timeout=5)
            assert error.value.code == 404
            with urlopen(Request(
                base + review_path,
                headers={"Authorization": "Bearer " + receipt["ticket"]},
            ), timeout=5) as response:
                result = json.loads(response.read())
            assert result["journal_head"] == registry.store.audit_history(
                receipt["run_id"]
            )[-1].event_hash
            assert result["plan_digest"] == receipt["review"]["plan_digest"]
            assert result["release_authorized"] is False
            assert result["real_qloo_requests"] == 0
            assert submit()[0] == 429
            with urlopen(base + "/api/runs/status", timeout=5) as response:
                assert json.loads(response.read())["enabled"] is True
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_browser_run_quota_and_expiry_are_fail_closed(pg_url):
    from ofc_engine.hosted_runs import HostedRunRegistry, RunCapacityReached
    registry = HostedRunRegistry(
        PostgresRevisionStore(pg_url, insecure_local_test=True)
    )
    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute("update ofc_private.browser_budget set writes=0 where id=1")
    registry.max_daily_runs = 1
    saved = registry.create(canonical_proposal().model_dump(mode="json"))
    assert registry.review(saved["run_id"], saved["ticket"]) == saved["review"]
    with pytest.raises(KeyError):
        registry.review(saved["run_id"], "x" * 48)
    with pytest.raises(RunCapacityReached):
        registry.create(canonical_proposal().model_dump(mode="json"))
    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute(
            "update ofc_private.browser_runs set expires_at=now()-interval '1 second' "
            "where plan_id=%s", (saved["run_id"],)
        )
    with pytest.raises(KeyError):
        registry.review(saved["run_id"], saved["ticket"])


def test_durable_run_survives_new_server_and_storage_outage(pg_url, monkeypatch):
    from threading import Thread
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from ofc_engine.hosted_preview import HostedFixtureServer
    from ofc_engine.hosted_runs import HostedRunRegistry

    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute("update ofc_private.browser_budget set writes=0 where id=1")

    def start():
        registry = HostedRunRegistry(
            PostgresRevisionStore(pg_url, insecure_local_test=True)
        )
        registry.max_daily_runs = 1
        server = HostedFixtureServer(("127.0.0.1", 0), run_registry=registry)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        return registry, server, worker, "http://127.0.0.1:" + str(server.server_port)

    def post(base):
        return Request(
            base + "/api/runs",
            data=canonical_proposal().model_dump_json().encode(),
            headers={"Origin": base, "Content-Type": "application/json"},
        )

    registry, server, worker, base = start()
    try:
        with urlopen(post(base), timeout=5) as response:
            receipt = json.loads(response.read())
        assert receipt["review"]["release_authorized"] is False
        saved_digest = receipt["review"]["plan_digest"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

    reopened, server, worker, base = start()
    try:
        review_url = base + "/api/runs/" + receipt["run_id"] + "/review"
        authorized = Request(
            review_url, headers={"Authorization": "Bearer " + receipt["ticket"]}
        )
        with urlopen(base + "/readyz", timeout=5) as response:
            assert response.status == 200
        with urlopen(authorized, timeout=5) as response:
            review = json.loads(response.read())
        assert review["plan_digest"] == saved_digest
        assert review["journal_head"] == reopened.store.audit_history(
            receipt["run_id"]
        )[-1].event_hash
        assert review["release_authorized"] is False
        assert review["real_qloo_requests"] == 0
        with pytest.raises(HTTPError) as error:
            urlopen(post(base), timeout=5)
        assert error.value.code == 429

        def unavailable():
            raise psycopg.OperationalError("sensitive connection diagnostic")

        with monkeypatch.context() as patch:
            patch.setattr(reopened.store, "_connect", unavailable)
            for path in ("/readyz", "/api/runs/status", review_url[len(base):]):
                request = authorized if path == review_url[len(base):] else base + path
                with pytest.raises(HTTPError) as error:
                    urlopen(request, timeout=5)
                assert error.value.code == 503
                assert b"sensitive connection diagnostic" not in error.value.read()
            with pytest.raises(HTTPError) as error:
                urlopen(post(base), timeout=5)
            assert error.value.code == 503
            with urlopen(base + "/healthz", timeout=5) as response:
                assert response.status == 200

        with urlopen(authorized, timeout=5) as response:
            assert json.loads(response.read())["plan_digest"] == saved_digest
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_ticketed_browser_run_receives_only_its_own_qloo_rank(pg_url):
    from hashlib import sha256
    from threading import Thread
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from ofc_engine.hosted_preview import HostedFixtureServer
    from ofc_engine.hosted_runs import HostedRunRegistry
    from ofc_engine.qloo import brand_affinity_probe

    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute("delete from ofc_private.provider_attempt")
        db.execute("delete from ofc_private.provider_scope")
        db.execute("update ofc_private.browser_budget set writes=0 where id=1")

    proposal = plan_for_test()
    registry = HostedRunRegistry(
        PostgresRevisionStore(pg_url, insecure_local_test=True)
    )
    saved = registry.create(proposal.model_dump(mode="json"))
    run_id = saved["run_id"]
    restored = registry.operator_proposal(run_id)
    baseline, _ = freeze_proposal(restored)
    assert baseline.plan_id == run_id
    assert plan_digest(baseline) == saved["review"]["plan_digest"]
    assert restored.intent.taste_context["artist_id"] == "artist-ci"
    assert registry.review(run_id, saved["ticket"])["real_qloo_requests"] == 0

    spec = brand_affinity_probe("artist-ci", ("brand-a", "brand-b"))
    scope = sha256((
        spec.fingerprint + ":brand-a:" + plan_digest(baseline)
    ).encode()).hexdigest()
    transport = FixedTransport()
    first = HostedLiveReview(
        api_key="fake-ci-only",
        artist_id="artist-ci", brand_ids=("brand-a", "brand-b"),
        selected_brand_id="brand-a",
        ledger=PostgresAttemptLedger(pg_url, scope=scope, insecure_local_test=True),
        operator_token="operator-" + "x" * 40,
        client=QlooProbeClient(api_key="fake-ci-only", transport=transport),
        agent_evaluator=HostedAgentFlow(
            proposal=restored, store=registry.store
        ).evaluate,
        bound_run_id=run_id,
    )
    status, result = first.observe()
    assert status == 200 and transport.calls == 1
    assert result["agent_run"]["changed_ids"] == ["brand"]
    assert result["agent_run"]["pending_rechecks"] == ["brand", "afterparty"]
    assert result["agent_run"]["handoff"]["status"] == "WITHHELD"
    assert registry.store.get(run_id).revision == 1

    def inspect(live, *, ticket=saved["ticket"], requested_run=run_id):
        registry_copy = HostedRunRegistry(
            PostgresRevisionStore(pg_url, insecure_local_test=True)
        )
        with HostedFixtureServer(
            ("127.0.0.1", 0), hosted_live=live, run_registry=registry_copy
        ) as server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                base = "http://127.0.0.1:" + str(server.server_port)
                with pytest.raises(HTTPError) as error:
                    urlopen(base + "/api/qloo/result", timeout=5)
                assert error.value.code == 404
                with urlopen(base + "/", timeout=5) as response:
                    assert b"/hosted-live.js" not in response.read()
                endpoint = base + "/api/runs/" + requested_run + "/review"
                req = Request(endpoint, headers={
                    "Authorization": "Bearer " + ticket
                })
                try:
                    with urlopen(req, timeout=5) as response:
                        return response.status, json.loads(response.read())
                except HTTPError as error:
                    return error.code, json.loads(error.read())
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)

    code, review = inspect(first)
    assert code == 200
    assert review["source_mode"] == "LIVE_HTTP_UNVERIFIED"
    assert review["revision"] == 1
    assert review["agent_run"]["changed_ids"] == ["brand"]
    assert review["agent_run"]["off_on"]["same_baseline"] is True
    assert review["agent_run"]["off_on"]["off_verdicts"]["brand"] == "UNCERTAIN"
    assert review["agent_run"]["off_on"]["on_verdicts"]["brand"] == "REPAIR"
    assert review["agent_run"]["off_on"]["real_causal_proof"] is False
    assert review["agent_run"]["provenance"]["journal_head"] == review["journal_head"]
    assert review["agent_run"]["provenance"]["current_plan_digest"] == review["plan_digest"]
    assert review["agent_run"]["provenance"]["baseline_digest"] == review["baseline_digest"]
    assert review["agent_run"]["release_authorized"] is False
    assert review["release_authorized"] is False
    assert review["provider_authenticated"] is False
    assert review["independent_recheck_completed"] is False
    assert review["real_qloo_requests"] == 1
    assert [d["verdict"] for d in review["decisions"][1:]] == ["UNCERTAIN"] * 5
    for secret in ("fake-ci-only", "artist-ci", "brand-a", "brand-b"):
        assert secret not in json.dumps(review)
    assert inspect(first, ticket="x" * 50)[0] == 404

    other = registry.create(plan_for_test().model_dump(mode="json"))
    assert inspect(first, ticket=other["ticket"],
                   requested_run=other["run_id"])[1]["source_mode"] == "EVIDENCE_UNAVAILABLE"

    restarted = HostedLiveReview(
        api_key="fake-ci-only",
        artist_id="artist-ci", brand_ids=("brand-a", "brand-b"),
        selected_brand_id="brand-a",
        ledger=PostgresAttemptLedger(pg_url, scope=scope, insecure_local_test=True),
        operator_token="operator-" + "x" * 40,
        client=QlooProbeClient(api_key="fake-ci-only", transport=FixedTransport()),
        bound_run_id=run_id,
    )
    assert restarted.public_result()[1]["agent_run"] == result["agent_run"]
    assert inspect(restarted)[1]["agent_run"] == review["agent_run"]
    assert restarted.observe()[1]["requests_this_call"] == 0

    from ofc_engine.published_tour import PublishedTourReview

    published = PublishedTourReview(registry, run_id)
    preview = published.review()
    assert preview["status"] == "OBSERVED_QLOO_REVIEW_REQUIRED"
    assert preview["off_on"]["off_brand"] == "UNCERTAIN"
    assert preview["off_on"]["on_brand"] == "REPAIR"
    assert preview["changed_ids"] == ["brand"]
    assert preview["handoff"] == "WITHHELD"
    assert preview["additional_provider_requests"] == 0
    assert preview["provider_authenticated"] is False
    assert len(preview["decisions"]) == 6
    assert preview["journey_version"] == 1
    tour = preview["tour"]
    assert tour["budget_usd"] == 60000
    assert tour["cities"] == ["Chicago", "Austin", "Seattle"]
    assert tour["audience"] == "Indie music audiences"
    assert tour["operator_constructed_unverified_decisions"] == 5
    assert len(tour["decisions"]) == 6
    assert tour["decisions"][0]["decision_id"] == "brand"
    assert tour["decisions"][0]["candidate_count"] == 2
    assert tour["decisions"][0]["baseline_option"] == 1
    assert tour["decisions"][0]["reviewed_option"] == 2
    assert tour["decisions"][0]["changed"] is True
    assert tour["decisions"][0]["pending_recheck"] is True
    assert all(
        d["baseline_option"] == d["reviewed_option"] and not d["changed"]
        for d in tour["decisions"][1:]
    )
    assert tour["decisions"][-1]["depends_on"] == ["brand"]
    assert tour["decisions"][-1]["pending_recheck"] is True
    assert set(preview["pending_rechecks"]) == {"brand", "afterparty"}
    assert preview["off_on"]["real_causal_proof"] is False
    assert "candidate_id" not in json.dumps(preview)
    assert "candidate_pool" not in json.dumps(preview)
    assert "ticket" not in json.dumps(preview)
    for secret in ("fake-ci-only", "artist-ci", "brand-a", "brand-b", "operator-"):
        assert secret not in json.dumps(preview)

    with HostedFixtureServer(
        ("127.0.0.1", 0), run_registry=registry,
        public_showcase=published,
    ) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        try:
            with urlopen(base + "/", timeout=5) as response:
                assert b"/hosted-showcase.js" in response.read()
            with urlopen(base + "/hosted-showcase.js", timeout=5) as response:
                js = response.read()
                assert b"/api/showcase" in js
                assert b"start-qloo-journey" in js
                assert b"Next: " in js
                assert b"Qloo OFF" in js
                assert b"HANDOFF WITHHELD" in js
                assert b"innerHTML" not in js
                assert b"/api/qloo/live-rank" not in js
                assert b"SPEND_ONE_HOSTED_QLOO_REQUEST" not in js
            with urlopen(base + "/api/showcase", timeout=5) as response:
                assert json.loads(response.read()) == preview
            with pytest.raises(HTTPError) as denied:
                urlopen(base + "/api/showcase?unapproved=1", timeout=5)
            assert denied.value.code in (400, 404)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute(
            "update ofc_private.browser_runs "
            "set expires_at=now()-interval '2 hours' where plan_id=%s",
            (run_id,),
        )
    with pytest.raises(ValueError):
        registry.operator_proposal(run_id)
    assert published.review() == preview
    with pytest.raises(ValueError):
        PublishedTourReview(registry, other["run_id"])
    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute(
            "update ofc_private.provider_attempt set scope=%s where id=1",
            ("f" * 64,),
        )
    with pytest.raises(ValueError):
        published.review()


def test_private_browser_proposal_rejects_expiry_and_scope_tampering(pg_url):
    from ofc_engine.hosted_runs import HostedRunRegistry

    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute("update ofc_private.browser_budget set writes=0 where id=1")
    registry = HostedRunRegistry(
        PostgresRevisionStore(pg_url, insecure_local_test=True)
    )
    proposal = plan_for_test()
    saved = registry.create(proposal.model_dump(mode="json"))
    run_id = saved["run_id"]
    with pytest.raises(ValueError):
        registry.operator_proposal("not-a-browser-run")
    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute(
            "update ofc_private.browser_runs set proposal_snapshot=%s "
            "where plan_id=%s",
            (plan_for_test().model_dump_json(), run_id),
        )
    with pytest.raises(StorageIntegrityError):
        registry.operator_proposal(run_id)
    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute(
            "update ofc_private.browser_runs set proposal_snapshot=null "
            "where plan_id=%s", (run_id,),
        )
    with pytest.raises(ValueError, match="unavailable"):
        registry.operator_proposal(run_id)
    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute(
            "update ofc_private.browser_runs "
            "set expires_at=now()-interval '1 second' where plan_id=%s",
            (run_id,),
        )
    with pytest.raises(ValueError):
        registry.operator_proposal(run_id)


def test_public_six_decision_tour_freezes_scope_and_requires_operator_for_qloo(
    pg_url, monkeypatch
):
    from threading import Thread
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from ofc_engine.hosted_live import configured_live_review
    from ofc_engine.hosted_preview import HostedFixtureServer
    from ofc_engine.hosted_runs import HostedRunRegistry
    from ofc_engine.hosted_tour import configured_tour

    scope = {
        "OFC_TOUR_ARTIST_ID": "artist-ci",
        "OFC_TOUR_BRAND_IDS": "brand-a,brand-b",
        "OFC_TOUR_SELECTED_ID": "brand-a",
    }
    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute("update ofc_private.browser_budget set writes=0 where id=1")
        db.execute("delete from ofc_private.provider_attempt")
        db.execute("delete from ofc_private.provider_scope")

    registry = HostedRunRegistry(
        PostgresRevisionStore(pg_url, insecure_local_test=True)
    )
    with HostedFixtureServer(
        ("127.0.0.1", 0),
        run_registry=registry,
        tour_proposal=configured_tour(scope),
    ) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)

        def save(*, origin=None, confirmation="SAVE_SIX_DECISION_TOUR"):
            request = Request(
                base + "/api/runs/tour",
                method="POST",
                headers={
                    "Origin": origin or base,
                    "Content-Type": "application/json",
                },
                data=json.dumps({"confirm": confirmation}).encode(),
            )
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except HTTPError as error:
                return error.code, json.loads(error.read())

        try:
            with urlopen(base + "/", timeout=5) as response:
                page = response.read()
            assert b"/hosted-tour.js" in page
            assert b"/hosted-live.js" not in page
            with urlopen(base + "/hosted-tour.js", timeout=5) as response:
                script = response.read()
            assert b"/api/runs/tour" in script
            assert b'start.id = "start-hosted-tour"' in script
            assert b'#view-plan > .intro' in script
            assert b'actions.className = "hosted-tour-actions"' in script
            assert b'actions.style.alignItems = "center"' in script
            assert b'actions.style.marginBottom = "16px"' in script
            assert b'getElementById("load-example")' not in script
            assert b"innerHTML" not in script
            assert b"QLOO_API_KEY" not in script
            assert b'ofc-review-refresh' in script
            assert b'ofc-tour-run-id' in script
            assert b"artist-ci" not in script
            assert save(origin="https://attacker.example")[0] == 403
            assert save(confirmation="RUN_PROVIDER_NOW")[0] == 422
            status, saved = save()
            assert status == 201
            assert saved["review"]["release_authorized"] is False
            assert saved["review"]["real_qloo_requests"] == 0
            assert len(saved["review"]["decisions"]) == 6
            assert "artist-ci" not in json.dumps(saved)
            assert "brand-a" not in json.dumps(saved)
            review_url = base + "/api/runs/" + saved["run_id"] + "/review"
            req = Request(review_url, headers={
                "Authorization": "Bearer " + saved["ticket"]
            })
            with urlopen(req, timeout=5) as response:
                assert json.loads(response.read())["plan_digest"] == (
                    saved["review"]["plan_digest"]
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    frozen = registry.operator_proposal(saved["run_id"])
    brand = next(d for d in frozen.decisions if d.decision_id == "brand")
    assert frozen.intent.taste_context["artist_id"] == "artist-ci"
    assert brand.candidate_pool == ("brand-a", "brand-b")
    assert brand.candidate_id == "brand-a"
    assert len(registry.store.audit_history(saved["run_id"])) == 1

    monkeypatch.setattr(
        "ofc_engine.hosted_live.PostgresRevisionStore",
        lambda dsn: PostgresRevisionStore(dsn, insecure_local_test=True),
    )
    monkeypatch.setattr(
        "ofc_engine.hosted_live.PostgresAttemptLedger",
        lambda dsn, *, scope: PostgresAttemptLedger(
            dsn, scope=scope, insecure_local_test=True
        ),
    )
    environment = {
        "OFC_HOSTED_QLOO_MODE": "ONE_REQUEST_EXPLICITLY_APPROVED",
        "QLOO_API_KEY": "fake-ci-only",
        "OFC_HOSTED_OPERATOR_TOKEN": "operator-" + "x" * 40,
        "OFC_DATABASE_URL": pg_url,
        "OFC_HOSTED_BROWSER_RUN_ID": saved["run_id"],
    }
    from ofc_engine.qloo import brand_affinity_probe

    approved = configured_live_review(environment)
    assert approved is not None
    assert approved.bound_run_id == saved["run_id"]
    assert approved.spec.fingerprint == brand_affinity_probe(
        "artist-ci", ("brand-a", "brand-b")
    ).fingerprint
    assert approved.state()["status"] == "READY"
    with psycopg.connect(pg_url, sslmode="disable") as db:
        assert db.execute(
            "select count(*) from ofc_private.provider_attempt"
        ).fetchone()[0] == 0

    with pytest.raises(ValueError, match="conflicts"):
        configured_live_review({
            **environment, "OFC_HOSTED_QLOO_ARTIST_ID": "other-artist"
        })


def test_dynamic_postgres_qloo_budget_and_authenticated_hosted_api(pg_url):
    from threading import Thread
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from ofc_engine.dynamic_postgres import PostgresDynamicQlooReview
    from ofc_engine.hosted_preview import HostedFixtureServer
    from ofc_engine.qloo import TransportReply

    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute(
            (Path(__file__).resolve().parents[1] / "sql/003_dynamic_qloo.sql").read_text()
        )
        db.execute("update ofc_private.dynamic_qloo_budget set reserved=0,utc_day=current_date")

    class Transport:
        def __init__(self):
            self.calls = 0

        def get(self, url, headers, *, timeout, max_bytes):
            self.calls += 1
            assert "filter.type=urn%3Aentity%3Abrand" in url
            payload = {"success": True, "results": {"entities": [
                {"entity_id": "brand-b", "type": "urn:entity",
                 "query": {"affinity": 0.9, "explainability": {"signal.interests.entities": {}}}},
                {"entity_id": "brand-a", "type": "urn:entity",
                 "query": {"affinity": 0.6, "explainability": {"signal.interests.entities": {}}}},
            ]}}
            return TransportReply(200, json.dumps(payload).encode())

    client_transport = Transport()
    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    flow = PostgresDynamicQlooReview(
        store, QlooProbeClient(api_key="fake-local-only", transport=client_transport),
        daily_limit=3,
    )
    proposal = plan_for_test()
    response = flow.evaluate(proposal)
    assert response["revision"] == 1
    assert response["new_qloo_requests"] == 2
    assert response["changed_ids"] == ["brand"]
    assert response["decisions"][0]["rank_recheck_completed"] is True
    assert response["provider_authenticated"] is False
    assert response["full_plan_independently_verified"] is False
    assert flow.evaluate(proposal)["new_qloo_requests"] == 0
    assert client_transport.calls == 2

    token = "hosted-test-" + "x" * 40
    server = HostedFixtureServer(
        ("127.0.0.1", 0), build_sha="abc123d",
        dynamic_review=flow, dynamic_token=token,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = "http://127.0.0.1:" + str(server.server_port) + "/api/agent/evaluate"

    def request(value, *, supplied=token, origin=None):
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + supplied,
        }
        if origin:
            headers["Origin"] = origin
        req = Request(endpoint, method="POST", headers=headers,
                      data=value.model_dump_json().encode())
        try:
            with urlopen(req, timeout=10) as answer:
                return answer.status, json.load(answer)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    try:
        assert request(proposal, supplied="wrong")[0] == 403
        assert request(proposal, origin="https://example.com")[0] == 403
        status, replay = request(proposal)
        assert status == 200
        assert replay["new_qloo_requests"] == 0
        assert client_transport.calls == 2
        # A second customer is not bound to the first customer's fixed scope.
        other = plan_for_test()
        another = other.intent.model_copy(update={
            "run_id": "dynamic-" + uuid4().hex,
            "client_request_id": "dynamic-" + uuid4().hex,
        })
        other = other.model_copy(update={"intent": another})
        code, next_run = request(other)
        assert code == 200
        assert next_run["new_qloo_requests"] == 1
        assert next_run["decisions"][0]["reason_code"] == "DAILY_QLOO_BUDGET_EXHAUSTED"
        assert next_run["release_authorized"] is False
        assert client_transport.calls == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_hosted_copilot_owner_vault_sessions_and_live_shape_with_fake_providers(pg_url):
    """OFC B2 integration: external clients are injected, SQL and HTTP are real."""
    from cryptography.fernet import Fernet
    from urllib.parse import urlsplit, parse_qs
    from ofc_engine.copilot_credentials import CredentialVault, OwnerUnauthorized
    from ofc_engine.copilot_hosted import HostedCopilot, SessionDenied, SessionLimitReached
    from ofc_engine.copilot_llm import JSONReply

    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute(
            (Path(__file__).resolve().parents[1] / "sql/003_dynamic_qloo.sql").read_text()
        )
        db.execute(
            (Path(__file__).resolve().parents[1] / "sql/004_copilot_hosted.sql").read_text()
        )
        for table in (
            "copilot_credentials", "copilot_sessions", "copilot_messages",
            "copilot_llm_budget", "copilot_search",
        ):
            assert db.execute(
                "select to_regclass(%s)", ("ofc_private." + table,)
            ).fetchone()[0] is not None
        db.execute("update ofc_private.copilot_llm_budget set reserved=0,utc_day=current_date")
        db.execute("update ofc_private.dynamic_qloo_budget set reserved=0,utc_day=current_date")
        db.execute("update ofc_private.copilot_browser_budget set sessions=0,utc_day=current_date")
        db.execute("update ofc_private.copilot_connection_attempts set reserved=0,utc_day=current_date")

    artist = "0a0a0a0a-0000-4000-8000-000000000001"
    brand_a = "0a0a0a0a-0000-4000-8000-000000000002"
    brand_b = "0a0a0a0a-0000-4000-8000-000000000003"
    known = {
        "Artist Example": artist, "Brand A": brand_a, "Brand B": brand_b,
        "Taylor Swift": "c70ce2b8-0ad1-4150-bf18-5a6347f2e860",
        "Nike": "b8540be1-11a5-4920-ac15-bcdf7b06ce3d",
    }

    class FakeQloo:
        def __init__(self):
            self.searches = []
            self.ranks = 0

        def get(self, url, headers, *, timeout, max_bytes):
            assert headers["X-Api-Key"] == "private-qloo-key-not-a-real-provider-secret"
            route = urlsplit(url)
            if route.path == "/search":
                query = parse_qs(route.query)
                name = query["query"][0]
                self.searches.append(name)
                body = {"success": True, "results": {"entities": [{
                    "name": name,
                    "entity_id": known[name],
                    "type": "urn:entity",
                    "subtype": query["types"][0],
                }]}}
            else:
                self.ranks += 1
                body = {"success": True, "results": {"entities": [
                    {"entity_id": entity, "type": "urn:entity",
                     "query": {"affinity": score, "explainability": {
                         "signal.interests.entities": {},
                     }}}
                    for entity, score in ((brand_b, .9), (brand_a, .6))
                ]}}
            return TransportReply(200, json.dumps(body).encode())

    class FakeModel:
        def __init__(self):
            self.calls = 0

        def post(self, url, headers, data, *, timeout, max_bytes):
            self.calls += 1
            assert headers["x-goog-api-key"] == "private-gemini-key-not-real"
            payload = json.loads(data)
            schema = payload["generationConfig"]["responseJsonSchema"]
            if "ready" in schema["properties"]:
                assert schema["required"] == ["ready"]
                return JSONReply(200, json.dumps({"candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": '{"ready":true}'}]},
                }]}).encode())
            if "answer" in schema["properties"]:
                context = json.loads(payload["contents"][0]["parts"][0]["text"])
                mode_on = context["view"]["ofc_enabled"]
                assert set(context) == {
                    "prompt", "model_id", "baseline_digest",
                    "qloo_evidence_digest", "view",
                }
                if not mode_on:
                    assert "on" not in context and "ofc_reason" not in json.dumps(context)
                text = {"answer": (
                    "With the same Qloo evidence, OFC repairs Brand A to Brand B."
                    if mode_on else
                    "Qloo ranks Brand B; my original proposal was Brand A."
                )}
            else:
                query = payload["contents"][0]["parts"][0]["text"]
                prior = json.loads(query) if query.startswith("{") else {}
                budget = 80000 if (
                    isinstance(prior, dict)
                    and "previous_plan" in prior
                    and "80000" in prior.get("latest_user_message", "")
                ) else None
                text = {
                    "objective": "Check brand fit", "artist_name": "Artist Example",
                    "budget_usd": budget, "cities": [],
                    "choices": [{
                        "decision_id": "brand", "domain": "brand",
                        "selected_name": "Brand A",
                        "candidate_names": ["Brand A", "Brand B"],
                        "mutable": True, "depends_on": [],
                    }],
                    "baseline_answer": "Brand A is proposed.",
                    "clarification_question": None,
                }
            return JSONReply(200, json.dumps({"candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": json.dumps(text)}]},
            }]}).encode())

    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    vault = CredentialVault(
        store, master_key=Fernet.generate_key().decode(),
        owner_secret="integration-owner-secret-" + "x" * 42,
    )
    with pytest.raises(OwnerUnauthorized):
        vault.login("wrong-owner")
    owner = vault.login("integration-owner-secret-" + "x" * 42)
    vault.configure(owner, "qloo", "private-qloo-key-not-a-real-provider-secret")
    vault.configure(owner, "gemini", "private-gemini-key-not-real")
    status = vault.status(private=True)
    assert status["qloo"]["configured"] is True
    assert status["qloo"]["masked_suffix"] == "cret"
    assert status["qloo"]["status"] == "NOT_TESTED"
    assert vault.status()["qloo"].get("masked_suffix") is None
    with psycopg.connect(pg_url, sslmode="disable") as db:
        ciphertext = bytes(db.execute(
            "select ciphertext from ofc_private.copilot_credentials "
            "where provider='qloo'"
        ).fetchone()[0])
        assert b"private-qloo-key-not-a-real-provider-secret" not in ciphertext

    qloo, model = FakeQloo(), FakeModel()
    runtime = HostedCopilot(
        store, vault, qloo_daily_limit=12, llm_daily_limit=8,
        browser_daily_limit=6, qloo_transport=qloo, model_transport=model,
    )
    assert runtime.capabilities()["qloo"] == "NOT_TESTED"
    assert model.calls == qloo.ranks == len(qloo.searches) == 0

    session = runtime.session()
    report = runtime.submit(
        session_id=session["session_id"], ticket=session["ticket"],
        request_id="visitor-idempotent-001", prompt="Find a great sponsor",
    )
    assert report["status"] == "REVIEW_REQUIRED"
    assert report["schema_version"] == "copilot.v1"
    from scripts.qualify_copilot_hosted import (
        QualificationFailure, validate_saved_result,
    )
    validate_saved_result(report)
    with pytest.raises(QualificationFailure, match="OFC_ONLY"):
        validate_saved_result({
            **report,
            "on": {**report["on"], "qloo": {"source_mode": "EVIDENCE_UNAVAILABLE"}},
        })
    receipt = report["value_receipt"]
    assert receipt["schema_version"] == "ofc-value.v1"
    assert receipt["changed_count"] == 1
    assert receipt["changes"][0]["before"] == "Brand A"
    assert receipt["changes"][0]["after"] == "Brand B"
    assert receipt["shared_qloo_evidence_digest"] == report["qloo_evidence_digest"]
    assert report["grounded_explanations"]["source"] == "SERVER_VALIDATED_EVIDENCE_ONLY"
    assert report["narrative_trust"] == "MODEL_NARRATIVE_NOT_INDEPENDENTLY_VERIFIED"
    assert report["journal_head"]
    assert report["revision"] == 1
    assert report["rank_evidence_rechecked"] == 1
    assert report["full_plan_independently_verified"] is False
    assert report["plan"]["objective"] == "Check brand fit"
    assert report["plan"]["artist_name"] == "Artist Example"
    assert report["plan"]["budget_usd"] is None
    assert report["plan"]["choices"][0]["selected_name"] == "Brand A"
    assert report["plan"]["choices"][0]["source"] == "UNVERIFIED_LLM_PROPOSAL"
    assert report["off"]["qloo"] == report["on"]["qloo"]
    assert report["off"]["ofc_enabled"] is False
    assert report["on"]["ofc_enabled"] is True
    assert report["off"]["decisions"][0]["candidate_id"] == "Brand A"
    assert report["on"]["decisions"][0]["candidate_id"] == "Brand B"
    assert report["on"]["changed_ids"] == ["brand"]
    assert report["release_authorized"] is False
    assert report["qloo_requests_this_run"] == 5
    assert report["qloo_search_requests"] == 3
    assert report["qloo_rank_requests"] == 2
    assert report["llm_requests_this_run"] == 3
    assert len(qloo.searches) == 3 and qloo.ranks == 2 and model.calls == 3
    assert artist not in json.dumps(report)
    assert brand_a not in json.dumps(report)
    assert brand_b not in json.dumps(report)
    assert "private-gemini-key-not-real" not in json.dumps(report)
    assert "private-qloo-key-not-a-real-provider-secret" not in json.dumps(report)

    copy = HostedCopilot(
        store, vault, qloo_daily_limit=12, llm_daily_limit=8,
        browser_daily_limit=6, qloo_transport=qloo, model_transport=model,
    )
    assert copy.submit(
        session_id=session["session_id"], ticket=session["ticket"],
        request_id="visitor-idempotent-001", prompt="Find a great sponsor",
    ) == report
    assert qloo.ranks == 2 and model.calls == 3 and len(qloo.searches) == 3
    stored = copy.messages(session["session_id"], session["ticket"])["messages"]
    assert len(stored) == 1 and stored[0]["prompt"] == "Find a great sponsor"
    activity = stored[0]["activity"]
    assert activity["source"] == "PERSISTED_ATTEMPTS"
    assert activity["agent_plan"] == "OBSERVED"
    assert activity["qloo_evidence"] == "RANK_OBSERVED"
    assert activity["ofc_review"] == "OBSERVED"
    assert activity["requests"]["search_complete"] == 3
    assert activity["requests"]["rank_complete"] == 2
    assert activity["requests"]["rank_reserved"] == 0
    assert {key: value for key, value in stored[0].items()
            if key not in ("prompt", "activity")} == report
    with psycopg.connect(pg_url, sslmode="disable") as db:
        encrypted = bytes(db.execute(
            "select prompt_ciphertext from ofc_private.copilot_messages "
            "where run_id=%s", (report["run_id"],)
        ).fetchone()[0])
        assert b"Find a great sponsor" not in encrypted
        content = db.execute(
            "select result,result_ciphertext from "
            "ofc_private.copilot_messages where run_id=%s", (report["run_id"],)
        ).fetchone()
        assert content[0] is None
        assert b"Brand A" not in bytes(content[1])
        assert b"Brand B" not in bytes(content[1])
    with pytest.raises(SessionDenied):
        copy.messages(session["session_id"], "unauthorized-visitor-token")

    saved_usage = (len(qloo.searches), qloo.ranks, model.calls)
    assert copy.capabilities()["qloo"] == "NOT_TESTED"
    assert (len(qloo.searches), qloo.ranks, model.calls) == saved_usage
    assert copy.test_connection(owner, "qloo")["status"] == "ACTIVE"
    probe = copy.test_connection(owner, "gemini")
    assert probe == {"provider": "gemini", "status": "CONNECTED",
                     "qualification": "CONNECTIVITY_ONLY"}
    assert copy.vault.status(private=True)["gemini"]["qualification"] == "CONNECTIVITY_ONLY"
    assert (len(qloo.searches), qloo.ranks, model.calls) == (4, 2, 4)
    assert copy.capabilities()["product"] == "CHECK_REQUIRED"
    assert copy.capabilities()["llm"] == "CONNECTED"

    followup = copy.submit(
        session_id=session["session_id"], ticket=session["ticket"],
        request_id="visitor-followup-budget-001",
        prompt="Change the campaign budget to $80000; keep the brand options",
        parent_run_id=report["run_id"],
    )
    assert followup["status"] == "REVIEW_REQUIRED"
    assert followup["parent_run_id"] == report["run_id"]
    assert followup["plan"]["budget_usd"] == 80000
    assert followup["plan"]["choices"] == report["plan"]["choices"]
    assert followup["off"]["qloo"] == followup["on"]["qloo"]
    assert followup["llm_requests_this_run"] == 3
    assert (len(qloo.searches), qloo.ranks, model.calls) == (7, 4, 7)
    assert copy.submit(
        session_id=session["session_id"], ticket=session["ticket"],
        request_id="visitor-followup-budget-001",
        prompt="Change the campaign budget to $80000; keep the brand options",
        parent_run_id=report["run_id"],
    ) == followup
    with pytest.raises(SessionDenied):
        copy.submit(
            session_id=session["session_id"], ticket=session["ticket"],
            request_id="visitor-followup-budget-001",
            prompt="Change the campaign budget to $80000; keep the brand options",
            parent_run_id=None,
        )
    outsider = copy.session()
    with pytest.raises(SessionDenied):
        copy.submit(
            session_id=outsider["session_id"], ticket=outsider["ticket"],
            request_id="outsider-replay-001", prompt="Change campaign budget",
            parent_run_id=report["run_id"],
        )
    assert (len(qloo.searches), qloo.ranks, model.calls) == (7, 4, 7)

    from datetime import timedelta
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from ofc_engine.copilot_business import BusinessProofIntake
    from ofc_engine.revisions import canonical_bytes
    intake = BusinessProofIntake(copy)
    signing_key = Ed25519PrivateKey.generate()
    issuer = "independent-budget-attestor"
    enrolled = intake.register_authority(
        owner, issuer=issuer, requirements=["budget_feasibility"],
        public_key_b64=base64.b64encode(
            signing_key.public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw,
            )
        ).decode(),
    )
    assert enrolled["status"] == "OWNER_REGISTERED_AUTHORITY"
    plan_state = store.get(report["run_id"])
    now = datetime.now(timezone.utc)
    fields = {
        "requirement": "budget_feasibility",
        "decision_id": "brand",
        "plan_id": plan_state.plan_id,
        "plan_revision": plan_state.revision,
        "plan_digest": plan_digest(plan_state),
        "issuer": issuer,
        "issued_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "claims": {
            "currency": "USD",
            "approved_budget_usd": 80000,
            "fully_loaded_cost_usd": 60000,
        },
    }
    proof = {**fields, "signature_b64": base64.b64encode(
        signing_key.sign(canonical_bytes(fields))
    ).decode()}
    with pytest.raises(ValueError, match="SIGNED_BUSINESS_EVIDENCE_INVALID"):
        intake.attach(owner, run_id=report["run_id"], proof={
            **proof, "claims": {**fields["claims"], "fully_loaded_cost_usd": 90000},
        })
    with pytest.raises(OwnerUnauthorized):
        intake.attach("bad-owner", run_id=report["run_id"], proof=proof)
    assessment = intake.attach(owner, run_id=report["run_id"], proof=proof)
    assert assessment["status"] == "REVIEW_REQUIRED"
    assert "budget_feasibility" not in assessment["missing_requirements"]["brand"]
    assert "contract_rights" in assessment["missing_requirements"]["brand"]
    assert assessment["external_action_authorized"] is False
    with store._connect() as db:
        encrypted_proof = bytes(db.execute(
            "select proof_ciphertext from ofc_private.copilot_business_proofs "
            "where run_id=%s", (report["run_id"],)
        ).fetchone()[0])
        assert b"approved_budget_usd" not in encrypted_proof
    assert intake.revoke_authority(owner, issuer)["status"] == "REVOKED"
    withdrawn = intake.report(owner, report["run_id"])
    assert "budget_feasibility" in withdrawn["missing_requirements"]["brand"]
    assert withdrawn["external_action_authorized"] is False

    vault.revoke(owner, "qloo")
    vault.revoke(owner, "gemini")
    assert copy.submit(
        session_id=session["session_id"], ticket=session["ticket"],
        request_id="visitor-idempotent-001", prompt="Find a great sponsor",
    ) == report
    assert qloo.ranks == 4 and model.calls == 7 and len(qloo.searches) == 7
    deleted = copy.delete_session(session["session_id"], session["ticket"])
    assert deleted["removed_conversation_runs"] == 2
    with store._connect() as db:
        for table, key in (
            ("copilot_messages", "session_id"),
            ("copilot_sessions", "session_id"),
        ):
            assert db.execute(
                "select count(*) from ofc_private." + table +
                " where " + key + "=%s", (session["session_id"],)
            ).fetchone()[0] == 0
        for table, key in (
            ("plans", "plan_id"),
            ("events", "plan_id"),
            ("dynamic_qloo_attempts", "plan_id"),
            ("copilot_business_proofs", "run_id"),
            ("copilot_search", "run_id"),
        ):
            assert db.execute(
                "select count(*) from ofc_private." + table +
                " where " + key + "=%s", (report["run_id"],)
            ).fetchone()[0] == 0
    with pytest.raises(SessionDenied):
        copy.messages(session["session_id"], session["ticket"])
    with store._connect() as db:
        db.execute(
            "update ofc_private.copilot_sessions "
            "set expires_at=now()-interval '1 minute' where session_id=%s",
            (outsider["session_id"],),
        )
    assert copy.cleanup_expired(limit=8) >= 1
    with store._connect() as db:
        assert db.execute(
            "select count(*) from ofc_private.copilot_sessions "
            "where session_id=%s", (outsider["session_id"],),
        ).fetchone()[0] == 0
    vault.logout(owner)
    with pytest.raises(OwnerUnauthorized):
        vault.authorize(owner)
    assert copy.capabilities()["llm"] == "NOT_CONFIGURED"


def test_hosted_copilot_credential_routes_and_csrf(pg_url):
    from cryptography.fernet import Fernet
    from threading import Thread
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
    from ofc_engine.copilot_credentials import CredentialVault
    from ofc_engine.copilot_hosted import HostedCopilot
    from ofc_engine.hosted_preview import HostedFixtureServer

    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    vault = CredentialVault(
        store, master_key=Fernet.generate_key().decode(),
        owner_secret="http-owner-secret-" + "z" * 42,
    )
    server = HostedFixtureServer(
        ("127.0.0.1", 0), build_sha="abc123d",
        copilot=HostedCopilot(
            store, vault, qloo_daily_limit=12, llm_daily_limit=8,
            browser_daily_limit=8,
        ),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(server.server_port)

    def call(path, payload=None, *, cookie=None, origin=True):
        method = "POST" if payload is not None else "GET"
        headers = {}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            if origin:
                headers["Origin"] = base
        if cookie:
            headers["Cookie"] = cookie
        request = Request(
            base + path, method=method, headers=headers,
            data=json.dumps(payload).encode() if payload is not None else None,
        )
        try:
            with urlopen(request, timeout=12) as response:
                return response.status, json.load(response), response.headers.get("Set-Cookie")
        except HTTPError as error:
            return error.code, json.load(error), error.headers.get("Set-Cookie")

    try:
        assert call("/api/capabilities")[1]["qloo"] == "NOT_CONFIGURED"
        with urlopen(base + "/connect", timeout=12) as portal:
            html = portal.read().decode()
            assert portal.status == 200
            assert "API Connectivity" in html
            assert 'id="owner-secret"' in html
            assert "private-gemini-key-not-real" not in html
        with urlopen(base + "/connect.js", timeout=12) as bundle:
            script = bundle.read().decode()
            assert bundle.status == 200
            assert "SPEND_ONE_PROVIDER_REQUEST" in script
            assert "localStorage" not in script
        assert call("/api/owner/credentials")[0] == 403
        assert call("/api/owner/business", {
            "action": "assess", "run_id": "unauthorized",
        })[0] == 403
        assert call(
            "/api/copilot/sessions", {"confirm": "CREATE_COPILOT_SESSION"}
        )[0] == 503
        assert call(
            "/api/owner/login?secret=never-allowed",
            {"owner_secret": "http-owner-secret-" + "z" * 42},
        )[0] == 400
        assert call("/api/owner/login", {"owner_secret": "http-owner-secret-" + "z" * 42}, origin=False)[0] == 403
        status, value, cookie = call(
            "/api/owner/login", {"owner_secret": "http-owner-secret-" + "z" * 42}
        )
        assert status == 200 and value["authenticated"] is True
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        owner_cookie = cookie.split(";", 1)[0]
        assert call("/api/owner/credentials", {
            "action": "set", "provider": "gemini",
            "api_key": "owner-test-credential-12345",
        })[0] == 403
        status, configured, _ = call(
            "/api/owner/credentials", {
                "action": "set", "provider": "gemini",
                "api_key": "owner-test-credential-12345",
            }, cookie=owner_cookie,
        )
        assert status == 200
        assert configured["providers"]["gemini"]["masked_suffix"] == "2345"
        assert "owner-test-credential-12345" not in json.dumps(configured)
        status, _, _ = call(
            "/api/owner/credentials", {
                "action": "set", "provider": "qloo",
                "api_key": "owner-test-qloo-credential-12345",
            }, cookie=owner_cookie,
        )
        assert status == 200
        quota = call("/api/owner/credentials", cookie=owner_cookie)[1]["connection_tests"]
        assert quota["limit"] == 6 and quota["remaining"] >= 0
        with store._connect() as db:
            db.execute(
                "update ofc_private.copilot_connection_attempts "
                "set utc_day=(now() at time zone 'UTC')::date,reserved=3 where id=1"
            )
        remaining = call("/api/owner/credentials", cookie=owner_cookie)[1]["connection_tests"]
        assert remaining["used"] == 3 and remaining["remaining"] == 3
        with store._connect() as db:
            before_qloo = db.execute(
                "select utc_day,reserved from ofc_private.dynamic_qloo_budget where id=1"
            ).fetchone()
            before_model = db.execute(
                "select utc_day,reserved from ofc_private.copilot_llm_budget where id=1"
            ).fetchone()
            db.execute(
                "update ofc_private.copilot_connection_attempts "
                "set utc_day=(now() at time zone 'UTC')::date,reserved=6 where id=1"
            )
        budget = call("/api/owner/credentials", cookie=owner_cookie)[1]["connection_tests"]
        assert budget["used"] == 6 and budget["remaining"] == 0
        status, error, _ = call(
            "/api/owner/test-connection",
            {"provider": "qloo", "confirm": "SPEND_ONE_PROVIDER_REQUEST"},
            cookie=owner_cookie,
        )
        assert status == 429 and error == {"error": "CONNECTION_TEST_BUDGET_EXHAUSTED"}
        assert call("/api/owner/credentials", cookie=owner_cookie)[1]["connection_tests"]["used"] == 6
        with store._connect() as db:
            assert db.execute(
                "select utc_day,reserved from ofc_private.dynamic_qloo_budget where id=1"
            ).fetchone() == before_qloo
            assert db.execute(
                "select utc_day,reserved from ofc_private.copilot_llm_budget where id=1"
            ).fetchone() == before_model
            db.execute(
                "update ofc_private.copilot_connection_attempts "
                "set reserved=0 where id=1"
            )
        assert call("/api/capabilities")[1]["product"] == "CHECK_REQUIRED"
        status, visitors, visitor_cookie = call(
            "/api/copilot/sessions", {"confirm": "CREATE_COPILOT_SESSION"}
        )
        assert status == 201 and visitors["session_id"].startswith("cp-")
        visitor_cookie = visitor_cookie.split(";", 1)[0]
        assert call(
            "/api/copilot/sessions/" + visitors["session_id"],
            cookie=owner_cookie,
        )[0] == 403
        assert call(
            "/api/copilot/sessions/" + visitors["session_id"] + "/messages",
            {"request_id": "example-prompt-001", "prompt": "Choose brands"},
            cookie=owner_cookie,
        )[0] == 403
        assert call(
            "/api/copilot/sessions/" + visitors["session_id"],
            cookie=visitor_cookie,
        )[0] == 200
        assert call("/api/owner/business", {
            "action": "register", "issuer": "test-authority-001",
            "requirements": ["contract_rights"], "public_key_b64": "invalid!",
        }, cookie=owner_cookie)[0] == 422
        assert call(
            "/api/copilot/sessions/" + visitors["session_id"],
            {"confirm": "DELETE_MY_SESSION"},
            cookie=owner_cookie,
        )[0] == 403
        status, removed, clear_cookie = call(
            "/api/copilot/sessions/" + visitors["session_id"],
            {"confirm": "DELETE_MY_SESSION"},
            cookie=visitor_cookie,
        )
        assert status == 200 and removed["status"] == "SESSION_DELETED"
        assert "Max-Age=0" in clear_cookie
        assert call(
            "/api/copilot/sessions/" + visitors["session_id"],
            cookie=visitor_cookie,
        )[0] == 403
        assert call("/api/owner/logout", {}, cookie=owner_cookie)[0] == 200
        assert call("/api/owner/credentials", cookie=owner_cookie)[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_interrupted_copilot_job_blocks_after_timeout_without_provider_retry(pg_url):
    """A restart cannot leave a RESERVED run in PROCESSING forever."""
    from cryptography.fernet import Fernet
    from hashlib import sha256
    from ofc_engine.copilot_credentials import CredentialVault
    from ofc_engine.copilot_hosted import HostedCopilot

    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    vault = CredentialVault(
        store, master_key=Fernet.generate_key().decode(),
        owner_secret="stale-owner-" + "v" * 45,
    )
    owner = vault.login("stale-owner-" + "v" * 45)
    vault.configure(owner, "qloo", "stale-qloo-example-key-123456")
    vault.configure(owner, "gemini", "stale-model-example-key-123456")
    service = HostedCopilot(
        store, vault, qloo_daily_limit=12, llm_daily_limit=8,
        browser_daily_limit=40,
    )
    session = service.session()
    prompt = "Evaluate this fictional artist and sponsors"
    run_id = "cp-run-" + uuid4().hex
    request_id = "interrupted-job-001"
    with store._connect() as db:
        db.execute(
            "insert into ofc_private.copilot_messages("
            "run_id,session_id,request_id,prompt_digest,prompt_ciphertext,model_id,"
            "status,created_at) values(%s,%s,%s,%s,%s,%s,'RESERVED',"
            "now()-interval '10 minutes')",
            (run_id, session["session_id"], request_id,
             sha256(prompt.encode()).hexdigest(), vault.cipher.encrypt(prompt.encode()),
             "gemini-3.1-flash-lite"),
        )
    before = service.messages(session["session_id"], session["ticket"])["messages"]
    assert before[0]["prompt"] == prompt
    assert before[0]["status"] == "BLOCKED"
    assert before[0]["reason_code"] == "WORKFLOW_INTERRUPTED"
    saved = service.submit(
        session_id=session["session_id"], ticket=session["ticket"],
        request_id=request_id, prompt=prompt,
    )
    assert saved["status"] == "BLOCKED"
    assert saved["reason_code"] == "WORKFLOW_INTERRUPTED"
    assert saved["release_authorized"] is False
    assert saved["external_action_executed"] is False
    vault.revoke(owner, "qloo")
    vault.revoke(owner, "gemini")


def test_owner_login_throttled_in_private_postgres(pg_url):
    from cryptography.fernet import Fernet
    from ofc_engine.copilot_credentials import (
        CredentialVault, OwnerUnauthorized, OwnerRateLimited,
    )
    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    with store._connect() as db:
        db.execute(
            "update ofc_private.copilot_owner_login_budget "
            "set attempts=0,window_start=now()-interval '20 minutes' where id=1"
        )
    secret = "rate-gate-" + "q" * 48
    vault = CredentialVault(
        store, master_key=Fernet.generate_key().decode(), owner_secret=secret,
    )
    for _ in range(20):
        with pytest.raises(OwnerUnauthorized):
            vault.login("wrong-owner")
    with pytest.raises(OwnerRateLimited, match="OWNER_LOGIN_RATE_LIMITED"):
        vault.login(secret)
    with store._connect() as db:
        db.execute(
            "update ofc_private.copilot_owner_login_budget "
            "set attempts=0,window_start=now()-interval '20 minutes' where id=1"
        )
    assert vault.login(secret)


def test_invalid_qloo_success_payload_never_turns_status_green(pg_url):
    from cryptography.fernet import Fernet
    from ofc_engine.copilot_credentials import CredentialVault
    from ofc_engine.copilot_hosted import HostedCopilot
    from ofc_engine.qloo import TransportReply

    class BadQloo:
        def __init__(self):
            self.requests = 0

        def get(self, url, headers, *, timeout, max_bytes):
            self.requests += 1
            # An HTTP 200 can still carry a failed Qloo response.
            return TransportReply(
                200, json.dumps({
                    "success": False,
                    "results": {"entities": []},
                }).encode(),
            )

    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    with store._connect() as db:
        db.execute(
            "update ofc_private.copilot_connection_attempts "
            "set utc_day=current_date,reserved=0 where id=1"
        )
    vault = CredentialVault(
        store, master_key=Fernet.generate_key().decode(),
        owner_secret="bad-qloo-owner-" + "m" * 46,
    )
    owner = vault.login("bad-qloo-owner-" + "m" * 46)
    vault.configure(owner, "qloo", "bad-qloo-test-api-key-00001")
    bad = BadQloo()
    runtime = HostedCopilot(
        store, vault, qloo_daily_limit=64, llm_daily_limit=64,
        browser_daily_limit=40, qloo_transport=bad,
    )
    observed = runtime.test_connection(owner, "qloo")
    assert observed == {
        "provider": "qloo", "status": "UNAVAILABLE",
        "reason_code": "QLOO_SEARCH_RESPONSE_INVALID",
    }
    assert bad.requests == 1
    assert runtime.capabilities()["qloo"] == "UNAVAILABLE"
    assert vault.status(private=True)["qloo"]["reason_code"] == "QLOO_SEARCH_RESPONSE_INVALID"
    assert "reason_code" not in vault.status()["qloo"]

    class ForbiddenQloo:
        def __init__(self):
            self.calls = 0
        def get(self, url, headers, *, timeout, max_bytes):
            self.calls += 1
            return TransportReply(403, b'{"secret":"never-return-this"}')

    forbidden = ForbiddenQloo()
    second = HostedCopilot(
        store, vault, qloo_daily_limit=64, llm_daily_limit=64,
        browser_daily_limit=40, qloo_transport=forbidden,
    )
    denied = second.test_connection(owner, "qloo")
    assert denied == {
        "provider": "qloo", "status": "UNAVAILABLE", "reason_code": "HTTP_403",
    }
    assert forbidden.calls == 1 and "never-return-this" not in str(denied)
    assert vault.status(private=True)["qloo"]["reason_code"] == "HTTP_403"

    class RealSearchEnvelope:
        def __init__(self):
            self.calls = 0

        def get(self, url, headers, *, timeout, max_bytes):
            self.calls += 1
            return TransportReply(200, json.dumps({
                "results": [
                    {"entity_id": "c70ce2b8-0ad1-4150-bf18-5a6347f2e860",
                     "name": "Nike", "types": ["urn:entity:brand"]},
                ],
            }).encode())

    live_shape = RealSearchEnvelope()
    active = HostedCopilot(
        store, vault, qloo_daily_limit=64, llm_daily_limit=64,
        browser_daily_limit=40, qloo_transport=live_shape,
    ).test_connection(owner, "qloo")
    assert active == {"provider": "qloo", "status": "ACTIVE"}
    assert live_shape.calls == 1
    assert vault.status(private=True)["qloo"]["status"] == "ACTIVE"
    assert "reason_code" not in vault.status(private=True)["qloo"]

    from ofc_engine.copilot_llm import JSONReply

    class RejectedModel:
        def __init__(self):
            self.calls = 0
        def post(self, url, headers, data, *, timeout, max_bytes):
            self.calls += 1
            return JSONReply(404, b'{"key":"private-secret"}')

    vault.configure(owner, "gemini", "fake-gemini-key-for-connection-tests")
    rejection = RejectedModel()
    last = HostedCopilot(
        store, vault, qloo_daily_limit=64, llm_daily_limit=64,
        browser_daily_limit=40, model_transport=rejection,
    ).test_connection(owner, "gemini")
    assert last == {
        "provider": "gemini", "status": "UNAVAILABLE",
        "reason_code": "PROVIDER_HTTP_404",
    }
    assert rejection.calls == 1 and "private-secret" not in str(last)
    assert vault.status(private=True)["gemini"]["reason_code"] == "PROVIDER_HTTP_404"
    assert "reason_code" not in vault.status()["gemini"]
    vault.configure(owner, "gemini", "replacement-gemini-key-0001")
    assert vault.status(private=True)["gemini"]["status"] == "NOT_TESTED"
    assert "reason_code" not in vault.status(private=True)["gemini"]
    with store._connect() as db:
        before = db.execute(
            "select utc_day,reserved from ofc_private.copilot_connection_attempts "
            "where id=1"
        ).fetchone()
        db.execute(
            "update ofc_private.copilot_credentials "
            "set checked_at=now()-interval '35 minutes' "
            "where provider='qloo'"
        )
    private = vault.status(private=True)["qloo"]
    public = vault.status()["qloo"]
    assert private["status"] == "CHECK_REQUIRED"
    assert private["last_test"]["outcome"] == "ACTIVE"
    assert private["last_test"]["freshness"] == "EXPIRED"
    assert private["last_test"]["checked_at"]
    assert public["status"] == "CHECK_REQUIRED"
    assert "last_test" not in public
    with store._connect() as db:
        after = db.execute(
            "select utc_day,reserved from ofc_private.copilot_connection_attempts "
            "where id=1"
        ).fetchone()
    assert after == before

    vault.mark_test("gemini", True, connection_only=True)
    with store._connect() as db:
        db.execute(
            "update ofc_private.copilot_credentials "
            "set checked_at=now()-interval '35 minutes' "
            "where provider='gemini'"
        )
    gemini = vault.status(private=True)["gemini"]
    assert gemini["status"] == "CHECK_REQUIRED"
    assert gemini["last_test"]["outcome"] == "CONNECTED"
    assert gemini["last_test"]["freshness"] == "EXPIRED"
    assert gemini["qualification"] == "CONNECTIVITY_ONLY"
    assert "last_test" not in vault.status()["gemini"]
    vault.revoke(owner, "gemini")
    vault.revoke(owner, "qloo")


def test_hosted_copilot_clarifies_missing_artist_and_unsupported_scopes(pg_url):
    """Do not spend Qloo calls or misstate grounded answers outside qualified scopes."""
    from cryptography.fernet import Fernet
    from ofc_engine.copilot_credentials import CredentialVault
    from ofc_engine.copilot_hosted import HostedCopilot
    from ofc_engine.copilot_llm import JSONReply

    class DraftOnly:
        def __init__(self):
            self.calls = 0

        def post(self, url, headers, data, *, timeout, max_bytes):
            self.calls += 1
            prompt = json.loads(data)["contents"][0]["parts"][0]["text"]
            brand = "brand" in prompt.lower()
            outside_catalog = "contract" in prompt.lower()
            decision = (
                {
                    "decision_id": "brand", "domain": "brand",
                    "selected_name": "Brand A",
                    "candidate_names": ["Brand A", "Brand B"],
                    "mutable": True, "depends_on": [],
                } if brand else {
                    "decision_id": "venue",
                    "domain": "contract" if outside_catalog else "venue",
                    "selected_name": "Venue A",
                    "candidate_names": ["Venue A", "Venue B"],
                    "mutable": False, "depends_on": [],
                }
            )
            payload = {
                "objective": "Propose a fictional event", "artist_name": None,
                "budget_usd": None, "cities": [],
                "choices": [decision],
                "baseline_answer": "These are draft options, not verified.",
                "clarification_question": None,
            }
            return JSONReply(200, json.dumps({
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": json.dumps(payload)}]},
                }],
            }).encode())

    class NoQloo:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("unqualified scope should never query Qloo")

    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    vault = CredentialVault(
        store, master_key=Fernet.generate_key().decode(),
        owner_secret="clarify-owner-" + "c" * 45,
    )
    owner = vault.login("clarify-owner-" + "c" * 45)
    vault.configure(owner, "qloo", "local-test-qloo-credential-12345")
    vault.configure(owner, "gemini", "local-test-model-credential-12345")
    llm, qloo = DraftOnly(), NoQloo()
    runtime = HostedCopilot(
        store, vault, qloo_daily_limit=64, llm_daily_limit=64,
        browser_daily_limit=80, model_transport=llm, qloo_transport=qloo,
    )
    visitor = runtime.session()
    args = {
        "session_id": visitor["session_id"], "ticket": visitor["ticket"],
        "request_id": "missing-artist-123",
        "prompt": "Pick a brand partner",
    }
    missing = runtime.submit(**args)
    assert missing["status"] == "CLARIFICATION_REQUIRED"
    assert missing["reason_code"] == "ARTIST_NAME_REQUIRED"
    assert missing["schema_version"] == "copilot.v1"
    assert missing["llm_requests_this_run"] == 1
    assert missing["qloo_requests_this_run"] == 0
    assert missing["qloo_status"] == "NOT_QUERIED"
    assert missing["plan"]["choices"][0]["domain"] == "brand"
    assert missing["release_authorized"] is False
    assert runtime.submit(**args) == missing

    venue = runtime.submit(
        session_id=visitor["session_id"], ticket=visitor["ticket"],
        request_id="venue-needs-artist-456", prompt="Plan a venue",
    )
    assert venue["status"] == "CLARIFICATION_REQUIRED"
    assert venue["reason_code"] == "ARTIST_NAME_REQUIRED"
    assert venue["plan"]["choices"][0]["domain"] == "venue"
    assert venue["qloo_requests_this_run"] == 0
    unsupported = runtime.submit(
        session_id=visitor["session_id"], ticket=visitor["ticket"],
        request_id="unsupported-contract-789", prompt="Plan a contract",
    )
    assert unsupported["status"] == "CAPABILITY_UNAVAILABLE"
    assert unsupported["reason_code"] == "QLOO_DECISION_DOMAIN_UNQUALIFIED"
    assert unsupported["llm_requests_this_run"] == 1
    assert unsupported["qloo_search_requests"] == 0
    assert unsupported["qloo_rank_requests"] == 0
    assert unsupported["plan"]["choices"][0]["domain"] == "contract"
    assert unsupported["qloo_status"] == "NOT_QUERIED"
    assert llm.calls == 3
    assert qloo.calls == 0
    vault.revoke(owner, "qloo")
    vault.revoke(owner, "gemini")


def test_provider_failure_admissions_visible_without_retry(pg_url):
    from cryptography.fernet import Fernet
    from ofc_engine.copilot_credentials import CredentialVault
    from ofc_engine.copilot_hosted import HostedCopilot
    from ofc_engine.copilot_llm import JSONReply

    class RateLimitedModel:
        def __init__(self):
            self.attempts = 0

        def post(self, *args, **kwargs):
            self.attempts += 1
            return JSONReply(429, b"{}")

    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    vault = CredentialVault(
        store, master_key=Fernet.generate_key().decode(),
        owner_secret="failure-count-owner-" + "f" * 44,
    )
    owner = vault.login("failure-count-owner-" + "f" * 44)
    vault.configure(owner, "gemini", "failure-demo-model-key-000123")
    vault.configure(owner, "qloo", "failure-demo-qloo-key-000123")
    model = RateLimitedModel()
    service = HostedCopilot(
        store, vault, qloo_daily_limit=64, llm_daily_limit=64,
        browser_daily_limit=80, model_transport=model,
    )
    session = service.session()
    args = {
        "session_id": session["session_id"], "ticket": session["ticket"],
        "request_id": "failing-extraction-001",
        "prompt": "Make a cultural brand shortlist",
    }
    blocked = service.submit(**args)
    assert blocked["status"] == "BLOCKED"
    assert blocked["reason_code"] == "PROVIDER_HTTP_429"
    assert blocked["llm_requests_this_run"] == 1
    assert blocked["qloo_requests_this_run"] == 0
    assert blocked["release_authorized"] is False
    assert service.submit(**args) == blocked
    assert model.attempts == 1
    vault.revoke(owner, "gemini")
    vault.revoke(owner, "qloo")


def test_user_selected_qloo_identity_is_bound_to_prior_session(pg_url):
    from cryptography.fernet import Fernet
    from urllib.parse import parse_qs, urlsplit
    from ofc_engine.copilot_credentials import CredentialVault
    from ofc_engine.copilot_hosted import HostedCopilot, SessionDenied
    from ofc_engine.copilot_llm import JSONReply

    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute((Path(__file__).resolve().parents[1] / "sql/003_dynamic_qloo.sql").read_text())
        db.execute((Path(__file__).resolve().parents[1] / "sql/004_copilot_hosted.sql").read_text())
        db.execute("update ofc_private.copilot_llm_budget set reserved=0,utc_day=current_date")
        db.execute("update ofc_private.dynamic_qloo_budget set reserved=0,utc_day=current_date")

    artist = "c70ce2b8-0ad1-4150-bf18-5a6347f2e860"
    brand_a = "43a36e73-9bc7-4b09-a116-78e51554c3e3"
    brand_b = "e26bc58d-18d2-405b-bc7b-7c556ade3829"

    class Model:
        def __init__(self):
            self.calls = 0

        def post(self, url, headers, data, *, timeout, max_bytes):
            self.calls += 1
            request = json.loads(data)
            schema = request["generationConfig"]["responseJsonSchema"]
            if "answer" in schema["properties"]:
                statement = json.loads(request["contents"][0]["parts"][0]["text"])
                answer = {"answer": (
                    "OFC checks and repairs the brand." if statement["view"]["ofc_enabled"]
                    else "Qloo ranks candidate brands against the artist."
                )}
            else:
                answer = {
                    "objective": "Select brand", "artist_name": "Taylor Swft",
                    "budget_usd": None, "cities": [],
                    "choices": [{
                        "decision_id": "brand", "domain": "brand",
                        "selected_name": "Brand A",
                        "candidate_names": ["Brand A", "Brand B"],
                        "mutable": True, "depends_on": [],
                    }],
                    "baseline_answer": "Brand A is a proposal.",
                    "clarification_question": None,
                }
            return JSONReply(200, json.dumps({"candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": json.dumps(answer)}]},
            }]}).encode())

    class Provider:
        def __init__(self):
            self.calls = 0

        def get(self, url, headers, *, timeout, max_bytes):
            self.calls += 1
            route = urlsplit(url)
            if route.path == "/search":
                args = parse_qs(route.query)
                query = args["query"][0]
                names = {
                    "Taylor Swft": ("Taylor Swift", artist),
                    "Brand A": ("Brand A", brand_a),
                    "Brand B": ("Brand B", brand_b),
                }
                name, entity_id = names[query]
                body = {"success": True, "results": {"entities": [{
                    "name": name, "entity_id": entity_id,
                    "type": "urn:entity", "subtype": args["types"][0],
                }]}}
            else:
                body = {"success": True, "results": {"entities": [
                    {"entity_id": i, "type": "urn:entity",
                     "query": {"affinity": score, "explainability": {
                         "signal.interests.entities": {},
                     }}}
                    for i, score in ((brand_b, 0.9), (brand_a, 0.6))
                ]}}
            return TransportReply(200, json.dumps(body).encode())

    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    vault = CredentialVault(
        store, master_key=Fernet.generate_key().decode(),
        owner_secret="selection-owner-" + "q" * 47,
    )
    token = vault.login("selection-owner-" + "q" * 47)
    vault.configure(token, "qloo", "fake-qloo-selection-key")
    vault.configure(token, "gemini", "fake-gemini-selection-key")
    model, provider = Model(), Provider()
    service = HostedCopilot(
        store, vault, qloo_daily_limit=20, llm_daily_limit=20,
        browser_daily_limit=60, model_transport=model, qloo_transport=provider,
    )
    session = service.session()
    first = service.submit(
        session_id=session["session_id"], ticket=session["ticket"],
        request_id="first-disambiguation", prompt="Taylor Swft brand suggestions",
    )
    assert first["status"] == "CLARIFICATION_REQUIRED"
    assert first["reason_code"] == "SEARCH_SELECTION_REQUIRED"
    assert first["suggestions"][0]["name"] == "Taylor Swift"
    ref = first["suggestions"][0]["option_ref"]
    assert "entity_id" not in json.dumps(first["suggestions"])

    stranger = service.session()
    with pytest.raises(SessionDenied):
        service.submit(
            session_id=stranger["session_id"], ticket=stranger["ticket"],
            request_id="wrong-session-choice", prompt="Choose that artist",
            parent_run_id=first["run_id"], entity_selection_ref=ref,
        )

    followup = service.submit(
        session_id=session["session_id"], ticket=session["ticket"],
        request_id="confirm-taylor-option",
        prompt="Use the suggested artist and retain both brands",
        parent_run_id=first["run_id"], entity_selection_ref=ref,
    )
    assert followup["status"] == "REVIEW_REQUIRED"
    assert followup["on"]["changed_ids"] == ["brand"]
    assert followup["parent_run_id"] == first["run_id"]
    assert followup["off"]["qloo"] == followup["on"]["qloo"]
    assert service.submit(
        session_id=session["session_id"], ticket=session["ticket"],
        request_id="confirm-taylor-option",
        prompt="Use the suggested artist and retain both brands",
        parent_run_id=first["run_id"], entity_selection_ref=ref,
    ) == followup
    assert model.calls == 4
    assert provider.calls == 6
    vault.revoke(token, "qloo")
    vault.revoke(token, "gemini")


def test_cross_domain_movie_evidence_is_shared_but_never_auto_verified(pg_url):
    from cryptography.fernet import Fernet
    from urllib.parse import parse_qs, urlsplit
    from ofc_engine.copilot_credentials import CredentialVault
    from ofc_engine.copilot_hosted import HostedCopilot
    from ofc_engine.copilot_llm import JSONReply

    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute((Path(__file__).resolve().parents[1] / "sql/003_dynamic_qloo.sql").read_text())
        db.execute((Path(__file__).resolve().parents[1] / "sql/004_copilot_hosted.sql").read_text())
        db.execute("update ofc_private.copilot_llm_budget set reserved=0,utc_day=current_date")
        db.execute("update ofc_private.dynamic_qloo_budget set reserved=0,utc_day=current_date")

    ids = {
        "Artist Example": "0a0a0a0a-0000-4000-8000-000000000001",
        "Movie A": "0a0a0a0a-0000-4000-8000-000000000002",
        "Movie B": "0a0a0a0a-0000-4000-8000-000000000003",
    }

    class Model:
        def __init__(self):
            self.calls = 0

        def post(self, url, headers, data, *, timeout, max_bytes):
            self.calls += 1
            payload = json.loads(data)
            fields = payload["generationConfig"]["responseJsonSchema"]["properties"]
            if "answer" in fields:
                view = json.loads(payload["contents"][0]["parts"][0]["text"])["view"]
                answer = {"answer": (
                    "OFC cannot independently certify the movie relationship."
                    if view["ofc_enabled"] else
                    "Qloo suggests a cultural movie ranking."
                )}
            else:
                answer = {
                    "objective": "Compare movies for a music artist",
                    "artist_name": "Artist Example",
                    "budget_usd": None, "cities": [],
                    "choices": [{
                        "decision_id": "film", "domain": "movie",
                        "selected_name": "Movie A",
                        "candidate_names": ["Movie A", "Movie B"],
                        "mutable": True, "depends_on": [],
                    }],
                    "baseline_answer": "Movie A is only a draft.",
                    "clarification_question": None,
                }
            return JSONReply(200, json.dumps({"candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": json.dumps(answer)}]},
            }]}).encode())

    class Provider:
        def __init__(self):
            self.calls = []

        def get(self, url, headers, *, timeout, max_bytes):
            route = urlsplit(url)
            args = parse_qs(route.query)
            self.calls.append(route.path)
            if route.path == "/search":
                name = args["query"][0]
                payload = {"success": True, "results": {"entities": [{
                    "name": name, "entity_id": ids[name],
                    "type": "urn:entity",
                    "subtype": args["types"][0],
                }]}}
            else:
                assert args["filter.type"] == ["urn:entity:movie"]
                payload = {"success": True, "results": {"entities": [
                    {"entity_id": id_, "type": "urn:entity",
                     "query": {"affinity": affinity}}
                    for id_, affinity in ((ids["Movie B"], 0.92), (ids["Movie A"], 0.6))
                ]}}
            return TransportReply(200, json.dumps(payload).encode())

    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    vault = CredentialVault(
        store, master_key=Fernet.generate_key().decode(),
        owner_secret="catalog-owner-" + "k" * 47,
    )
    owner = vault.login("catalog-owner-" + "k" * 47)
    vault.configure(owner, "qloo", "private-test-qloo-catalog-key")
    vault.configure(owner, "gemini", "private-test-gemini-catalog-key")
    model, provider = Model(), Provider()
    service = HostedCopilot(
        store, vault, qloo_daily_limit=30, llm_daily_limit=30,
        browser_daily_limit=70, model_transport=model, qloo_transport=provider,
    )
    session = service.session()
    reply = service.submit(
        session_id=session["session_id"], ticket=session["ticket"],
        request_id="movie-observation-001",
        prompt="Which movies culturally resonate with Artist Example?",
    )
    assert reply["status"] == "REVIEW_REQUIRED"
    assert reply["off"]["qloo"] == reply["on"]["qloo"]
    assert reply["off"]["ofc_enabled"] is False
    assert reply["on"]["ofc_enabled"] is True
    assert reply["on"]["changed_ids"] == []
    assert reply["on"]["decisions"][0]["ofc_verdict"] == "UNCERTAIN"
    assert reply["on"]["decisions"][0]["ofc_reason"] == "CATALOG_POLICY_NOT_LIVE_QUALIFIED"
    catalog = reply["off"]["qloo"]["catalog_observations"]
    assert catalog[0]["status"] == "OBSERVED_NOT_POLICY_QUALIFIED"
    assert catalog[0]["ranked_candidates"] == ["Movie B", "Movie A"]
    assert catalog[0]["policy_qualified"] is False
    assert reply["qloo_requests_this_run"] == 4
    assert reply["qloo_search_requests"] == 3
    assert reply["qloo_catalog_requests"] == 1
    assert reply["llm_requests_this_run"] == 3
    assert len(provider.calls) == 4 and model.calls == 3
    assert service.submit(
        session_id=session["session_id"], ticket=session["ticket"],
        request_id="movie-observation-001",
        prompt="Which movies culturally resonate with Artist Example?",
    ) == reply
    assert len(provider.calls) == 4 and model.calls == 3
    vault.revoke(owner, "qloo")
    vault.revoke(owner, "gemini")


def test_native_qloo_discovery_from_candidate_free_taste_prompt(pg_url):
    from cryptography.fernet import Fernet
    from urllib.parse import parse_qs, urlsplit
    from ofc_engine.copilot_credentials import CredentialVault
    from ofc_engine.copilot_hosted import HostedCopilot
    from ofc_engine.copilot_llm import JSONReply

    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute((Path(__file__).resolve().parents[1] / "sql/003_dynamic_qloo.sql").read_text())
        db.execute((Path(__file__).resolve().parents[1] / "sql/004_copilot_hosted.sql").read_text())
        db.execute("update ofc_private.copilot_llm_budget set reserved=0,utc_day=current_date")
        db.execute("update ofc_private.dynamic_qloo_budget set reserved=0,utc_day=current_date")
    ids = {
        "Movie Example": "0a0a0a0a-0000-4000-8000-000000000001",
        "Restaurant Alpha": "0a0a0a0a-0000-4000-8000-000000000002",
        "Restaurant Beta": "0a0a0a0a-0000-4000-8000-000000000003",
    }

    class Model:
        def __init__(self):
            self.calls = 0

        def post(self, url, headers, data, *, timeout, max_bytes):
            self.calls += 1
            payload = json.loads(data)
            fields = payload["generationConfig"]["responseJsonSchema"]["properties"]
            if "answer" in fields:
                view = json.loads(payload["contents"][0]["parts"][0]["text"])["view"]
                answer = {"answer": (
                    "OFC withholds restaurant availability and unverified claims."
                    if view["ofc_enabled"] else
                    "Qloo provided cultural candidates, not reservations."
                )}
            else:
                answer = {
                    "objective": "Recommend places for a movie audience",
                    "artist_name": None,
                    "taste_seed_name": "Movie Example",
                    "taste_seed_category": "movie",
                    "discovery_category": "place",
                    "taste_location": "New York",
                    "budget_usd": None, "cities": [],
                    "choices": [],
                    "baseline_answer": "I need Qloo recommendations.",
                    "clarification_question": None,
                }
            return JSONReply(200, json.dumps({"candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": json.dumps(answer)}]},
            }]}).encode())

    class Provider:
        def __init__(self):
            self.calls = []

        def get(self, url, headers, *, timeout, max_bytes):
            route = urlsplit(url)
            args = parse_qs(route.query)
            self.calls.append(route.path)
            if route.path == "/search":
                assert args["types"] == ["urn:entity:movie"]
                body = {"success": True, "results": {"entities": [{
                    "name": "Movie Example", "entity_id": ids["Movie Example"],
                    "type": "urn:entity", "subtype": "urn:entity:movie",
                }]}}
            else:
                assert args["filter.type"] == ["urn:entity:place"]
                assert args["filter.location.query"] == ["New York"]
                assert "filter.results.entities" not in args
                body = {"success": True, "results": {"entities": [
                    {"entity_id": ids["Restaurant Beta"], "name": "Restaurant Beta",
                     "type": "urn:entity",
                     "query": {"affinity": 0.9, "explainability": {
                         "signal.interests.entities": {
                             ids["Movie Example"]: 0.77
                         }
                     }}},
                    {"entity_id": ids["Restaurant Alpha"], "name": "Restaurant Alpha",
                     "type": "urn:entity",
                     "query": {"affinity": 0.4}},
                ]}}
            return TransportReply(200, json.dumps(body).encode())

    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    vault = CredentialVault(
        store, master_key=Fernet.generate_key().decode(),
        owner_secret="discovery-owner-" + "d" * 47,
    )
    owner = vault.login("discovery-owner-" + "d" * 47)
    vault.configure(owner, "qloo", "test-qloo-discovery-key-12345")
    vault.configure(owner, "gemini", "test-gemini-discovery-key-12345")
    transport, provider = Model(), Provider()
    runtime = HostedCopilot(
        store, vault, qloo_daily_limit=50, llm_daily_limit=50,
        browser_daily_limit=75, model_transport=transport, qloo_transport=provider,
    )
    session = runtime.session()
    args = {
        "session_id": session["session_id"], "ticket": session["ticket"],
        "request_id": "candidate-free-local-001",
        "prompt": "Suggest New York restaurants for people who enjoy Movie Example.",
    }
    result = runtime.submit(**args)
    assert result["status"] == "REVIEW_REQUIRED", (result, provider.calls, transport.calls)
    assert result["plan"]["choices"][0]["candidate_names"] == [
        "Restaurant Beta", "Restaurant Alpha"
    ]
    assert result["off"]["qloo"] == result["on"]["qloo"]
    assert result["taste_policy"]["automatic_repair"] == "NOT_QUALIFIED"
    assert result["on"]["changed_ids"] == []
    assert result["on"]["decisions"][0]["ofc_verdict"] == "UNCERTAIN"
    assert result["off"]["qloo"]["native_discovery"]["candidates"][0]["name"] == "Restaurant Beta"
    assert "entity_id" not in json.dumps(result["off"]["qloo"]["native_discovery"])
    assert result["qloo_discovery_requests"] == 1
    assert result["qloo_requests_this_run"] == 2
    assert result["llm_requests_this_run"] == 3
    assert len(provider.calls) == 2 and transport.calls == 3
    assert runtime.submit(**args) == result
    assert len(provider.calls) == 2 and transport.calls == 3
    vault.revoke(owner, "qloo")
    vault.revoke(owner, "gemini")


def test_multicity_judge_request_retains_qualified_brand_review(pg_url):
    from cryptography.fernet import Fernet
    from urllib.parse import parse_qs, urlsplit
    from ofc_engine.copilot_credentials import CredentialVault
    from ofc_engine.copilot_hosted import HostedCopilot
    from ofc_engine.copilot_llm import JSONReply

    with psycopg.connect(pg_url, sslmode="disable") as db:
        db.execute((Path(__file__).resolve().parents[1] / "sql/003_dynamic_qloo.sql").read_text())
        db.execute((Path(__file__).resolve().parents[1] / "sql/004_copilot_hosted.sql").read_text())
        db.execute("update ofc_private.dynamic_qloo_budget set utc_day=current_date,reserved=0")
        db.execute("update ofc_private.copilot_llm_budget set utc_day=current_date,reserved=0")
        db.execute("update ofc_private.copilot_browser_budget set utc_day=current_date,sessions=0")
    artist, first, second = (
        "0a0a0a0a-0000-4000-8000-000000000001",
        "0a0a0a0a-0000-4000-8000-000000000002",
        "0a0a0a0a-0000-4000-8000-000000000003",
    )
    names = {"Artist Example": artist, "Brand A": first, "Brand B": second}

    class FakeQloo:
        def __init__(self):
            self.searches = []
            self.ranks = 0

        def get(self, url, headers, *, timeout, max_bytes):
            route = urlsplit(url)
            if route.path == "/search":
                query = parse_qs(route.query)
                name = query["query"][0]
                self.searches.append(name)
                assert name in names, "unqualified place search cannot spend a call"
                body = {"success": True, "results": {"entities": [{
                    "name": name, "entity_id": names[name], "type": "urn:entity",
                    "subtype": query["types"][0],
                }]}}
            else:
                self.ranks += 1
                body = {"success": True, "results": {"entities": [
                    {"entity_id": key, "type": "urn:entity",
                     "query": {"affinity": value, "explainability": {
                         "signal.interests.entities": {},
                     }}} for key, value in ((second, .9), (first, .6))
                ]}}
            return TransportReply(200, json.dumps(body).encode())

    class FakeModel:
        def __init__(self):
            self.calls = 0

        def post(self, url, headers, data, *, timeout, max_bytes):
            self.calls += 1
            schema = json.loads(data)["generationConfig"]["responseJsonSchema"]
            if "answer" in schema["properties"]:
                value = {"answer": "Brand evidence validated; venue choices withheld."}
            else:
                decisions = [{
                    "decision_id": "brand", "domain": "brand",
                    "selected_name": "Brand A",
                    "candidate_names": ["Brand A", "Brand B"],
                    "mutable": True, "depends_on": [],
                }]
                for city in ("Chicago", "Austin", "Seattle"):
                    decisions.append({
                        "decision_id": "venue_" + city.lower(), "domain": "place",
                        "selected_name": city + " Venue A",
                        "candidate_names": [city + " Venue A", city + " Venue B"],
                        "mutable": True, "depends_on": ["brand"],
                    })
                value = {
                    "objective": "Check an artist's hypothetical three-city tour",
                    "artist_name": "Artist Example", "taste_seed_name": "Artist Example",
                    "taste_seed_category": "artist", "discovery_category": None,
                    "taste_location": None, "budget_usd": 60000,
                    "cities": ["Chicago", "Austin", "Seattle"],
                    "choices": decisions, "baseline_answer": "Brand A proposed.",
                    "clarification_question": None,
                }
            return JSONReply(200, json.dumps({"candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": json.dumps(value)}]},
            }]}).encode())

    store = PostgresRevisionStore(pg_url, insecure_local_test=True)
    vault = CredentialVault(
        store, master_key=Fernet.generate_key().decode(),
        owner_secret="judge-test-owner-" + "x" * 48,
    )
    owner = vault.login("judge-test-owner-" + "x" * 48)
    vault.configure(owner, "qloo", "qloo-local-test-key")
    vault.configure(owner, "gemini", "gemini-local-test-key")
    fake_qloo, fake_model = FakeQloo(), FakeModel()
    runtime = HostedCopilot(
        store, vault, qloo_daily_limit=12, llm_daily_limit=9,
        browser_daily_limit=5,
        qloo_transport=fake_qloo, model_transport=fake_model,
    )
    session = runtime.session()
    result = runtime.submit(
        session_id=session["session_id"], ticket=session["ticket"],
        request_id="judge-freeform-three-city",
        prompt=("My agent picked Brand A for Artist Example. Compare Brand B "
                "and dependent venues across Chicago, Austin and Seattle. "
                "No commercial action is allowed."),
    )
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["plan"]["cities"] == ["Chicago", "Austin", "Seattle"]
    assert len(result["plan"]["choices"]) == 4
    assert result["revision"] == 1
    assert result["off"]["decisions"][0]["candidate_id"] == "Brand A"
    assert result["on"]["decisions"][0]["candidate_id"] == "Brand B"
    assert result["qloo_requests_this_run"] == 5
    assert result["qloo_search_requests"] == 3
    assert result["qloo_rank_requests"] == 2
    assert fake_qloo.ranks == 2 and len(fake_qloo.searches) == 3
    assert fake_model.calls == 3
    observations = result["on"]["qloo"]["catalog_observations"]
    assert len(observations) == 3
    assert all(item["status"] == "NOT_EVALUATED" and
               item["reason_code"] == "OPTIONAL_EVIDENCE_QUOTA_GUARD" and
               item["policy_qualified"] is False for item in observations)
    assert result["off"]["qloo"] == result["on"]["qloo"]
    assert result["release_authorized"] is False
    assert result["external_action_executed"] is False
