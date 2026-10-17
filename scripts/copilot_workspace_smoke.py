"""Browser qualification of the real workspace against controlled HTTP responses."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


FAKE_SESSION = "cp-" + "a" * 32
FAKE_RUN = "cp-run-" + "b" * 32
PROMPT = "Review a hypothetical brand partnership for a named artist."
SAVED = {
    "run_id": FAKE_RUN,
    "prompt": PROMPT,
    "request_id": "ui-test",
    "status": "REVIEW_REQUIRED",
    "original_answer": "LLM + Qloo: the baseline cultural ranking is recorded.",
    "ofc_answer": "LLM + Qloo + OFC: a decision still requires review.",
    "model_id": "mock-llm",
    "plan": {
        "objective": "Review an artist's brand partnership.",
        "artist_name": "Example Artist",
        "budget_usd": 60000,
        "cities": ["Chicago"],
        "choices": [
            {
                "decision_id": "brand",
                "domain": "brand",
                "selected_name": "Brand A",
                "candidate_names": ["Brand A", "Brand B"],
                "depends_on": [],
            },
            {
                "decision_id": "afterparty",
                "domain": "experience",
                "selected_name": "Experience A",
                "candidate_names": ["Experience A"],
                "depends_on": ["brand"],
            },
        ],
    },
    "off": {"ofc_enabled": False, "qloo": {"evidence_digest": "x"}},
    "on": {
        "ofc_enabled": True, "qloo": {"evidence_digest": "x"},
        "decisions": [
            {"decision_id": "brand", "changed": True, "candidate_id": "Brand B",
             "ofc_verdict": "UNCERTAIN"},
            {"decision_id": "afterparty", "changed": False,
             "candidate_id": "Experience A", "ofc_verdict": "UNCERTAIN"},
        ],
    },
    "activity": {
        "source": "PERSISTED_ATTEMPTS", "agent_plan": "OBSERVED",
        "qloo_evidence": "RANK_OBSERVED", "ofc_review": "OBSERVED",
        "results": "REVIEW_REQUIRED",
        "requests": {
            "search_complete": 3, "search_reserved": 0,
            "rank_complete": 1, "rank_reserved": 0,
        },
    },
    "release_authorized": False,
    "external_action_executed": False,
}


def qualify(origin: str) -> dict:
    cases = []
    with sync_playwright() as engine:
        browser = engine.chromium.launch(
            headless=True, executable_path=os.environ.get("OFC_CHROMIUM_PATH"),
        )
        try:
            for width, height in ((1440, 900), (1024, 768), (390, 844), (320, 740)):
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    reduced_motion="reduce",
                )
                page = context.new_page()
                errors = []
                calls = {"session": 0, "message": 0, "provider": 0}
                page.on("pageerror", lambda error: errors.append(str(error)))

                def mock_request(route):
                    req = route.request
                    path = req.url.split("/api/", 1)[-1]
                    if path == "capabilities":
                        return route.fulfill(
                            status=200, content_type="application/json",
                            body=json.dumps({
                                "ofc": "ACTIVE", "storage": "ACTIVE",
                                "qloo": "CHECK_REQUIRED", "llm": "CONNECTED",
                                "product": "CHECK_REQUIRED",
                                "capacity": {"qloo_requests_remaining": 5,
                                             "llm_requests_remaining": 4},
                            }),
                        )
                    if path == "copilot/sessions" and req.method == "POST":
                        calls["session"] += 1
                        assert req.post_data_json == {"confirm": "CREATE_COPILOT_SESSION"}
                        return route.fulfill(
                            status=201, content_type="application/json",
                            body=json.dumps({"session_id": FAKE_SESSION, "expires_in_hours": 24}),
                        )
                    if path == "copilot/sessions/" + FAKE_SESSION and req.method == "GET":
                        messages = [SAVED] if calls["message"] else []
                        return route.fulfill(
                            status=200, content_type="application/json",
                            body=json.dumps({"session_id": FAKE_SESSION, "messages": messages}),
                        )
                    if path == "copilot/sessions/" + FAKE_SESSION + "/messages" and req.method == "POST":
                        calls["message"] += 1
                        body = req.post_data_json
                        assert body["prompt"] == PROMPT
                        assert body["request_id"].startswith("ui-")
                        assert len(body["request_id"]) >= 8
                        return route.fulfill(
                            status=200, content_type="application/json",
                            body=json.dumps(SAVED),
                        )
                    calls["provider"] += 1
                    return route.fulfill(status=404, body="{}")

                page.route("**/api/**", mock_request)
                page.goto(origin + "/workspace", wait_until="domcontentloaded")
                page.locator("#ofc-intro").wait_for(state="visible")
                assert not page.locator("#empty-title").is_visible()
                assert page.locator("body > .shell").get_attribute("inert") == ""
                assert calls == {"session": 0, "message": 0, "provider": 0}
                page.locator("#ofc-intro-understand").click()
                page.locator("#ofc-intro").wait_for(state="detached")
                page.locator("#empty-title").wait_for(state="visible")
                assert not page.locator("#chat-send").is_enabled()
                assert page.locator("#conversation-sidebar").count() == 1
                assert page.locator("#mode-toggle").is_visible()
                assert page.locator("#status-toggle").is_visible()
                assert not page.locator("#proof-entry").is_enabled()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                review = Path("ui-review")
                review.mkdir(exist_ok=True)
                page.screenshot(path=str(review / f"copilot-empty-{width}.png"), full_page=True)
                assert calls["session"] == 0 and calls["message"] == 0
                page.locator("#mode-toggle").click()
                assert page.locator("#mode-label").inner_text() == "OFF"
                page.locator("#mode-toggle").click()
                assert calls["message"] == 0
                page.locator("#status-toggle").click()
                page.locator("#status-rows .status-row").first.wait_for(state="visible")
                assert "Qloo" in page.locator("#status-rows").inner_text()
                page.locator("#status-close").click()
                assert calls["provider"] == 0

                input_box = page.locator("#chat-input")
                input_box.fill(PROMPT)
                input_box.press("Enter")
                page.locator("#run-confirm").wait_for(state="visible")
                assert calls["message"] == 0
                page.locator("#run-cancel").click()
                assert calls["session"] == 0
                input_box.press("Enter")
                page.locator("#run-approve").click()
                try:
                    page.locator(".message.assistant").wait_for(state="visible", timeout=10_000)
                except PlaywrightTimeout as exc:
                    raise AssertionError(
                        f"chat result not visible: calls={calls}, "
                        f"status={page.locator('#composer-status').inner_text()}, "
                        f"dialog_open={page.locator('#run-confirm').evaluate('x=>x.open')}, "
                        f"errors={errors}"
                    ) from exc
                page.get_by_text("LLM + Qloo + OFC: a decision still requires review.").wait_for(
                    state="visible",
                )
                assert calls["session"] == 1
                assert calls["message"] == 1
                if width <= 1180:
                    page.locator("#activity-open").click()
                page.locator("#activity-rail").wait_for(state="visible")
                assert "Saved rank observations present" in page.locator("#activity-steps").inner_text()
                page.locator("#plan-expand").click()
                assert "Example Artist" in page.locator("#plan-content").inner_text()
                assert "Agent proposal" in page.locator("#plan-content").inner_text()
                page.locator("#map-expand").click()
                graph = page.locator("#map-content").inner_text()
                assert "Brand A" in graph and "Depends on" in graph
                assert "Revised by OFC" in graph
                assert calls["message"] == 1 and calls["provider"] == 0
                if width <= 1180:
                    page.locator("#activity-hide").click()
                if width == 1440:
                    page.screenshot(path=str(review / "copilot-saved-fixture-desktop.png"),
                                    full_page=True)
                page.locator("#mode-toggle").click()
                page.get_by_text("LLM + Qloo: the baseline cultural ranking is recorded.").wait_for(
                    state="visible",
                )
                assert calls["message"] == 1 and calls["provider"] == 0
                if width <= 768:
                    page.locator("#sidebar-toggle").click()
                    page.locator("#sidebar-backdrop").wait_for(state="visible")
                page.locator("#new-chat").click()
                page.locator("#empty-title").wait_for(state="visible")
                assert calls["session"] == 1 and calls["message"] == 1
                page.reload(wait_until="domcontentloaded")
                page.locator("#ofc-intro").wait_for(state="visible")
                assert not page.locator("#empty-title").is_visible()
                page.locator("#ofc-intro-understand").click()
                page.locator("#ofc-intro").wait_for(state="detached")
                page.get_by_text("LLM + Qloo + OFC: a decision still requires review.").wait_for(
                    state="visible",
                )
                assert calls["session"] == 1 and calls["message"] == 1
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                assert not errors, errors
                cases.append({"viewport": width, "gated": True, "one_send": True,
                              "toggle_no_spend": True, "replay_no_spend": True})
                context.close()
        finally:
            browser.close()
    return {"result": "PASS", "cases": cases, "real_provider_requests": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    args = parser.parse_args()
    print(json.dumps(qualify(args.base_url.rstrip("/")), sort_keys=True))
