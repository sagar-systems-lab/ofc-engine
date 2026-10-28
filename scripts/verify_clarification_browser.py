"""Keyless browser regression for saved missing-detail continuation."""
import argparse
import json

from playwright.sync_api import sync_playwright

SESSION = "cp-" + "a" * 32
PARENT = "cp-run-" + "b" * 32
CHILD = "cp-run-" + "c" * 32
ORIGINAL = "Bhai mujhe koi hill station jaana hai koi best Hill Station suggest kar."


def check(base_url):
    results = []
    for width, reason in (
        (1440, "TASTE_SEED_REQUIRED"),
        (390, "TASTE_SEED_REQUIRED"),
        (390, "DECISIONS_MISSING"),
        (1440, "TASTE_SEED_NOT_FOUND"),
        (390, "SEARCH_EXACT_NAME_NOT_FOUND"),
    ):
        with sync_playwright() as engine:
            browser = engine.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": width, "height": 840})
            page = context.new_page()
            saved, posted, unexpected, errors = [], [], [], []
            page.on("pageerror", lambda exc: errors.append(str(exc)))

            def respond(route):
                req = route.request
                path = req.url.split("/api/", 1)[-1]
                method = req.method
                def jsonreply(payload, status=200):
                    return route.fulfill(
                        status=status, content_type="application/json",
                        body=json.dumps(payload),
                    )
                if path == "capabilities":
                    return jsonreply({
                        "ofc": "ACTIVE", "storage": "ACTIVE",
                        "qloo": "CHECK_REQUIRED", "llm": "CONNECTED",
                        "product": "CHECK_REQUIRED",
                        "session_message_limit": 8,
                        "capacity": {"qloo_requests_remaining": 20,
                                     "llm_requests_remaining": 20},
                    })
                if path == "copilot/sessions" and method == "POST":
                    return jsonreply({"session_id": SESSION, "expires_in_hours": 24}, 201)
                if path == "copilot/sessions/" + SESSION and method == "GET":
                    return jsonreply({"session_id": SESSION,
                                      "session_message_limit": 8, "messages": saved})
                if path == "copilot/sessions/" + SESSION + "/messages" and method == "POST":
                    data = req.post_data_json
                    posted.append(data)
                    if len(posted) == 1:
                        assert set(data) == {"request_id", "prompt"}
                        assert data["prompt"] == ORIGINAL
                        result = {
                            "run_id": PARENT, "prompt": ORIGINAL,
                            "status": "BLOCKED" if reason == "SEARCH_EXACT_NAME_NOT_FOUND" else "CLARIFICATION_REQUIRED",
                            "reason_code": reason,
                            "question": "Please provide a cultural reference.",
                            "plan": {"objective": ORIGINAL, "choices": [], "cities": []},
                            "qloo_requests_this_run": 0,
                            "llm_requests_this_run": 1,
                            "release_authorized": False,
                            "external_action_executed": False,
                        }
                    else:
                        assert len(posted) == 2
                        if reason == "SEARCH_EXACT_NAME_NOT_FOUND":
                            assert "parent_run_id" not in data
                        else:
                            assert data["parent_run_id"] == PARENT
                        assert data["prompt"].startswith(ORIGINAL + "\n\n")
                        if reason != "DECISIONS_MISSING":
                            assert "Taste seed name: " in data["prompt"]
                            assert "Taste seed category: place" in data["prompt"]
                            assert "Use this latest named taste seed" in data["prompt"]
                        else:
                            assert "Shimla versus Mussoorie" in data["prompt"]
                        result = {
                            "run_id": CHILD, "prompt": data["prompt"],
                            "status": "CAPABILITY_UNAVAILABLE",
                            "reason_code": "OBSERVATION_ONLY",
                            "question": "A new scoped review was saved; real Qloo evidence is unavailable.",
                            "plan": {"objective": ORIGINAL, "choices": [], "cities": []},
                            "qloo_requests_this_run": 0,
                            "llm_requests_this_run": 0,
                            "release_authorized": False,
                            "external_action_executed": False,
                        }
                    saved.append(result)
                    return jsonreply(result)
                unexpected.append(path)
                return jsonreply({"error": "UNEXPECTED_ENDPOINT"}, 404)

            try:
                page.route("**/api/**", respond)
                page.goto(base_url + "/workspace", wait_until="domcontentloaded")
                page.locator("#ofc-intro-understand").click()
                page.locator("#ofc-intro").wait_for(state="detached")
                if page.locator("#scope-guide").is_visible():
                    page.locator("#scope-example").click()
                page.locator("#chat-input").fill(ORIGINAL)
                page.locator("#chat-send").click()
                assert not posted
                page.locator("#run-approve").click()
                page.locator(".clarification-panel").wait_for(state="visible")
                # A saved response may render before the request's finally block.
                # Continue must become enabled once submission settles.
                page.locator(".clarification-continue").wait_for(state="visible")
                page.wait_for_function(
                    "() => document.querySelector('.clarification-continue')?.disabled === false",
                    timeout=10000,
                )
                assert len(posted) == 1
                assert page.locator(".clarification-field").count() >= 1
                assert page.locator(".clarification-feedback").count() == 1
                assert page.locator("#run-confirm").evaluate("node => node.open") is False
                page.locator(".clarification-continue").click()
                assert "required" in page.locator(".clarification-feedback").inner_text()
                assert len(posted) == 1
                if reason != "DECISIONS_MISSING":
                    page.locator(".clarification-input").fill(
                        "Something beach side or something like glowing city beach side and chill"
                    )
                    page.locator(".clarification-continue").click()
                    assert "named place or entity" in page.locator(".clarification-feedback").inner_text()
                    assert len(posted) == 1
                value = "Shimla versus Mussoorie" if reason == "DECISIONS_MISSING" else "Baga Beach"
                page.locator(".clarification-input").fill(value)
                if reason != "DECISIONS_MISSING":
                    page.locator(".clarification-type").select_option("place")
                page.locator(".clarification-continue").click()
                page.locator("#run-confirm").wait_for(state="visible")
                assert len(posted) == 1
                page.locator("#run-cancel").click()
                assert len(posted) == 1
                page.locator(".clarification-continue").click()
                page.locator("#run-approve").click()
                page.get_by_text("A new scoped review was saved; real Qloo evidence is unavailable.").wait_for(
                    state="visible", timeout=10000,
                )
                assert len(posted) == 2
                assert posted[0]["request_id"] != posted[1]["request_id"]
                assert not unexpected and not errors
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                results.append({
                    "viewport": width, "reason": reason,
                    "inline_required": True, "cancel_no_spend": True,
                    "linked_parent": reason != "SEARCH_EXACT_NAME_NOT_FOUND",
                    "descriptive_seed_rejected": reason != "DECISIONS_MISSING",
                    "provider_network_calls": 0,
                })
            finally:
                context.close()
                browser.close()
    return {"result": "PASS", "cases": results, "real_provider_requests": 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    args = parser.parse_args()
    print(json.dumps(check(args.base_url.rstrip("/")), sort_keys=True))


if __name__ == "__main__":
    main()
