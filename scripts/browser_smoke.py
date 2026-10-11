"""Chromium journey check for the deployed synthetic proof console."""
from __future__ import annotations

import argparse
import json
import os
import sys

from playwright.sync_api import sync_playwright
from smoke_hosted import normalize_url


class BrowserFailure(RuntimeError):
    pass


def run_browser(origin: str) -> dict:
    origin = normalize_url(origin)
    tested = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=os.environ.get("OFC_CHROMIUM_PATH"))
        try:
            for width, height in ((1440, 900), (375, 812), (320, 740)):
                page = browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce")
                errors: list[str] = []
                outside: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("request", lambda request: outside.append(request.url) if not request.url.startswith(origin + "/") else None)
                try:
                    page.goto(origin + "/", wait_until="domcontentloaded", timeout=90_000)
                    page.locator("#objective").wait_for(state="visible", timeout=40_000)
                    if not page.get_by_text("PROVIDER PENDING").is_visible():
                        raise BrowserFailure("provider boundary missing from writable workspace")
                    page.locator("#validate").click()
                    if page.locator("#proposal-state").inner_text() != "Contract valid":
                        raise BrowserFailure("default typed proposal cannot be validated")
                    if page.locator("#export-proposal").is_disabled():
                        raise BrowserFailure("validated proposal cannot be exported")
                    page.locator("#objective").fill("Custom cultural decision proposal")
                    if not page.locator("#export-proposal").is_disabled():
                        raise BrowserFailure("changed proposal reused stale validation")
                    page.locator("#save-draft").click()
                    page.reload(wait_until="domcontentloaded")
                    page.locator("#draft-search").fill("Custom cultural")
                    if page.locator("#draft-list .draft-item").count() != 1:
                        raise BrowserFailure("draft save/search failed")
                    page.goto(origin + "/proof", wait_until="domcontentloaded", timeout=90_000)
                    page.locator("#run-status").wait_for(state="visible", timeout=40_000)
                    if page.locator("#run-status").inner_text().strip() != "INTEGRITY ONLY":
                        raise BrowserFailure("initial fixture check not visible")
                    if not page.get_by_text("SANDBOX / SYNTHETIC").is_visible():
                        raise BrowserFailure("synthetic provenance banner missing")
                    if not page.get_by_text("NOT AUTHORIZED").is_visible():
                        raise BrowserFailure("blocked release state missing")
                    toggle = page.locator("#inspect-toggle")
                    toggle.click()
                    if toggle.get_attribute("aria-expanded") != "true" or not page.locator("#inspector-details").is_visible():
                        raise BrowserFailure("inspector progressive disclosure broken")
                    verdicts = page.locator("#verdict-list .verdict-trigger")
                    if verdicts.count() != 6:
                        raise BrowserFailure("decision inspection controls missing")
                    verdicts.first.click()
                    if verdicts.first.get_attribute("aria-pressed") != "true":
                        raise BrowserFailure("selected decision not reflected accessibly")
                    if "Focused:" not in page.locator("#evidence-count").inner_text():
                        raise BrowserFailure("selected evidence not focused")
                    if page.locator("#inspector-data .inspection-record.selected-evidence").count() != 1:
                        raise BrowserFailure("focused decision does not match one evidence record")
                    if page.locator('[data-stage="ACT"]').get_attribute("data-state") != "blocked":
                        raise BrowserFailure("action boundary not locked")
                    page.locator("#scenario").select_option("outage")
                    page.locator("#run").click()
                    page.get_by_text("USER REVIEW", exact=True).first.wait_for(state="visible", timeout=30_000)
                    if not page.get_by_text("NOT AUTHORIZED").is_visible():
                        raise BrowserFailure("outage falsely authorized release")
                    if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
                        raise BrowserFailure("horizontal overflow at width " + str(width))
                    if errors or outside:
                        raise BrowserFailure("browser error or unexpected external request")
                    tested.append(width)
                finally:
                    page.close()
        finally:
            browser.close()
    return {"result": "PASS", "viewports": tested, "mode": "SYNTHETIC", "external_action_executed": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Browser smoke for hosted OFC fixture console")
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args(argv)
    try:
        value = run_browser(args.base_url)
    except (ValueError, BrowserFailure) as err:
        print(json.dumps({"result": "FAIL", "reason": str(err)}), file=sys.stderr)
        return 1
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
