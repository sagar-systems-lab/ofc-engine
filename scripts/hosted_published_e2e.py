"""Qualify the public guided tour against a real local Postgres revision journal.

Qloo transport is a deterministic test fixture. No external provider requests.
"""
from __future__ import annotations

from hashlib import sha256
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import os
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import psycopg
from playwright.sync_api import sync_playwright

from ofc_engine.hosted_agent import HostedAgentFlow
from ofc_engine.hosted_live import HostedLiveReview
from ofc_engine.hosted_preview import HostedFixtureServer
from ofc_engine.hosted_runs import HostedRunRegistry
from ofc_engine.hosted_tour import configured_tour
from ofc_engine.intake import freeze_proposal
from ofc_engine.postgres_attempt import PostgresAttemptLedger
from ofc_engine.postgres_store import PostgresRevisionStore
from ofc_engine.published_tour import PublishedTourReview
from ofc_engine.qloo import QlooProbeClient, TransportReply, brand_affinity_probe
from ofc_engine.revisions import plan_digest


class TestOnlyTransport:
    def __init__(self):
        self.calls = 0

    def get(self, url, headers, *, timeout, max_bytes):
        self.calls += 1
        if not url.startswith("https://hackathon.api.qloo.com/v2/insights?"):
            raise AssertionError("unexpected Qloo request shape")
        return TransportReply(200, json.dumps({
            "success": True,
            "results": {"entities": [
                {
                    "entity_id": entity, "type": "urn:entity",
                    "query": {
                        "affinity": score,
                        "explainability": {"signal.interests.entities": {}},
                    }
                }
                for entity, score in (
                    ("ci-brand-b", 0.94),
                    ("ci-brand-a", 0.53),
                )
            ]}
        }).encode())


def get_json(url, *, headers=None):
    with urlopen(Request(url, headers=headers or {}), timeout=10) as response:
        assert response.status == 200
        return json.load(response)


def denied(url, expected, *, method="GET", data=None, headers=None):
    try:
        with urlopen(Request(
            url, data=data, method=method, headers=headers or {},
        ), timeout=10):
            raise AssertionError("unsafe request accepted")
    except HTTPError as exc:
        assert exc.code == expected, (url, exc.code, expected)
        exc.read()


def boot(dsn):
    root = Path(__file__).resolve().parents[1]
    with psycopg.connect(dsn, sslmode="disable") as db:
        for role in ("anon", "authenticated"):
            db.execute(
                "do $$ begin if not exists (select from pg_roles where "
                f"rolname = '{role}') then create role {role}; end if; end $$"
            )
        for filename in ("001_private_runtime.sql", "002_browser_proposal_scope.sql"):
            db.execute((root / "sql" / filename).read_text())
        assert db.execute(
            "select has_schema_privilege('anon','ofc_private','USAGE'),"
            "has_schema_privilege('authenticated','ofc_private','USAGE')"
        ).fetchone() == (False, False)

    tour = configured_tour({
        "OFC_TOUR_ARTIST_ID": "ci-artist",
        "OFC_TOUR_BRAND_IDS": "ci-brand-a,ci-brand-b",
        "OFC_TOUR_SELECTED_ID": "ci-brand-a",
    })
    assert tour is not None
    registry = HostedRunRegistry(PostgresRevisionStore(dsn, insecure_local_test=True))
    saved = registry.create(tour.model_dump(mode="json"))
    run_id, ticket = saved["run_id"], saved["ticket"]
    baseline = registry.operator_proposal(run_id)
    frozen, _ = freeze_proposal(baseline)
    assert frozen.revision == 0
    assert len(saved["review"]["decisions"]) == 6
    assert saved["review"]["source_mode"] == "EVIDENCE_UNAVAILABLE"

    other = registry.create(tour.model_dump(mode="json"))
    spec = brand_affinity_probe("ci-artist", ("ci-brand-a", "ci-brand-b"))
    scope = sha256((
        spec.fingerprint + ":ci-brand-a:" + plan_digest(frozen)
    ).encode()).hexdigest()
    transport = TestOnlyTransport()
    live = HostedLiveReview(
        api_key="ci-key-never-public",
        artist_id="ci-artist",
        brand_ids=("ci-brand-a", "ci-brand-b"),
        selected_brand_id="ci-brand-a",
        ledger=PostgresAttemptLedger(dsn, scope=scope, insecure_local_test=True),
        operator_token="ci-operator-" + "x" * 40,
        client=QlooProbeClient(api_key="ci-key-never-public", transport=transport),
        agent_evaluator=HostedAgentFlow(proposal=baseline, store=registry.store).evaluate,
        bound_run_id=run_id,
    )
    status, result = live.observe()
    assert status == 200 and result["status"] == "LIVE_RANK_OBSERVED"
    assert transport.calls == 1
    assert registry.store.get(run_id).revision == 1
    assert len(registry.store.audit_history(run_id)) == 2
    assert live.observe()[1]["requests_this_call"] == 0
    assert transport.calls == 1

    published = PublishedTourReview(registry, run_id)
    proof = published.review()
    assert proof["tour"]["decisions"][0]["reviewed_option"] == 2
    assert proof["changed_ids"] == ["brand"]
    assert proof["handoff"] == "WITHHELD"
    assert proof["additional_provider_requests"] == 0
    return registry, saved, other, published, live, transport


def server_for(registry, published, live):
    server = HostedFixtureServer(
        ("127.0.0.1", 0),
        build_sha="abc123d",
        run_registry=registry,
        public_showcase=published,
        hosted_live=live,
    )
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, worker, "http://127.0.0.1:" + str(server.server_port)


def stop(server, worker):
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)


def browser_test(origin):
    viewports = (1440, 375, 320)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for width in viewports:
                page = browser.new_page(
                    viewport={"width": width, "height": 800}, reduced_motion="reduce"
                )
                errors, outgoing = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: outgoing.append(
                    (request.method, request.url)
                ) if request.method != "GET" or
                    not request.url.startswith(origin + "/") else None)
                try:
                    page.goto(origin + "/#plan", wait_until="domcontentloaded")
                    launch = page.locator("#start-qloo-journey")
                    launch.wait_for(state="visible", timeout=10000)
                    assert page.locator("#start-hosted-tour").is_hidden()
                    launch.focus()
                    launch.press("Enter")
                    stages = (
                        ("01 / PROPOSE", "$60,000"),
                        ("02 / QLOO CHECK", "Qloo OFF"),
                        ("03 / REPAIR", "REPAIRED"),
                        ("04 / VERIFY", "RECHECK PENDING"),
                        ("05 / HANDOFF", "HANDOFF WITHHELD"),
                    )
                    for heading, phrase in stages:
                        assert page.locator("#qloo-journey-heading").inner_text() == heading
                        assert phrase in page.locator("#qloo-journey-body").inner_text()
                        if heading != "05 / HANDOFF":
                            next_button = page.locator("#qloo-journey-next")
                            next_button.focus()
                            next_button.press("Enter")
                    assert page.locator("#qloo-journey-previous").is_enabled()
                    page.locator("#qloo-journey-previous").click()
                    assert page.locator("#qloo-journey-heading").inner_text() == "04 / VERIFY"
                    assert page.locator("#view-plan .state-item").nth(2).locator(
                        "small").inner_text() == "HANDOFF BLOCKED"
                    assert "REVIEW REQUIRED" in page.locator(".status-label").inner_text()
                    assert not page.evaluate(
                        "document.documentElement.scrollWidth > innerWidth + 1"
                    ), "horizontal overflow"
                    assert not errors, errors
                    assert not outgoing, outgoing
                    assert page.evaluate(
                        "window.matchMedia('(prefers-reduced-motion: reduce)').matches"
                    )
                    page.reload(wait_until="domcontentloaded")
                    page.locator("#start-qloo-journey").wait_for(state="visible")
                    assert page.locator("#published-qloo-review").is_hidden()
                finally:
                    page.close()
        finally:
            browser.close()
    return viewports


def qualify(dsn):
    registry, saved, other, published, live, transport = boot(dsn)
    server, worker, origin = server_for(registry, published, live)
    try:
        ready = get_json(origin + "/readyz")
        assert ready["build_sha"] == "abc123d"
        assert ready["mode"] == "PUBLIC_REDACTED_QLOO_REVIEW"
        preview = get_json(origin + "/api/showcase")
        assert preview == published.review()
        assert len(preview["tour"]["decisions"]) == 6
        serialized = json.dumps(preview)
        for sensitive in (
            "ci-artist", "ci-brand-a", "ci-brand-b", "ci-key-never-public",
            "ci-operator", saved["ticket"], other["ticket"]
        ):
            assert sensitive not in serialized
        assert get_json(origin + "/api/runs/status")["release_authorized"] is False
        denied(origin + "/api/qloo/live-rank", 403, method="POST",
               data=b'{"confirm":"SPEND_ONE_HOSTED_QLOO_REQUEST"}',
               headers={"Origin": origin, "Content-Type": "application/json"})
        denied(origin + "/api/runs/" + other["run_id"] + "/review", 404,
               headers={"Authorization": "Bearer " + saved["ticket"]})
        assert get_json(
            origin + "/api/runs/" + other["run_id"] + "/review",
            headers={"Authorization": "Bearer " + other["ticket"]}
        )["source_mode"] == "EVIDENCE_UNAVAILABLE"
        assert browser_test(origin) == (1440, 375, 320)
        assert transport.calls == 1
    finally:
        stop(server, worker)

    with psycopg.connect(dsn, sslmode="disable") as db:
        db.execute(
            "update ofc_private.browser_runs set expires_at=now()-interval '1 hour' "
            "where plan_id=%s", (saved["run_id"],)
        )
    server, worker, origin = server_for(registry, published, live)
    try:
        assert get_json(origin + "/api/showcase") == published.review()
        denied(origin + "/api/runs/" + saved["run_id"] + "/review", 404,
               headers={"Authorization": "Bearer " + saved["ticket"]})
        assert transport.calls == 1
        with psycopg.connect(dsn, sslmode="disable") as db:
            db.execute(
                "update ofc_private.provider_attempt set scope=%s where id=1",
                ("f" * 64,)
            )
        denied(origin + "/api/showcase", 503)
        with psycopg.connect(dsn, sslmode="disable") as db:
            db.execute(
                "update ofc_private.provider_attempt set scope=%s where id=1",
                (live._ledger.scope,)
            )
            db.execute(
                "update ofc_private.events set event_hash=%s "
                "where plan_id=%s and sequence=1",
                ("e" * 64, saved["run_id"])
            )
        denied(origin + "/api/showcase", 503)
    finally:
        stop(server, worker)
    assert transport.calls == 1
    return {
        "result": "PASS",
        "scope": "POSTGRES_PERSISTED_TEST_ONLY_QLOO_OBSERVATION",
        "browser_widths": [1440, 375, 320],
        "reload": "PASS",
        "expired_ticket_restart": "PASS",
        "wrong_ticket": "BLOCKED",
        "unauthorized_provider_request": "BLOCKED",
        "tampered_scope": "BLOCKED",
        "tampered_journal": "BLOCKED",
        "provider_attempts": 1,
        "external_qloo_calls": 0,
        "handoff": "WITHHELD",
    }


if __name__ == "__main__":
    dsn = os.environ.get("OFC_TEST_POSTGRES_URL")
    if not dsn:
        raise SystemExit("isolated OFC_TEST_POSTGRES_URL required")
    print(json.dumps(qualify(dsn), sort_keys=True))
