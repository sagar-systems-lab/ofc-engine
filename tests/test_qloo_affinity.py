import json
from pathlib import Path

import pytest

from ofc_engine.qloo import brand_affinity_probe
from ofc_engine.qloo_affinity import AffinityCaptureError, main, normalize_affinity_capture


ARTIST = "4BBEF799-A0C4-4110-AB01-39216993C312"
BRANDS = ("C70CE2B8-0AD1-4150-BF18-5A6347F2E860",
          "43A36E73-9BC7-4B09-A116-78E51554C3E3",
          "E26BC58D-18D2-405B-BC7B-7C556ADE3829")
SPEC = brand_affinity_probe(ARTIST, BRANDS)


def capture():
    return {
        "summary": {
            "endpoint": "/v2/insights", "http_status": 200,
            "real_qloo_requests": 1, "mode": "LIVE_RESPONSE_UNQUALIFIED",
            "utc_collected_at": "2026-10-14T10:21:49.052043+00:00",
            "query_fingerprint": SPEC.fingerprint,
        },
        "payload": {
            "success": True, "query": {"explainability": {}},
            "results": {"entities": [
                {"entity_id": BRANDS[1], "type": "urn:entity",
                 "query": {"affinity": 0.88, "measurements": {},
                           "explainability": {"signal.interests.entities": {"score": 0.4}}}},
                {"entity_id": BRANDS[0], "type": "urn:entity",
                 "query": {"affinity": 0.6, "measurements": {},
                           "explainability": {"signal.interests.entities": {"score": 0.2}}}},
                {"entity_id": BRANDS[2], "type": "urn:entity",
                 "query": {"affinity": 0.2, "measurements": {},
                           "explainability": {"signal.interests.entities": {"score": 0.1}}}},
            ]},
        },
    }


def test_offline_observation_uses_exact_fingerprint_and_ranked_real_ids():
    result = normalize_affinity_capture(capture(), SPEC)
    assert result["status"] == "OFFLINE_AFFINITY_OBSERVED"
    assert result["query_fingerprint"] == SPEC.fingerprint
    assert result["candidate_count"] == 3
    assert [(v["entity_id"], v["rank"]) for v in result["ranking"]] == [
        (BRANDS[1], 1), (BRANDS[0], 2), (BRANDS[2], 3)]
    assert result["real_qloo_requests"] == 0
    assert result["provider_policy_verified"] is False
    assert result["release_authorized"] is False
    assert result["entity_taxonomy_independently_verified"] is False
    assert result["external_action_executed"] is False
    assert "0.88" not in json.dumps(result)


@pytest.mark.parametrize(("change", "expected"), [
    (lambda c: c["summary"].update(query_fingerprint="a" * 64), "CAPTURE_QUERY_MISMATCH"),
    (lambda c: c["summary"].update(http_status=401), "CAPTURE_QUERY_MISMATCH"),
    (lambda c: c["summary"].update(utc_collected_at="yesterday"), "INVALID_CAPTURE_TIME"),
    (lambda c: c["payload"].update(success=False), "INSIGHTS_NOT_SUCCESSFUL"),
    (lambda c: c["payload"]["results"]["entities"].pop(), "INCOMPLETE_CANDIDATE_RESULTS"),
    (lambda c: c["payload"]["results"]["entities"][0].update(entity_id="unknown"), "UNEXPECTED_RESULT_ENTITY"),
    (lambda c: c["payload"]["results"]["entities"][0].update(type="urn:entity:artist"), "RESULT_TYPE_MISMATCH"),
    (lambda c: c["payload"]["results"]["entities"][0]["query"].update(affinity=None), "INVALID_AFFINITY"),
    (lambda c: c["payload"]["results"]["entities"][0]["query"].update(affinity=float("nan")), "INVALID_AFFINITY"),
    (lambda c: c["payload"]["results"]["entities"][0]["query"].update(affinity=0.6), "AMBIGUOUS_AFFINITY_TIE"),
    (lambda c: c["payload"]["results"]["entities"].reverse(), "UNEXPECTED_AFFINITY_ORDER"),
])
def test_fail_closed_on_invalid_evidence(change, expected):
    data = capture()
    change(data)
    with pytest.raises(AffinityCaptureError, match=expected):
        normalize_affinity_capture(data, SPEC)



def test_insights_uuid_casing_matches_resolved_search_identity():
    # Search canonicalizes Qloo IDs to lowercase; insights may echo uppercase.
    lower = tuple(value.lower() for value in BRANDS)
    spec = brand_affinity_probe(ARTIST.lower(), lower)
    data = capture()
    data["summary"]["query_fingerprint"] = spec.fingerprint
    result = normalize_affinity_capture(data, spec)
    assert [row["entity_id"] for row in result["ranking"]] == [
        lower[1], lower[0], lower[2],
    ]


def test_foreign_uuid_is_not_normalized_into_candidate_pool():
    lower = tuple(value.lower() for value in BRANDS)
    spec = brand_affinity_probe(ARTIST.lower(), lower)
    data = capture()
    data["summary"]["query_fingerprint"] = spec.fingerprint
    data["payload"]["results"]["entities"][0]["entity_id"] = (
        "00000000-0000-4000-8000-000000000001"
    )
    with pytest.raises(AffinityCaptureError, match="UNEXPECTED_RESULT_ENTITY"):
        normalize_affinity_capture(data, spec)


def test_case_variant_duplicate_candidates_are_rejected():
    spec = brand_affinity_probe(ARTIST, (BRANDS[0], BRANDS[0].lower()))
    with pytest.raises(AffinityCaptureError, match="INVALID_QUERY_POOL"):
        normalize_affinity_capture(capture(), spec)


def test_uuid_format_is_not_relaxed_for_insights_results():
    data = capture()
    data["payload"]["results"]["entities"][0]["entity_id"] = BRANDS[1].replace("-", "")
    with pytest.raises(AffinityCaptureError, match="UNEXPECTED_RESULT_ENTITY"):
        normalize_affinity_capture(data, SPEC)


def test_generic_insights_type_is_not_independent_brand_proof():
    data = capture()
    assert all(e["type"] == "urn:entity" for e in data["payload"]["results"]["entities"])
    result = normalize_affinity_capture(data, SPEC)
    assert result["candidate_count"] == 3
    assert result["entity_taxonomy_independently_verified"] is False
    assert result["provider_policy_verified"] is False


def test_unexpected_specific_taxonomy_is_rejected():
    data = capture()
    data["payload"]["results"]["entities"][0]["type"] = "urn:entity:brand"
    with pytest.raises(AffinityCaptureError, match="RESULT_TYPE_MISMATCH"):
        normalize_affinity_capture(data, SPEC)

def test_unrelated_query_cannot_reuse_cached_evidence():
    altered = brand_affinity_probe("another-artist", BRANDS)
    with pytest.raises(AffinityCaptureError, match="CAPTURE_QUERY_MISMATCH"):
        normalize_affinity_capture(capture(), altered)


def test_cli_is_offline(tmp_path, capsys, monkeypatch):
    p = tmp_path / "captured.json"
    p.write_text(json.dumps(capture()))
    from ofc_engine import qloo_affinity
    def forbidden(*args, **kwargs):
        raise AssertionError("No Qloo request allowed")
    monkeypatch.setattr(qloo_affinity, "brand_affinity_probe", brand_affinity_probe)
    assert main(["--capture", str(p), "--artist-id", ARTIST, "--brands", ",".join(BRANDS)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["candidate_count"] == 3


def test_cli_never_prints_raw_capture_on_error(tmp_path, capsys):
    p = tmp_path / "broken.json"
    p.write_text('{"raw":"private-raw-value"}')
    assert main(["--capture", str(p), "--artist-id", ARTIST, "--brands", ",".join(BRANDS)]) == 2
    output = capsys.readouterr().out
    assert "private-raw-value" not in output
    assert json.loads(output)["status"] == "BLOCKED"


def test_cli_rejects_symlink(tmp_path, capsys):
    original = tmp_path / "raw.json"
    original.write_text(json.dumps(capture()))
    link = tmp_path / "link.json"
    link.symlink_to(original)
    assert main(["--capture", str(link), "--artist-id", ARTIST, "--brands", ",".join(BRANDS)]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "UNSAFE_CAPTURE_FILE"
