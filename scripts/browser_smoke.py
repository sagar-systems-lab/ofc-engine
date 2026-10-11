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
