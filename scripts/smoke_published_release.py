"""Read-only, strict hosted release gate for the approved Qloo tour."""
from __future__ import annotations

import argparse
import json
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright


def qualify(origin: str, expected_sha: str):
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https" or not parsed.hostname
        or not parsed.hostname.endswith(".onrender.com")
        or parsed.username or parsed.password or parsed.port
        or parsed.path not in ("", "/") or parsed.query or parsed.fragment
    ):
        raise ValueError("public HTTPS Render origin required")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("40-character expected SHA required")

    base = origin.rstrip("/")
    def fetch(path):
        with urlopen(Request(base + path, headers={
            "Accept": "application/json", "Cache-Control": "no-store"
        }), timeout=150) as response:
            assert response.status == 200
            assert response.headers.get("Content-Type", "").startswith("application/json")
            return json.load(response)

    ready = fetch("/readyz")
    assert ready == {
        "status": "ok", "mode": "PUBLIC_REDACTED_QLOO_REVIEW",
        "build_sha": expected_sha,
    }, "deployment is not the approved published Qloo revision"
    result = fetch("/api/showcase")
    assert result["journey_version"] == 1
    assert result["status"] == "OBSERVED_QLOO_REVIEW_REQUIRED"
    assert result["source_mode"] == "CAPTURED_LIVE_QLOO_UNVERIFIED"
    assert result["revision"] == 1 and result["decision_count"] == 6
    assert result["changed_ids"] == ["brand"]
    assert set(result["invalidated_ids"]) == {"brand", "afterparty"}
    assert set(result["pending_rechecks"]) == {"brand", "afterparty"}
    assert result["handoff"] == "WITHHELD"
    assert result["release_authorized"] is False
    assert result["provider_authenticated"] is False
    assert result["independent_recheck_completed"] is False
    assert result["external_action_executed"] is False
    assert result["additional_provider_requests"] == 0
    assert result["off_on"] == {
        "same_baseline": True, "off_brand": "UNCERTAIN",
        "on_brand": "REPAIR", "real_causal_proof": False,
    }
    tour = result["tour"]
    assert tour["budget_usd"] == 60000
    assert tour["cities"] == ["Chicago", "Austin", "Seattle"]
    assert tour["operator_constructed_unverified_decisions"] == 5
    assert len(tour["decisions"]) == 6
    assert tour["decisions"][0]["changed"] is True
    assert all(not d["changed"] for d in tour["decisions"][1:])
    for value in ("ticket_digest", "proposal_snapshot", "api_key",
                  "operator_token", "candidate_id", "candidate_pool"):
        assert value not in json.dumps(result).lower()
    for key in ("baseline_plan_digest", "current_plan_digest", "journal_head",
                "query_fingerprint", "capture_digest"):
        assert re.fullmatch(r"[a-f0-9]{64}", result["evidence"][key])

    widths = (1440, 375, 320)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for width in widths:
                page = browser.new_page(
                    viewport={"width": width, "height": 800}, reduced_motion="reduce"
                )
                errors, writes, outside = [], [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: (
                    writes.append(request.url)
                    if request.method != "GET" else (
                        outside.append(request.url)
                        if not request.url.startswith(base + "/") else None
                    )
                ))
                try:
                    page.goto(base + "/#plan", wait_until="domcontentloaded", timeout=90000)
                    launch = page.locator("#start-qloo-journey")
                    launch.wait_for(state="visible", timeout=40000)
                    launch.focus()
                    launch.press("Enter")
                    assert page.locator("#qloo-copilot").is_visible()
                    assert page.locator("#qloo-copilot-decision").inner_text() == "UNCERTAIN"
                    assert page.locator("#qloo-copilot-off").get_attribute(
                        "aria-pressed") == "true"
                    page.locator("#qloo-copilot-on").focus()
                    page.locator("#qloo-copilot-on").press("Enter")
                    assert page.locator("#qloo-copilot-decision").inner_text() == "REPAIR"
                    assert page.locator("#qloo-copilot-on").get_attribute(
                        "aria-pressed") == "true"
                    assert "option 1 → 2 of 3" in page.locator(
                        "#qloo-copilot-choice").inner_text()
                    assert "HANDOFF WITHHELD" in page.locator(
                        "#qloo-copilot-safety").inner_text()
                    page.locator("#qloo-copilot-off").click()
                    assert page.locator("#qloo-copilot-decision").inner_text() == "UNCERTAIN"
                    page.locator("#qloo-copilot-on").click()
                    for name, content in (
                        ("01 / PROPOSE", "$60,000"),
                        ("02 / QLOO CHECK", "Qloo OFF"),
                        ("03 / REPAIR", "REPAIRED"),
                        ("04 / VERIFY", "RECHECK PENDING"),
                        ("05 / HANDOFF", "HANDOFF WITHHELD"),
                    ):
                        assert page.locator("#qloo-journey-heading").inner_text() == name
                        assert content in page.locator("#qloo-journey-body").inner_text()
                        if name != "05 / HANDOFF":
                            page.locator("#qloo-journey-next").focus()
                            page.locator("#qloo-journey-next").press("Enter")
                    assert "HANDOFF BLOCKED" == page.locator(
                        "#view-plan .state-item").nth(2).locator("small").inner_text()
                    overflow = page.evaluate("""() => {
                        const width = innerWidth;
                        const documentWidth = document.documentElement.scrollWidth;
                        const nodes = [...document.querySelectorAll('*')]
                          .filter(el => {
                            const style = getComputedStyle(el);
                            return style.display !== 'none' &&
                              style.visibility !== 'hidden';
                          })
                          .map(el => {
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            return {
                              tag: el.tagName.toLowerCase(),
                              id: el.id || null,
                              className: typeof el.className === 'string'
                                ? el.className.slice(0, 110) : '',
                              docRight: Math.round(rect.right + scrollX),
                              docLeft: Math.round(rect.left + scrollX),
                              width: Math.round(rect.width),
                              scrollWidth: el.scrollWidth,
                              clientWidth: el.clientWidth,
                              overflowX: style.overflowX,
                              whiteSpace: style.whiteSpace
                            };
                          });
                        const candidates = nodes.filter(el =>
                          el.docRight > width + 1 ||
                          el.scrollWidth > el.clientWidth + 1
                        );
                        candidates.sort((a, b) =>
                          Math.max(b.docRight, b.scrollWidth) -
                          Math.max(a.docRight, a.scrollWidth));
                        return {
                          viewport: width, scrollWidth: documentWidth,
                          scrollX, bodyScrollWidth: document.body.scrollWidth,
                          overflow: documentWidth > width + 1,
                          offenders: candidates.slice(0, 20)
                        };
                    }""")
                    assert not overflow["overflow"], (
                        "published tour horizontal overflow: " +
                        json.dumps(overflow, sort_keys=True)
                    )
                    page.reload(wait_until="domcontentloaded")
                    page.locator("#start-qloo-journey").wait_for(state="visible")
                    assert not errors, errors
                    assert not writes, writes
                    assert not outside, outside
                finally:
                    page.close()
        finally:
            browser.close()

    return {
        "result": "PASS", "sha": expected_sha,
        "browser_widths": list(widths),
        "source_mode": "CAPTURED_LIVE_QLOO_UNVERIFIED",
        "provider_requests_by_public_browser": 0,
        "handoff": "WITHHELD",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    print(json.dumps(qualify(args.base_url, args.expected_sha), sort_keys=True))


if __name__ == "__main__":
    main()
