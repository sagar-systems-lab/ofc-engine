from pathlib import Path

from ofc_engine.webdemo import _ASSETS

ROOT = Path(__file__).resolve().parents[1] / "src" / "ofc_engine" / "web"


def test_proof_first_workspace_and_provenance():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    for marker in (
        'id="lifecycle-stages"', 'id="baseline-list"', 'id="verdict-list"',
        'id="repair-list"', 'id="inspector-details"', 'id="timeline-list"',
        'id="compare-validity"', 'id="insight-text"', 'id="run-status"',
        'id="scenario"', 'aria-live="polite"', 'href="#workspace"',
        'SANDBOX / SYNTHETIC', 'NOT AUTHORIZED', 'src="/experience.js"',
    ):
        assert marker in html
    assert not any(text in html for text in ('cdn.', 'https://', 'autoplay', '<iframe'))


def test_visual_feedback_follows_snapshot_not_simulated_time():
    styles = (ROOT / "styles.css").read_text(encoding="utf-8")
    interaction = (ROOT / "experience.js").read_text(encoding="utf-8")
    main = (ROOT / "app.js").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in styles
    assert "focus-visible" in styles
    assert "@media (max-width: 390px)" in styles
    assert "ofc:snapshot" in interaction and "ofc:snapshot" in main
    assert "PATCH_APPLIED" in interaction
    assert 'labelStage("ACT", "blocked", "LOCKED")' in interaction
    assert '"INTEGRITY_ONLY"' in interaction
    assert 'aria-pressed' in interaction
    assert "textContent" in interaction and "innerHTML" not in interaction
    assert "setInterval(" not in interaction and "setTimeout(" not in interaction
    assert "VERIFIED_LIVE" not in interaction
    assert '_ASSETS' not in interaction
    assert _ASSETS["/experience.js"][0] == "experience.js"
