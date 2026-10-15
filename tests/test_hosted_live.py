"""No-network qualification of explicit server-side Qloo inspection."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import stat
import json
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ofc_engine.hosted_live import HostedLiveReview, configured_live_review
from ofc_engine.hosted_attempt import HostedAttemptLedger
from ofc_engine.hosted_preview import HostedFixtureServer
from ofc_engine.qloo import (
    HOST, QlooProbeClient, QlooProbeError, TransportReply,
)

ARTIST = "fixed-artist-id"
POOL = ("brand-first", "brand-second", "brand-third")
KEY = "fake-ci-key-never-real"
OPERATOR = "operator-demo-key-" + "x" * 40


class FakeTransport:
    def __init__(self, *, error=None):
        self.calls = []
        self.error = error

    def get(self, url, headers, *, timeout, max_bytes):
        self.calls.append((url, headers, timeout, max_bytes))
        if self.error is not None:
            raise self.error
        payload = {"success": True, "results": {"entities": [
            {
                "entity_id": name, "type": "urn:entity",
                "query": {"affinity": score,
                          "explainability": {"signal.interests.entities": {}}},
            }
            for name, score in zip(
                (POOL[1], POOL[0], POOL[2]), (0.93, 0.71, 0.28)
            )
        ]}}
        return TransportReply(200, json.dumps(payload).encode())


def live(transport=None):
    transport = transport or FakeTransport()
    controller = HostedLiveReview(
        api_key=KEY, artist_id=ARTIST, brand_ids=POOL,
        selected_brand_id=POOL[0],
        client=QlooProbeClient(api_key=KEY, transport=transport),
        operator_token=OPERATOR,
    )
    return controller, transport


def http(base, path, *, body=None, origin=None, method="GET", headers=None):
    values = dict(headers or {})
    if origin:
        values["Origin"] = origin
    if body is not None:
        values["Content-Type"] = "application/json"
    try:
        with urlopen(Request(
            base + path, method=method, headers=values,
            data=json.dumps(body).encode() if body is not None else None,
        ), timeout=5) as result:
            return result.status, result.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def test_disabled_by_default_even_if_qloo_key_exists():
    assert configured_live_review({"QLOO_API_KEY": KEY}) is None
    for env in (
        {"OFC_HOSTED_QLOO_MODE": "ONE_REQUEST_EXPLICITLY_APPROVED"},
        {"OFC_HOSTED_QLOO_MODE": "ON", "QLOO_API_KEY": KEY},
        {"OFC_HOSTED_QLOO_ARTIST_ID": ARTIST},
        {"OFC_HOSTED_QLOO_MODE": "ONE_REQUEST_EXPLICITLY_APPROVED",
         "QLOO_API_KEY": KEY, "OFC_HOSTED_QLOO_ARTIST_ID": ARTIST,
         "OFC_HOSTED_QLOO_BRAND_IDS": ",".join(POOL),
         "OFC_HOSTED_QLOO_SELECTED_ID": "not-in-pool"},
    ):
        with pytest.raises(ValueError):
            configured_live_review(env)


def test_single_bounded_live_request_is_rank_only_and_cached():
    controller, fake = live()
    assert controller.state()["requests_remaining_this_process"] == 1
    code, result = controller.observe()
    assert code == 200
    assert result["status"] == "LIVE_RANK_OBSERVED"
    assert result["source_mode"] == "LIVE_HTTP_UNVERIFIED"
    assert result["verdict"] == "RANK_REPAIR_SUGGESTED"
    assert result["evidence_off"] == "UNCERTAIN"
    assert result["evidence_on"] == "REPAIR"
    assert result["candidate_count"] == 3 and result["baseline_rank"] == 2
    assert result["requests_this_call"] == 1
    assert result["release_authorized"] is False
    assert result["overall_plan_verified"] is False
    assert result["provider_authenticated"] is False
    assert result["independent_recheck_completed"] is False
    assert result["external_action_executed"] is False
    assert result["historical_replay"] is False
    assert datetime.fromisoformat(result["collected_at"]).utcoffset() is not None
    assert len(fake.calls) == 1
    url, headers, _, max_bytes = fake.calls[0]
    assert url.startswith(HOST + "/v2/insights?")
    assert headers["X-Api-Key"] == KEY
    assert max_bytes <= 1024 * 1024
    status, saved = controller.observe()
    assert status == 200 and saved["cached"] is True
    assert saved["requests_this_call"] == 0
    assert len(fake.calls) == 1
    assert controller.state()["requests_remaining_this_process"] == 0
    public = json.dumps(saved)
    for secret in (KEY, ARTIST, *POOL, "affinity"):
        assert secret not in public


def test_provider_failure_consumes_attempt_without_auto_retry():
    controller, fake = live(FakeTransport(error=QlooProbeError("HTTP_429")))
    code, result = controller.observe()
    assert code == 503
    assert result["status"] == "BLOCKED"
    assert result["attempt_outcome_unknown"] is True
    assert result["requests_this_call"] == 1
    assert len(fake.calls) == 1
    code, second = controller.observe()
    assert code == 503 and second["requests_this_call"] == 0
    assert len(fake.calls) == 1


def test_concurrent_calls_cannot_allocate_two_attempts():
    started, release = Event(), Event()

    class DelayedTransport(FakeTransport):
        def get(self, url, headers, *, timeout, max_bytes):
            started.set()
            assert release.wait(5)
            return super().get(url, headers, timeout=timeout, max_bytes=max_bytes)

    fake = DelayedTransport()
    controller, _ = live(fake)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(controller.observe)
        assert started.wait(5)
        blocked, error = controller.observe()
        assert blocked == 409 and error["reason"] == "REQUEST_ALREADY_RESERVED"
        release.set()
        code, _ = first.result(timeout=5)
        assert code == 200 and len(fake.calls) == 1


def test_hosted_http_requires_browser_consent_and_never_leaks_key():
    controller, fake = live()
    with HostedFixtureServer(("127.0.0.1", 0), hosted_live=controller) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        try:
            status, page = http(base, "/")
            assert status == 200
            assert b'/hosted-live.js' in page
            status, script = http(base, "/hosted-live.js")
            assert status == 200
            assert b"SPEND_ONE_HOSTED_QLOO_REQUEST" not in script
            assert b"window.prompt" not in script
            assert b"agent_run" in script
            status, state = http(base, "/api/qloo/status")
            assert status == 200
            assert json.loads(state)["requests_remaining_this_process"] == 1
            assert len(fake.calls) == 0
            request = {"confirm": "SPEND_ONE_HOSTED_QLOO_REQUEST"}
            assert http(base, "/api/qloo/live-rank",
                        method="POST", body=request)[0] == 403
            assert http(base, "/api/qloo/live-rank", method="POST", body=request,
                        origin="https://attacker.example")[0] == 403
            assert http(base, "/api/qloo/live-rank", method="POST",
                        body={"confirm": "wrong"}, origin=base)[0] == 400
            assert len(fake.calls) == 0
            assert http(base, "/api/qloo/live-rank", method="POST",
                        body=request, origin=base)[0] == 403
            status, raw = http(base, "/api/qloo/live-rank",
                               method="POST", body=request, origin=base,
                               headers={"X-OFC-Operator-Token": OPERATOR})
            report = json.loads(raw)
            assert status == 200 and report["status"] == "LIVE_RANK_OBSERVED"
            status, public_result = http(base, "/api/qloo/result")
            assert status == 200 and json.loads(public_result)["baseline_rank"] == 2
            status, cached = http(base, "/api/qloo/live-rank",
                                  method="POST", body=request, origin=base,
                                  headers={"X-OFC-Operator-Token": OPERATOR})
            assert status == 200 and json.loads(cached)["cached"] is True
            assert len(fake.calls) == 1
            status, synthetic = http(base, "/api/demo?scenario=repair")
            assert status == 200 and json.loads(synthetic)["source_mode"] == "SYNTHETIC"
            for raw_data in (page, script, state, raw, cached, public_result, synthetic):
                for secret in (KEY, ARTIST, *POOL):
                    assert secret.encode() not in raw_data
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_public_preview_stays_keyless_read_only_without_enablement():
    with HostedFixtureServer(("127.0.0.1", 0)) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        try:
            status, root = http(base, "/")
            assert status == 200 and b"/hosted-live.js" not in root
            assert http(base, "/api/qloo/status")[0] == 404
            assert http(base, "/api/qloo/result")[0] == 404
            assert http(base, "/api/qloo/live-rank", method="POST",
                        body={"confirm": "SPEND_ONE_HOSTED_QLOO_REQUEST"},
                        origin=base)[0] == 405
        finally:
            server.shutdown()
            thread.join(timeout=5)


def ledger_at(tmp_path: Path, *, scope: str = "a" * 64):
    folder = tmp_path / "secure"
    folder.mkdir(mode=0o700, exist_ok=True)
    return HostedAttemptLedger(folder / "qloo.sqlite3", scope=scope)


def test_durable_live_observation_survives_restart_without_second_request(tmp_path):
    first = ledger_at(tmp_path)
    fake = FakeTransport()
    controller = HostedLiveReview(
        api_key=KEY, artist_id=ARTIST, brand_ids=POOL,
        selected_brand_id=POOL[0], ledger=first, operator_token=OPERATOR,
        client=QlooProbeClient(api_key=KEY, transport=fake),
    )
    assert controller.state()["status"] == "READY"
    assert stat.S_IMODE(first.path.stat().st_mode) == 0o600
    status, result = controller.observe()
    assert status == 200 and result["baseline_rank"] == 2
    assert len(fake.calls) == 1
    restarted = HostedLiveReview(
        api_key=KEY, artist_id=ARTIST, brand_ids=POOL,
        selected_brand_id=POOL[0], ledger=ledger_at(tmp_path),
        operator_token=OPERATOR,
        client=QlooProbeClient(api_key=KEY, transport=FakeTransport(
            error=AssertionError("restart must never call provider")
        )),
    )
    assert restarted.state()["status"] == "LIVE_RANK_OBSERVED"
    assert restarted.state()["requests_remaining_this_process"] == 0
    status, cached = restarted.public_result()
    assert status == 200 and cached["cached"] is True
    assert cached["requests_this_call"] == 0
    status, blocked = restarted.observe()
    assert status == 200 and blocked["cached"] is True
    assert blocked["requests_this_call"] == 0
    for value in (ARTIST, KEY, *POOL, OPERATOR):
        assert value not in json.dumps(cached)


def test_reserved_call_cannot_be_refunded_after_crash_or_process_restart(tmp_path):
    ledger = ledger_at(tmp_path)
    assert ledger.reserve() is True
    assert ledger_at(tmp_path).reserve() is False
    fake = FakeTransport()
    second = HostedLiveReview(
        api_key=KEY, artist_id=ARTIST, brand_ids=POOL,
        selected_brand_id=POOL[0], ledger=ledger_at(tmp_path),
        operator_token=OPERATOR,
        client=QlooProbeClient(api_key=KEY, transport=fake),
    )
    assert second.state()["status"] == "SPENT"
    code, result = second.observe()
    assert code == 409 and result["reason"] == "REQUEST_ALREADY_RESERVED"
    assert len(fake.calls) == 0
    assert second.public_result()[0] == 404


def test_cross_controller_race_reserves_only_one_provider_call(tmp_path):
    start, finish = Event(), Event()

    class BlockingTransport(FakeTransport):
        def get(self, url, headers, *, timeout, max_bytes):
            start.set()
            assert finish.wait(5)
            return super().get(url, headers, timeout=timeout, max_bytes=max_bytes)

    first_transport = BlockingTransport()
    second_transport = FakeTransport()
    def controller(transport):
        return HostedLiveReview(
            api_key=KEY, artist_id=ARTIST, brand_ids=POOL,
            selected_brand_id=POOL[0], ledger=ledger_at(tmp_path),
            operator_token=OPERATOR,
            client=QlooProbeClient(api_key=KEY, transport=transport),
        )

    first, second = controller(first_transport), controller(second_transport)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(first.observe)
        assert start.wait(5)
        code, result = second.observe()
        assert code == 409 and result["reason"] == "REQUEST_ALREADY_RESERVED"
        finish.set()
        assert future.result(timeout=5)[0] == 200
    assert len(first_transport.calls) == 1
    assert len(second_transport.calls) == 0
    assert second.public_result()[0] == 200


def test_durable_ledger_rejects_changed_scope_and_unprotected_folder(tmp_path):
    ledger_at(tmp_path)
    with pytest.raises(ValueError, match="another frozen Qloo scope"):
        ledger_at(tmp_path, scope="b" * 64)
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    unsafe.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        HostedAttemptLedger(unsafe / "qloo.sqlite3", scope="a" * 64)


def test_hosted_configuration_needs_explicit_persistent_ledger_and_operator_secret(tmp_path):
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    environment = {
        "OFC_HOSTED_QLOO_MODE": "ONE_REQUEST_EXPLICITLY_APPROVED",
        "QLOO_API_KEY": KEY,
        "OFC_HOSTED_QLOO_ARTIST_ID": ARTIST,
        "OFC_HOSTED_QLOO_BRAND_IDS": ",".join(POOL),
        "OFC_HOSTED_QLOO_SELECTED_ID": POOL[0],
    }
    with pytest.raises(ValueError):
        configured_live_review(environment)
    environment["OFC_HOSTED_OPERATOR_TOKEN"] = OPERATOR
    with pytest.raises(ValueError):
        configured_live_review(environment)
    environment["OFC_HOSTED_QLOO_LEDGER_PATH"] = str(folder / "reservation.sqlite3")
    from ofc_engine.contracts import ActionIntent, Decision
    from ofc_engine.intake import AgentProposal
    proposal_file = folder / "proposal.json"
    proposal = AgentProposal(
        intent=ActionIntent(
            run_id="fixed-agent-plan-001", client_request_id="fixed-agent-proposal-001",
            objective="Review tour decisions against artist taste",
            constraints={"external_action": False},
            taste_context={"artist_id": ARTIST},
        ),
        decisions=(
            Decision(decision_id="brand", domain="partner", candidate_id=POOL[0],
                     candidate_pool=POOL, mutable_fields=("candidate_id",)),
            Decision(decision_id="opener", domain="artist", candidate_id="opener-x",
                     candidate_pool=("opener-x",)),
            Decision(decision_id="chicago", domain="venue", candidate_id="venue-x",
                     candidate_pool=("venue-x",), locality_ref="Chicago"),
            Decision(decision_id="austin", domain="venue", candidate_id="venue-y",
                     candidate_pool=("venue-y",), locality_ref="Austin"),
            Decision(decision_id="seattle", domain="venue", candidate_id="venue-z",
                     candidate_pool=("venue-z",), locality_ref="Seattle"),
            Decision(decision_id="afterparty", domain="experience", candidate_id="event-x",
                     candidate_pool=("event-x",), depends_on=("brand",)),
        ),
    )
    proposal_file.write_text(proposal.model_dump_json())
    proposal_file.chmod(0o600)
    environment["OFC_HOSTED_AGENT_PROPOSAL_PATH"] = str(proposal_file)
    enabled = configured_live_review(environment)
    assert enabled is not None
    assert enabled.state()["status"] == "READY"
    assert enabled.authorize(OPERATOR)
    assert not enabled.authorize("not-valid")
    assert not enabled.authorize(KEY)
