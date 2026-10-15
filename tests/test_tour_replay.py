"""Private tour replay acceptance without contacting a Qloo endpoint."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ofc_engine.contracts import ActionIntent, Decision
from ofc_engine.intake import AgentProposal
from ofc_engine.qloo import QlooProbeClient, brand_affinity_probe
from ofc_engine.tour_replay import (
    TourReplayRejected, build_tour_proposal, prepare_tour_replay,
)

ARTIST = "replay-artist"
BRANDS = ("alpha", "beta", "gamma")
SPEC = brand_affinity_probe(ARTIST, BRANDS)


def proposal():
    return AgentProposal(
        intent=ActionIntent(
            run_id="original-brand-001", client_request_id="initial-001",
            objective="Review artist partner choice",
            constraints={"external_action": False},
            taste_context={"artist": ARTIST},
        ),
        decisions=(Decision(
            decision_id="brand", domain="partner", candidate_id=BRANDS[0],
            candidate_pool=BRANDS, mutable_fields=("candidate_id",),
        ),),
    )


def capture(at, *, order=None, fingerprint=None):
    choices = order or (BRANDS[1], BRANDS[0], BRANDS[2])
    return {
        "summary": {
            "endpoint": "/v2/insights", "http_status": 200,
            "real_qloo_requests": 1, "mode": "LIVE_RESPONSE_UNQUALIFIED",
            "query_fingerprint": fingerprint or SPEC.fingerprint,
            "utc_collected_at": at.isoformat(),
        },
        "payload": {
            "success": True,
            "results": {"entities": [{
                "entity_id": name,
                "type": "urn:entity",
                "query": {
                    "affinity": 0.94 - position * 0.18,
                    "explainability": {"signal.interests.entities": {}},
                },
            } for position, name in enumerate(choices)]},
        },
    }


def inputs(tmp_path, *, first=None, later=None):
    now = datetime.now(timezone.utc)
    paths = tuple(tmp_path / name for name in ("source-plan.json", "first.json", "later.json"))
    paths[0].write_text(proposal().model_dump_json())
    paths[1].write_text(json.dumps(first if first is not None else capture(now - timedelta(hours=4))))
    paths[2].write_text(json.dumps(later if later is not None else capture(now - timedelta(hours=1))))
    return paths


def prepare(paths, **extra):
    return prepare_tour_replay(
        original_proposal=paths[0], original_capture=paths[1],
        later_capture=paths[2], artist_id=ARTIST, brand_ids=BRANDS,
        port=0, **extra,
    )


def test_six_decisions_replay_prior_agent_brand_as_private_historical_product(
    tmp_path, monkeypatch,
):
    from ofc_engine.durable import SQLiteRevisionStore

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        QlooProbeClient, "from_environment",
        lambda: (_ for _ in ()).throw(AssertionError("Qloo request forbidden")),
    )
    paths = inputs(tmp_path)
    workspace, folder = prepare(paths)
    assert workspace.rank_status == "RECORDED_RANK_RECHECK_PASS_NO_RELEASE"
    assert workspace.historical is True
    assert workspace.proof_file is None
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "proposal.json").stat().st_mode) == 0o600
    assert not (folder / "first.json").exists()
    store = SQLiteRevisionStore(folder / "plan.sqlite3")
    plan = store.get(workspace.run_id)
    assert len(plan.decisions) == 6
    assert plan.decisions[0].candidate_id == BRANDS[1]
    assert store.pending_rechecks(plan.plan_id) == ("brand", "afterparty")
    assert len(store.audit_history(plan.plan_id)) == 2

    runner = Thread(target=workspace.server.serve_forever, daemon=True)
    runner.start()
    origin = "http://127.0.0.1:" + str(workspace.server.server_port)
    try:
        with urlopen(origin + "/", timeout=5) as response:
            html = response.read()
        assert b"Decision Workspace" in html
        assert b"/local-run.js" in html
        with urlopen(origin + "/api/local-run", timeout=5) as response:
            raw = response.read()
        body = json.loads(raw)
        assert body["decision_count"] == 6
        assert body["operator_constructed_unverified_decisions"] == 5
        assert body["changed_ids"] == ["brand"]
        assert body["preserved_ids"] == [
            "opener", "chicago", "austin", "seattle", "afterparty",
        ]
        assert body["invalidated_ids"] == ["brand", "afterparty"]
        assert body["off_on"]["off_verdicts"]["brand"] == "UNCERTAIN"
        assert body["off_on"]["on_verdicts"]["brand"] == "REPAIR"
        assert body["recorded_rank_recheck_pass"] is True
        assert body["historical_replay"] is True
        assert "HISTORICAL_REPLAY_ONLY" in body["blockers"]
        assert body["release_authorized"] is False
        assert body["real_qloo_requests"] == 0
        assert all(v.encode() not in raw for v in (ARTIST, *BRANDS))
        assert b"affinity" not in raw
        with urlopen(origin + "/local-run.js", timeout=5) as response:
            js = response.read()
        assert b"operator_constructed_unverified_decisions" in js
        assert b"unverified operator hypotheses" in js
        with pytest.raises(HTTPError) as exc:
            urlopen(Request(origin + "/api/local-run", headers={
                "Origin": "https://attacker.example",
            }), timeout=5)
        assert exc.value.code == 403
    finally:
        workspace.server.shutdown()
        runner.join(timeout=5)
        workspace.close()
    assert store.pending_rechecks(plan.plan_id) == ("brand", "afterparty")
    assert len(store.audit_history(plan.plan_id)) == 2


def test_original_agent_brand_choice_is_preserved_in_constructed_baseline():
    draft = build_tour_proposal(
        proposal(), artist_id=ARTIST, brand_ids=BRANDS, run_id="tour-001"
    )
    assert draft.decisions[0] == proposal().decisions[0]
    assert draft.decisions[-1].depends_on == ("brand",)
    assert all(len(x.candidate_pool) == 1 for x in draft.decisions[1:])
    assert all(x.mutable_fields == () for x in draft.decisions[1:])
    assert draft.intent.taste_context["supplemental_decisions"] == "OPERATOR_CONSTRUCTED_UNVERIFIED"
    assert draft.intent.constraints["external_action"] is False


@pytest.mark.parametrize("invalid", ["same", "earlier", "future", "wrong_pool", "wrong_artist", "already_winner"])
def test_invalid_private_capture_or_original_selection_fails_before_any_persisted_repair(
    tmp_path, monkeypatch, invalid
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(timezone.utc)
    first, later = capture(now - timedelta(hours=4)), capture(now - timedelta(hours=1))
    if invalid == "same":
        later = first
    elif invalid == "earlier":
        later = capture(now - timedelta(hours=5))
    elif invalid == "future":
        later = capture(now + timedelta(days=1))
    elif invalid == "wrong_pool":
        later = capture(now - timedelta(hours=1), fingerprint="f" * 64)
    elif invalid == "wrong_artist":
        first = capture(now - timedelta(hours=4), fingerprint="a" * 64)
    elif invalid == "already_winner":
        first = capture(now - timedelta(hours=4), order=BRANDS)
    files = inputs(tmp_path, first=first, later=later)
    with pytest.raises((TourReplayRejected, ValueError)):
        prepare(files)
    base = tmp_path / ".local/share/ofc-engine/private-tour-runs"
    if base.exists():
        assert not list(base.glob("*/plan.sqlite3-wal"))


def test_mismatched_source_proposal_is_rejected_before_database_creation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    paths = inputs(tmp_path)
    invalid = proposal().model_copy(update={"decisions": (
        proposal().decisions[0].model_copy(update={
            "candidate_pool": ("different", "pool"),
            "candidate_id": "different",
        }),
    )})
    paths[0].write_text(invalid.model_dump_json())
    with pytest.raises(TourReplayRejected, match="ORIGINAL_BRAND_SCOPE_MISMATCH"):
        prepare(paths)


def test_original_declared_artist_cannot_be_rebound_in_tour_replay():
    p = proposal()
    p = p.model_copy(update={"intent": p.intent.model_copy(update={
        "taste_context": {"artist_id": "different-artist"},
    })})
    with pytest.raises(TourReplayRejected, match="ORIGINAL_ARTIST_SCOPE_MISMATCH"):
        build_tour_proposal(
            p, artist_id=ARTIST, brand_ids=BRANDS, run_id="scope-001"
        )
