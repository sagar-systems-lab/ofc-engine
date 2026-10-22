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
                provider_budget = {"qloo": 5}
                submitted = []
                pending_response = {"active": False, "polls": 0}
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
                                "capacity": {"qloo_requests_remaining": provider_budget["qloo"],
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
                        if pending_response["active"] and not submitted:
                            # An older saved conversation may be visible before the
                            # currently admitted request reaches durable session history.
                            messages = [{
                                **SAVED, "run_id": "cp-run-" + "c" * 32,
                                "prompt": "Previous saved comparison (not current request)",
                            }]
                            if pending_response["polls"]:
                                messages.append({
                                "run_id": FAKE_RUN, "status": "PROCESSING",
                                "prompt": (
                                    PROMPT + "\n\n--- USER-SUPPLIED TEXT NOTES (context, not Qloo evidence) ---\n"
                                    + "Document: production-notes.md\n"
                                    + "confidential planning context; artist, brand, venue\n"
                                    + "--- END USER-SUPPLIED NOTES ---"
                                ),
                                "activity": {
                                    "source": "PERSISTED_ATTEMPTS",
                                    "agent_plan": "ADMITTED",
                                    "qloo_evidence": "SEARCH_ONLY",
                                    "ofc_review": "NOT_OBSERVED",
                                    "results": "PROCESSING",
                                    "requests": {
                                        "search_complete": 1, "search_reserved": 0,
                                        "rank_complete": 0, "rank_reserved": 0,
                                    },
                                },
                                })
                            pending_response["polls"] += 1
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
                page.locator("#scope-guide").wait_for(state="visible")
                guide = page.locator("#scope-guide").inner_text()
                assert "Not a general chatbot." in guide
                assert "Outside this beta" in guide
                assert "bookings" in guide.lower() and "qloo" in guide.lower()
                assert calls["message"] == 0 and calls["provider"] == 0
                page.locator("#scope-example").click()
                assert "Taylor Swift" in page.locator("#chat-input").input_value()
                assert calls["message"] == 0 and calls["provider"] == 0
                page.locator("#chat-input").fill("")
                page.locator("#scope-reopen").click()
                page.locator("#scope-guide").wait_for(state="visible")
                page.locator("#scope-done").click()
                page.locator("#scope-guide").wait_for(state="hidden")
                assert page.evaluate("localStorage.getItem('ofc.copilot.scope-reviewed.v1')") == "yes"
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
                page.evaluate("""() => {
                  const underlying = window.fetch.bind(window);
                  window.fetch = (resource, options) => {
                    const url = typeof resource === "string" ? resource : resource.url;
                    if (url.includes("/messages") && options?.method === "POST") {
                      return new Promise(resolve => {
                        window.releaseSavedMessage = () => resolve(underlying(resource, options));
                      });
                    }
                    return underlying(resource, options);
                  };
                }""")
                input_box.press("Enter")
                pending_response["active"] = True
                page.locator("#run-approve").click()
                page.get_by_text("Waiting for a saved server result. No automatic retry will be made.").wait_for(
                    state="visible", timeout=8_000)
                page.locator(".processing-elapsed").wait_for(state="visible")
                assert "Submitting your review" in page.locator(".processing-heading").inner_text()
                hosting_note = page.locator(".processing-host-note")
                assert "Free Render hosting" in hosting_note.inner_text()
                assert "2–3 minutes or longer" in hosting_note.inner_text()
                assert "Keep this tab open" in hosting_note.inner_text()
                assert hosting_note.locator("a").count() == 2
                for label, expected_path in (("Plan Review", "/plans"), ("Lab", "/proof")):
                    link = hosting_note.get_by_role("link", name="Open " + label + " in a new tab")
                    assert link.get_attribute("href") == expected_path
                    assert link.get_attribute("target") == "_blank"
                    assert "noopener" in link.get_attribute("rel")
                with page.expect_popup() as explore:
                    hosting_note.get_by_role(
                        "link", name="Open Plan Review in a new tab"
                    ).click()
                explore.value.close()
                assert page.locator(".processing-panel").is_visible()
                assert calls["message"] == 0 and calls["provider"] == 0
                page.evaluate("""() => {
                  document.querySelector(".processing-elapsed").dataset.startedAt =
                    String(Date.now() - 125000);
                }""")
                page.wait_for_function("""() =>
                  document.querySelector(".processing-elapsed")?.textContent.startsWith("02:") &&
                  !document.querySelector(".processing-longwait")?.hidden""")
                assert "Do not resend" in page.locator(".processing-longwait").inner_text()
                assert page.locator(".message.user").count() == 1
                assert "confidential planning context" in page.locator(".message.user").inner_text()
                assert input_box.input_value() == ""
                page.wait_for_function("typeof window.releaseSavedMessage === 'function'")
                page.locator(".conversation-row").first.wait_for(state="attached", timeout=10_000)
                assert "confidential planning context" in page.locator(".message.user").inner_text()
                assert "Previous saved comparison" not in page.locator("#chat-transcript").inner_text()
                page.get_by_text("Qloo entity search saved. Ranking evidence not yet observed.").wait_for(
                    state="visible", timeout=15_000)
                assert "Stage details are based on saved server evidence" in page.locator(".processing-proof").inner_text()
                assert page.locator(".processing-elapsed").is_visible()
                assert page.locator("#activity-steps").inner_text().count("Qloo evidence") == 1
                assert "entity search" in page.locator("#activity-steps").inner_text()
                assert calls["message"] == 0 and calls["provider"] == 0
                pending_response["active"] = False
                provider_budget["qloo"] = 1  # reflect usage after the first completed review
                page.evaluate("window.releaseSavedMessage()")
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
                page.locator(".decision-brief").wait_for(state="visible")
                decision_brief = page.locator(".decision-brief").inner_text()
                assert "Qloo observation" in decision_brief
                assert "OFC intervention" in decision_brief
                assert "Partnership readiness" in decision_brief
                assert "HOLD" in decision_brief
                assert "Model explanation · not independently verified" in page.locator(".narrative-section").inner_text()
                flow = page.locator(".live-decision-graph")
                flow.wait_for(state="visible")
                assert flow.locator(".graph-decision").count() == 2
                assert flow.locator(".graph-stage").count() == 8
                assert "Arrows show review stages" in flow.inner_text()
                assert "Qloo cultural rank" in flow.inner_text()
                assert "Business gate" in flow.inner_text()
                assert "HOLD" in flow.inner_text()
                assert "30 decisions" not in flow.inner_text()

                page.locator("#mode-toggle").click()
                assert "OFC not applied" in page.locator(".live-decision-graph").inner_text()
                baseline = page.locator(".result-caveat").inner_text()
                assert "2 original choices. OFC controls not applied." in baseline
                assert "revised" not in baseline
                page.locator("#mode-toggle").click()
                assert "1 revised" in page.locator(".result-caveat").inner_text()
                # Archived, already-saved narration is scrubbed only for display.
                legacy = page.evaluate("""saved => {
                  const doc = {...saved, original_answer:
                    "Nike (00000000-0000-4000-8000-000000000003) remains original."};
                  const content = window.OFCResults.renderRun(doc, "OFF", (role, text) => {
                    const element = document.createElement("div");
                    element.textContent = text;
                    return element;
                  });
                  return content?.textContent || "";
                }""", SAVED)
                assert "Nike remains original." in legacy
                assert "00000000-0000-4000-8000-000000000003" not in legacy
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
                assert exported["ranked_evidence"] == []
                ranked = page.evaluate("""saved => {
                  const source = structuredClone(saved);
                  source._sessionReadConfirmed = true;
                  source.off.decisions[0].candidate_pool = ["Brand A", "Brand B"];
                  source.off.decisions[0].qloo_observation = {
                    ranked_candidate_ids: ["Brand B", "Brand A"],
                    source_mode: "LIVE_HTTP_UNVERIFIED",
                    query_fingerprint: "f".repeat(64),
                    capture_digest: "a".repeat(64),
                    collected_at: "2026-10-18T08:00:00Z"
                  };
                  source.on.decisions[0].rank_recheck_completed = true;
                  source.on.decisions[0].ofc_reason = "QLOO_RANK_RECHECK_MATCHED";
                  const proof = window.OFCResults.buildProof(source, "ON");
                  const resultCSV = window.OFCResults.csv(proof);
                  source.off.decisions[0].qloo_observation.ranked_candidate_ids[0] = "Unknown Brand";
                  const blocked = window.OFCResults.buildProof(source, "ON");
                  return {proof, resultCSV, blocked:blocked.ranked_evidence};
                }""", SAVED)
                assert ranked["proof"]["ranked_evidence"][0]["ranked_candidates"] == ["Brand B", "Brand A"]
                assert ranked["proof"]["ranked_evidence"][0]["rank_recheck_matched"]
                assert "Brand B > Brand A" in ranked["resultCSV"]
                assert ranked["blocked"] == []
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
                assert "Brand A → Brand B" in graph
                assert "Original → OFC reviewed" in graph
                assert "Depends on" in graph and "Experience A" in graph
                assert "Original preserved · review uncertain" in graph
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
                # A second user-authored review must not become a phantom run
                # when the refreshed global Qloo allowance is insufficient.
                input_box.fill("Review venues in Chicago, Austin and Seattle for this tour.")
                input_box.press("Enter")
                page.get_by_text("NOT SUBMITTED — only 1 Qloo calls remain", exact=False).wait_for(
                    state="visible", timeout=8_000)
                assert page.locator("#run-confirm").is_hidden()
                assert page.locator(".message.user").count() == 1
                assert "Review venues in Chicago" in page.locator(".message.user").inner_text()
                assert calls["message"] == 1 and calls["provider"] == 0
                assert page.locator(".conversation-row").count() == 1
                if width <= 1180:
                    page.locator("#sidebar-toggle").click()
                page.locator(".conversation-row").first.click()
                page.get_by_text("LLM + Qloo: the baseline cultural ranking is recorded.").wait_for(
                    state="visible")
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
                assert page.locator("#scope-guide").is_hidden()
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



def qualify_selection(origin: str) -> dict:
    # Mocked session and provider budgets; exercising the actual browser controls.
    results = []
    for width, height in ((1440, 900), (390, 844)):
        with sync_playwright() as engine:
            browser = engine.chromium.launch(
                headless=True, executable_path=os.environ.get("OFC_CHROMIUM_PATH"),
            )
            context = browser.new_context(viewport={"width": width, "height": height})
            page = context.new_page()
            posted = []
            provider_calls = []
            saved = []
            candidate = {"name": "Levi Strauss & Co.", "category": "brand",
                         "is_exact": False, "option_ref": "a" * 16}
            first = {
                "run_id": FAKE_RUN, "status": "CLARIFICATION_REQUIRED",
                "reason_code": "SEARCH_SELECTION_REQUIRED",
                "question": "Please select a matching Qloo entity or enter its exact name.",
                "entity_request": {"name": "Levi's", "category": "brand"},
                "suggestions": [candidate],
                "release_authorized": False, "external_action_executed": False,
                "plan": {"objective": PROMPT, "artist_name": "Example Artist",
                         "cities": [], "choices": []},
            }

            def mock(route):
                path = route.request.url.split("/api/", 1)[-1]
                method = route.request.method
                if path == "capabilities":
                    return route.fulfill(
                        status=200, content_type="application/json",
                        body=json.dumps({
                            "product": "CHECK_REQUIRED", "ofc": "ACTIVE",
                            "storage": "ACTIVE", "qloo": "CHECK_REQUIRED",
                            "llm": "CONNECTED", "session_message_limit": 8,
                            "capacity": {"qloo_requests_remaining": 12,
                                         "llm_requests_remaining": 9},
                        }),
                    )
                if path == "copilot/sessions" and method == "POST":
                    return route.fulfill(
                        status=201, content_type="application/json",
                        body=json.dumps({"session_id": FAKE_SESSION,
                                         "expires_in_hours": 24}),
                    )
                if path == "copilot/sessions/" + FAKE_SESSION and method == "GET":
                    return route.fulfill(
                        status=200, content_type="application/json",
                        body=json.dumps({"session_id": FAKE_SESSION,
                                         "session_message_limit": 8,
                                         "messages": saved}),
                    )
                if path == "copilot/sessions/" + FAKE_SESSION + "/messages" and method == "POST":
                    req = route.request.post_data_json
                    posted.append(req)
                    if len(posted) == 1:
                        assert set(req) == {"request_id", "prompt"}
                        output = {**first, "prompt": req["prompt"]}
                    else:
                        assert req["parent_run_id"] == FAKE_RUN
                        assert req["entity_selection_ref"] == candidate["option_ref"]
                        assert req["prompt"] == PROMPT
                        output = {
                            "run_id": "cp-run-" + "c" * 32,
                            "status": "CAPABILITY_UNAVAILABLE",
                            "question": "Offline selection boundary accepted.",
                            "prompt": req["prompt"], "release_authorized": False,
                        }
                    saved.append({**output, "_sessionReadConfirmed": True})
                    return route.fulfill(
                        status=200, content_type="application/json",
                        body=json.dumps(output),
                    )
                provider_calls.append(path)
                return route.fulfill(status=404, body="{}")

            try:
                page.route("**/api/**", mock)
                page.goto(origin + "/workspace", wait_until="domcontentloaded")
                page.locator("#ofc-intro-understand").click()
                page.locator("#ofc-intro").wait_for(state="detached")
                if page.locator("#scope-guide").is_visible():
                    page.locator("#scope-example").click()
                page.locator("#chat-input").fill(PROMPT)
                page.locator("#chat-send").click()
                page.locator("#run-confirm").wait_for(state="visible")
                page.locator("#run-approve").click()
                page.locator(".qloo-option").wait_for(state="visible", timeout=8000)
                assert "Levi's" in page.locator(".qloo-selection-title").inner_text()
                assert "Levi Strauss & Co." in page.locator(".qloo-option").inner_text()
                assert len(posted) == 1
                page.locator(".qloo-option").click()
                page.locator("#run-confirm").wait_for(state="visible")
                assert "new review" in page.locator("#run-confirm-budget").inner_text()
                assert len(posted) == 1
                page.locator("#run-cancel").click()
                assert len(posted) == 1
                page.locator(".qloo-option").click()
                page.locator("#run-approve").click()
                page.get_by_text("Offline selection boundary accepted.").wait_for(
                    state="visible", timeout=8000,
                )
                assert len(posted) == 2 and not provider_calls
                results.append({"viewport": width, "selection_requires_confirm": True,
                                "server_bound_option": True})
            finally:
                context.close()
                browser.close()
    return {"result": "PASS", "cases": results, "real_provider_requests": 0}


def qualify_confirmed_intake(origin: str) -> dict:
    results = []
    for width, height, outage in (
        (1440, 900, "MODEL_PROVIDER_UNAVAILABLE"),
        (390, 844, "MODEL_PROVIDER_UNAVAILABLE"),
        (1440, 900, "MODEL_NETWORK_UNAVAILABLE"),
        (390, 844, "MODEL_TIMEOUT"),
    ):
        with sync_playwright() as engine:
            browser = engine.chromium.launch(
                headless=True, executable_path=os.environ.get("OFC_CHROMIUM_PATH"),
            )
            context = browser.new_context(viewport={"width": width, "height": height})
            page = context.new_page()
            posted = []
            provider_calls = []
            saved = []
            failed = {
                "run_id": FAKE_RUN, "status": "BLOCKED",
                "reason_code": outage,
                "qloo_requests_this_run": 0, "llm_requests_this_run": 1,
                "release_authorized": False, "external_action_executed": False,
            }

            def mock(route):
                path = route.request.url.split("/api/", 1)[-1]
                method = route.request.method
                if path == "capabilities":
                    return route.fulfill(status=200, content_type="application/json",
                        body=json.dumps({
                            "product": "CHECK_REQUIRED", "ofc": "ACTIVE",
                            "storage": "ACTIVE", "qloo": "CHECK_REQUIRED",
                            "llm": "CONNECTED", "session_message_limit": 8,
                            "capacity": {"qloo_requests_remaining": 12,
                                         "llm_requests_remaining": 9},
                        }))
                if path == "copilot/sessions" and method == "POST":
                    return route.fulfill(status=201, content_type="application/json",
                        body=json.dumps({"session_id": FAKE_SESSION,
                                         "expires_in_hours": 24}))
                if path == "copilot/sessions/" + FAKE_SESSION and method == "GET":
                    return route.fulfill(status=200, content_type="application/json",
                        body=json.dumps({"session_id": FAKE_SESSION,
                                         "session_message_limit": 8, "messages": saved}))
                if path == "copilot/sessions/" + FAKE_SESSION + "/messages" and method == "POST":
                    req = route.request.post_data_json
                    posted.append(req)
                    if len(posted) == 1:
                        assert set(req) == {"request_id", "prompt"}
                        output = {**failed, "prompt": req["prompt"]}
                    else:
                        assert req["parent_run_id"] == FAKE_RUN
                        assert req["prompt"] == PROMPT
                        assert req["confirmed_plan"] == {
                            "artist": "Billie Eilish",
                            "brands": ["Nike", "Adidas"], "selected": "Nike",
                        }
                        assert "entity_selection_ref" not in req
                        output = {
                            "run_id": "cp-run-" + "d" * 32,
                            "status": "CAPABILITY_UNAVAILABLE",
                            "question": "Manual intake boundary accepted.",
                            "prompt": req["prompt"], "release_authorized": False,
                        }
                    saved.append({**output, "_sessionReadConfirmed": True})
                    return route.fulfill(status=200, content_type="application/json",
                        body=json.dumps(output))
                provider_calls.append(path)
                return route.fulfill(status=404, body="{}")

            try:
                page.route("**/api/**", mock)
                page.goto(origin + "/workspace", wait_until="domcontentloaded")
                page.locator("#ofc-intro-understand").click()
                page.locator("#ofc-intro").wait_for(state="detached")
                if page.locator("#scope-guide").is_visible():
                    page.locator("#scope-example").click()
                page.locator("#chat-input").fill(PROMPT)
                page.locator("#chat-send").click()
                page.locator("#run-approve").click()
                page.locator(".intake-recovery").wait_for(state="visible", timeout=8000)
                assert len(posted) == 1 and not provider_calls
                page.locator(".intake-recovery-field input").nth(0).fill("Billie Eilish")
                page.locator(".intake-recovery-field input").nth(1).fill("Nike")
                page.locator(".intake-recovery-field input").nth(2).fill("Adidas")
                page.locator(".intake-recovery-field select").select_option("0")
                page.get_by_role("button", name="Review confirmed details").click()
                page.locator("#run-confirm").wait_for(state="visible")
                assert "not model extracted" in page.locator("#run-confirm-budget").inner_text()
                assert len(posted) == 1
                page.locator("#run-cancel").click()
                assert len(posted) == 1
                page.get_by_role("button", name="Review confirmed details").click()
                page.locator("#run-approve").click()
                page.get_by_text("Manual intake boundary accepted.").wait_for(
                    state="visible", timeout=8000)
                assert len(posted) == 2 and not provider_calls
                results.append({"viewport": width, "outage": outage,
                                "confirmed_plan_scoped": True,
                                "cancel_has_no_spend": True})
            finally:
                context.close()
                browser.close()
    return {"result": "PASS", "cases": results, "real_provider_requests": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    report = qualify(base)
    report["entity_selection"] = qualify_selection(base)
    report["confirmed_intake"] = qualify_confirmed_intake(base)
    print(json.dumps(report, sort_keys=True))
