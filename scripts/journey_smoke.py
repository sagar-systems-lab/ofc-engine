"""Exercise the read-only hosted tour walkthrough with test-only Qloo-shaped evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

from smoke_hosted import normalize_url


def proof():
    ids = ("brand", "opener", "venue-1", "venue-2", "venue-3", "afterparty")
    decisions = [{
        "decision_id": item,
        "domain": "partner" if item == "brand" else "experience",
        "required": True,
        "depends_on": ["brand"] if item == "afterparty" else [],
        "candidate_count": 3 if item == "brand" else 1,
        "baseline_option": 1,
        "reviewed_option": 2 if item == "brand" else 1,
        "verdict": "PASS" if item == "brand" else "UNCERTAIN",
        "changed": item == "brand",
        "pending_recheck": item in ("brand", "afterparty"),
    } for item in ids]
    return {
        "journey_version": 1,
        "status": "OBSERVED_QLOO_REVIEW_REQUIRED",
        "source_mode": "CAPTURED_LIVE_QLOO_UNVERIFIED",
        "revision": 1,
        "decision_count": 6,
        "tour": {
            "objective": "Select culturally suitable partners and experiences for an emerging artist's three-city tour.",
            "budget_usd": 60000,
            "cities": ["Chicago", "Austin", "Seattle"],
            "audience": "Indie music audiences",
            "operator_constructed_unverified_decisions": 5,
            "decisions": decisions,
        },
        "changed_ids": ["brand"],
        "preserved_ids": list(ids[1:]),
        "invalidated_ids": ["brand", "afterparty"],
        "pending_rechecks": ["brand", "afterparty"],
        "blockers": [
            "UNCERTAIN:opener", "UNCERTAIN:venue-1", "UNCERTAIN:venue-2",
            "UNCERTAIN:venue-3", "UNCERTAIN:afterparty",
            "PENDING_RECHECKS", "PROVIDER_AUTHENTICATION_NOT_ESTABLISHED"
        ],
        "off_on": {
            "same_baseline": True,
            "off_brand": "UNCERTAIN",
            "on_brand": "REPAIR",
            "real_causal_proof": False,
        },
        "handoff": "WITHHELD",
        "provider_authenticated": False,
        "independent_recheck_completed": False,
        "release_authorized": False,
        "external_action_executed": False,
        "additional_provider_requests": 0,
        "evidence": {
            "collected_at": "2026-10-16T08:25:17Z",
            "query_fingerprint": "a" * 64,
            "capture_digest": "b" * 64,
            "baseline_plan_digest": "c" * 64,
            "current_plan_digest": "d" * 64,
            "journal_head": "e" * 64,
        },
    }


def qualify(origin: str) -> dict:
    js = (Path(__file__).resolve().parents[1] /
          "src/ofc_engine/web/hosted-showcase.js").read_text()
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for width, height in ((1440, 900), (375, 812), (320, 740)):
                page = browser.new_page(viewport={"width": width, "height": height},
                                        reduced_motion="reduce")
                errors = []
                posts = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: posts.append(request.url)
                        if request.method != "GET" else None)
                try:
                    page.route(origin + "/hosted-showcase.js",
                               lambda route: route.fulfill(
                                   status=200, content_type="text/javascript", body=js))
                    page.route(origin + "/api/showcase",
                               lambda route: route.fulfill(
                                   status=200, content_type="application/json",
                                   body=json.dumps(proof())))
                    page.goto(origin + "/", wait_until="domcontentloaded", timeout=90000)
                    page.locator("#ofc-intro").wait_for(state="visible")
                    assert not page.locator("#home-create").is_visible()
                    page.locator("#ofc-intro-understand").click()
                    page.locator("#ofc-intro").wait_for(state="detached")
                    page.locator("#home-create").click()
                    page.locator("#objective").wait_for(state="visible")
                    page.evaluate("""() => {
                        const script = document.createElement('script');
                        script.src = '/hosted-showcase.js';
                        document.head.appendChild(script);
                    }""")
                    start = page.locator("#start-qloo-journey")
                    start.wait_for(state="visible", timeout=10000)
                    start.click()
                    for heading, phrase in (
                        ("01 / PROPOSE", "$60,000"),
                        ("02 / QLOO CHECK", "Qloo OFF"),
                        ("03 / REPAIR", "REPAIRED"),
                        ("04 / VERIFY", "RECHECK PENDING"),
                        ("05 / HANDOFF", "HANDOFF WITHHELD"),
                    ):
                        assert page.locator("#qloo-journey-heading").inner_text() == heading
                        assert phrase in page.locator("#qloo-journey-body").inner_text()
                        if heading != "05 / HANDOFF":
                            page.locator("#qloo-journey-next").click()
                    page.locator("#qloo-journey-previous").click()
                    assert page.locator("#qloo-journey-heading").inner_text() == "04 / VERIFY"
                    assert "HANDOFF BLOCKED" == page.locator(
                        "#view-plan .state-item").nth(2).locator("small").inner_text()
                    assert "The separate editable draft below" in page.locator(
                        "#view-plan > .intro p").inner_text()
                    assert "artist-ci" not in page.locator("#published-qloo-review").inner_text()
                    assert not posts, f"unexpected provider/network request: {posts}"
                    assert not errors, errors
                    assert not page.evaluate(
                        "document.documentElement.scrollWidth > innerWidth + 1"
                    ), "guided tour overflowed mobile viewport"
                    results.append(width)
                finally:
                    page.close()

            page = browser.new_page()
            try:
                corrupt = proof()
                corrupt["tour"]["decisions"][0]["reviewed_option"] = 1
                page.route(origin + "/hosted-showcase.js",
                           lambda route: route.fulfill(
                               status=200, content_type="text/javascript", body=js))
                page.route(origin + "/api/showcase",
                           lambda route: route.fulfill(
                               status=200, content_type="application/json",
                               body=json.dumps(corrupt)))
                page.goto(origin + "/", wait_until="domcontentloaded", timeout=90000)
                page.locator("#ofc-intro").wait_for(state="visible")
                assert not page.locator("#home-create").is_visible()
                page.locator("#ofc-intro-understand").click()
                page.locator("#ofc-intro").wait_for(state="detached")
                page.locator("#home-create").click()
                page.evaluate("""() => {
                    const script = document.createElement('script');
                    script.src = '/hosted-showcase.js';
                    document.head.appendChild(script);
                }""")
                page.wait_for_timeout(350)
                assert page.locator("#start-qloo-journey").count() == 0
                assert page.locator("#published-qloo-review").is_hidden()
            finally:
                page.close()
        finally:
            browser.close()
    return {
        "result": "PASS",
        "input": "SIMULATED_REDACTED_QLOO_PROOF",
        "viewports": results,
        "stages": 5,
        "negative": "MISMATCHED_BRAND_OPTION_FAILS_CLOSED",
        "provider_requests": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    print(json.dumps(qualify(normalize_url(args.base_url)), sort_keys=True))


if __name__ == "__main__":
    main()
