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
            if "off_answer" in schema["properties"]:
                text = {
                    "off_answer": "Qloo ranks Brand B, but the proposal retained Brand A.",
                    "on_answer": "With the same Qloo evidence, OFC revises to Brand B, pending review.",
                }
            else:
                text = {
                    "objective": "Check brand fit", "artist_name": "Artist Example",
                    "budget_usd": None, "cities": [],
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
    assert len(qloo.searches) == 3 and qloo.ranks == 2 and model.calls == 2
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
    assert qloo.ranks == model.calls == 2 and len(qloo.searches) == 3
    stored = copy.messages(session["session_id"], session["ticket"])["messages"]
    assert len(stored) == 1 and stored[0]["prompt"] == "Find a great sponsor"
    assert {key: value for key, value in stored[0].items() if key != "prompt"} == report
    with psycopg.connect(pg_url, sslmode="disable") as db:
        encrypted = bytes(db.execute(
            "select prompt_ciphertext from ofc_private.copilot_messages "
            "where run_id=%s", (report["run_id"],)
        ).fetchone()[0])
        assert b"Find a great sponsor" not in encrypted
    with pytest.raises(SessionDenied):
        copy.messages(session["session_id"], "unauthorized-visitor-token")

    saved_usage = (len(qloo.searches), qloo.ranks, model.calls)
    assert copy.capabilities()["qloo"] == "NOT_TESTED"
    assert (len(qloo.searches), qloo.ranks, model.calls) == saved_usage
    assert copy.test_connection(owner, "qloo")["status"] == "ACTIVE"
    assert copy.test_connection(owner, "gemini")["status"] == "ACTIVE"
    assert (len(qloo.searches), qloo.ranks, model.calls) == (4, 2, 3)
    assert copy.capabilities()["product"] == "ACTIVE"

    vault.revoke(owner, "qloo")
    vault.revoke(owner, "gemini")
    assert copy.submit(
        session_id=session["session_id"], ticket=session["ticket"],
        request_id="visitor-idempotent-001", prompt="Find a great sponsor",
    ) == report
    assert qloo.ranks == 2 and model.calls == 3 and len(qloo.searches) == 4
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
        assert call(
            "/api/copilot/sessions", {"confirm": "CREATE_COPILOT_SESSION"}
        )[0] == 503
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
        assert call("/api/owner/logout", {}, cookie=owner_cookie)[0] == 200
        assert call("/api/owner/credentials", cookie=owner_cookie)[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
