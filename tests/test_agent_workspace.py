"""One-command local operator workspace: agent review and redacted proof."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ofc_engine.agent_workspace import prepare_workspace
from ofc_engine.contracts import ActionIntent, Decision
from ofc_engine.durable import SQLiteRevisionStore
from ofc_engine.intake import AgentProposal, accept_proposal
from ofc_engine.qloo import QlooProbeClient, brand_affinity_probe
from ofc_engine.qloo_recorded_commit import commit_recorded_repair

ARTIST = "local-artist"
BRANDS = ("brand-alpha", "brand-beta", "brand-gamma")
SPEC = brand_affinity_probe(ARTIST, BRANDS)
TOKEN = "workspace-test-" + "a" * 40


def capture(collected, *, query_fingerprint=None, winning=True):
    order = (BRANDS[1], BRANDS[0], BRANDS[2]) if winning else BRANDS
    return {
        "summary": {
            "endpoint": SPEC.path, "http_status": 200,
            "real_qloo_requests": 1, "mode": "LIVE_RESPONSE_UNQUALIFIED",
            "query_fingerprint": query_fingerprint or SPEC.fingerprint,
            "utc_collected_at": collected.isoformat(),
        },
        "payload": {
            "success": True,
            "results": {"entities": [
                {"entity_id": item, "type": "urn:entity",
                 "query": {"affinity": round(0.92 - i * 0.22, 3),
                           "explainability": {"signal.interests.entities": {}}}}
                for i, item in enumerate(order)
            ]},
        },
    }


def files(tmp_path, *, stale=False, bad_recheck=False, missing_commit=False):
    now = datetime.now(timezone.utc)
    shift = timedelta(days=5) if stale else timedelta(0)
    p = AgentProposal(
        intent=ActionIntent(
            run_id="connected-operator-001", client_request_id="new-001",
            objective="Evaluate candidate partner against audience",
            constraints={"external_action": False}, taste_context={"country": "example"},
        ),
        decisions=(Decision(
            decision_id="brand", domain="partner", candidate_id=BRANDS[0],
            candidate_pool=BRANDS, mutable_fields=("candidate_id",),
        ),),
    )
    db = tmp_path / "stored.sqlite3"
    store = SQLiteRevisionStore(db)
    accept_proposal(store, p)
    original = capture(now - shift - timedelta(minutes=22))
    latest = capture(now - shift - timedelta(minutes=2),
                     query_fingerprint="f" * 64 if bad_recheck else None)
    if not missing_commit:
        commit_recorded_repair(
            store, p, original, decision_id="brand", artist_id=ARTIST,
            brand_ids=BRANDS, request_id="persist-001",
            acknowledgement="COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE",
            as_of=now - shift, max_evidence_age=timedelta(hours=1),
        )
    paths = [tmp_path / item for item in ("proposal.json", "original.json", "new.json")]
    for path, content in zip(paths, (p.model_dump_json(), json.dumps(original), json.dumps(latest))):
        path.write_text(content)
    return store, db, paths


def prepare(tmp_path, db, paths, **kw):
    return prepare_workspace(
        database=db, proposal=paths[0],
        original_capture=paths[1], later_capture=paths[2],
        artist_id=ARTIST, brand_ids=BRANDS, decision_id="brand",
        **kw,
    )


@contextmanager
def running(workspace):
    thread = Thread(target=workspace.server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{workspace.server.server_port}"
    try:
        yield base, base
    finally:
        workspace.server.shutdown()
        thread.join(timeout=5)
        workspace.close()


def request(url, *, token=None, method="GET", headers=None):
    h = dict(headers or {})
    if token is not None:
        h["Authorization"] = "Bearer " + token
    try:
        with urlopen(Request(url, headers=h, method=method), timeout=5) as response:
            return response.status, response.read(), dict(response.headers)
    except HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def test_connected_session_exposes_authenticated_review_and_existing_proof(
    tmp_path, monkeypatch
):
    store, db, paths = files(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        QlooProbeClient, "from_environment",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected Qloo call")),
    )
    original_journal = store.audit_history("connected-operator-001")
    workspace = prepare(tmp_path, db, paths, auth_token=TOKEN)
    with running(workspace) as (agent, proof):
        review_url = agent + "/api/proposals/connected-operator-001/review"
        code, body, headers = request(review_url, token=TOKEN)
        result = json.loads(body)
        assert code == 200 and headers["Cache-Control"] == "no-store"
        assert result["status"] == "RECORDED_RANK_RECHECK_PASS_NO_RELEASE"
        assert result["pending_recheck_ids"] == ["brand"]
        assert result["decision_reviews"][0]["verdict"] == "PASS"
        assert result["real_qloo_requests"] == 0
        assert result["release_authorized"] is False
        assert result["independent_recheck_completed"] is False
        assert request(review_url)[0] == 401
        assert request(review_url, token=TOKEN, headers={"Host": "evil.test"})[0] == 403
        assert request(review_url, token=TOKEN, method="POST")[0] == 405
        assert request(agent + "/api/proposals/other/review", token=TOKEN)[0] == 404
        from importlib.resources import files as package_files

        code, page, _ = request(proof + "/")
        assert code == 200
        assert page == package_files("ofc_engine").joinpath("web", "studio.html").read_bytes()
        assert b"Decision Workspace" in page
        code, recorded_page, _ = request(proof + "/recorded")
        assert code == 200 and b'data-ofc-local-proof="true"' in recorded_page
        assert b"Recorded Proof" in recorded_page
        code, data, _ = request(proof + "/api/local-proof")
        manifest = json.loads(data)
        assert code == 200
        assert manifest["payload"]["recheck"]["status"] == "RECORDED_LATER_CAPTURE_RANK_PASS_ONLY"
        assert manifest["payload"]["qualifications"]["release_authorized"] is False
        assert manifest["payload"]["plan"]["stored_digest"] == result["plan_digest"]
        assert request(proof + "/api/local-proof", method="POST")[0] == 405
        assert request(proof + "/api/local-proof", headers={"Host": "evil.test"})[0] == 403
        for payload in (body, data, page, recorded_page):
            assert ARTIST.encode() not in payload
            assert all(brand.encode() not in payload for brand in BRANDS)
            assert TOKEN.encode() not in payload
            assert b"query.affinity" not in payload
    assert store.audit_history("connected-operator-001") == original_journal
    assert store.pending_rechecks("connected-operator-001") == ("brand",)


def test_workspace_auto_generated_token_is_private_and_cleaned_up(
    tmp_path, monkeypatch
):
    _, db, paths = files(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = prepare(tmp_path, db, paths)
    token_file = workspace.token_file
    assert token_file is not None and token_file.is_file()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
    token = token_file.read_text().strip()
    assert len(token) >= 32
    with running(workspace) as (agent, _):
        code, result, _ = request(
            agent + "/api/proposals/connected-operator-001/review", token=token
        )
        assert code == 200
        assert token.encode() not in result
    assert not token_file.exists()


def test_stale_proof_requires_explicit_historical_replay(tmp_path, monkeypatch):
    _, db, paths = files(tmp_path, stale=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(ValueError):
        prepare(tmp_path, db, paths, auth_token=TOKEN)
    workspace = prepare(tmp_path, db, paths, auth_token=TOKEN, historical=True)
    assert workspace.historical is True
    assert workspace.rank_status == "RECORDED_RANK_RECHECK_PASS_NO_RELEASE"
    with running(workspace) as (agent, proof):
        code, report, _ = request(
            agent + "/api/proposals/connected-operator-001/review", token=TOKEN
        )
        assert code == 200
        assert json.loads(report)["historical_replay"] is True
        status, raw, _ = request(proof + "/api/local-proof")
        assert status == 200 and json.loads(raw)["payload"]["historical_replay"] is True


@pytest.mark.parametrize("kwargs", [
    {"bad_recheck": True}, {"missing_commit": True},
])
def test_bad_or_uncommitted_sources_fail_before_any_server(tmp_path, kwargs, monkeypatch):
    _, db, paths = files(tmp_path, **kwargs)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises((ValueError, KeyError)):
        prepare(tmp_path, db, paths, auth_token=TOKEN)
    assert not (tmp_path / ".local/share/ofc-engine/private-agent-sessions").exists()


def test_wrong_database_and_symlink_capture_fail_closed(tmp_path, monkeypatch):
    _, db, paths = files(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    linked = tmp_path / "linked-capture.json"
    linked.symlink_to(paths[1])
    with pytest.raises(ValueError):
        prepare(tmp_path, db, (paths[0], linked, paths[2]), auth_token=TOKEN)
    linked_db = tmp_path / "linked.sqlite3"
    linked_db.symlink_to(db)
    with pytest.raises(ValueError):
        prepare(tmp_path, linked_db, paths, auth_token=TOKEN)


def test_one_origin_rejects_rebinding_and_keeps_public_fixture_routes(tmp_path, monkeypatch):
    _, db, paths = files(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = prepare(tmp_path, db, paths, auth_token=TOKEN)
    with running(workspace) as (base, same_base):
        assert base == same_base
        for path in ("/", "/recorded", "/api/local-proof", "/api/demo?scenario=repair"):
            assert request(base + path)[0] == 200
            assert request(base + path, headers={"Host": "attacker.test"})[0] == 403
            assert request(base + path, headers={"Origin": "https://attacker.test"})[0] == 403
            assert request(base + path, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert request(base + "/api/local-proof?bad=1")[0] == 400
        assert request(base + "/api/execute")[0] == 404
        assert request(base + "/api/local-proof", method="POST")[0] == 405
        assert request(base + "/api/proposals/connected-operator-001/review")[0] == 401
        assert request(base + "/recorded.css")[0] == 200
        assert request(base + "/studio.js")[0] == 200
