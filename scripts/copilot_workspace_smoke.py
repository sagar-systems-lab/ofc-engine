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
    "baseline_digest": "a" * 64,
    "reviewed_digest": "d" * 64,
    "qloo_evidence_digest": "e" * 64,
    "prompt_digest": "f" * 64,
    "value_receipt": {
        "schema_version": "ofc-value.v1",
        "run_id": FAKE_RUN,
        "shared_qloo_evidence_digest": "e" * 64,
        "model_id": "mock-llm",
        "decision_count": 2,
        "changed_count": 1,
        "receipt_digest": "c" * 64,
        "business_evidence_satisfied": False,
    },
    "off": {
        "ofc_enabled": False,
        "plan_digest": "a" * 64,
        "qloo": {"provider": "QLOO", "evidence_digest": "e" * 64, "observations": []},
        "decisions": [
            {"decision_id": "brand", "domain": "brand", "candidate_id": "Brand A",
             "cultural_rank_observed": True},
            {"decision_id": "afterparty", "domain": "experience",
             "candidate_id": "Experience A", "cultural_rank_observed": False},
        ],
    },
    "on": {
        "ofc_enabled": True, "plan_digest": "d" * 64, "changed_ids": ["brand"],
        "qloo": {"provider": "QLOO", "evidence_digest": "e" * 64, "observations": []},
        "decisions": [
            {"decision_id": "brand", "domain": "brand", "changed": True,
             "candidate_id": "Brand B", "ofc_verdict": "UNCERTAIN",
             "ofc_reason": "RANK_CHANGED"},
            {"decision_id": "afterparty", "domain": "experience", "changed": False,
             "candidate_id": "Experience A", "ofc_verdict": "UNCERTAIN",
             "ofc_reason": "NO_VERIFIED_RANK"},
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
                calls = {"session": 0, "message": 0, "provider": 0,
                         "demo": 0, "owner_read": 0, "owner_login": 0, "owner_test": 0}
                owner_active = {"value": False}
                submitted = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def mock_request(route):
                    req = route.request
                    path = req.url.split("/api/", 1)[-1]
                    if path.startswith("demo?scenario="):
                        calls["demo"] += 1
                        scenario = path.split("=", 1)[1]
                        assert scenario in {"repair", "outage", "dependent_missing", "budget_exhausted"}
                        return route.fulfill(status=200, content_type="application/json",
                            body=json.dumps({
                                "scenario": scenario, "demo_mode": "SYNTHETIC",
                                "source_mode": "SYNTHETIC", "run_status": "REVIEW_REQUIRED",
                                "provider_authenticated": False, "real_qloo_requests": 0,
                                "decisions": [{"id": "brand", "changed": True}],
                                "pending_rechecks": [],
                            }))
                    if path == "owner/credentials" and req.method == "GET":
                        calls["owner_read"] += 1
                        if not owner_active["value"]:
                            return route.fulfill(status=403, content_type="application/json",
                                body='{"error":"ACCESS_DENIED"}')
                        return route.fulfill(status=200, content_type="application/json",
                            body=json.dumps({"providers": {
                                "qloo": {"status": "CHECK_REQUIRED", "configured": True,
                                         "masked_suffix": "8354", "last_test": {
                                          "outcome": "ACTIVE", "freshness": "EXPIRED",
                                          "checked_at": "2026-10-17T10:00:00Z"}},
                                "gemini": {"status": "CONNECTED", "configured": True,
                                           "qualification": "CONNECTIVITY_ONLY"},
                                "groq": {"status": "NOT_CONFIGURED", "configured": False}
                            }, "connection_tests": {"limit": 6, "used": 5, "remaining": 1,
                                                     "resets_at_utc": "2026-10-18T00:00:00Z"}}))
                    if path == "owner/login" and req.method == "POST":
                        calls["owner_login"] += 1
                        assert len(req.post_data_json["owner_secret"]) >= 40
                        owner_active["value"] = True
                        return route.fulfill(status=200, content_type="application/json",
                            body='{"authenticated":true,"expires_in_minutes":30}')
                    if path == "owner/test-connection":
                        calls["owner_test"] += 1
                        return route.fulfill(status=500, body='{"error":"UNEXPECTED_TEST"}')
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
                        messages = [{**SAVED, "prompt": submitted[0]}] if submitted else []
                        return route.fulfill(
                            status=200, content_type="application/json",
                            body=json.dumps({"session_id": FAKE_SESSION, "messages": messages}),
                        )
                    if path == "copilot/sessions/" + FAKE_SESSION + "/messages" and req.method == "POST":
                        calls["message"] += 1
                        body = req.post_data_json
                        assert body["prompt"].startswith(PROMPT)
                        assert "USER-SUPPLIED TEXT NOTES" in body["prompt"]
                        assert "production-notes.md" in body["prompt"]
                        assert "confidential planning context" in body["prompt"]
                        assert len(body["prompt"]) <= 4000
                        submitted.append(body["prompt"])
                        assert body["request_id"].startswith("ui-")
                        assert len(body["request_id"]) >= 8
                        return route.fulfill(
                            status=200, content_type="application/json",
                            body=json.dumps({**SAVED, "prompt": body["prompt"]}),
                        )
                    calls["provider"] += 1
                    return route.fulfill(status=404, body="{}")

                page.route("**/api/**", mock_request)
                page.goto(origin + "/workspace", wait_until="domcontentloaded")
                page.locator("#ofc-intro").wait_for(state="visible")
                assert not page.locator("#empty-title").is_visible()
                assert page.locator("body > .shell").get_attribute("inert") == ""
                assert calls["session"] == 0 and calls["message"] == 0
                assert calls["provider"] == 0 and calls["owner_test"] == 0
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
                page.locator("#status-toggle").click()
                page.locator("#status-mode-explanation").wait_for(state="visible")
                assert "Selected view: OFC OFF" in page.locator("#status-mode-explanation").inner_text()
                assert "OFC engine available" in page.locator("#status-rows").inner_text()
                assert "ACTIVE" in page.locator("#status-rows").inner_text()
                page.locator("#status-close").click()
                page.locator("#mode-toggle").click()
                assert calls["message"] == 0
                page.locator("#status-toggle").click()
                page.locator("#status-rows .status-row").first.wait_for(state="visible")
                assert "Qloo" in page.locator("#status-rows").inner_text()
                page.locator("#status-close").click()
                assert calls["provider"] == 0
                page.locator("#demo-entry").click()
                page.locator("#demo-dialog").wait_for(state="visible")
                page.locator("[data-demo=repair]").click()
                page.get_by_text("SYNTHETIC · NO PROVIDER CALLS").wait_for(state="visible")
                assert "Selective repair" in page.locator("#demo-result").inner_text()
                assert calls["demo"] == 1 and calls["owner_test"] == 0
                if width == 1440:
                    page.screenshot(path=str(review / "copilot-demo-desktop.png"),
                                    full_page=True)
                page.locator("#demo-close").click()
                page.locator("#settings-entry").click()
                page.locator("#settings-dialog").wait_for(state="visible")
                page.locator("#settings-public-status .settings-public-row").first.wait_for(
                    state="visible")
                page.locator("#settings-owner-form").wait_for(state="visible")
                assert page.locator("#settings-owner-manager").is_hidden()
                assert calls["owner_test"] == 0
                page.locator("[data-settings-tab=preferences]").click()
                page.locator("#settings-reduce-motion").check()
                assert page.locator("body").evaluate(
                    "el=>el.classList.contains('workspace-reduced-motion')")
                page.locator("#settings-reduce-motion").uncheck()
                page.locator("[data-settings-tab=privacy]").click()
                assert "24-hour" in page.locator("#settings-privacy").inner_text()
                page.locator("[data-settings-tab=about]").click()
                assert "Qloo Taste AI" in page.locator("#settings-about").inner_text()
                page.locator("[data-settings-tab=connections]").click()
                page.locator("#settings-owner-secret").fill("x" * 48)
                page.locator("#settings-owner-form button").click()
                try:
                    page.locator("#settings-owner-manager").wait_for(
                        state="visible", timeout=12_000)
                except PlaywrightTimeout as exc:
                    raise AssertionError(
                        f"owner drawer did not unlock: owner_login={calls['owner_login']}, "
                        f"owner_read={calls['owner_read']}, "
                        f"status={page.locator('#settings-owner-feedback').inner_text()}, "
                        f"panel_hidden={page.locator('#settings-connections').is_hidden()}, "
                        f"js_errors={errors}"
                    ) from exc
                assert page.locator("#settings-owner-secret").input_value() == ""
                assert "API only" in page.locator("#settings-owner-providers").inner_text()
                assert "1 / 6" in page.locator("#settings-test-quota").inner_text()
                assert calls["owner_login"] == 1 and calls["owner_test"] == 0
                if width == 1440:
                    page.screenshot(path=str(review / "copilot-settings-desktop.png"),
                                    full_page=True)
                page.locator("#settings-close").click()
                assert calls["message"] == 0 and calls["provider"] == 0
                assert calls["owner_test"] == 0
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

                input_box = page.locator("#chat-input")
                assert page.locator("#attachment-add").is_visible()
                page.locator("#attachment-file").set_input_files({
                    "name": "production-notes.pdf", "mimeType": "application/pdf",
                    "buffer": b"%PDF-1.4",})
                assert page.locator("#attachment-chip").is_hidden()
                assert "PDF/DOCX" in page.locator("#composer-status").inner_text()
                page.locator("#attachment-file").set_input_files({
                    "name": "huge-notes.txt", "mimeType": "text/plain",
                    "buffer": b"A" * (16 * 1024 + 1),})
                assert page.locator("#attachment-chip").is_hidden()
                page.locator("#attachment-file").set_input_files({
                    "name": "production-notes.md", "mimeType": "text/markdown",
                    "buffer": b"confidential planning context; artist, brand, venue",})
                page.locator("#attachment-chip").wait_for(state="visible")
                assert calls["message"] == 0 and calls["provider"] == 0
                input_box.fill("x" * 3999)
                assert not page.locator("#chat-send").is_enabled()
                assert "4,000 characters" in page.locator("#composer-status").inner_text()
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
                assert len(submitted) == 1
                assert page.locator("#attachment-chip").is_hidden()
                page.locator("#proof-entry").wait_for(state="visible")
                assert page.locator("#proof-entry").is_enabled()
                page.get_by_role("button", name="Compare OFF vs ON").click()
                page.locator("#comparison-dialog").wait_for(state="visible")
                assert "Brand B" in page.locator("#comparison-rows").inner_text()
                assert "Same Qloo evidence" in page.locator("#comparison-title").inner_text()
                assert calls["message"] == 1 and calls["provider"] == 0
                if width == 1440:
                    page.screenshot(path=str(review / "copilot-comparison-desktop.png"),
                                    full_page=True)
                page.locator("#comparison-close").click()
                page.locator("#proof-entry").click()
                page.locator("#proof-dialog").wait_for(state="visible")
                assert "Not independently signed" in page.locator("#proof-dialog").inner_text() or (
                    "not independently signed" in page.locator("#proof-dialog").inner_text())
                if width == 1440:
                    page.screenshot(path=str(review / "copilot-proof-desktop.png"),
                                    full_page=True)
                with page.expect_download() as receipt_file:
                    page.locator("#proof-json-download").click()
                exported = json.loads(Path(receipt_file.value.path()).read_text())
                assert exported["kind"] == "paired-saved-run-export"
                assert exported["comparison_available"] is True
                assert exported["qloo_evidence_digest"] == "e" * 64
                assert exported["decisions"][0]["changed"] is True
                assert "prompt" not in exported and "private" not in json.dumps(exported).lower()
                with page.expect_download() as csv_file:
                    page.locator("#proof-csv-download").click()
                csv_bytes = Path(csv_file.value.path()).read_bytes()
                assert b"Brand A" in csv_bytes and b"Brand B" in csv_bytes
                assert calls["message"] == 1 and calls["provider"] == 0
                if width == 1440:
                    negative = page.evaluate("""saved => {
                      const source = {...saved, _sessionReadConfirmed: true};
                      const bad = {...source, on: {...source.on,
                        qloo: {...source.on.qloo, evidence_digest: "0".repeat(64)}}};
                      const partial = {...source, status: "BLOCKED", off: null, on: null};
                      const injection = structuredClone(source);
                      injection.off.decisions[0].candidate_id = "=HYPERLINK(1,2)";
                      const blockedExport = window.OFCResults.buildProof(partial, "ON");
                      const formulaCSV = window.OFCResults.csv(
                        window.OFCResults.buildProof(injection, "ON"));
                      return {
                        mismatchRejected: window.OFCResults.pairing(bad) === null,
                        blockedExport: blockedExport?.kind,
                        blockedPaired: blockedExport?.comparison_available,
                        formulaProtected: formulaCSV?.includes("\\\"'=HYPERLINK(1,2)\\\""),
                        noPrompt: !Object.hasOwn(blockedExport, "prompt")
                      };
                    }""", SAVED)
                    assert negative["mismatchRejected"]
                    assert negative["blockedExport"] == "saved-status-export"
                    assert negative["blockedPaired"] is False
                    assert negative["formulaProtected"]
                    assert negative["noPrompt"]
                page.locator("#proof-close").click()
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
