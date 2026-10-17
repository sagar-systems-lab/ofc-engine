"""One-screen Copilot route stays gated without changing the legacy studio."""
from types import SimpleNamespace

from ofc_engine.hosted_preview import HostedFixtureHandler


def render(workspace: bool) -> str:
    handler = HostedFixtureHandler.__new__(HostedFixtureHandler)
    handler.server = SimpleNamespace(
        tour_proposal=None, run_registry=None, public_showcase=None,
    )
    replies = []
    handler._send = lambda status, body, content_type: replies.append(
        (status, body, content_type)
    )
    handler._studio_page(workspace=workspace)
    assert len(replies) == 1
    status, body, content_type = replies[0]
    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    return body.decode()


def test_new_workspace_keeps_original_intro_gate():
    html = render(workspace=True)
    assert html.count('id="ofc-intro"') == 1
    assert html.count('id="ofc-intro-understand"') == 1
    assert html.count('<div class="shell" inert aria-hidden="true">') == 1
    assert html.index('id="ofc-intro"') < html.index('id="conversation-sidebar"')
    assert '<link rel="stylesheet" href="/intro.css">' in html
    assert '<script defer src="/intro.js"></script>' in html
    assert '<script defer src="/copilot-workspace.js"></script>' in html
    assert 'id="chat-input"' in html
    assert 'id="mode-toggle"' in html
    assert 'id="activity-rail"' in html
    assert 'id="run-confirm"' in html
    assert 'id="view-home"' not in html
    assert 'id="more-tools"' not in html
    assert 'id="ofc-intro-title"' in html
    assert "Same evidence." in html


def test_legacy_studio_remains_available_for_compatibility():
    html = render(workspace=False)
    assert 'id="home-title"' in html
    assert 'id="more-tools"' in html
    assert 'id="conversation-sidebar"' not in html
    assert '<div class="shell" inert aria-hidden="true">' in html


def test_workspace_sends_no_provider_requests_before_explicit_send():
    from importlib.resources import files

    root = files("ofc_engine").joinpath("web")
    js = root.joinpath("copilot-workspace.js").read_text()
    css = root.joinpath("copilot-workspace.css").read_text()
    template = root.joinpath("copilot-workspace.html").read_text()
    assert "SPEND_ONE_PROVIDER_REQUEST" not in js
    assert "generativelanguage.googleapis.com" not in js
    assert "qloo.com" not in js
    assert "new request_id" not in js
    assert 'state.sessionId = readId()' in js
    assert 'void loadCapabilities()' in js
    assert 'jsonRequest("/api/copilot/sessions"' in js
    assert "crypto.randomUUID()" in js
    assert "requestSend();" in js
    assert 'role="group"' in template
    assert 'aria-label="Conversation history"' in template
    assert ':focus-visible' in css
    assert '@media(prefers-reduced-motion:reduce)' in css


def test_workspace_secondary_tools_respect_owner_and_fixture_boundaries():
    from importlib.resources import files

    base = files("ofc_engine").joinpath("web")
    document = base.joinpath("copilot-workspace.html").read_text()
    tools_js = base.joinpath("copilot-tools.js").read_text()
    assert 'id="demo-dialog"' in document and 'id="settings-dialog"' in document
    assert 'data-demo="repair"' in document and 'data-demo="outage"' in document
    assert 'data-demo="dependent_missing"' in document
    assert 'data-demo="budget_exhausted"' in document
    assert 'id="settings-owner-form"' in document
    assert 'autocomplete="off"' in document
    assert 'id="settings-delete-session"' in document
    assert 'type="password"' in document
    assert 'SPEND_ONE_PROVIDER_REQUEST' in tools_js
    assert 'DELETE_MY_SESSION' in tools_js
    assert 'CREATE_COPILOT_SESSION' not in tools_js
    assert 'generativelanguage.googleapis.com' not in tools_js
    assert 'window.OFCTools' in tools_js
    assert 'owner/credentials' in tools_js
    assert 'owner/login' in tools_js
    assert 'owner/test-connection' in tools_js
    assert 'demo?scenario=' in tools_js
    assert "<iframe" not in document
    assert 'src="https://' not in document
    assert 'src="http://' not in document
