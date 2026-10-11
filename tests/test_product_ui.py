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
        'id="save-draft"', 'id="validate"', 'id="import-file"', 'id="taste-locality"',
        'id="export-proposal"', 'PROVIDER PENDING', 'href="/proof"',
    ):
        assert marker in html
    assert _ASSETS["/"][0] == "studio.html"
    assert _ASSETS["/proof"][0] == "index.html"
    assert _ASSETS["/studio.js"][0] == "studio.js"
    assert _ASSETS["/studio.css"][0] == "studio.css"
    assert _ASSETS["/product.js"][0] == "product.js"
    assert _ASSETS["/proof-premium.css"][0] == "proof-premium.css"
    assert _ASSETS["/optical.css"][0] == "optical.css"
    assert "localStorage" in source
    assert 'schema_version:"0.1.0"' in source
    assert 'data.release_authorized !== false' in source
    assert 'fetch("/api/example"' in source
    assert 'innerHTML' not in source
    assert 'eval(' not in source
    assert 'stableJson(regenerated) !== stableJson(data)' in source



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
    assert 'data.release_authorized !== false' in js
    assert 'innerHTML' not in js


def test_premium_product_surfaces_without_fake_provider_results():
    html = (ROOT / "studio.html").read_text(encoding="utf-8")
    css = (ROOT / "studio.css").read_text(encoding="utf-8")
    js = (ROOT / "product.js").read_text(encoding="utf-8")
    proof = (ROOT / "index.html").read_text(encoding="utf-8")
    for name in ("home", "plan", "map", "evidence", "history", "provider", "settings"):
        assert f'id="view-{name}"' in html
        assert f'data-view-link="{name}"' in html
    for marker in ('id="global-search"', 'id="home-create"', 'id="home-example"',
                   'id="map-nodes"', 'id="evidence-preview"', 'id="history-rows"',
                   'id="motion-enabled"'):
        assert marker in html
    assert '/proof-premium.css' in proof
    assert 'prefers-reduced-motion' in css
    assert '.motion-off' in css
    assert 'localStorage' in js
    assert 'data.source_mode!=="SYNTHETIC"' in js
    assert 'data.release_authorized!==false' in js
    assert 'data.external_action_executed!==false' in js
    assert 'innerHTML' not in js
    assert 'setInterval(' not in js



def test_optical_hardening_keeps_readable_content_and_motion_controls():
    css = (ROOT / "optical.css").read_text(encoding="utf-8")
    home = (ROOT / "studio.html").read_text(encoding="utf-8")
    proof = (ROOT / "index.html").read_text(encoding="utf-8")
    product = (ROOT / "product.js").read_text(encoding="utf-8")
    assert 'href="/optical.css"' in home
    assert 'href="/optical.css"' in proof
    for token in ("--opt-ink:", "--opt-copy:", "--opt-support:", "--opt-shadow:",
                  "radial-gradient(", "linear-gradient(", ".editor-panel",
                  ".decision-card", ".side-link.active", ".hero-art",
                  ".primary::before", ".primary::after"):
        assert token in css
    assert "prefers-reduced-motion: reduce" in css
    assert ".motion-off *" in css
    assert "enableOpticalReflections()" in product
    assert "reducedMotion.matches" in product
    assert "setInterval(" not in product
    assert "innerHTML" not in product
    assert not any(external in css for external in ("@import", "url(http", "url(//"))
