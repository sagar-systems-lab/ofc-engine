import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ofc_engine.contracts import ActionIntent, Decision
from ofc_engine.durable import SQLiteRevisionStore
from ofc_engine.intake import AgentProposal, accept_proposal
from ofc_engine.qloo import (
    QlooProbeClient, QlooProbeError, TransportReply, brand_affinity_probe,
)
from ofc_engine.qloo_live_recheck import (
    LiveRecheckFailure, _preflight, main, run_live_recheck,
)
from ofc_engine.qloo_recorded_commit import commit_recorded_repair
from ofc_engine.qloo_recorded_recheck import inspect_recorded_recheck


ARTIST = "artist-a"
BRANDS = ("brand-a", "brand-b", "brand-c")
SPEC = brand_affinity_probe(ARTIST, BRANDS)
ACK_COMMIT = "COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE"
ACK_LIVE = "EXACTLY_ONE_QLOO_RECHECK_REQUEST"


def proposal():
    return AgentProposal(
        intent=ActionIntent(
            run_id="fresh-recheck-001", client_request_id="intake-001",
            objective="Evaluate brand replacement after commit",
            constraints={"external_action": False},
            taste_context={"audience": "operator-provided"},
        ),
        decisions=(Decision(
            decision_id="brand", domain="partner", candidate_id=BRANDS[0],
            candidate_pool=BRANDS, mutable_fields=("candidate_id",),
        ),),
    )


def capture(collected, *, winner=BRANDS[1]):
    order = (winner, BRANDS[0], BRANDS[2]) if winner == BRANDS[1] else (
        winner, BRANDS[1], BRANDS[2],
    )
    return {
        "summary": {
            "endpoint": SPEC.path, "http_status": 200,
            "real_qloo_requests": 1, "mode": "LIVE_RESPONSE_UNQUALIFIED",
            "query_fingerprint": SPEC.fingerprint,
            "utc_collected_at": collected.isoformat(),
        },
        "payload": {
            "success": True,
            "results": {"entities": [
                {"entity_id": entity_id, "type": "urn:entity", "query": {
                    "affinity": round(0.9 - rank * 0.2, 2),
                    "explainability": {"signal.interests.entities": {}}}}
                for rank, entity_id in enumerate(order)
            ]},
        },
    }


def seeded(tmp_path):
    db = tmp_path / "agent.sqlite3"
    store = SQLiteRevisionStore(db)
    p = proposal()
    accept_proposal(store, p)
    now = datetime.now(timezone.utc)
    original = capture(now - timedelta(minutes=20))
    commit_recorded_repair(
        store, p, original,
        decision_id="brand", artist_id=ARTIST, brand_ids=BRANDS,
        request_id="rank-commit-001", acknowledgement=ACK_COMMIT,
        as_of=now, max_evidence_age=timedelta(hours=1),
    )
    return db, store, p, original


class LocalTransport:
    def __init__(self, payload, *, reject=False):
        self.payload = payload
        self.reject = reject
        self.calls = 0
        self.last_url = None

    def get(self, url, headers, *, timeout, max_bytes):
        self.calls += 1
        self.last_url = url
        assert headers["X-Api-Key"] == "test-key"
        assert timeout <= 20
        assert max_bytes <= 512 * 1024
        if self.reject:
            raise QlooProbeError("NETWORK_ERROR")
        return TransportReply(200, json.dumps(self.payload).encode())


def fresh_client(*, winner=BRANDS[1], reject=False):
    fresh = capture(datetime.now(timezone.utc), winner=winner)
    transport = LocalTransport(fresh["payload"], reject=reject)
    return QlooProbeClient(api_key="test-key", transport=transport), transport


def invoke(store, p, original, client, tmp_path, *, approve=ACK_LIVE, reserve=None):
    captured = []
    reserved = []
    def write(payload, summary):
        captured.append({"summary": summary, "payload": payload})
        return tmp_path / "private-capture.json"
    def once(key):
        reserved.append(key)
        if reserve is not None:
            return reserve(key)
        return tmp_path / "attempt"
    result = run_live_recheck(
        store, p, original, decision_id="brand", artist_id=ARTIST,
        brand_ids=BRANDS, acknowledgement=approve,
        as_of=datetime.now(timezone.utc),
        max_evidence_age=timedelta(hours=1), client=client,
        reserve_attempt=once, write_capture=write,
    )
    return result, captured, reserved


def test_fresh_qloo_request_is_revision_bound_and_never_grants_release(tmp_path):
    db, store, p, original = seeded(tmp_path)
    client, transport = fresh_client()
    result, captured, reserved = invoke(
        SQLiteRevisionStore(db), p, original, client, tmp_path
    )
    assert transport.calls == 1
    assert transport.last_url.startswith("https://hackathon.api.qloo.com/v2/insights?")
    assert len(captured) == len(reserved) == 1
    assert captured[0]["summary"]["mode"] == "LIVE_RESPONSE_UNQUALIFIED"
    assert result["status"] == "FRESH_QLOO_RANK_PASS_REVIEW_PENDING"
    assert result["fresh_provider_request_observed"] is True
    assert result["request_attempts_this_run"] == 1
    assert result["real_qloo_requests"] == 1
    assert result["pending_recheck_ids"] == ["brand"]
    assert result["pending_recheck_cleared"] is False
    assert result["provider_authenticated"] is False
    assert result["independent_recheck_completed"] is False
    assert result["release_authorized"] is False
    assert result["external_action_executed"] is False
    assert store.get(p.intent.run_id).revision == 1
    assert store.pending_rechecks(p.intent.run_id) == ("brand",)
    assert len(store.audit_history(p.intent.run_id)) == 2
    assert '"affinity":' not in json.dumps(result)


def test_one_attempt_reservation_is_scoped_to_revision_not_request_id(tmp_path, monkeypatch):
    _, store, p, original = seeded(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from ofc_engine.qloo_operator import _reserve_once
    first, transport = fresh_client()
    run_live_recheck(
        store, p, original, decision_id="brand",
        artist_id=ARTIST, brand_ids=BRANDS, acknowledgement=ACK_LIVE,
        as_of=datetime.now(timezone.utc), max_evidence_age=timedelta(hours=1),
        client=first, reserve_attempt=_reserve_once,
        write_capture=lambda payload, summary: tmp_path / "private.json",
    )
    assert transport.calls == 1
    second, second_transport = fresh_client()
    with pytest.raises(ValueError, match="REQUEST_ALREADY_RESERVED"):
        run_live_recheck(
            store, p, original, decision_id="brand",
            artist_id=ARTIST, brand_ids=BRANDS, acknowledgement=ACK_LIVE,
            as_of=datetime.now(timezone.utc), max_evidence_age=timedelta(hours=1),
            client=second, reserve_attempt=_reserve_once,
            write_capture=lambda payload, summary: tmp_path / "private.json",
        )
    assert second_transport.calls == 0


def test_transport_failure_keeps_attempt_reservation_and_pending_state(tmp_path, monkeypatch):
    _, store, p, original = seeded(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from ofc_engine.qloo_operator import _reserve_once
    client, transport = fresh_client(reject=True)
    with pytest.raises(LiveRecheckFailure, match="QLOO_NETWORK_ERROR") as exc:
        run_live_recheck(
            store, p, original, decision_id="brand",
            artist_id=ARTIST, brand_ids=BRANDS, acknowledgement=ACK_LIVE,
            as_of=datetime.now(timezone.utc), max_evidence_age=timedelta(hours=1),
            client=client, reserve_attempt=_reserve_once,
        )
    assert exc.value.request_attempted is True
    assert transport.calls == 1
    marker = tmp_path / ".local/share/ofc-engine/private-qloo-attempts"
    assert len(list(marker.glob("*.json"))) == 1
    assert store.pending_rechecks(p.intent.run_id) == ("brand",)


def test_missing_acknowledgement_never_reserves_or_calls_provider(tmp_path):
    _, store, p, original = seeded(tmp_path)
    client, transport = fresh_client()
    with pytest.raises(ValueError, match="EXPLICIT_LIVE_RECHECK_APPROVAL_REQUIRED"):
        invoke(store, p, original, client, tmp_path, approve="")
    assert transport.calls == 0


def test_without_committed_repair_live_probe_is_blocked(tmp_path):
    store = SQLiteRevisionStore(tmp_path / "uncommitted.sqlite3")
    p = proposal()
    accept_proposal(store, p)
    client, transport = fresh_client()
    with pytest.raises(ValueError, match="STORED_REPAIR_STATE_MISMATCH"):
        invoke(store, p, capture(datetime.now(timezone.utc) - timedelta(minutes=10)), client, tmp_path)
    assert transport.calls == 0


def test_recheck_with_changed_rank_is_unresolved_not_approved(tmp_path):
    _, store, p, original = seeded(tmp_path)
    client, transport = fresh_client(winner=BRANDS[0])
    result, _, _ = invoke(store, p, original, client, tmp_path)
    assert transport.calls == 1
    assert result["status"] == "FRESH_QLOO_RANK_UNRESOLVED"
    assert result["recorded_rank_verdict"] == "REPAIR"
    assert result["pending_recheck_cleared"] is False
    assert result["release_authorized"] is False


def test_public_cli_dry_run_has_zero_requests_without_key(tmp_path, capsys, monkeypatch):
    db, store, p, original = seeded(tmp_path)
    proposal_path = tmp_path / "proposal.json"
    original_path = tmp_path / "original.json"
    proposal_path.write_text(p.model_dump_json())
    original_path.write_text(json.dumps(original))
    monkeypatch.setattr(
        QlooProbeClient, "from_environment",
        lambda: (_ for _ in ()).throw(AssertionError("no provider read in dry-run"))
    )
    args = [
        "--database", str(db), "--proposal", str(proposal_path),
        "--original-capture", str(original_path), "--decision-id", "brand",
        "--artist-id", ARTIST, "--brands", ",".join(BRANDS),
    ]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "LIVE_RECHECK_READY_NO_NETWORK"
    assert result["release_authorized"] is False
    assert result["real_qloo_requests"] == 0
    assert store.pending_rechecks(p.intent.run_id) == ("brand",)
    assert main([*args, "--live"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"


def test_bad_capture_blocks_preflight_before_network(tmp_path):
    _, store, p, original = seeded(tmp_path)
    original["summary"]["query_fingerprint"] = "f" * 64
    client, transport = fresh_client()
    with pytest.raises(ValueError):
        invoke(store, p, original, client, tmp_path)
    assert transport.calls == 0


def test_historical_original_reference_can_preflight_without_new_request(tmp_path):
    _, store, p, original = seeded(tmp_path)
    original["summary"]["utc_collected_at"] = (
        datetime.now(timezone.utc) - timedelta(days=6)
    ).isoformat()
    spec, before, reservation = _preflight(
        store, p, original, decision_id="brand", artist_id=ARTIST,
        brand_ids=BRANDS, as_of=datetime.now(timezone.utc),
        max_evidence_age=timedelta(hours=24),
    )
    assert spec == SPEC
    assert before["historical_original_reference"] is True
    assert before["status"] == "ORIGINAL_CAPTURE_REPLAY_ONLY"
    assert before["pending_recheck_ids"] == ["brand"]
    assert len(reservation) == 64
    assert store.pending_rechecks(p.intent.run_id) == ("brand",)


def test_historical_original_with_new_mock_response_remains_blocked(tmp_path):
    _, store, p, original = seeded(tmp_path)
    original["summary"]["utc_collected_at"] = (
        datetime.now(timezone.utc) - timedelta(days=6)
    ).isoformat()
    client, transport = fresh_client()
    result, captured, reserved = invoke(store, p, original, client, tmp_path)
    assert transport.calls == 1
    assert len(captured) == len(reserved) == 1
    assert result["historical_original_reference"] is True
    assert result["status"] == "FRESH_QLOO_RANK_PASS_REVIEW_PENDING"
    assert result["provider_authenticated"] is False
    assert result["independent_recheck_completed"] is False
    assert result["pending_recheck_cleared"] is False
    assert result["release_authorized"] is False
    assert store.pending_rechecks(p.intent.run_id) == ("brand",)


def test_future_original_capture_rejected_before_request(tmp_path):
    _, store, p, original = seeded(tmp_path)
    original["summary"]["utc_collected_at"] = (
        datetime.now(timezone.utc) + timedelta(days=1)
    ).isoformat()
    client, transport = fresh_client()
    with pytest.raises(ValueError, match="ORIGINAL_CAPTURE_FROM_FUTURE"):
        invoke(store, p, original, client, tmp_path)
    assert transport.calls == 0
