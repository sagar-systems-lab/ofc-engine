import json

import pytest

from ofc_engine.qloo_capture import CaptureInvalid, inspect_capture, main


SEARCH = {
    "summary": {
        "http_status": 200, "real_qloo_requests": 1,
        "mode": "LIVE_RESPONSE_UNQUALIFIED", "endpoint": "/search",
        "query_fingerprint": "a" * 64,
    },
    "payload": {"results": [
        {"entity_id": "artist-id", "name": "Artist One", "types": ["urn:entity:artist"]},
        {"entity_id": "brand-id", "name": "Brand Two", "types": ["urn:entity:brand"]},
    ]},
}


def test_search_filters_only_observed_types():
    result = inspect_capture(SEARCH, expected_type="urn:entity:brand")
    assert result["status"] == "SEARCH_OBSERVED"
    assert result["matches"] == [{
        "rank": 2, "name": "Brand Two", "entity_id": "brand-id", "types": ["urn:entity:brand"]
    }]
    assert result["provider_policy_verified"] is False
    assert result["real_qloo_requests"] == 0


def test_live_status_is_required():
    bogus = {**SEARCH, "summary": {**SEARCH["summary"], "http_status": 401}}
    with pytest.raises(CaptureInvalid, match="UNQUALIFIED_CAPTURE"):
        inspect_capture(bogus)


def test_unexpected_search_schema_fails_closed():
    bad = {**SEARCH, "payload": {"results": [{"id": "missing-entity-id", "name": "Fake"}]}}
    with pytest.raises(CaptureInvalid, match="UNEXPECTED_SEARCH_ENTITY"):
        inspect_capture(bad)


def test_insights_inspector_observes_only_shape():
    data = {**SEARCH, "summary": {**SEARCH["summary"], "endpoint": "/v2/insights"},
            "payload": {"entities": [{"entity_id": "x", "query": "private-data"}], "meta": {}}}
    result = inspect_capture(data)
    assert result["entity_count"] == 1
    assert result["first_entity_fields"] == ["entity_id", "query"]
    assert "private-data" not in json.dumps(result)
    assert result["provider_policy_verified"] is False


def test_capture_cli_reads_offline(tmp_path, capsys):
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(SEARCH))
    assert main(["--capture", str(path), "--type", "brand"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["matches"][0]["entity_id"] == "brand-id"


def test_bad_json_blocked_without_exception(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("{invalid")
    assert main(["--capture", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"

def test_unqualified_brand_locality_is_blocked_before_live_request():
    from ofc_engine.qloo_probe import main as probe_main

    with pytest.raises(SystemExit, match="Brand locality parameters are not qualified"):
        probe_main([
            "--live", "--artist-id", "ARTIST_ID", "--brands", "BRAND_A,BRAND_B",
            "--locality", "Chicago", "--locality-mode", "signal",
        ])
