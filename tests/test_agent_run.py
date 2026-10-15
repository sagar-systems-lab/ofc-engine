"""A frozen six-decision tour with one genuinely scoped recorded brand signal."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from ofc_engine.agent_run import commit_rank_repair, review_run
from ofc_engine.agent_run_cli import main as run_cli
from ofc_engine.contracts import ActionIntent, Decision, Verdict
from ofc_engine.durable import SQLiteRevisionStore, StorageIntegrityError
from ofc_engine.intake import AgentProposal, accept_proposal
from ofc_engine.qloo import QlooProbeClient, brand_affinity_probe
from ofc_engine.qloo_agent_bridge import AgentEvidenceRejected
from ofc_engine.qloo_flow import RecordedSource
from ofc_engine.revisions import plan_digest

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
AGE = timedelta(hours=1)
ARTIST = "artist-qloo-id"
BRANDS = ("brand-a", "brand-b", "brand-c")
SPEC = brand_affinity_probe(ARTIST, BRANDS)
ACK = "COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE"


def proposal():
    return AgentProposal(
        intent=ActionIntent(
            run_id="tour-qloo-001", client_request_id="operator-001",
            objective="Plan an emerging artist's three-city tour",
            constraints={"budget_usd": 60000, "cities": ["Chicago", "Austin", "Seattle"],
                         "external_action": False},
            taste_context={"artist_id": ARTIST, "audience": "indie music"},
        ),
        decisions=(
            Decision(decision_id="brand", domain="partner", candidate_id=BRANDS[0],
                     candidate_pool=BRANDS, mutable_fields=("candidate_id",)),
            Decision(decision_id="opener", domain="artist", candidate_id="opener-a",
                     candidate_pool=("opener-a", "opener-b")),
            Decision(decision_id="chicago", domain="venue", candidate_id="chicago-a",
                     candidate_pool=("chicago-a", "chicago-b"), locality_ref="Chicago"),
            Decision(decision_id="austin", domain="venue", candidate_id="austin-a",
                     candidate_pool=("austin-a", "austin-b"), locality_ref="Austin"),
            Decision(decision_id="seattle", domain="venue", candidate_id="seattle-a",
                     candidate_pool=("seattle-a", "seattle-b"), locality_ref="Seattle"),
            Decision(decision_id="afterparty", domain="experience",
                     candidate_id="afterparty-a", candidate_pool=("afterparty-a",),
                     depends_on=("brand",)),
        ),
    )


def capture(*, at=None, order=None, fingerprint=None, scores=None):
    ids = order or (BRANDS[1], BRANDS[0], BRANDS[2])
    vals = scores or (0.95, 0.73, 0.25)
    return {
        "summary": {
            "endpoint": SPEC.path, "http_status": 200, "real_qloo_requests": 1,
            "mode": "LIVE_RESPONSE_UNQUALIFIED",
            "query_fingerprint": fingerprint or SPEC.fingerprint,
            "utc_collected_at": (at or NOW - timedelta(minutes=15)).isoformat(),
        },
        "payload": {"success": True, "results": {
            "entities": [
                {"entity_id": name, "type": "urn:entity", "query": {
                    "affinity": val,
                    "explainability": {"signal.interests.entities": {}},
                }} for name, val in zip(ids, vals)
            ],
        }},
    }


def seeded(tmp_path):
    database = tmp_path / "tour.sqlite3"
    store = SQLiteRevisionStore(database)
    p = proposal()
    receipt = accept_proposal(store, p)
    original = RecordedSource(capture(), SPEC)
    return store, database, p, receipt, {"brand": original}


def inspect(store, p, source, **kwargs):
    return review_run(store, p, source, as_of=kwargs.pop("as_of", NOW),
                      max_evidence_age=AGE, **kwargs)


def commit(store, p, source, request_id="tour-repair-001", **kwargs):
    return commit_rank_repair(
        store, p, source, request_id=request_id,
        acknowledgement=kwargs.pop("acknowledgement", ACK),
        as_of=kwargs.pop("as_of", NOW), max_evidence_age=AGE, **kwargs,
    )


def test_six_decision_recorded_qloo_change_is_durable_and_fail_closed(tmp_path, monkeypatch):
    store, database, p, receipt, source = seeded(tmp_path)
    monkeypatch.setattr(
        QlooProbeClient, "from_environment",
        lambda: (_ for _ in ()).throw(AssertionError("Qloo traffic forbidden")),
    )
    before = inspect(store, p, source)
    assert before.plan_revision == 0
    assert before.changed_ids == ()
    assert before.comparison.same_baseline is True
    assert before.comparison.evidence_off_verdicts["brand"] == Verdict.UNCERTAIN
    assert before.comparison.evidence_on_verdicts["brand"] == Verdict.REPAIR
    assert before.release_authorized is False
    assert len(before.events) == 1

    result = commit(store, p, source)
    assert result.plan_revision == 1
    assert result.changed_ids == ("brand",)
    assert result.preserved_ids == (
        "opener", "chicago", "austin", "seattle", "afterparty",
    )
    assert result.invalidated_ids == ("brand", "afterparty")
    assert result.pending_rechecks == ("brand", "afterparty")
    assert result.decision_verdicts[0].verdict == Verdict.PASS
    assert result.decision_verdicts[-1].verdict == Verdict.UNCERTAIN
    assert result.decision_verdicts[-1].reason_codes == ("DEPENDENCY_INVALIDATED",)
    assert all(v.verdict == Verdict.UNCERTAIN for v in result.decision_verdicts[1:])
    assert result.comparison.evidence_caused_change_ids == ("brand",)
    assert result.release_state == "REVIEW_REQUIRED"
    assert result.handoff.verification_status == "REVIEW_REQUIRED"
    assert result.handoff.verified_plan_digest == result.plan_digest
    assert result.verification.status == "REVIEW_REQUIRED"
    assert result.provider_authenticated is False
    assert result.independent_recheck_completed is False
    assert result.release_authorized is False
    assert result.real_qloo_requests == 0
    assert len(result.events) == 2
    assert result.events[0].plan_digest == before.baseline_plan_digest
    assert result.events[-1].invalidated_ids == result.invalidated_ids
    assert result.plan_digest == plan_digest(store.get(p.intent.run_id))
    assert result.baseline_digest == receipt.baseline_digest

    original = source["brand"]
    later = {"brand": RecordedSource(capture(
        at=NOW - timedelta(minutes=2), scores=(0.98, 0.7, 0.15)
    ), original.spec)}
    replay = inspect(SQLiteRevisionStore(database), p, source, later_sources=later)
    assert replay.recorded_rank_recheck_pass is True
    assert replay.plan_digest == result.plan_digest
    assert replay.pending_rechecks == result.pending_rechecks
    assert "PROVIDER_INDEPENDENCE_NOT_ESTABLISHED" in replay.blockers
    assert replay.handoff.verification_status == "REVIEW_REQUIRED"
    assert replay.verification.checked_decision_ids == ("brand",)
    assert len(store.audit_history(p.intent.run_id)) == 2

    assert commit(store, p, source).model_dump() == result.model_dump()
    assert len(store.audit_history(p.intent.run_id)) == 2


def test_recorded_recheck_replay_and_wrong_rank_cannot_clear_pending(tmp_path):
    store, _, p, _, source = seeded(tmp_path)
    commit(store, p, source)
    same = inspect(store, p, source, later_sources=source)
    assert same.recorded_rank_recheck_pass is False
    assert "RECHECK_NOT_DISTINCT" in same.blockers
    incorrect = {"brand": RecordedSource(capture(
        at=NOW - timedelta(minutes=1), order=(BRANDS[0], BRANDS[1], BRANDS[2])
    ), SPEC)}
    mismatch = inspect(store, p, source, later_sources=incorrect)
    assert mismatch.recorded_rank_recheck_pass is False
    assert "RANK_RECHECK_UNRESOLVED" in mismatch.blockers
    assert store.pending_rechecks(p.intent.run_id) == ("brand", "afterparty")
    assert mismatch.release_authorized is False


@pytest.mark.parametrize("bad", ["wrong_fingerprint", "tie", "stale", "missing", "unscoped"])
def test_bad_original_evidence_never_causes_repair(tmp_path, bad):
    store, _, p, _, source = seeded(tmp_path)
    spec = SPEC
    raw = capture()
    if bad == "wrong_fingerprint":
        raw = capture(fingerprint="e" * 64)
    elif bad == "tie":
        raw = capture(scores=(0.91, 0.91, 0.2))
    elif bad == "stale":
        raw = capture(at=NOW - timedelta(days=3))
    elif bad == "missing":
        raw["payload"]["results"]["entities"].pop()
    elif bad == "unscoped":
        spec = brand_affinity_probe(ARTIST, ("brand-x", "brand-y"))
    evidence = {"brand": RecordedSource(raw, spec)}
    if bad == "unscoped":
        with pytest.raises(AgentEvidenceRejected):
            commit(store, p, evidence)
    else:
        with pytest.raises(AgentEvidenceRejected):
            commit(store, p, evidence)
    assert store.get(p.intent.run_id).revision == 0
    assert not store.pending_rechecks(p.intent.run_id)
    assert len(store.audit_history(p.intent.run_id)) == 1


def test_unsupported_locality_brand_scope_fails_closed(tmp_path):
    store, _, p, _, source = seeded(tmp_path)
    adjusted = p.model_copy(update={"decisions": (
        p.decisions[0].model_copy(update={"locality_ref": "Chicago"}),
        *p.decisions[1:],
    )})
    with pytest.raises(AgentEvidenceRejected):
        commit(store, adjusted, source)


def test_request_id_conflict_and_different_plan_cannot_reuse_repair(tmp_path):
    store, _, p, _, source = seeded(tmp_path)
    with pytest.raises(AgentEvidenceRejected):
        commit(store, p, source, acknowledgement="NO")
    assert commit(store, p, source).plan_revision == 1
    with pytest.raises(AgentEvidenceRejected):
        commit(store, p, source, request_id="another-key")
    with pytest.raises(AgentEvidenceRejected):
        altered = p.model_copy(update={"intent": p.intent.model_copy(update={
            "constraints": {"budget_usd": 1}
        })})
        inspect(store, altered, source)
    assert len(store.audit_history(p.intent.run_id)) == 2


def test_journal_tampering_fails_closed(tmp_path):
    store, database, p, _, source = seeded(tmp_path)
    commit(store, p, source)
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE events SET event_payload='{}' WHERE plan_id=? AND sequence=1",
                     (p.intent.run_id,))
    with pytest.raises(StorageIntegrityError):
        inspect(store, p, source)


def test_concurrent_repair_has_single_revision(tmp_path):
    store, database, p, _, source = seeded(tmp_path)
    def apply(_):
        return commit(SQLiteRevisionStore(database), p, source).plan_revision
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(apply, i) for i in range(2)]
    results = []
    for attempt in attempts:
        try:
            results.append(attempt.result())
        except AgentEvidenceRejected:
            pass
    assert results
    assert all(x == 1 for x in results)
    assert store.get(p.intent.run_id).revision == 1
    assert len(store.audit_history(p.intent.run_id)) == 2


def test_cli_reads_private_files_and_never_calls_qloo(tmp_path, monkeypatch, capsys):
    store, database, p, _, source = seeded(tmp_path)
    monkeypatch.setattr(
        QlooProbeClient, "from_environment",
        lambda: (_ for _ in ()).throw(AssertionError("Qloo traffic forbidden")),
    )
    proposal_file = tmp_path / "proposal.json"
    source_file = tmp_path / "original.json"
    later_file = tmp_path / "later.json"
    proposal_file.write_text(p.model_dump_json())
    wall_clock = datetime.now(timezone.utc)
    source_file.write_text(json.dumps(capture(
        at=wall_clock - timedelta(hours=5)
    )))
    later_file.write_text(json.dumps(capture(
        at=wall_clock - timedelta(hours=2), scores=(0.96, 0.72, 0.24)
    )))
    args = [
        "--database", str(database), "--proposal", str(proposal_file),
        "--original-capture", str(source_file), "--artist-id", ARTIST,
        "--brands", ",".join(BRANDS),
    ]
    assert run_cli(args + ["--historical"]) == 0
    frozen = json.loads(capsys.readouterr().out)
    assert frozen["plan_revision"] == 0 and frozen["release_authorized"] is False
    assert run_cli(args + ["--historical", "--commit",
                           "--request-id", "id"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    assert store.get(p.intent.run_id).revision == 0


def test_fresh_later_rank_cannot_rescue_expired_original(tmp_path):
    store, _, p, _, sources = seeded(tmp_path)
    commit(store, p, sources)
    later = {"brand": RecordedSource(capture(
        at=NOW + timedelta(hours=2, minutes=57),
        scores=(0.96, 0.72, 0.21),
    ), SPEC)}
    result = inspect(
        store, p, sources, as_of=NOW + timedelta(hours=3), later_sources=later,
    )
    assert result.recorded_rank_recheck_pass is False
    assert "ORIGINAL_EVIDENCE_UNQUALIFIED" in result.blockers
    assert result.verification.status == "REVIEW_REQUIRED"
    assert result.release_authorized is False


def test_authenticated_agent_snapshot_exposes_six_decisions_without_private_captures(
    tmp_path,
):
    from threading import Thread
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from ofc_engine.agent_api import AgentAPIServer
    from ofc_engine.agent_evidence_session import OperatorEvidenceSession

    store, database, p, _, sources = seeded(tmp_path)
    commit(store, p, sources)
    path = tmp_path / "proposal.json"
    path.write_text(p.model_dump_json())
    original = tmp_path / "original.json"
    original.write_text(json.dumps(sources["brand"].capture))
    index = tmp_path / "sources.json"
    index.write_text(json.dumps({"decisions": {"brand": {
        "artist_id": ARTIST, "brand_ids": list(BRANDS),
        "capture_file": str(original),
    }}}))
    session = OperatorEvidenceSession.load(
        proposal_path=path, evidence_index=index,
    )
    token = "operator-only-" + "a" * 40
    server = AgentAPIServer(
        ("127.0.0.1", 0), database=str(database),
        auth_token=token, evidence_session=session,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/api/proposals/{p.intent.run_id}/snapshot"
    try:
        with urlopen(Request(url, headers={
            "Authorization": "Bearer " + token,
        }), timeout=5) as response:
            assert response.status == 200
            body = response.read()
        report = json.loads(body)
        assert report["run_id"] == p.intent.run_id
        assert len(report["decision_verdicts"]) == 6
        assert report["changed_ids"] == ["brand"]
        assert report["invalidated_ids"] == ["brand", "afterparty"]
        assert report["handoff"]["verification_status"] == "REVIEW_REQUIRED"
        assert report["release_authorized"] is False
        assert ARTIST.encode() not in body
        assert "query.affinity" not in body.decode()
        with pytest.raises(HTTPError) as exc:
            urlopen(url, timeout=5)
        assert exc.value.code == 401
        with pytest.raises(HTTPError) as exc:
            urlopen(Request(url, headers={
                "Authorization": "Bearer " + token,
                "Origin": "https://hostile.example",
            }), timeout=5)
        assert exc.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert len(store.audit_history(p.intent.run_id)) == 2


def test_artist_signal_cannot_be_swapped_across_frozen_agent_context(tmp_path):
    store, _, p, _, _ = seeded(tmp_path)
    mismatched = brand_affinity_probe("other-artist", BRANDS)
    wrong = {"brand": RecordedSource(capture(
        fingerprint=mismatched.fingerprint
    ), mismatched)}
    with pytest.raises(AgentEvidenceRejected, match="ARTIST_EVIDENCE_SCOPE_MISMATCH"):
        commit(store, p, wrong)
    assert store.get(p.intent.run_id).revision == 0
