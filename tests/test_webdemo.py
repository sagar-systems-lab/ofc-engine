"""Local demo produces only synthetic, non-authoritative views."""
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ofc_engine.webdemo import DemoHandler, SCENARIOS, baseline_plan, demo_snapshot


def test_single_baseline_and_structural_repair():
    baseline = baseline_plan()
    snapshot = demo_snapshot("repair")
    assert baseline.revision == 0
    assert snapshot["baseline_digest"] == baseline.baseline_digest or len(snapshot["baseline_digest"]) == 64
    assert snapshot["source_mode"] == "SYNTHETIC"
    assert snapshot["run_status"] == "INTEGRITY_ONLY"
    assert snapshot["current_revision"] == 1
    assert snapshot["comparison"]["identical_baseline"]
    assert snapshot["comparison"]["without_evidence"] == "USER_REVIEW"
    assert snapshot["comparison"]["with_fixture_evidence"] == "INTEGRITY_ONLY"
    assert not snapshot["comparison"]["real_causal_proof"]
    changed = [d for d in snapshot["decisions"] if d["changed"]]
    assert len(changed) == 1 and changed[0]["id"] == "brand"
    afterparty = next(d for d in snapshot["decisions"] if d["id"] == "afterparty")
    assert afterparty["rechecked"] and not afterparty["changed"]
    assert snapshot["pending_rechecks"] == []
    assert snapshot["traces"][0]["recheck_state"] == "STRUCTURAL_PASS"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_scenario_is_non_authoritative(scenario):
    snap = demo_snapshot(scenario)
    assert snap["demo_mode"] == "SYNTHETIC"
    assert snap["release_state"] == "USER_REVIEW"
    assert snap["release_authorized"] is False
    assert snap["external_action_executed"] is False
    assert snap["provider_authenticated"] is False
    assert snap["comparison"]["business_outcomes_measured"] is False
    assert snap["comparison"]["real_causal_proof"] is False
    assert len(snap["decisions"]) == 6
    assert len(json.dumps(snap)) < 120000


def test_outage_and_missing_evidence_are_review_required():
    outage = demo_snapshot("outage")
    assert outage["run_status"] == "USER_REVIEW"
    assert not outage["evidence"]
    missing = demo_snapshot("dependent_missing")
    assert missing["run_status"] == "USER_REVIEW"
    assert "afterparty" in missing["pending_rechecks"]
    assert missing["traces"][0]["recheck_state"] == "BLOCKED"


def test_budget_exhaustion_is_not_reported_as_ready():
    result = demo_snapshot("budget_exhausted")
    assert result["run_status"] == "REPAIR_EXHAUSTED"
    assert result["attempts_used"] == result["max_attempts"] == 1
    assert result["release_state"] == "USER_REVIEW"


def test_invalid_scenario_rejected():
    with pytest.raises(ValueError, match="unknown scenario"):
        demo_snapshot("anything")


@contextmanager
def running_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_exposes_only_read_only_fixture_and_static_assets():
    with running_server() as base:
        with urlopen(base + "/api/demo?scenario=repair", timeout=5) as response:
            payload = json.load(response)
            assert response.status == 200
            assert payload["demo_mode"] == "SYNTHETIC"
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        for path in ("/", "/index.html", "/styles.css", "/app.js"):
            with urlopen(base + path, timeout=5) as response:
                assert response.status == 200
                assert len(response.read()) > 300
        for path in ("/api/demo?scenario=invalid", "/api/demo", "/api/demo?scenario=repair&junk=1", "/secret", "/../README.md"):
            with pytest.raises(HTTPError) as error:
                urlopen(base + path, timeout=5)
            assert error.value.code in (400, 404)
        with pytest.raises(HTTPError) as err:
            urlopen(Request(base + "/api/demo?scenario=repair", method="POST", data=b"{}"), timeout=5)
        assert err.value.code == 405


def test_ui_assets_do_not_import_remote_scripts():
    root = Path(__file__).resolve().parents[1] / "src" / "ofc_engine" / "web"
    html = (root / "index.html").read_text(encoding="utf-8")
    script = (root / "app.js").read_text(encoding="utf-8")
    assert '<script defer src="/app.js"></script>' in html
    assert "cdn." not in html and "https://" not in html
    assert "textContent" in script
    assert "innerHTML" not in script
    assert "aria-expanded" in html or "aria-expanded" in script
    assert "prefers-reduced-motion" in (root / "styles.css").read_text(encoding="utf-8")
