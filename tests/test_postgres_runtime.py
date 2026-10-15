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
