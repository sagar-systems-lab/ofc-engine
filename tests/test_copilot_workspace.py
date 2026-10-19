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


def test_readiness_controls_are_bounded_without_new_endpoints():
    from importlib.resources import files

    web = files("ofc_engine").joinpath("web")
    page = web.joinpath("copilot-workspace.html").read_text()
    script = web.joinpath("copilot-workspace.js").read_text()
    assert 'id="status-mode-explanation"' in page
    assert 'id="attachment-file"' in page
    assert 'type="file"' in page
    assert 'accept=".txt,.md,.csv,.json' in page
    assert 'id="attachment-remove"' in page
    assert 'id="composer-session-info"' in page
    assert 'id="run-confirm-budget"' in page
    assert "Server availability is separate" in script
    assert 'limits.qloo_requests_remaining' in script
    assert 'state.capability?.capacity?.qloo_requests_remaining' in script
    assert 'NOT SUBMITTED — only ' in script
    assert 'state.localNotice = {prompt, detail};' in script
    assert 'await loadCapabilities(); // Read-only refresh after durable provider admission.' in script
    assert 'No Gemini or Qloo request was made.' in script
    assert 'The last saved review is still available ' in script
    assert 'state.runs.length >= state.sessionMessageLimit' in script
    assert 'caps.session_message_limit' in script
    assert 'saved.session_message_limit' in script
    assert 'session_message_limit' in web.joinpath("copilot-workspace.js").read_text()
    assert 'prompt.length > 4000' in script
    assert 'file.size > 16 * 1024' in script
    assert 'content.length > 3000' in script
    assert 'file.text()' in script
    assert 'window.OFCResults' in script
    assert 'FileReader' not in script
    assert 'FormData(' not in script
    assert '/api/attachments' not in script


def test_qloo_entity_suggestions_are_actionable_and_explicitly_confirmed():
    from importlib.resources import files
    js = files("ofc_engine").joinpath("web", "copilot-workspace.js").read_text()
    css = files("ofc_engine").joinpath("web", "copilot-workspace.css").read_text()
    for token in ("qloo-selection", "requestSelection", "entity_selection_ref",
                  "parent_run_id", "run.suggestions", "state.pendingSelection",
                  'if (state.pendingSelection) Object.assign(payload, state.pendingSelection)'):
        assert token in js
    assert ".qloo-option" in css
    assert 'option.option_ref' in js and 'option.name' in js
    assert 'No provider request occurs until you confirm.' in js
