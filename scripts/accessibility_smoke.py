"""Keyboard, disclosure, and reduced-motion checks for the synthetic preview."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright
from smoke_hosted import normalize_url


class AccessibilityFailure(RuntimeError):
    pass


def qualify(origin: str) -> dict:
    origin = normalize_url(origin)
    viewports = (1440, 375, 320)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, executable_path=os.environ.get("OFC_CHROMIUM_PATH"),
        )
        try:
            for width in viewports:
                page = browser.new_page(viewport={"width": width, "height": 800}, reduced_motion="reduce")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    page.goto(origin + "/", wait_until="domcontentloaded", timeout=90_000)
                    page.locator("#run-status").wait_for(state="visible", timeout=40_000)
                    if page.get_by_role("main").count() != 1:
                        raise AccessibilityFailure("main landmark missing")
                    if page.get_by_label("Test condition").count() != 1:
                        raise AccessibilityFailure("scenario control has no associated label")
                    if page.locator("#status").get_attribute("aria-live") != "polite":
                        raise AccessibilityFailure("status change is not announced")
                    if not page.evaluate("window.matchMedia('(prefers-reduced-motion: reduce)').matches"):
                        raise AccessibilityFailure("reduced motion not applied")
                    page.keyboard.press("Tab")
                    if not page.locator(".skip-link").evaluate("(node) => node === document.activeElement"):
                        raise AccessibilityFailure("skip navigation link not first in keyboard order")
                    if not page.locator(".skip-link").evaluate("(node) => node.matches(':focus-visible')"):
                        raise AccessibilityFailure("skip navigation link focus indicator missing")
                    page.keyboard.press("Enter")
                    if not page.evaluate("location.hash === '#workspace'"):
                        raise AccessibilityFailure("skip navigation target missing")

                    toggle = page.locator("#inspect-toggle")
                    if toggle.get_attribute("aria-controls") != "inspector-details":
                        raise AccessibilityFailure("inspector control missing relationship")
                    toggle.focus()
                    toggle.press("Enter")
                    if toggle.get_attribute("aria-expanded") != "true":
                        raise AccessibilityFailure("keyboard cannot expand evidence inspector")
                    first = page.locator("#verdict-list .verdict-trigger").first
                    first.focus()
                    first.press("Enter")
                    if first.get_attribute("aria-pressed") != "true":
                        raise AccessibilityFailure("keyboard cannot select a verdict")
                    if page.locator("#inspector-data .selected-evidence").count() != 1:
                        raise AccessibilityFailure("keyboard selection did not focus evidence")

                    page.get_by_label("Test condition").select_option("outage")
                    page.locator("#run").focus()
                    page.locator("#run").press("Enter")
                    page.get_by_text("USER REVIEW", exact=True).first.wait_for(state="visible", timeout=30_000)
                    if not page.get_by_text("NOT AUTHORIZED").is_visible():
                        raise AccessibilityFailure("outage allowed action")
                    if page.evaluate("document.documentElement.scrollWidth > innerWidth + 1"):
                        raise AccessibilityFailure("horizontal overflow")
                    if errors:
                        raise AccessibilityFailure("unhandled browser JavaScript exception")
                finally:
                    page.close()
        finally:
            browser.close()
    return {
        "result": "PASS",
        "viewports": list(viewports),
        "input": "KEYBOARD",
        "reduced_motion": True,
        "source_mode": "SYNTHETIC",
        "release_authorized": False,
        "wcag_certified": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Targeted keyboard and reduced-motion smoke")
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args(argv)
    try:
        result = qualify(args.base_url)
    except (AccessibilityFailure, ValueError) as exc:
        print(json.dumps({"result": "FAIL", "reason": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
