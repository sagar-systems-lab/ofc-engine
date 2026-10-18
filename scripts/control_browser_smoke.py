from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright

from ofc_engine.control.verifier import verify
from ofc_engine.hosted_preview import HostedFixtureServer


def main():
    parser = argparse.ArgumentParser(description="Exercise the keyless plan-review UI against the real controller")
    parser.add_argument("--output", type=Path, default=Path("ui-review/control"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    server = HostedFixtureServer(("127.0.0.1", 0), control_enabled=True)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    report, errors, external = [], [], []
    try:
        with sync_playwright() as playwright:
            launch = {"headless": True}
            if os.environ.get("OFC_CHROMIUM_EXECUTABLE_PATH"):
                launch["executable_path"] = os.environ["OFC_CHROMIUM_EXECUTABLE_PATH"]
                launch["args"] = ["--no-sandbox", "--no-zygote", "--single-process", "--disable-webgl", "--in-process-gpu"]
            browser = playwright.chromium.launch(**launch)
            page = browser.new_page(accept_downloads=True, reduced_motion="reduce")
            page.on("pageerror", lambda exc: errors.append(str(exc)))

            def restrict(route):
                if route.request.url.startswith(origin + "/"):
                    route.continue_()
                else:
                    external.append(route.request.url)
                    route.abort()

            page.route("**/*", restrict)
            for width, height in ((1440, 1100), (375, 1000), (320, 900)):
                page.set_viewport_size({"width": width, "height": height})
                page.goto(origin + "/plans", wait_until="networkidle")
                page.wait_for_function("() => document.getElementById('changed-count').textContent === '1'")
                assert page.locator("#affected-count").inner_text() == "9"
                assert page.locator("#preserved-count").inner_text() == "20"
                assert page.locator(".decision").count() == 30
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                assert page.locator("#source-badge").inner_text() == "Synthetic sandbox"
                page.screenshot(path=str(args.output / f"plan-{width}.png"), full_page=True)
                report.append({"width": width, "overflow": False, "decisions": 30, "changed": 1, "reopened": 9, "untouched": 20})
            page.set_viewport_size({"width": 1440, "height": 1100})
            page.locator('[data-node-id="D04"]').click()
            assert page.locator(".dependency-path").inner_text() == "B01 → D01 → D04"
            page.locator("#tab-impact").focus(); page.keyboard.press("ArrowRight")
            assert page.locator("#tab-compare").get_attribute("aria-selected") == "true"
            assert page.locator("#comparison-body tr").count() == 30
            page.get_by_role("tab", name="Proof", exact=True).click()
            assert "Hash and journal consistency checked" in page.locator("#proof-facts").inner_text()
            with page.expect_download() as download:
                page.get_by_role("button", name="Export proof").click()
            saved = args.output / "review-proof.json"
            download.value.save_as(str(saved))
            assert verify(json.loads(saved.read_text()))["valid"]
            page.get_by_role("tab", name="Impact graph").click()
            page.locator(".input-details").evaluate("element => element.open = true")
            for scenario in ("preserve", "abstain", "stale", "conflict"):
                page.locator("#scenario").select_option(scenario)
                page.wait_for_function("() => document.getElementById('run-review').disabled === false && document.getElementById('changed-count').textContent === '0'")
                assert page.locator("#affected-count").inner_text() == "0"
                if scenario == "conflict":
                    assert page.locator("#constraint-panel").is_visible()
            page.get_by_role("button", name="Edit typed plan").click()
            payload = json.loads(page.locator("#plan-json").input_value())
            payload["release_authorized"] = True
            page.locator("#plan-json").fill(json.dumps(payload))
            page.get_by_role("button", name="Review edited plan").click()
            page.wait_for_function("() => document.getElementById('editor-error').hidden === false")
            assert "Not submitted" in page.locator("#editor-error").inner_text()
            page.keyboard.press("Escape")
            assert not page.locator("#editor").is_visible()
            assert not external and not errors, {"external": external, "errors": errors}
            browser.close()
        print(json.dumps({"result": "PASS", "viewports": report, "external_requests": 0,
                          "provider_calls": 0, "javascript_errors": 0, "proof_verified": True}))
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=3)


if __name__ == "__main__":
    main()
