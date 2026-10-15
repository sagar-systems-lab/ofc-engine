"""Hosted provider-to-agent qualification without external network traffic."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import stat
from threading import Thread
from urllib.request import urlopen

import pytest

from ofc_engine.agent_run import review_run
from ofc_engine.contracts import ActionIntent, Decision
from ofc_engine.durable import SQLiteRevisionStore
from ofc_engine.hosted_agent import HostedAgentFlow, load_hosted_proposal
from ofc_engine.hosted_attempt import HostedAttemptLedger
from ofc_engine.hosted_live import HostedLiveReview, configured_live_review
from ofc_engine.hosted_preview import HostedFixtureServer
from ofc_engine.intake import AgentProposal
from ofc_engine.qloo import QlooProbeClient, TransportReply

ARTIST = "artist-example"
BRANDS = ("first-brand", "second-brand", "third-brand")
TOKEN = "private-operator-" + "x" * 40


class FixedTransport:
    def __init__(self, *, selected_top=BRANDS[1]):
        self.calls = 0
        self.selected_top = selected_top

    def get(self, url, headers, *, timeout, max_bytes):
        self.calls += 1
        order = (self.selected_top, *[x for x in BRANDS if x != self.selected_top])
        payload = {
            "success": True,
            "results": {"entities": [{
                "entity_id": name,
                "type": "urn:entity",
                "query": {
                    "affinity": 0.95 - rank * 0.24,
                    "explainability": {"signal.interests.entities": {}},
                },
            } for rank, name in enumerate(order)]},
        }
        return TransportReply(200, json.dumps(payload).encode("utf-8"))


def plan():
    return AgentProposal(
        intent=ActionIntent(
            run_id="hosted-tour-001", client_request_id="frozen-tour-001",
            objective="Evaluate a three-city artist tour before action",
            constraints={"external_action": False, "budget_usd": 60000,
                         "cities": ["Chicago", "Austin", "Seattle"]},
            taste_context={"artist_id": ARTIST,
                           "supplemental_decisions": "OPERATOR_CONSTRUCTED_UNVERIFIED"},
        ),
        decisions=(
            Decision(decision_id="brand", domain="partner",
                     candidate_id=BRANDS[0], candidate_pool=BRANDS,
                     mutable_fields=("candidate_id",)),
            Decision(decision_id="opener", domain="artist",
                     candidate_id="unverified-opener", candidate_pool=("unverified-opener",)),
            Decision(decision_id="chicago", domain="venue", locality_ref="Chicago",
                     candidate_id="unverified-chicago", candidate_pool=("unverified-chicago",)),
            Decision(decision_id="austin", domain="venue", locality_ref="Austin",
                     candidate_id="unverified-austin", candidate_pool=("unverified-austin",)),
            Decision(decision_id="seattle", domain="venue", locality_ref="Seattle",
                     candidate_id="unverified-seattle", candidate_pool=("unverified-seattle",)),
            Decision(decision_id="afterparty", domain="experience",
                     candidate_id="unverified-afterparty",
                     candidate_pool=("unverified-afterparty",),
                     depends_on=("brand",)),
        ),
    )


def hosted(tmp_path, *, first=BRANDS[1], transport=None):
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700, exist_ok=True)
    proposal_path = folder / "proposal.json"
    proposal_path.write_text(plan().model_dump_json())
    proposal_path.chmod(0o600)
    proposal = load_hosted_proposal(
        proposal_path, artist_id=ARTIST, brand_ids=BRANDS,
        selected_brand_id=BRANDS[0],
    )
    ledger = HostedAttemptLedger(folder / "qloo.sqlite3", scope="c" * 64)
    workflow = HostedAgentFlow(proposal=proposal, database=ledger.path)
    fake = transport or FixedTransport(selected_top=first)
    controller = HostedLiveReview(
        api_key="fake-do-not-send", artist_id=ARTIST, brand_ids=BRANDS,
        selected_brand_id=BRANDS[0], ledger=ledger, operator_token=TOKEN,
        client=QlooProbeClient(api_key="fake-do-not-send", transport=fake),
        agent_evaluator=workflow.evaluate,
    )
    return controller, fake, ledger, proposal_path


def test_one_real_format_qloo_result_revises_actual_six_decision_plan(tmp_path):
    controller, fake, ledger, proposal_path = hosted(tmp_path)
    frozen = SQLiteRevisionStore(ledger.path).get("hosted-tour-001")
    assert frozen.revision == 0 and frozen.decisions[0].candidate_id == BRANDS[0]

    code, result = controller.observe()
    assert code == 200
    assert fake.calls == 1
    assert result["verdict"] == "RANK_REPAIR_SUGGESTED"
    report = result["agent_run"]
    assert report["status"] == "HOSTED_AGENT_REVIEW"
    assert report["source_mode"] == "LIVE_HTTP_UNVERIFIED"
    assert report["historical_replay"] is False
    assert report["decision_count"] == 6
    assert report["operator_constructed_unverified_decisions"] == 5
    assert report["revision"] == 1
    assert report["changed_ids"] == ["brand"]
    assert report["preserved_ids"] == [
        "opener", "chicago", "austin", "seattle", "afterparty"
    ]
    assert report["invalidated_ids"] == ["brand", "afterparty"]
    assert report["pending_rechecks"] == ["brand", "afterparty"]
    assert report["off_on"]["off_verdicts"]["brand"] == "UNCERTAIN"
    assert report["off_on"]["on_verdicts"]["brand"] == "REPAIR"
    assert report["off_on"]["same_baseline"] is True
    assert report["verification_status"] == "REVIEW_REQUIRED"
    assert report["release_authorized"] is False
    assert report["independent_recheck_completed"] is False
    assert report["provider_authenticated"] is False
    assert report["real_qloo_requests"] == 1
    assert all(d["verdict"] == "UNCERTAIN" for d in report["decisions"][1:])

    store = SQLiteRevisionStore(ledger.path)
    repaired = store.get("hosted-tour-001")
    assert repaired.revision == 1
    assert repaired.decisions[0].candidate_id == BRANDS[1]
    assert all(a == b for a, b in zip(frozen.decisions[1:], repaired.decisions[1:]))
    assert store.pending_rechecks("hosted-tour-001") == ("brand", "afterparty")
    assert len(store.audit_history("hosted-tour-001")) == 2

    status, saved = controller.public_result()
    assert status == 200 and saved["cached"] is True
    assert saved["agent_run"] == report
    for item in (ARTIST, *BRANDS, TOKEN, "fake-do-not-send"):
        assert item not in json.dumps(saved)
    assert controller.observe()[1]["requests_this_call"] == 0
    assert fake.calls == 1

    restarted = HostedAgentFlow(
        proposal=load_hosted_proposal(
            proposal_path, artist_id=ARTIST, brand_ids=BRANDS,
            selected_brand_id=BRANDS[0],
        ),
        database=ledger.path,
    )
    assert restarted.store.get("hosted-tour-001").revision == 1
    assert len(restarted.store.audit_history("hosted-tour-001")) == 2


def test_already_top_brand_causes_no_unnecessary_patch(tmp_path):
    controller, fake, ledger, _ = hosted(tmp_path, first=BRANDS[0])
    status, observed = controller.observe()
    assert status == 200 and fake.calls == 1
    assert observed["agent_run"]["revision"] == 0
    assert observed["agent_run"]["changed_ids"] == []
    assert observed["agent_run"]["release_authorized"] is False
    assert SQLiteRevisionStore(ledger.path).get("hosted-tour-001").revision == 0


def test_hosted_browser_reads_engine_result_without_provider_or_operator_secrets(tmp_path):
    controller, fake, ledger, _ = hosted(tmp_path)
    controller.observe()
    assert fake.calls == 1
    with HostedFixtureServer(("127.0.0.1", 0), hosted_live=controller) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        try:
            with urlopen(base + "/", timeout=5) as response:
                root = response.read()
            with urlopen(base + "/hosted-live.js", timeout=5) as response:
                script = response.read()
            with urlopen(base + "/api/qloo/result", timeout=5) as response:
                body = response.read()
            assert b"Decision Workspace" in root
            assert b"hosted-live.js" in root
            assert b"REVIEW REQUIRED" in script
            assert b"window.prompt" not in script
            assert json.loads(body)["agent_run"]["changed_ids"] == ["brand"]
            for protected in (ARTIST, *BRANDS, TOKEN, "fake-do-not-send"):
                for output in (root, script, body):
                    assert protected.encode() not in output
            assert fake.calls == 1
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_proposal_scope_and_private_permissions_are_required(tmp_path):
    controller, fake, ledger, path = hosted(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe hosted proposal"):
        load_hosted_proposal(path, artist_id=ARTIST, brand_ids=BRANDS,
                             selected_brand_id=BRANDS[0])
    path.chmod(0o600)
    with pytest.raises(ValueError, match="frozen Qloo scope"):
        load_hosted_proposal(path, artist_id="other-artist", brand_ids=BRANDS,
                             selected_brand_id=BRANDS[0])
    with pytest.raises(ValueError, match="frozen Qloo scope"):
        load_hosted_proposal(path, artist_id=ARTIST, brand_ids=BRANDS[::-1],
                             selected_brand_id=BRANDS[0])


def test_configured_hosting_requires_private_existing_agent_plan(tmp_path):
    _, _, ledger, proposal_path = hosted(tmp_path)
    config = {
        "OFC_HOSTED_QLOO_MODE": "ONE_REQUEST_EXPLICITLY_APPROVED",
        "QLOO_API_KEY": "fake",
        "OFC_HOSTED_OPERATOR_TOKEN": TOKEN,
        "OFC_HOSTED_QLOO_ARTIST_ID": ARTIST,
        "OFC_HOSTED_QLOO_BRAND_IDS": ",".join(BRANDS),
        "OFC_HOSTED_QLOO_SELECTED_ID": BRANDS[0],
        "OFC_HOSTED_QLOO_LEDGER_PATH": str(ledger.path.parent / "different.sqlite3"),
    }
    with pytest.raises(ValueError):
        configured_live_review(config)
    config["OFC_HOSTED_AGENT_PROPOSAL_PATH"] = str(proposal_path)
    configured = configured_live_review(config)
    assert configured is not None
    assert configured.state()["status"] == "READY"
    assert configured.public_result()[0] == 404
