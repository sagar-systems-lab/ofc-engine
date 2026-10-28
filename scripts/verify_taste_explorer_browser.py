"""Keyless desktop/mobile taste-explorer and consent regression."""
import argparse
from copy import deepcopy
import json

from playwright.sync_api import sync_playwright

from copilot_workspace_smoke import SAVED

SESSION = "cp-" + "a" * 32
PARENT = "cp-run-" + "b" * 32
CHILD = "cp-run-" + "c" * 32
ORIGINAL = "Bhai mujhe koi hill station jana hai. Manali jaisa feel chahiye."


def parent_run():
    run = deepcopy(SAVED)
    run["run_id"] = PARENT
    run["prompt"] = ORIGINAL
    run["original_answer"] = "A guest house is the best hill station (affinity 0.9789)."
    run["ofc_answer"] = "A guest house is the best hill station (affinity 0.9789)."
    run["baseline_digest"] = "a" * 64
    run["reviewed_digest"] = "a" * 64
    run["qloo_evidence_digest"] = "e" * 64
    run.pop("value_receipt", None)
    run["plan"] = {
        "objective": ORIGINAL, "taste_seed_name": "Manali Hills",
        "taste_seed_category": "place", "cities": [],
        "choices": [{"decision_id": "taste_recommendation", "domain": "place",
                     "selected_name": "Karma Guest House", "candidate_names": [
                         "Karma Guest House", "Hotel Cherry Manali", "Hotel Seraj", "Shimla"],
                     "depends_on": []}],
    }
    native = {
        "status": "CULTURAL_CANDIDATES_OBSERVED",
        "source_mode": "LIVE_HTTP_UNVERIFIED",
        "candidates": [{"name": "Karma Guest House"},
                       {"name": "Hotel Cherry Manali"},
                       {"name": "Hotel Seraj"}, {"name": "Shimla"}],
        "policy_qualified": False,
        "business_verified": False,
    }
    qloo = {"provider": "QLOO", "evidence_digest": "e" * 64,
            "native_discovery": native}
    original = {
        "decision_id": "taste_recommendation", "domain": "place",
        "candidate_id": "Karma Guest House", "candidate_pool": [
            "Karma Guest House", "Hotel Cherry Manali", "Hotel Seraj", "Shimla"],
        "cultural_rank_observed": False,
    }
    run["off"] = {
        "ofc_enabled": False, "plan_digest": "a" * 64,
        "qloo": qloo, "decisions": [original],
    }
    run["on"] = {
        "ofc_enabled": True, "plan_digest": "a" * 64,
        "qloo": deepcopy(qloo), "changed_ids": [],
        "decisions": [{
            **original, "changed": False, "ofc_verdict": "UNCERTAIN",
            "ofc_reason": "CATALOG_POLICY_NOT_LIVE_QUALIFIED",
            "rank_recheck_completed": False,
        }],
    }
    return run


def qualify(base_url):
    reports = []
    for width in (1440, 390):
        with sync_playwright() as engine:
            browser = engine.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": width, "height": 840})
            page = context.new_page()
            saved, posted, unexpected, errors = [], [], [], []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def mock(route):
                request = route.request
                path = request.url.split("/api/", 1)[-1]
                def reply(data, status=200):
                    return route.fulfill(status=status, content_type="application/json",
                                         body=json.dumps(data))
                if path == "capabilities":
                    return reply({"ofc": "ACTIVE", "qloo": "CHECK_REQUIRED",
                                  "llm": "CONNECTED", "storage": "ACTIVE",
                                  "product": "CHECK_REQUIRED",
                                  "capacity": {"qloo_requests_remaining": 15,
                                               "llm_requests_remaining": 15}})
                if path == "copilot/sessions" and request.method == "POST":
                    return reply({"session_id": SESSION, "expires_in_hours": 24}, 201)
                if path == "copilot/sessions/" + SESSION and request.method == "GET":
                    return reply({"session_id": SESSION, "messages": saved,
                                  "session_message_limit": 8})
                if (path == "copilot/sessions/" + SESSION + "/messages"
                        and request.method == "POST"):
                    payload = request.post_data_json
                    posted.append(payload)
                    if len(posted) == 1:
                        assert payload["prompt"] == ORIGINAL
                        assert "parent_run_id" not in payload
                        response = parent_run()
                    else:
                        assert len(posted) == 2
                        assert payload["parent_run_id"] == PARENT
                        text = payload["prompt"]
                        assert "NEW USER REQUEST" in text
                        assert "discovery_category='place' and choices=[]" in text
                        assert "Named taste seed: Manali Hills" in text
                        assert "snow scenery and mountain views" in text
                        assert "prefer quieter destinations" in text
                        assert "Travel month: 2026-12" in text
                        assert "Preferred destination region: Himachal Pradesh" in text
                        response = {
                            "run_id": CHILD, "prompt": text,
                            "status": "CAPABILITY_UNAVAILABLE",
                            "reason_code": "MOCK_NO_NEW_QLOO",
                            "question": "A linked taste request was saved without provider calls.",
                            "plan": {"objective": "Explore", "choices": [], "cities": []},
                            "release_authorized": False,
                            "external_action_executed": False,
                        }
                    saved.append(response)
                    return reply(response)
                unexpected.append(path)
                return reply({"error": "UNEXPECTED_ENDPOINT"}, 404)

            try:
                page.route("**/api/**", mock)
                page.goto(base_url + "/workspace", wait_until="domcontentloaded")
                page.locator("#ofc-intro-understand").click()
                page.locator("#ofc-intro").wait_for(state="detached")
                if page.locator("#scope-guide").is_visible():
                    page.locator("#scope-example").click()
                page.locator("#chat-input").fill(ORIGINAL)
                page.locator("#chat-send").click()
                page.locator("#run-approve").click()
                page.locator(".taste-explorer").wait_for(state="visible")
                page.wait_for_function(
                    "() => document.querySelector('#chat-input')?.readOnly === false",
                    timeout=10000,
                )
                assert len(posted) == 1
                assert "No verified place winner" in page.locator(".answer-advisory").inner_text()
                assert "0.9789" not in page.locator("#chat-transcript").inner_text()
                assert "Shimla" in page.locator(".taste-observed").inner_text()
                assert "Hotel Cherry Manali" not in page.locator(".taste-observed").inner_text()
                assert "not qualified hill-station alternatives" in page.locator(".taste-observed").inner_text()
                assert "2 place result(s)" in page.locator(".taste-observed").inner_text()
                assert page.locator(".taste-form").is_hidden()
                page.locator(".taste-open").click()
                page.locator(".taste-form").wait_for(state="visible")
                assert page.locator(".taste-input").first.input_value() == "Manali Hills"
                # A provider-status read must not remount an open, edited questionnaire.
                page.locator(".taste-input").first.fill("Chamonix")
                page.evaluate("window.__tastePanel = document.querySelector('.taste-explorer')")
                with page.expect_response(lambda r: "/api/capabilities" in r.url):
                    page.locator("#status-toggle").click()
                page.wait_for_timeout(150)
                assert page.evaluate(
                    "document.querySelector('.taste-explorer') === window.__tastePanel"
                ), "Capability refresh discarded the open Taste Explorer"
                assert page.locator(".taste-input").first.input_value() == "Chamonix"
                page.locator("#status-close").click()
                page.locator(".taste-input").first.fill("Manali Hills")
                assert len(posted) == 1
                page.locator(".taste-select").nth(0).select_option("snow_views")
                page.locator(".taste-select").nth(1).select_option("avoid_crowds")
                page.locator(".taste-select").nth(2).select_option("midrange")
                page.locator('input[type="month"]').fill("2026-12")
                page.locator(".taste-field.taste-wide input").nth(1).fill("Himachal Pradesh")
                page.locator(".taste-submit").click()
                page.locator("#run-confirm").wait_for(state="visible")
                assert len(posted) == 1
                assert "NEW linked" in page.locator("#run-confirm-budget").inner_text()
                page.locator("#run-cancel").click()
                assert len(posted) == 1
                page.locator(".taste-submit").click()
                page.locator("#run-approve").click()
                page.get_by_text("A linked taste request was saved without provider calls.").wait_for(
                    state="visible", timeout=10000)
                assert len(posted) == 2
                assert posted[0]["request_id"] != posted[1]["request_id"]
                assert not errors and not unexpected, (errors, unexpected)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                reports.append({"viewport": width, "questions_visible": True,
                                "parent_linked": True, "cancel_no_spend": True,
                                "unqualified_candidate_label": True})
            finally:
                context.close()
                browser.close()
    return {"result": "PASS", "cases": reports, "real_provider_requests": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    args = parser.parse_args()
    print(json.dumps(qualify(args.base_url.rstrip("/")), sort_keys=True))
