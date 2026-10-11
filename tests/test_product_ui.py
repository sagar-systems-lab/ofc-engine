from pathlib import Path

from ofc_engine.webdemo import _ASSETS

ROOT = Path(__file__).resolve().parents[1] / "src" / "ofc_engine" / "web"


def test_proof_first_workspace_and_provenance():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    for marker in (
        'id="lifecycle-stages"', 'id="baseline-list"', 'id="verdict-list"',
        'id="repair-list"', 'id="inspector"', 'id="inspector-details"', 'id="timeline-list"',
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


def test_plan_authoring_is_distinct_from_synthetic_proof():
    from ofc_engine.webdemo import _ASSETS
    html = (ROOT / "studio.html").read_text(encoding="utf-8")
    source = (ROOT / "studio.js").read_text(encoding="utf-8")
    for marker in (
        'id="objective"', 'id="budget"', 'id="cities"', 'id="audience"',
        'id="decision-list"', 'id="new-draft"', 'id="draft-search"',
        'id="save-draft"', 'id="validate"', 'id="import-file"',
        'id="export-proposal"', 'PROVIDER PENDING', 'href="/proof"',
    ):
        assert marker in html
    assert _ASSETS["/"][0] == "studio.html"
    assert _ASSETS["/proof"][0] == "index.html"
    assert _ASSETS["/studio.js"][0] == "studio.js"
    assert _ASSETS["/studio.css"][0] == "studio.css"
    assert "localStorage" in source
    assert 'schema_version:"0.1.0"' in source
    assert 'release_authorized' not in source
    assert 'fetch("/api/example"' in source
    assert 'innerHTML' not in source
    assert 'eval(' not in source



def test_connected_preflight_keys_and_strict_fixture_boundary():
    html = (ROOT / "studio.html").read_text(encoding="utf-8")
    js = (ROOT / "studio.js").read_text(encoding="utf-8")
    for marker in ('id="load-example"', 'id="check-preflight"', 'id="preflight-panel"',
                   'id="preflight-decisions"', 'id="open-fixture"'):
        assert marker in html
    assert '"/api/example"' in js
    assert 'comparable(validated) === comparable(canonicalExample.example)' in js
    assert 'USER REVIEW — NO EVIDENCE' in js
    assert 'UNCERTAIN / EVIDENCE UNAVAILABLE' in js
    assert 'release_authorized' not in js
    assert 'innerHTML' not in js
