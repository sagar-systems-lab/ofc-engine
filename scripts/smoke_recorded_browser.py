"""Chromium verification of the local-only recorded proof manifest flow."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import sys
from threading import Thread

from playwright.sync_api import sync_playwright

from ofc_engine.local_proof_server import LocalProofServer
from ofc_engine.revisions import digest_obj
from smoke_hosted import normalize_url


class ProofBrowserError(RuntimeError):
    pass


def sample_report():
    return {
        "schema": "ofc-private-operator-workspace-v1",
        "title": "Recorded decision proof",
        "source_mode": "RECORDED_REDACTED",
        "historical_replay": True,
        "generated_at": "2026-10-14T13:00:00+00:00",
        "captured_at": "2026-10-13T13:00:00+00:00",
        "plan": {
            "baseline_digest": "a" * 64,
            "stored_digest": "b" * 64,
            "revision": 1,
            "journal_events": 2,
            "decision_count": 1,
        },
        "decision": {
            "initial": "Candidate 01",
            "replacement": "Candidate 02",
            "changed": True,
            "rank_order": ["Candidate 02", "Candidate 01", "Candidate 03"],
            "evidence_off": "UNCERTAIN",
            "evidence_on_initial": "REPAIR",
            "evidence_on_after_repair": "PASS",
            "repair_attempts": 1,
            "affected_decision_count": 1,
            "structural_recheck": "STRUCTURAL_PASS",
        },
        "recheck": {
            "status": "ORIGINAL_CAPTURE_REPLAY_ONLY",
            "recorded_rank_verdict": "PASS",
            "same_capture": True,
            "later_capture_observed": False,
            "pending_count": 1,
            "pending_recheck_cleared": False,
        },
        "qualifications": {
            "operator_supplied_capture_not_commit_attested": True,
            "provider_authenticated": False,
            "independent_recheck_completed": False,
            "overall_plan_verified": False,
            "release_authorized": False,
            "external_action_executed": False,
            "real_qloo_requests_this_export": 0,
        },
    }


def manifest(payload):
    return {"payload": payload, "sha256": digest_obj(payload)}


def upload(page, document):
    page.locator("#proof-file").set_input_files({
        "name": "proof.json",
        "mimeType": "application/json",
        "buffer": json.dumps(document, ensure_ascii=False).encode("utf-8"),
    })


def run(origin):
    origin = normalize_url(origin)
    widths = (1440, 375, 320)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, executable_path=os.environ.get("OFC_CHROMIUM_PATH")
        )
        try:
            for width in widths:
                page = browser.new_page(
                    viewport={"width": width, "height": 840}, reduced_motion="reduce"
                )
                errors = []
                requests = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: requests.append(request.url))
                try:
                    page.goto(origin + "/recorded", wait_until="networkidle")
                    if page.locator("#proof-result").is_visible():
                        raise ProofBrowserError("empty viewer exposed fake proof")
                    if page.locator("#proof-file").count() != 1:
                        raise ProofBrowserError("private file control missing")
                    before = list(requests)
                    payload = sample_report()
                    upload(page, manifest(payload))
                    page.locator("#proof-result").wait_for(state="visible", timeout=12000)
                    if "checksum matched" not in page.locator("#import-status").inner_text():
                        raise ProofBrowserError("local manifest was not verified")
                    if page.locator("#after-candidate").inner_text() != "Candidate 02":
                        raise ProofBrowserError("candidate replacement not rendered")
                    if page.locator("#pending").inner_text() != "1 PENDING":
                        raise ProofBrowserError("pending review was incorrectly cleared")
                    if page.locator("#rank-list .rank-row").count() != 3:
                        raise ProofBrowserError("recorded rank list incomplete")
                    if page.locator("#recheck-note").inner_text().find("still pending") < 0:
                        raise ProofBrowserError("unresolved recheck not shown")
                    if page.locator("#plan-digest").inner_text() != "b" * 64:
                        raise ProofBrowserError("persisted revision digest missing")
                    if requests != before:
                        raise ProofBrowserError("private import triggered network request")

                    tampered = deepcopy(payload)
                    tampered["captured_at"] = "2026-10-13T13:01:00+00:00"
                    upload(page, {"payload": tampered, "sha256": digest_obj(payload)})
                    page.locator("#import-status").filter(has_text="checksum mismatch").wait_for(
                        state="visible", timeout=12000
                    )
                    if page.locator("#proof-result").is_visible():
                        raise ProofBrowserError("checksum mismatch retained verified proof")

                    forged = deepcopy(payload)
                    forged["qualifications"]["release_authorized"] = True
                    upload(page, manifest(forged))
                    page.locator("#import-status").filter(has_text="Proof blocked").wait_for(
                        state="visible", timeout=12000
                    )
                    if page.locator("#proof-result").is_visible():
                        raise ProofBrowserError("forged release state presented as valid")
                    if requests != before:
                        raise ProofBrowserError("private import attempted to contact backend")

                    upload(page, manifest(payload))
                    page.locator("#proof-result").wait_for(state="visible", timeout=12000)
                    if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
                        raise ProofBrowserError(f"horizontal overflow at width {width}")
                    if errors:
                        raise ProofBrowserError("browser JavaScript error: " + "; ".join(errors))
                    if any(not uri.startswith(origin + "/") for uri in requests):
                        raise ProofBrowserError("unexpected cross-origin request")
                finally:
                    page.close()
            local_server = LocalProofServer(
                ("127.0.0.1", 0), manifest=manifest(sample_report())
            )
            local_url = f"http://127.0.0.1:{local_server.server_port}"
            thread = Thread(target=local_server.serve_forever, daemon=True)
            thread.start()
            local_page = browser.new_page(
                viewport={"width": 375, "height": 812}, reduced_motion="reduce"
            )
            local_requests = []
            local_errors = []
            local_page.on("request", lambda req: local_requests.append(req.url))
            local_page.on("pageerror", lambda err: local_errors.append(str(err)))
            try:
                local_page.goto(local_url + "/", wait_until="networkidle")
                local_page.locator("#proof-result").wait_for(
                    state="visible", timeout=12000
                )
                if local_page.locator("#after-candidate").inner_text() != "Candidate 02":
                    raise ProofBrowserError("local session failed to auto-load stored proof")
                if local_page.locator("#pending").inner_text() != "1 PENDING":
                    raise ProofBrowserError("local session wrongly cleared pending state")
                if not any(uri == local_url + "/api/local-proof" for uri in local_requests):
                    raise ProofBrowserError("local session did not fetch scoped manifest")
                if any(not uri.startswith(local_url + "/") for uri in local_requests):
                    raise ProofBrowserError("local proof session attempted external network")
                if local_page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
                    raise ProofBrowserError("local proof session overflows mobile viewport")
                if local_errors:
                    raise ProofBrowserError("local session JavaScript errors")
            finally:
                local_page.close()
                local_server.shutdown()
                local_server.server_close()
                thread.join(timeout=5)
        finally:
            browser.close()
    return {"result": "PASS", "widths": list(widths), "local_file_only": True, "loopback_auto_load": True}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args(argv)
    try:
        output = run(args.base_url)
    except (ValueError, ProofBrowserError) as exc:
        print(json.dumps({"result": "FAIL", "reason": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
