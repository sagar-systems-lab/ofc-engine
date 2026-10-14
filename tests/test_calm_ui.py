"""Product shell keeps the primary journey obvious without hiding functionality."""
from pathlib import Path

from ofc_engine.webdemo import _ASSETS


ROOT = Path(__file__).resolve().parents[1] / "src/ofc_engine/web"


def test_primary_workspace_journey_and_secondary_tools():
    html = (ROOT / "studio.html").read_text(encoding="utf-8")
    assert 'id="home-title">Better decisions.' in html
    assert 'id="more-tools"' in html
    assert '<summary>More tools' in html
    assert html.count('class="side-link') >= 9
    for marker in (
        'id="home-create"', 'id="home-example"', 'id="home-build"',
        'href="/proof" class="primary hero-button"',
        'href="/recorded"', 'id="view-plan"', 'id="view-map"',
        'id="view-evidence"', 'id="view-history"',
        'id="view-provider"', 'id="view-settings"',
        'DEMO MODE · PROVIDER PENDING',
    ):
        assert marker in html
    assert 'class="hero-art"' not in html
    assert 'class="simple-preview"' in html
    assert html.index('id="view-home"') < html.index('id="view-plan"')
    assert _ASSETS["/calm.css"][0] == "calm.css"


def test_readable_headlines_and_clear_source_labels():
    recorded = (ROOT / "recorded.html").read_text(encoding="utf-8")
    proof = (ROOT / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "calm.css").read_text(encoding="utf-8")
    assert "See why a choice <em>changed.</em>" in recorded
    assert "Recorded example, not live approval" in recorded
    assert 'id="proof-file"' in recorded and 'id="proof-result"' in recorded
    assert "Check the plan. <em>Keep what works.</em>" in proof
    assert "SANDBOX / SYNTHETIC" in proof
    for tag in ("studio-page", "proof-page", "recorded-page"):
        assert f'class="{tag}"' in (recorded if tag == "recorded-page" else proof if tag == "proof-page" else (ROOT / "studio.html").read_text())
        assert f".{tag} " in css
    assert "--calm-bg:#f7f9fc" in css
    assert "font-weight:650" in css
    assert "prefers-reduced-motion" in css
    assert not any(ext in css for ext in ("@import", "url(http", "url(//"))
