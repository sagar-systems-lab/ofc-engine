import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ofc_engine.qloo import brand_affinity_probe
from ofc_engine.qloo_proof_export import (
    ProofError, _save_proof, build_proof, main, verify_proof,
)
from ofc_engine.revisions import digest_obj

ARTIST = "4BBEF799-A0C4-4110-AB01-39216993C312"
BRANDS = (
    "C70CE2B8-0AD1-4150-BF18-5A6347F2E860",
    "43A36E73-9BC7-4B09-A116-78E51554C3E3",
    "E26BC58D-18D2-405B-BC7B-7C556ADE3829",
)
SPEC = brand_affinity_probe(ARTIST, BRANDS)
COLLECTED = datetime(2026, 10, 14, 10, 21, 49, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 14, 10, 50, tzinfo=timezone.utc)


def capture(*, when=COLLECTED):
    return {
        "summary": {
            "endpoint": "/v2/insights", "http_status": 200,
            "real_qloo_requests": 1, "mode": "LIVE_RESPONSE_UNQUALIFIED",
            "query_fingerprint": SPEC.fingerprint,
            "utc_collected_at": when.isoformat(),
        },
        "payload": {
            "success": True,
            "query": {"explainability": {"signal.interests.entities": {}}},
            "results": {
                "entities": [
                    {"entity_id": BRANDS[1], "type": "urn:entity",
                     "query": {"affinity": 0.9,
                               "explainability": {"signal.interests.entities": {}}}},
                    {"entity_id": BRANDS[0], "type": "urn:entity",
                     "query": {"affinity": 0.6,
                               "explainability": {"signal.interests.entities": {}}}},
                    {"entity_id": BRANDS[2], "type": "urn:entity",
                     "query": {"affinity": 0.2,
                               "explainability": {"signal.interests.entities": {}}}},
                ]
            },
        },
    }


def make(data=None, *, now=NOW, historical=False, selected=BRANDS[0]):
    return build_proof(
        data or capture(), artist_id=ARTIST, brand_ids=BRANDS,
        selected_id=selected, now=now, historical=historical,
    )


def test_proof_exports_exact_recorded_repair_without_live_claim():
    p = make()
    assert p["schema"] == "ofc-recorded-proof-v1"
    assert p["evidence_off"] == {"verdict": "UNCERTAIN", "revision": 0}
    assert p["evidence_on"] == {
        "initial_verdict": "REPAIR",
        "final_verdict": "PASS",
        "initial_candidate_id": BRANDS[0],
        "final_candidate_id": BRANDS[1],
        "final_revision": 1,
        "repair_attempts": 1,
        "recheck_states": ["STRUCTURAL_PASS"],
        "pending_recheck_ids": [],
    }
    assert p["same_frozen_baseline"]
    assert p["rank_order"] == [BRANDS[1], BRANDS[0], BRANDS[2]]
    assert p["historical_replay"] is False
    for key in ("provider_authenticated", "fresh_provider_recheck",
                "release_authorized", "real_causal_proof", "external_action_executed"):
        assert p[key] is False
    assert p["real_qloo_requests"] == 0
    assert '"affinity":' not in json.dumps(p)


def test_top_rank_no_repair():
    p = make(selected=BRANDS[1])
    assert p["evidence_on"]["initial_verdict"] == "PASS"
    assert p["evidence_on"]["final_revision"] == 0
    assert p["evidence_on"]["repair_attempts"] == 0
    assert p["evidence_on"]["recheck_states"] == []


def test_stale_requires_explicit_historical():
    later = NOW + timedelta(days=10)
    with pytest.raises(ProofError, match="STALE_REQUIRES_HISTORICAL_MODE"):
        make(now=later)
    p = make(now=later, historical=True)
    assert p["historical_replay"] is True
    assert p["captured_at"] != p["generated_at"]
    assert p["provider_authenticated"] is False
    assert p["real_qloo_requests"] == 0


def test_future_capture_rejected_even_in_historical_mode():
    with pytest.raises(ProofError, match="CAPTURE_FROM_FUTURE"):
        make(capture(when=NOW + timedelta(minutes=5)), historical=True)


def test_tampered_query_fingerprint_rejected():
    c = capture()
    c["summary"]["query_fingerprint"] = "f" * 64
    with pytest.raises(ValueError):
        make(c)


def test_missing_candidate_rejected():
    c = capture()
    c["payload"]["results"]["entities"].pop()
    with pytest.raises(ValueError):
        make(c)


def test_unexpected_selected_candidate_rejected():
    with pytest.raises(ProofError, match="SELECTED_OUTSIDE_POOL"):
        make(selected="brand-not-in-pool")


def test_invalid_clock_rejected():
    with pytest.raises(ProofError, match="CLOCK_NOT_AWARE"):
        make(now=NOW.replace(tzinfo=None))


def test_local_digest_check_and_tampering():
    p = make()
    document = {"payload": p, "sha256": digest_obj(p)}
    result = verify_proof(document)
    assert result["status"] == "LOCAL_DIGEST_VALID"
    assert result["release_authorized"] is False
    document["payload"] = {**p, "real_qloo_requests": 9}
    with pytest.raises(ProofError, match="PROOF_DIGEST_MISMATCH"):
        verify_proof(document)


def test_recomputed_digest_cannot_relabel_as_live():
    p = make()
    p["source_mode"] = "VERIFIED_LIVE"
    with pytest.raises(ProofError, match="INVALID_PROOF_BOUNDARY"):
        verify_proof({"payload": p, "sha256": digest_obj(p)})


def test_export_private_permissions_no_overwrite(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    p = make()
    destination = _save_proof(p)
    assert destination.exists()
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700
    assert verify_proof(json.loads(destination.read_text()))["status"] == "LOCAL_DIGEST_VALID"
    with pytest.raises(FileExistsError):
        _save_proof(p)


def test_cli_round_trip_never_prints_provider_payload(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = tmp_path / "private-capture.json"
    captured.write_text(json.dumps(capture(
        when=datetime.now(timezone.utc) - timedelta(minutes=1),
    )))
    assert main([
        "--capture", str(captured), "--artist-id", ARTIST,
        "--brands", ",".join(BRANDS), "--selected", BRANDS[0],
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "PRIVATE_PROOF_EXPORTED"
    assert output["real_qloo_requests"] == 0
    assert '"affinity":' not in json.dumps(output)
    path = Path(output["path"])
    document = json.loads(path.read_text())
    assert '"affinity":' not in json.dumps(document)
    assert main(["--verify", str(path)]) == 0
    verification = json.loads(capsys.readouterr().out)
    assert verification["status"] == "LOCAL_DIGEST_VALID"
    assert verification["real_qloo_requests"] == 0


def test_cli_symlink_capture_blocked(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original = tmp_path / "raw.json"
    original.write_text(json.dumps(capture()))
    link = tmp_path / "link.json"
    link.symlink_to(original)
    assert main([
        "--capture", str(link), "--artist-id", ARTIST,
        "--brands", ",".join(BRANDS), "--selected", BRANDS[0],
    ]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"


def test_verify_arguments_are_exclusive(tmp_path, capsys):
    assert main(["--verify", str(tmp_path / "missing.json"), "--historical"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
