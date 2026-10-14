import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ofc_engine.contracts import ActionIntent, Decision
from ofc_engine.durable import SQLiteRevisionStore
from ofc_engine.intake import AgentProposal, accept_proposal
from ofc_engine.qloo import brand_affinity_probe
from ofc_engine.qloo_agent_bridge import AgentEvidenceRejected
from ofc_engine.qloo_operator_workspace import build_workspace, main, render_html
from ofc_engine.qloo_recorded_commit import commit_recorded_repair
from ofc_engine.revisions import digest_obj

ARTIST = "artist-test-001"
BRANDS = ("brand-a", "brand-b", "brand-c")
SPEC = brand_affinity_probe(ARTIST, BRANDS)
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
ACK = "COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE"


def proposal(*, objective="Review affinity"):
    return AgentProposal(
        intent=ActionIntent(
            run_id="operator-workspace-001",
            client_request_id="intake-001",
            objective=objective,
            constraints={"external_action": False, "max_budget": 100},
            taste_context={"audience": "private"},
        ),
        decisions=(Decision(
            decision_id="brand", domain="partner",
            candidate_id=BRANDS[0], candidate_pool=BRANDS,
            mutable_fields=("candidate_id",),
        ),),
    )


def capture(*, collected=None, values=(0.9, 0.6, 0.2)):
    return {
        "summary": {
            "endpoint": SPEC.path, "http_status": 200,
            "real_qloo_requests": 1, "mode": "LIVE_RESPONSE_UNQUALIFIED",
            "query_fingerprint": SPEC.fingerprint,
            "utc_collected_at": (collected or NOW - timedelta(minutes=10)).isoformat(),
        },
        "payload": {
            "success": True,
            "results": {"entities": [
                {"entity_id": candidate, "type": "urn:entity", "query": {
                    "affinity": rank,
                    "explainability": {"signal.interests.entities": {}}}}
                for candidate, rank in zip((BRANDS[1], BRANDS[0], BRANDS[2]), values)
            ]},
        },
    }


def seeded(tmp_path):
    db = tmp_path / "recorded.sqlite3"
    store = SQLiteRevisionStore(db)
    p = proposal()
    original = capture()
    accept_proposal(store, p)
    commit_recorded_repair(
        store, p, original,
        decision_id="brand", artist_id=ARTIST, brand_ids=BRANDS,
        request_id="persist-001", acknowledgement=ACK,
        as_of=NOW, max_evidence_age=timedelta(hours=1),
    )
    return db, store, p, original


def build(store, p, original, **kwargs):
    return build_workspace(
        store, p, original, artist_id=ARTIST, brand_ids=BRANDS,
        decision_id="brand", now=kwargs.pop("now", NOW),
        **kwargs,
    )


def test_workspace_binds_stored_revision_to_recorded_repair_and_is_private(tmp_path):
    _, store, p, original = seeded(tmp_path)
    report = build(store, p, original)
    assert report["plan"]["revision"] == 1
    assert report["plan"]["journal_events"] == 2
    assert report["decision"]["evidence_off"] == "UNCERTAIN"
    assert report["decision"]["evidence_on_initial"] == "REPAIR"
    assert report["decision"]["evidence_on_after_repair"] == "PASS"
    assert report["decision"]["initial"] == "Candidate 01"
    assert report["decision"]["replacement"] == "Candidate 02"
    assert report["decision"]["rank_order"] == ["Candidate 02", "Candidate 01", "Candidate 03"]
    assert report["recheck"]["status"] == "ORIGINAL_CAPTURE_REPLAY_ONLY"
    assert report["qualifications"]["provider_authenticated"] is False
    assert report["qualifications"]["release_authorized"] is False
    assert report["qualifications"]["independent_recheck_completed"] is False
    assert store.pending_rechecks(p.intent.run_id) == ("brand",)
    assert len(store.audit_history(p.intent.run_id)) == 2

    text = render_html(report)
    assert "Evidence changes the decision" in text
    assert "Observed affinity order" in text
    assert "Still pending" in text and "LOCKED" in text
    assert "Candidate 02" in text and "RECORDED EVIDENCE" in text
    for private in (*BRANDS, ARTIST, "0.9", "0.6", "0.2", "max_budget", "private"):
        assert private not in json.dumps(report)
        assert private not in text
    assert "https://" not in text
    assert "<script" not in text


def test_historical_capture_requires_explicit_historical_mode(tmp_path):
    _, store, p, original = seeded(tmp_path)
    later = NOW + timedelta(days=4)
    with pytest.raises(AgentEvidenceRejected, match="STALE_REQUIRES_HISTORICAL_MODE"):
        build(store, p, original, now=later)
    result = build(store, p, original, now=later, historical=True)
    assert result["historical_replay"] is True
    assert result["recheck"]["status"] == "ORIGINAL_CAPTURE_REPLAY_ONLY"
    assert result["qualifications"]["release_authorized"] is False


def test_distinct_later_capture_is_rank_only_not_verified(tmp_path):
    _, store, p, original = seeded(tmp_path)
    second = capture(collected=NOW - timedelta(minutes=2), values=(0.95, 0.58, 0.12))
    result = build(store, p, original, recheck_capture=second)
    assert result["recheck"]["status"] == "RECORDED_LATER_CAPTURE_RANK_PASS_ONLY"
    assert result["recheck"]["later_capture_observed"] is True
    assert result["recheck"]["pending_recheck_cleared"] is False
    assert result["qualifications"]["independent_recheck_completed"] is False
    assert store.pending_rechecks(p.intent.run_id) == ("brand",)


def test_changed_original_capture_fingerprint_is_rejected(tmp_path):
    _, store, p, original = seeded(tmp_path)
    original["summary"]["query_fingerprint"] = "f" * 64
    with pytest.raises(ValueError, match="CAPTURE_QUERY_MISMATCH"):
        build(store, p, original)


def test_mismatched_proposal_cannot_reuse_stored_repair(tmp_path):
    _, store, _, original = seeded(tmp_path)
    with pytest.raises(AgentEvidenceRejected, match="STORED_REPAIR_STATE_MISMATCH"):
        build(store, proposal(objective="Changed intent"), original)


def test_cli_outputs_private_read_only_bundle_without_network(tmp_path, monkeypatch, capsys):
    db, store, p, original = seeded(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from ofc_engine import qloo
    monkeypatch.setattr(qloo.QlooProbeClient, "from_environment",
                        lambda: (_ for _ in ()).throw(AssertionError("no provider calls")))
    pth, cap = tmp_path / "proposal.json", tmp_path / "capture.json"
    pth.write_text(p.model_dump_json())
    cap.write_text(json.dumps(original))
    args = [
        "--database", str(db), "--proposal", str(pth),
        "--capture", str(cap), "--artist-id", ARTIST,
        "--brands", ",".join(BRANDS), "--decision-id", "brand",
    ]
    assert main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "PRIVATE_RECORDED_PROOF_CREATED"
    assert output["real_qloo_requests"] == 0
    assert output["release_authorized"] is False
    manifest = Path(output["manifest_path"])
    page = Path(output["html_path"])
    assert manifest.is_file() and page.is_file()
    assert manifest.stat().st_mode & 0o077 == 0
    assert page.stat().st_mode & 0o077 == 0
    assert page.parent.stat().st_mode & 0o077 == 0
    document = json.loads(manifest.read_text())
    assert digest_obj(document["payload"]) == document["sha256"]
    assert output["proof_sha256"] == document["sha256"]
    assert store.get(p.intent.run_id).revision == 1
    assert store.pending_rechecks(p.intent.run_id) == ("brand",)
    for private in (*BRANDS, ARTIST, '"affinity":'):
        assert private not in manifest.read_text()
        assert private not in page.read_text()


def test_symlink_database_is_blocked_without_creating_bundle(tmp_path, monkeypatch, capsys):
    db, _, p, original = seeded(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(db)
    pth, cap = tmp_path / "proposal.json", tmp_path / "capture.json"
    pth.write_text(p.model_dump_json())
    cap.write_text(json.dumps(original))
    assert main([
        "--database", str(alias), "--proposal", str(pth),
        "--capture", str(cap), "--artist-id", ARTIST,
        "--brands", ",".join(BRANDS), "--decision-id", "brand",
    ]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    assert not (tmp_path / ".local/share/ofc-engine/private-qloo-proof-workspaces").exists()


def test_symlink_private_output_directory_is_blocked(tmp_path, monkeypatch, capsys):
    db, _, p, original = seeded(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    directory = tmp_path / ".local/share/ofc-engine/private-qloo-proof-workspaces"
    directory.parent.mkdir(parents=True)
    directory.symlink_to(tmp_path)
    pth, cap = tmp_path / "proposal.json", tmp_path / "capture.json"
    pth.write_text(p.model_dump_json())
    cap.write_text(json.dumps(original))
    assert main([
        "--database", str(db), "--proposal", str(pth),
        "--capture", str(cap), "--artist-id", ARTIST,
        "--brands", ",".join(BRANDS), "--decision-id", "brand",
    ]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
