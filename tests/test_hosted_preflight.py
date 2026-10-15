"""Hosted plan authoring reaches the canonical evaluator without provider calls."""
import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ofc_engine.hosted_preview import HostedFixtureServer
from ofc_engine.hosted_preflight import preflight
from ofc_engine.webdemo import canonical_proposal


def request(base, path, proposal=None, *, origin=None, content_type="application/json"):
    body = json.dumps(proposal).encode() if proposal is not None else None
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if body is not None:
        headers["Content-Type"] = content_type
    try:
        with urlopen(Request(base + path, data=body, headers=headers), timeout=5) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def test_server_matches_canonical_fixture_without_claiming_qloo_evidence():
    original = canonical_proposal().model_dump(mode="json")
    result = preflight(original)
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["source_mode"] == "EVIDENCE_UNAVAILABLE"
    assert result["synthetic_fixture_available"] is True
    assert result["same_run_as_fixture"] is True
    assert result["decision_count"] == 6
    assert result["revision"] == 0
    assert len(result["plan_digest"]) == 64
    assert len(result["baseline_digest"]) == 64
    assert result["plan_digest"] != result["baseline_digest"]
    assert all(x["verdict"] == "UNCERTAIN" for x in result["decisions"])
    for flag in (
        "provider_authenticated", "release_authorized",
        "external_action_executed", "stored_on_server",
    ):
        assert result[flag] is False
    assert result["real_qloo_requests"] == 0
    assert result["independent_recheck_completed"] is False

    original["intent"]["run_id"] = "browser-review-new-run"
    original["intent"]["client_request_id"] = "browser-review-new-request"
    renamed = preflight(original)
    assert renamed["synthetic_fixture_available"] is True
    assert renamed["same_run_as_fixture"] is False
    assert renamed["baseline_digest"] != result["baseline_digest"]


def test_custom_tour_is_server_validated_but_never_assigned_fake_cultural_pass():
    original = canonical_proposal().model_dump(mode="json")
    original["intent"]["objective"] = "Custom venue eligibility review"
    original["decisions"][0]["candidate_id"] = "brand-b"
    result = preflight(original)
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["source_mode"] == "EVIDENCE_UNAVAILABLE"
    assert not result["synthetic_fixture_available"]
    assert all(x["reason_codes"] == ["EVIDENCE_UNAVAILABLE"] for x in result["decisions"])
    assert all(x["verdict"] == "UNCERTAIN" for x in result["decisions"])
    assert not result["release_authorized"]


@pytest.mark.parametrize("attack", ["unknown", "cycle", "out_of_pool", "missing", "duplicate"])
def test_malformed_agent_proposals_rejected_at_server_boundary(attack):
    plan = canonical_proposal().model_dump(mode="json")
    if attack == "unknown":
        plan["raw_api_key"] = "not-permitted"
    elif attack == "cycle":
        plan["decisions"][0]["depends_on"] = ["afterparty"]
    elif attack == "out_of_pool":
        plan["decisions"][0]["candidate_id"] = "not-in-shortlist"
    elif attack == "missing":
        del plan["intent"]["objective"]
    elif attack == "duplicate":
        plan["decisions"][1]["decision_id"] = "brand"
    with pytest.raises(ValueError):
        preflight(plan)


def test_hosted_studio_preflight_http_same_origin_and_read_only_result():
    with HostedFixtureServer(("127.0.0.1", 0)) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        try:
            with urlopen(base + "/", timeout=5) as response:
                page = response.read()
            with urlopen(base + "/hosted-preflight.js", timeout=5) as response:
                script = response.read()
            assert b"/hosted-preflight.js" in page
            assert b"/api/preflight" in script
            assert b"check-preflight" in script
            assert b"innerHTML" not in script
            assert b"textContent" in script

            plan = canonical_proposal().model_dump(mode="json")
            assert request(base, "/api/preflight", plan)[0] == 403
            assert request(base, "/api/preflight", plan, origin="https://attacker.test")[0] == 403
            status, answer = request(base, "/api/preflight", plan, origin=base)
            assert status == 200
            assert json.loads(answer)["synthetic_fixture_available"] is True
            assert b"QLOO_API_KEY" not in answer
            assert b"release_authorized" in answer
            plan["intent"]["objective"] += " modified"
            status, answer = request(base, "/api/preflight", plan, origin=base)
            assert status == 200
            assert json.loads(answer)["status"] == "REVIEW_REQUIRED"
            assert request(base, "/api/preflight", {"not": "a plan"}, origin=base)[0] == 422
            assert request(base, "/api/preflight", plan, origin=base,
                           content_type="text/plain")[0] == 403
            with urlopen(base + "/api/demo?scenario=repair", timeout=5) as response:
                existing = json.load(response)
            assert existing["source_mode"] == "SYNTHETIC"
            assert not hasattr(server, "database")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
