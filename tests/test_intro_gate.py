"""Welcome gate preserves the frozen studio and requires a deliberate entry."""
from types import SimpleNamespace

from importlib.resources import files

from ofc_engine.hosted_preview import HostedFixtureHandler


def test_hosted_intro_injection_preserves_frozen_studio():
    page = HostedFixtureHandler.__new__(HostedFixtureHandler)
    page.server = SimpleNamespace(
        tour_proposal=None, run_registry=None, public_showcase=None,
    )
    replies = []
    page._send = lambda status, body, mime: replies.append((status, body, mime))
    page._studio_page()

    assert len(replies) == 1
    status, body, mime = replies[0]
    assert status == 200 and mime == "text/html; charset=utf-8"
    html = body.decode("utf-8")
    assert html.count('id="ofc-intro"') == 1
    assert html.count('id="ofc-intro-understand"') == 1
    assert 'Same AI.' in html and 'Same evidence.' in html
    assert 'Better decisions.' in html and 'Qloo evidence' in html
    assert html.index('id="ofc-intro"') < html.index('<div class="shell" inert aria-hidden="true">')
    assert '<link rel="stylesheet" href="/intro.css">' in html
    assert '<script defer src="/intro.js"></script>' in html
    assert '<script defer src="/studio.js"></script>' in html
    assert "<div class=\"shell\">" in files("ofc_engine").joinpath("web", "studio.html").read_text()
    assert not any(token in html for token in ("sk-example-key", "PLACEHOLDER_SECRET"))


def test_intro_has_no_extra_navigation_or_external_provider_links():
    web = files("ofc_engine").joinpath("web")
    intro = web.joinpath("intro.html").read_text()
    css = web.joinpath("intro.css").read_text()
    js = web.joinpath("intro.js").read_text()
    assert intro.count("<button ") == 1
    assert "<nav" not in intro and "<aside" not in intro and "<a " not in intro
    assert "src=\"http" not in intro
    assert 'aria-modal="true"' in intro
    assert 'prefers-reduced-motion: reduce' in css
    assert 'body.studio-page:not(.ofc-intro-opened) .shell' in css
    assert "root.remove()" in js
    assert "sessionStorage" not in js and "localStorage" not in js
    assert "fetch(" not in js
