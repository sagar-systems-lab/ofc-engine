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



def test_saved_result_brief_separates_cultural_review_from_commercial_release():
    from importlib.resources import files
    web = files("ofc_engine").joinpath("web")
    script = web.joinpath("copilot-proof.js").read_text()
    css = web.joinpath("copilot-workspace.css").read_text()
    assert '"Evidence-grounded decision brief"' in script
    assert '"OFC intervention"' in script
    assert '"Partnership readiness"' in script
    assert '"HOLD — commercial evidence incomplete."' in script
    assert '"City allocation"' in script
    assert '"Model explanation · not independently verified"' in script
    assert 'business_evidence_satisfied===true' in script
    assert 'Repeat Qloo ranking matched (same provider)' in script
    assert 'Cultural check: ' in script
    assert '.decision-brief{' in css and '.narrative-section{' in css



def test_live_graph_is_evidence_only_and_bounded():
    from importlib.resources import files
    web = files("ofc_engine").joinpath("web")
    graph = web.joinpath("copilot-proof.js").read_text()
    styles = web.joinpath("copilot-workspace.css").read_text()
    assert 'const decisionFlow=(run,pair,mode)=>' in graph
    assert 'if(!pair) return null' in graph
    assert '[...pair.before].slice(0,4)' in graph
    assert 'rankedEvidence(pair)' in graph
    assert 'run.business_verification?.missing_requirements' in graph
    assert 'OFC not applied' in graph
    assert 'not a synthetic dependency graph' in graph
    assert 'Arrows show review stages' in graph
    assert '.live-decision-graph{' in styles
    assert '.graph-track{' in styles


def test_network_failure_has_manual_recovery_without_automatic_submit():
    from importlib.resources import files
    js = files('ofc_engine').joinpath('web', 'copilot-workspace.js').read_text()
    assert 'MODEL_NETWORK_UNAVAILABLE' in js
    assert 'MODEL_TIMEOUT' in js
    assert 'Prepare a new attempt' in js
    assert 'el("chat-input").value = run.prompt' in js

def test_decision_advisory_is_saved_evidence_only():
    from pathlib import Path
    web = Path(__file__).resolve().parents[1] / "src" / "ofc_engine" / "web"
    js = web.joinpath("copilot-proof.js").read_text()
    css = web.joinpath("copilot-workspace.css").read_text()
    assert "const advisoryPanel=(run,pair,mode)=>" in js
    assert 'if(!pair)return null;' in js
    assert "const ranked=rankedEvidence(pair);" in js
    assert 'reviewed.ofc_reason==="QLOO_RANK_RECHECK_MATCHED"' in js
    assert 'mode==="OFF"?"Starting choice: "+choice' in js
    assert "No qualified brand recommendation yet" in js
    assert "No deal, booking or payment is authorized" in js
    assert "advisoryPanel(run,pair,mode)" in js
    assert js.index("body.append(narrative,decisionBrief(run,pair,mode))") < js.index("const flow=decisionFlow(run,pair,mode);")
    assert ".advisory-insights{" in css
    assert "@media(max-width:900px)" in css



def test_missing_detail_continuation_is_scoped_and_confirmed():
    from importlib.resources import files
    web = files("ofc_engine").joinpath("web")
    js = web.joinpath("copilot-workspace.js").read_text()
    css = web.joinpath("copilot-workspace.css").read_text()
    assert 'const clarificationPanel = run =>' in js
    assert 'const requestClarification = (run, detail, category) =>' in js
    assert '"TASTE_SEED_REQUIRED", "ARTIST_NAME_REQUIRED", "DECISIONS_MISSING"' in js
    assert 'state.pendingSelection = {parent_run_id: run.run_id}' in js
    assert 'run.prompt.trim() + addition' in js
    assert 'if (prompt.length > 4000)' in js
    assert 'No provider request occurs until you confirm.' in js
    assert 'const missingDetail = clarificationPanel(run);' in js
    assert 'if (missingDetail) assistant.append(missingDetail)' in js
    assert '.clarification-panel{' in css
    assert '.clarification-field{' in css
    assert 'for (const button of followUps || [])' in js
    assert 'button.disabled = busy || state.runs.length >= state.sessionMessageLimit' in js
    assert 'setBusy(false); // Re-enable saved follow-ups without clearing typed details.' in js


def test_processing_resync_is_read_only_and_bounded():
    from importlib.resources import files
    web = files("ofc_engine").joinpath("web")
    js = web.joinpath("copilot-workspace.js").read_text()
    assert 'if (payload === undefined) opts.signal = AbortSignal.timeout(20000)' in js
    assert 'state.syncing = true' in js and 'state.syncing = false' in js
    assert 'Check saved result' in js
    assert 'try { await restore(); } finally {' in js
    assert 'document.addEventListener("visibilitychange"' in js
    assert 'Do not resend or refresh to retry.' in js

def test_unqualified_place_never_displays_unverified_narration():
    from importlib.resources import files
    web = files("ofc_engine").joinpath("web")
    js = web.joinpath("copilot-proof.js").read_text()
    activity = web.joinpath("copilot-activity.js").read_text()
    assert 'const unqualifiedPlace=Boolean(pair' in js
    assert 'No verified place winner — recommendation withheld' in js
    assert 'Evidence-limited place review · unverified model claims withheld' in js
    assert 'A discovered cafe, guest house or hotel is not necessarily a hill station' in js
    assert 'placeOnly?"Venue / commercial readiness":"Partnership readiness"' in js
    assert 'qualified repeated rank not established' in activity


def test_taste_explorer_follows_saved_place_and_requires_user_approval():
    from importlib.resources import files
    web = files("ofc_engine").joinpath("web")
    proof = web.joinpath("copilot-proof.js").read_text()
    workspace = web.joinpath("copilot-workspace.js").read_text()
    css = web.joinpath("copilot-workspace.css").read_text()
    assert 'const tasteExplorer=(run,pair,mode,onTaste)=>' in proof
    assert 'if(mode!=="ON" || !pair || !run._sessionReadConfirmed' in proof
    assert 'No verified place winner — recommendation withheld' in proof
    assert 'Find alternatives for my taste' in proof
    assert 'Existing Qloo discovery · destination screening' in proof
    assert "These are saved Qloo place entities, not qualified hill-station alternatives" in proof
    assert "excluded from the destination shortlist" in proof
    assert 'const requestTasteAlternatives=(run,profile)=>' in workspace
    assert 'state.pendingSelection={parent_run_id:run.run_id}' in workspace
    assert 'if(!el("run-confirm").open)el("run-confirm").showModal()' in workspace
    assert 'window.OFCResults?.renderRun(run, state.mode, message, requestTasteAlternatives)' in workspace
    assert 'No bookings or external actions.' in workspace
    assert '.taste-form[hidden]{display:none}' in css
    assert '@media(max-width:650px)' in css


def test_new_taste_discovery_overrides_parent_choice_in_intake_guidance():
    from pathlib import Path
    instructions = (
        Path(__file__).resolve().parents[1] / "src" / "ofc_engine" / "copilot_llm.py"
    ).read_text()
    assert "latest request explicitly asks to discover NEW alternatives" in instructions
    assert "treat previous selections as history" in instructions
    assert "Never present the taste seed as the chosen destination" in instructions



def test_saved_status_reconciliation_preserves_visible_dom():
    from importlib.resources import files
    web = files("ofc_engine").joinpath("web")
    js = web.joinpath("copilot-workspace.js").read_text()
    css = web.joinpath("copilot-workspace.css").read_text()
    assert 'const contentChanged = JSON.stringify(nextRuns) !== JSON.stringify(state.runs)' in js
    assert 'if (contentChanged || previousSelected !== state.selected ||' in js
    assert 'previousLimit !== state.sessionMessageLimit)' in js
    assert 'if (priorLimit !== state.sessionMessageLimit) render()' in js
    assert '}, 8000);' in js
    assert 'AbortSignal.timeout(20000)' in js
    assert '.message{margin:0 0 23px' in css
    assert 'animation:message-rise .22s ease both' not in css


def test_copilot_discovery_failure_is_explained_without_provider_leakage():
    from importlib.resources import files
    js = files("ofc_engine").joinpath("web/copilot-workspace.js").read_text()
    for code in (
        "QLOO_DISCOVERY_PROVIDER_RATE_LIMITED",
        "QLOO_DISCOVERY_PROVIDER_UNAVAILABLE",
        "QLOO_DISCOVERY_INSUFFICIENT_RESULTS",
        "QLOO_DISCOVERY_AFFINITY_MISSING",
        "QLOO_DISCOVERY_RANK_AMBIGUOUS",
    ):
        assert code in js
    assert "No automatic retry was made" in js
