"""Chromium journey check for the deployed synthetic proof console."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
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
                    page.locator("#home-title").wait_for(state="visible", timeout=40_000)
                    if page.locator(".primary-nav > .side-link").count() != 4:
                        raise BrowserFailure("primary navigation is not judge-focused")
                    if page.locator("#more-tools").get_attribute("open") is not None:
                        raise BrowserFailure("advanced tools should be collapsed by default")
                    if not page.locator(".simple-preview").is_visible():
                        raise BrowserFailure("plain-language decision example missing")
                    hero_type = page.locator("#home-title").evaluate("""el => ({
                        weight: parseFloat(getComputedStyle(el).fontWeight),
                        size: parseFloat(getComputedStyle(el).fontSize)
                    })""")
                    if hero_type["weight"] > 700 or hero_type["size"] > 70:
                        raise BrowserFailure("hero typography is oversized or too heavy")
                    if not page.locator(".status-label").is_visible():
                        raise BrowserFailure("provider boundary missing from premium shell")
                    workspace_brand = page.locator(".brand-icon").evaluate("""el => ({
                        width: getComputedStyle(el).width,
                        height: getComputedStyle(el).height,
                        radius: getComputedStyle(el).borderRadius,
                        color: getComputedStyle(el).color,
                        text: el.textContent.trim()
                    })""")
                    if workspace_brand["text"] != "◇":
                        raise BrowserFailure("workspace canonical brand mark changed")
                    if page.request.get(origin + "/optical.css").status != 200:
                        raise BrowserFailure("optical depth stylesheet not served")
                    optical = page.locator(".topbar").evaluate("""el => ({
                        shadow: getComputedStyle(el).boxShadow,
                        border: getComputedStyle(el).borderBottomColor
                    })""")
                    if optical["shadow"] == "none":
                        raise BrowserFailure("glass header lost visual depth")
                    if page.locator("#objective").is_visible():
                        raise BrowserFailure("home should open before the plan editor")
                    page.locator("#home-create").click()
                    page.locator("#objective").wait_for(state="visible", timeout=40_000)
                    if page.locator("#check-preflight").is_enabled():
                        raise BrowserFailure("unvalidated plan allowed preflight")
                    heading_style = page.locator("#draft-title").evaluate("""el => ({
                        family: getComputedStyle(el).fontFamily,
                        weight: getComputedStyle(el).fontWeight,
                        size: parseFloat(getComputedStyle(el).fontSize)
                    })""")
                    if "Georgia" not in heading_style["family"] or heading_style["weight"] != "700":
                        raise BrowserFailure("plan section heading is not using the crisp display role")
                    if heading_style["size"] < 20:
                        raise BrowserFailure("plan section heading size was unintentionally reduced")
                    card_style = page.locator(".editor-panel").evaluate("""el => ({
                        shadow: getComputedStyle(el).boxShadow,
                        textColor: getComputedStyle(el.querySelector('.section-head h2')).color
                    })""")
                    if card_style["shadow"] == "none" or card_style["textColor"] == "rgb(0, 0, 0)":
                        raise BrowserFailure("editor optical contrast or depth missing")
                    page.locator('[data-view-link="home"]').click()
                    page.locator("#home-example").click()
                    page.locator("#decision-list .decision-card").nth(5).wait_for(state="attached", timeout=30_000)
                    if page.locator("#decision-list .decision-card").count() != 6:
                        raise BrowserFailure("canonical fixture did not load exactly six decisions")
                    page.locator("#validate").click()
                    if page.locator("#proposal-state").inner_text() != "Contract valid":
                        raise BrowserFailure("canonical typed proposal cannot be validated")
                    if page.locator("#export-proposal").is_disabled():
                        raise BrowserFailure("validated proposal cannot be exported")
                    page.locator("#check-preflight").click()
                    if page.locator("#preflight-state").inner_text() != "SYNTHETIC FIXTURE READY":
                        raise BrowserFailure("same canonical plan did not unlock synthetic fixture")
                    if not page.locator("#open-fixture").is_visible():
                        raise BrowserFailure("canonical proof link unavailable")
                    page.locator("#objective").fill("Custom cultural decision proposal")
                    if not page.locator("#export-proposal").is_disabled():
                        raise BrowserFailure("changed proposal reused stale validation")
                    if not page.locator("#preflight-panel").is_hidden():
                        raise BrowserFailure("stale result remained after edit")
                    page.locator("#validate").click()
                    page.locator("#check-preflight").click()
                    if page.locator("#preflight-state").inner_text() != "USER REVIEW — NO EVIDENCE":
                        raise BrowserFailure("custom proposal received synthetic verdict")
                    if not page.locator("#open-fixture").is_hidden():
                        raise BrowserFailure("custom proposal linked to unrelated fixture")
                    if page.locator("#preflight-decisions li").count() != 6:
                        raise BrowserFailure("custom review did not account for every decision")
                    page.locator("#save-draft").click()
                    page.reload(wait_until="domcontentloaded")
                    page.locator("#more-tools summary").click()
                    page.locator('[data-view-link="history"]').click()
                    page.locator("#history-search").fill("Custom cultural")
                    if page.locator("#history-rows .history-row:not(.history-row-head)").count() != 1:
                        raise BrowserFailure("draft save/search failed")
                    page.locator("#history-rows .history-row:not(.history-row-head) button").first.click()
                    if page.locator("#objective").input_value() != "Custom cultural decision proposal":
                        raise BrowserFailure("reopening saved draft lost user input")
                    page.locator('[data-view-link="map"]').click()
                    if page.locator("#map-nodes .map-node").count() != 6:
                        raise BrowserFailure("decision map did not reflect current proposal")
                    map_panel = page.locator(".map-board")
                    before_style = map_panel.get_attribute("style") or ""
                    before_bg = map_panel.evaluate("el => getComputedStyle(el).backgroundImage")
                    page.mouse.move(420, 360)
                    page.mouse.move(760, 530)
                    after_style = map_panel.get_attribute("style") or ""
                    after_bg = map_panel.evaluate("el => getComputedStyle(el).backgroundImage")
                    if before_style != after_style or before_bg != after_bg:
                        raise BrowserFailure("decision map still reacts to mouse position")
                    page.locator("#map-nodes .map-node").first.click()
                    if page.locator("#map-selected-title").inner_text() != "brand":
                        raise BrowserFailure("decision map inspector selection failed")
                    page.locator('[data-view-link="evidence"]').click()
                    page.locator("#evidence-preview .evidence-facts").wait_for(state="visible",timeout=30_000)
                    if not page.get_by_text("SYNTHETIC FIXTURE",exact=True).is_visible():
                        raise BrowserFailure("evidence provenance unavailable")
                    page.locator('[data-view-link="settings"]').click()
                    motion_switch = page.locator(".switch")
                    motion_switch.scroll_into_view_if_needed()
                    motion_switch.click()
                    if page.locator("#motion-enabled").is_checked():
                        raise BrowserFailure("motion toggle did not switch off")
                    if "motion-off" not in (page.locator("html").get_attribute("class") or ""):
                        raise BrowserFailure("motion preference did not apply")
                    motion_switch.click()
                    if not page.locator("#motion-enabled").is_checked():
                        raise BrowserFailure("motion toggle did not switch back on")
                    page.locator('[data-view-link="plan"]').click()
                    example_file = Path(__file__).resolve().parents[1] / "examples" / "agent_proposal.json"
                    page.locator("#import-file").set_input_files({
                        "name": "agent_proposal.json", "mimeType": "application/json",
                        "buffer": example_file.read_bytes(),
                    })
                    page.locator("#editor-message").filter(has_text="Proposal imported").wait_for(state="visible",timeout=12_000)
                    if page.locator("#taste-locality").input_value() != "three cities":
                        raise BrowserFailure("JSON import discarded cultural locality")
                    if page.locator("#decision-list .decision-card").count() != 3:
                        raise BrowserFailure("JSON import discarded decisions")
                    page.locator("#validate").click()
                    if page.locator("#proposal-state").inner_text() != "Contract valid":
                        raise BrowserFailure("imported typed proposal failed local validation")
                    imported = page.request.get(origin + "/api/example").json()["example"]
                    imported["intent"]["constraints"]["unrepresented"] = "must-not-drop"
                    page.locator("#import-file").set_input_files({
                        "name": "unsupported.json", "mimeType": "application/json",
                        "buffer": json.dumps(imported).encode("utf-8"),
                    })
                    page.locator("#editor-message").filter(
                        has_text="Unsupported imported fields"
                    ).wait_for(state="visible", timeout=12_000)
                    if page.locator("#taste-locality").input_value() != "three cities":
                        raise BrowserFailure("failed import replaced the existing draft")
                    page.goto(origin + "/proof", wait_until="domcontentloaded", timeout=90_000)
                    page.locator("#run-status").wait_for(state="visible", timeout=40_000)
                    proof_brand = page.locator(".proof-brand-icon").evaluate("""el => ({
                        width: getComputedStyle(el).width,
                        height: getComputedStyle(el).height,
                        radius: getComputedStyle(el).borderRadius,
                        color: getComputedStyle(el).color,
                        text: el.textContent.trim()
                    })""")
                    if proof_brand["text"] != "◇":
                        raise BrowserFailure("proof lab is not using canonical diamond mark")
                    if width >= 768 and proof_brand != workspace_brand:
                        raise BrowserFailure("proof lab brand mark does not match workspace brand")
                    if page.locator(".proof-brand-copy small").inner_text().strip() != "AGENT DECISION CONTROL":
                        raise BrowserFailure("proof lab brand subtitle does not match workspace")
                    proof_heading = page.locator(".panel h2").first.evaluate("""el => ({
                        family: getComputedStyle(el).fontFamily,
                        weight: getComputedStyle(el).fontWeight,
                        size: parseFloat(getComputedStyle(el).fontSize)
                    })""")
                    if "Georgia" not in proof_heading["family"] or proof_heading["weight"] != "700":
                        raise BrowserFailure("proof panel heading is not using the crisp display role")
                    if proof_heading["size"] < 20:
                        raise BrowserFailure("proof panel heading size was unintentionally reduced")
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
                        offenders = page.evaluate("""() => [...document.querySelectorAll('*')].map(el => ({
                            tag: el.tagName, cls: el.className?.baseVal ?? el.className,
                            scroll: el.scrollWidth, width: el.clientWidth,
                            right: Math.round(el.getBoundingClientRect().right)
                        })).filter(x => x.right > innerWidth + 1 || x.scroll > x.width + 4).slice(0,16)""")
                        raise BrowserFailure("horizontal overflow at width " + str(width) + ": " + str(offenders))
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
