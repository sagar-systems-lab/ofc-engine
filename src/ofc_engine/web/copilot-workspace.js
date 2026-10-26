"use strict";

(() => {
  const el = id => document.getElementById(id);
  if (!el("chat-input") || !el("conversation-list")) return;
  const shell = document.querySelector("body.copilot-page > .shell");
  const storageKey = "ofc.copilot.visitor.session.v1";
  const state = {
    sessionId: null, runs: [], selected: null, mode: "ON", busy: false, syncing: false,
    pendingPrompt: "", localNotice: null, sidebarOpen: window.innerWidth > 1180, search: "",
    capability: null, sessionMessageLimit: 4, activeRequestId: null, pendingBaselineIds: null, pollTimer: null,
    attachment: null, attachmentVersion: 0, pendingStartedAt: null, elapsedTimer: null,
    pendingSelection: null,
  };

  const icon = (id, size = 17) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "ico");
    svg.style.width = size + "px";
    svg.style.height = size + "px";
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#ofc-icon-" + id);
    svg.append(use);
    return svg;
  };

  const node = (tag, klass = "", text = "") => {
    const n = document.createElement(tag);
    if (klass) n.className = klass;
    if (text) n.textContent = text;
    return n;
  };

  const readId = () => {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || "null");
      return value && /^cp-[a-f0-9]{32}$/.test(value.id)
        && Date.now() - value.saved < 86_400_000 ? value.id : null;
    } catch {
      return null;
    }
  };

  const saveId = id => {
    try {
      if (!id) localStorage.removeItem(storageKey);
      else localStorage.setItem(storageKey, JSON.stringify({id, saved: Date.now()}));
    } catch {
      // The HttpOnly cookie, not this nonsecret locator, grants server access.
    }
  };

  const jsonRequest = async (path, payload) => {
    const opts = {
      credentials: "same-origin", cache: "no-store",
      headers: {"Accept":"application/json"},
    };
    if (payload !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(payload);
    }
    if (payload === undefined) opts.signal = AbortSignal.timeout(12000);
    const res = await fetch(path, opts);
    let data;
    try {
      data = await res.json();
    } catch {
      throw Error("INVALID_SERVER_RESPONSE");
    }
    if (!res.ok) throw Error(typeof data.error === "string" ? data.error : "HTTP_" + res.status);
    return data;
  };

  const safeStatus = code => {
    const labels = {
      REVIEW_REQUIRED: "Reviewed with available Qloo evidence; further checks remain before any action.",
      CLARIFICATION_REQUIRED: "I need one detail before I can review this cultural decision.",
      CAPABILITY_UNAVAILABLE: "This request is outside the currently qualified cultural decision scope.",
      BLOCKED: "The review could not complete. The saved record may contain partial provider usage.",
      PROCESSING: "The server is processing your request. This view does not automatically retry inference.",
      NOT_CONFIGURED: "Provider configuration is incomplete.",
      CHECK_REQUIRED: "Previous provider checks are older than 30 minutes. This does not mean disconnected.",
      DEGRADED: "A required provider is unavailable.",
      COPILOT_CAPACITY_REACHED: "Daily capacity is reached. No new run was started.",
      COPILOT_NOT_CONFIGURED: "Copilot is not configured for live requests.",
      ACCESS_DENIED: "Your private session expired or is no longer accessible.",
      COPILOT_STORAGE_UNAVAILABLE: "Saved-result storage is currently unavailable.",
      COPILOT_SESSION_MESSAGE_LIMIT: "This visitor session reached its submitted-request limit. Save any proof before creating a new session.",
      INVALID_COPILOT_MESSAGE: "The request is outside supported bounds. Please shorten your prompt or attached notes.",
    };
    return labels[code] || "This review is unavailable (" + String(code || "UNKNOWN").slice(0, 64) + ").";
  };

  const statusText = label => {
    el("composer-status").textContent = label;
  };

  const setBusy = busy => {
    state.busy = busy;
    el("chat-input").readOnly = busy;
    el("attachment-add").disabled = busy;
    el("attachment-remove").disabled = busy;
    el("chat-send").disabled = busy || state.runs.length >= state.sessionMessageLimit ||
      !composedPrompt() || composedPrompt().length > 4000;
    const followUps = el("chat-transcript")?.querySelectorAll(
      ".clarification-continue, .intake-recovery > .result-action, .message.assistant > .result-action"
    );
    for (const button of followUps || [])
      button.disabled = busy || state.runs.length >= state.sessionMessageLimit;
    if (!busy) resizeComposer();
  };

  const composedPrompt = () => {
    const message = el("chat-input").value.trim();
    if (!message) return null;
    if (!state.attachment) return message;
    const attached = state.attachment;
    return message + "\n\n--- USER-SUPPLIED TEXT NOTES (context, not Qloo evidence) ---\n" +
      "Document: " + attached.name + "\n" + attached.text +
      "\n--- END USER-SUPPLIED NOTES ---";
  };

  const resizeComposer = () => {
    const input = el("chat-input");
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 138) + "px";
    const prompt = composedPrompt();
    if (!state.busy) el("chat-send").disabled = state.runs.length >= state.sessionMessageLimit ||
      !prompt || prompt.length > 4000;
    if (state.attachment && prompt && prompt.length > 4000)
      statusText("Combined prompt exceeds 4,000 characters. Shorten your question or remove notes; no upload occurred.");
  };

  const clearAttachment = () => {
    state.attachmentVersion++;
    state.attachment = null;
    el("attachment-file").value = "";
    el("attachment-chip").hidden = true;
    el("attachment-label").textContent = "";
    resizeComposer();
  };

  const attachText = async file => {
    if (!file) return;
    const version = ++state.attachmentVersion;
    state.attachment = null;
    el("attachment-chip").hidden = true;
    el("attachment-label").textContent = "";
    resizeComposer();
    const extension = file.name.split(".").pop().toLowerCase();
    if (!["txt", "md", "csv", "json"].includes(extension)) {
      statusText("PDF/DOCX are not supported. Please export a short text excerpt to .txt or .md.");
      return;
    }
    if (!file.size || file.size > 16 * 1024) {
      statusText("This file is empty or exceeds the 16 KiB local-reading cap. Use a shorter text excerpt.");
      return;
    }
    try {
      const content = (await file.text()).replace(/^\uFEFF/, "").trim();
      if (version !== state.attachmentVersion) return;
      if (!content || content.includes("\u0000") || content.includes("\uFFFD") || content.length > 3000) {
        statusText("Only nonempty UTF-8 text excerpts up to 3,000 characters are accepted. Nothing uploaded.");
        return;
      }
      state.attachment = {name: file.name.slice(0, 80).replace(/[\r\n\t]/g, " "), text: content};
      el("attachment-chip").hidden = false;
      el("attachment-label").textContent = state.attachment.name + " · " + content.length + " characters · included on Send";
      statusText("Local text notes ready. Sending requires confirmation and may use real provider quota.");
      resizeComposer();
    } catch {
      statusText("Cannot read that text file. Nothing was uploaded.");
    } finally {
      el("attachment-file").value = "";
      resizeComposer();
    }
  };

  const updateShell = () => {
    const mobile = window.innerWidth <= 768;
    shell.classList.toggle("mobile-sidebar-open", mobile && state.sidebarOpen);
    if (!mobile) shell.classList.toggle("sidebar-collapsed", !state.sidebarOpen);
    else shell.classList.remove("sidebar-collapsed");
    el("sidebar-backdrop").hidden = !mobile || !state.sidebarOpen;
    el("sidebar-toggle").setAttribute("aria-expanded", String(state.sidebarOpen));
    el("sidebar-collapse").setAttribute("aria-label",
      state.sidebarOpen ? "Collapse conversations" : "Expand conversations");
  };

  const setSidebar = value => {
    state.sidebarOpen = value;
    updateShell();
  };

  const setActivity = show => {
    shell.classList.toggle("activity-collapsed", !show);
    shell.classList.toggle("activity-overlay-open", show && window.innerWidth <= 1180);
    el("activity-open").hidden = show;
    el("activity-hide").setAttribute("aria-expanded", String(show));
  };

  const selectedRun = () => state.runs.find(x => x.run_id === state.selected) || null;

  const renderHistory = () => {
    const list = el("conversation-list");
    list.replaceChildren();
    const runs = [...state.runs].reverse().filter(run =>
      String(run.prompt || run.question || "Untitled review").toLowerCase().includes(state.search));
    if (!runs.length) {
      list.append(node("p", "history-empty",
        state.search ? "No matching conversation in this session." :
          "Saved conversations in this private session will appear here."));
      return;
    }
    list.append(node("p", "history-group-heading", "This private session"));
    for (const run of runs) {
      const button = node("button", "conversation-row" + (state.selected === run.run_id ? " active" : ""));
      button.type = "button";
      button.title = String(run.prompt || "Saved review");
      button.setAttribute("aria-current", state.selected === run.run_id ? "true" : "false");
      button.append(node("span", "", String(run.prompt || "Saved review")));
      button.append(node("time", "", String(run.status || "SAVED").replaceAll("_", " ")));
      button.addEventListener("click", () => {
        state.selected = run.run_id;
        state.localNotice = null;
        render();
        if (window.innerWidth <= 768) setSidebar(false);
      });
      list.append(button);
    }
  };

  const message = (role, textValue) => {
    const wrap = node("article", "message " + role);
    if (role === "assistant") {
      const header = node("div", "message-meta");
      const avatar = node("span", "avatar");
      avatar.append(icon("spark", 15));
      header.append(avatar, node("strong", "", "OFC Engine"));
      wrap.append(header);
    }
    wrap.append(node("div", "message-copy", String(textValue || "")));
    return wrap;
  };

  const safeReason = value => {
    const status = String(value?.status || "BLOCKED");
    const code = String(value?.reason_code || "");
    if (status === "BLOCKED" &&
        ["MODEL_NETWORK_UNAVAILABLE", "MODEL_TIMEOUT",
         "MODEL_PROVIDER_UNAVAILABLE"].includes(code)) {
      const connection = code === "MODEL_TIMEOUT"
        ? "The model timed out during initial plan understanding."
        : code === "MODEL_PROVIDER_UNAVAILABLE"
        ? "The AI provider was temporarily unavailable during initial plan understanding."
        : "The model connection failed during initial plan understanding.";
      const qloo = value?.qloo_requests_this_run === 0
        ? " No Qloo request was recorded." : "";
      return connection + qloo +
        " The review was saved as blocked, and no automatic retry was attempted. " +
        "You can prepare a new attempt after checking quota; a new model request may consume capacity.";
    }
    const reason = code ? " Reason: " + code.slice(0, 80) + "." : "";
    return safeStatus(status) + reason;
  };


  // This clock measures waiting time; saved attempt states alone establish progress.
  const pendingDetail = run => {
    const activity = run?.activity;
    if (!run) return "Waiting for a saved server result. No automatic retry will be made.";
    if (activity?.source !== "PERSISTED_ATTEMPTS")
      return "Request admitted. Saved stage evidence is not yet available.";
    const evidence = activity.qloo_evidence;
    if (evidence === "SEARCH_ONLY")
      return "Qloo entity search saved. Ranking evidence not yet observed.";
    if (evidence === "RANK_PENDING_OR_INTERRUPTED")
      return "Qloo search completed; ranking request reserved, result not yet validated.";
    if (evidence === "RANK_OBSERVED")
      return "Qloo observation saved. Checking final review and whether ranking qualifies.";
    if (evidence === "PARTIAL")
      return "Some Qloo observations saved; unresolved provider attempts remain.";
    if (evidence === "SEARCH_PENDING_OR_INTERRUPTED")
      return "Qloo entity search reserved; awaiting a verified response.";
    return "Request admitted. No verified Qloo stage is saved yet.";
  };

  const pendingPanel = run => {
    const panel = node("section", "processing-panel");
    panel.setAttribute("aria-label", "Review progress");
    const header = node("div", "processing-heading");
    const pulse = node("span", "processing-pulse");
    pulse.setAttribute("aria-hidden", "true");
    const label = node("strong", "", run ? "Review in progress" : "Submitting your review");
    const clock = node("time", "processing-elapsed", "00:00 elapsed");
    // The timer is visual-only to avoid repeating announcements each second.
    clock.setAttribute("aria-hidden", "true");
    const serverTime = Date.parse(run?.admitted_at_utc || "");
    const fallback = state.pendingStartedAt || Date.now();
    clock.dataset.startedAt = String(
      Number.isFinite(serverTime) ? Math.min(serverTime, fallback) : fallback
    );
    header.append(pulse, label, clock);
    panel.append(header, node("p", "processing-stage", pendingDetail(run)));
    const note = node("p", "processing-proof",
      run ? "Stage details are based on saved server evidence. Reserved requests are not completed evidence." :
        "Waiting for server admission. No provider stage is claimed yet.");
    panel.append(note);
    const hosting = node("p", "processing-host-note");
    hosting.append(document.createTextNode(
      "Free Render hosting and Qloo/model processing can make a live review take 2–3 minutes or longer. " +
      "Keep this tab open; meanwhile explore "
    ));
    for (const [label, path] of [["Plan Review", "/plans"], ["Lab", "/proof"]]) {
      if (path === "/proof") hosting.append(document.createTextNode(" or "));
      const link = node("a", "", label);
      link.href = path;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.setAttribute("aria-label", "Open " + label + " in a new tab");
      hosting.append(link);
    }
    hosting.append(document.createTextNode(" in another tab."));
    panel.append(hosting);
    const warning = node("p", "processing-longwait");
    warning.hidden = true;
    panel.append(warning);
    if (run && /^cp-run-[a-f0-9]{32}$/.test(run.run_id || "")) {
      const sync = node("button", "result-action processing-sync", "Check saved result");
      sync.type = "button";
      sync.setAttribute("aria-label", "Read the saved review status without submitting again");
      sync.addEventListener("click", async () => {
        if (sync.disabled || state.syncing) return;
        sync.disabled = true;
        statusText("Checking saved session history only. No provider retry or quota spend.");
        try { await restore(); } finally {
          if (sync.isConnected) sync.disabled = false;
          monitor();
        }
      });
      panel.append(sync);
    }
    return panel;
  };

  const updateElapsed = () => {
    const transcript = el("chat-transcript");
    const clock = transcript.querySelector(".processing-elapsed");
    if (!clock) return;
    const start = Number(clock.dataset.startedAt);
    if (!Number.isFinite(start) || start <= 0) return;
    const seconds = Math.max(0, Math.floor((Date.now() - start) / 1000));
    const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
    const ss = String(seconds % 60).padStart(2, "0");
    clock.textContent = mm + ":" + ss + " elapsed";
    const warning = transcript.querySelector(".processing-longwait");
    if (!warning) return;
    warning.hidden = seconds < 60;
    if (seconds >= 120)
      warning.textContent = "The original response may have disconnected even if the server saved the result. Use Check saved result to resync read-only. Do not resend or refresh to retry.";
    else if (seconds >= 60)
      warning.textContent = "Still waiting for a confirmed result. Some provider reviews take longer; no retry has been sent.";
  };

  const syncElapsedTimer = () => {
    if (el("chat-transcript").querySelector(".processing-elapsed")) {
      if (state.elapsedTimer === null)
        state.elapsedTimer = window.setInterval(updateElapsed, 1000);
      updateElapsed();
    } else if (state.elapsedTimer !== null) {
      clearInterval(state.elapsedTimer);
      state.elapsedTimer = null;
    }
  };

  const requestSelection = (run, option) => {
    if (state.busy || state.runs.length >= state.sessionMessageLimit) {
      statusText("Session limit reached. No provider request was made.");
      return;
    }
    const remaining = state.capability?.capacity?.qloo_requests_remaining;
    if (Number.isInteger(remaining) && remaining < 5) {
      statusText("Not submitted: fewer than 5 Qloo requests remain. Wait for quota reset.");
      return;
    }
    if (!/^cp-run-[a-f0-9]{32}$/.test(run.run_id || "") ||
        !/^[a-f0-9]{16}$/.test(option.option_ref || "") ||
        !run.suggestions?.some(candidate => candidate.option_ref === option.option_ref) ||
        !run.prompt || run.prompt.length > 4000) return;
    state.pendingPrompt = run.prompt;
    state.pendingSelection = {
      parent_run_id: run.run_id, entity_selection_ref: option.option_ref,
    };
    const caps = state.capability?.capacity;
    el("run-confirm-budget").textContent =
      "Selected Qloo search suggestion: " + String(option.name).slice(0, 160) +
      ". This starts a new review (not a free retry). It may spend up to " +
      "5 Qloo requests and 3 model requests. Current remaining: Qloo " +
      (Number.isInteger(caps?.qloo_requests_remaining) ? caps.qloo_requests_remaining : "unknown") +
      ", model " + (Number.isInteger(caps?.llm_requests_remaining) ? caps.llm_requests_remaining : "unknown") +
      ". No provider request occurs until you confirm.";
    if (!el("run-confirm").open) el("run-confirm").showModal();
  };

  const requestClarification = (run, detail, category) => {
    if (state.busy || state.runs.length >= state.sessionMessageLimit) {
      statusText("Session limit reached. No new provider request made.");
      return false;
    }
    if (!/^cp-run-[a-f0-9]{32}$/.test(run.run_id || "") ||
        run.status !== "CLARIFICATION_REQUIRED" ||
        !["TASTE_SEED_REQUIRED", "ARTIST_NAME_REQUIRED", "DECISIONS_MISSING"].includes(run.reason_code) ||
        typeof run.prompt !== "string" || !run.prompt.trim()) return false;
    if (typeof detail !== "string" || !detail.trim() || detail.length > 240 ||
        /[\u0000-\u001f]/.test(detail)) {
      statusText("Enter the requested detail (up to 240 characters). Nothing submitted.");
      return false;
    }
    const isSeed = run.reason_code !== "DECISIONS_MISSING";
    if (isSeed && !["place", "artist", "brand", "movie"].includes(category)) return false;
    const addition = isSeed
      ? "\n\n--- USER-SUPPLIED CLARIFICATION ---\n" +
        "Taste seed name: " + detail.trim() + "\n" +
        "Taste seed category: " + category + "\n" +
        "This is a user-provided cultural reference, not Qloo verification. " +
        "Keep my original request and target category. If cultural discovery is unsupported, say so; " +
        "do not invent travel, business or venue facts.\n--- END CLARIFICATION ---"
      : "\n\n--- USER-SUPPLIED CLARIFICATION ---\n" +
        detail.trim() + "\nContinue my original request without inventing evidence.\n" +
        "--- END CLARIFICATION ---";
    const prompt = run.prompt.trim() + addition;
    if (prompt.length > 4000) {
      statusText("Original request plus clarification exceeds 4,000 characters. Use a shorter detail or new request.");
      return false;
    }
    const remaining = state.capability?.capacity;
    if (Number.isInteger(remaining?.qloo_requests_remaining) &&
        remaining.qloo_requests_remaining < 5) {
      statusText("At least 5 Qloo requests must remain. No request submitted.");
      return false;
    }
    if (Number.isInteger(remaining?.llm_requests_remaining) &&
        remaining.llm_requests_remaining < 3) {
      statusText("At least 3 model requests must remain. No request submitted.");
      return false;
    }
    state.pendingPrompt = prompt;
    state.pendingSelection = {parent_run_id: run.run_id};
    el("run-confirm-budget").textContent =
      "Continue this saved question with your provided detail. This is a NEW linked review, " +
      "not an automatic retry. It may use up to 5 Qloo and 3 model requests. " +
      "Current remaining: Qloo " +
      (Number.isInteger(remaining?.qloo_requests_remaining) ? remaining.qloo_requests_remaining : "unknown") +
      ", model " + (Number.isInteger(remaining?.llm_requests_remaining) ? remaining.llm_requests_remaining : "unknown") +
      ". No provider request occurs until you confirm. Place discovery is cultural only: " +
      "it cannot verify weather, transport, availability or the objectively best destination.";
    if (!el("run-confirm").open) el("run-confirm").showModal();
    return true;
  };

  const clarificationPanel = run => {
    if (run.status !== "CLARIFICATION_REQUIRED" ||
        !["TASTE_SEED_REQUIRED", "ARTIST_NAME_REQUIRED", "DECISIONS_MISSING"].includes(run.reason_code) ||
        !/^cp-run-[a-f0-9]{32}$/.test(run.run_id || "") ||
        typeof run.prompt !== "string") return null;
    const isSeed = run.reason_code !== "DECISIONS_MISSING";
    const panel = node("section", "clarification-panel");
    panel.setAttribute("aria-label", "Provide missing details");
    const title = node("h3", "clarification-heading",
      isSeed ? "One detail needed to continue" : "What should OFC compare?");
    const explanation = node("p", "clarification-help", isSeed
      ? "What existing place, artist, movie or brand has the style you like? " +
        "For a hill-station idea, enter a real place you already know. " +
        "OFC can explore cultural similarity only; this is not a verified travel ranking."
      : "Describe the specific candidates or choices you want reviewed. " +
        "OFC will keep your original request.");
    panel.append(title, explanation);
    const field = node("label", "clarification-field");
    field.append(node("span", "", isSeed ? "Known cultural reference" : "Additional choice details"));
    const input = node(isSeed ? "input" : "textarea", "clarification-input");
    if (isSeed) {
      input.type = "text";
      input.maxLength = 160;
      input.placeholder = "e.g., a place you have enjoyed";
    } else {
      input.maxLength = 240;
      input.rows = 3;
      input.placeholder = "Which options should be compared?";
    }
    input.autocomplete = "off";
    field.append(input);
    panel.append(field);
    let category = null;
    if (isSeed) {
      const kindLabel = node("label", "clarification-field");
      kindLabel.append(node("span", "", "Reference type"));
      category = node("select", "clarification-type");
      for (const [value, label] of [
        ["place", "Place"], ["artist", "Artist"], ["movie", "Movie"], ["brand", "Brand"]
      ]) {
        const option = node("option", "", label);
        option.value = value;
        category.append(option);
      }
      kindLabel.append(category);
      panel.append(kindLabel);
    }
    const feedback = node("p", "clarification-feedback", "");
    feedback.setAttribute("role", "status");
    panel.append(feedback);
    const button = node("button", "result-action clarification-continue", "Continue review");
    button.type = "button";
    button.disabled = state.busy || state.runs.length >= state.sessionMessageLimit;
    button.addEventListener("click", () => {
      const detail = input.value.trim();
      if (!detail) {
        feedback.textContent = "This detail is required. No request was sent.";
        input.focus();
        return;
      }
      if (!requestClarification(run, detail, category?.value || null)) {
        feedback.textContent = el("composer-status").textContent;
      } else {
        feedback.textContent = "";
      }
    });
    panel.append(button, node("p", "clarification-foot",
      "Editing these fields is free. Only Continue → Confirm sends a new request and consumes a visitor slot."));
    return panel;
  };

  const renderRun = (run) => {
    const box = el("chat-transcript");
    box.replaceChildren();
    const text = String(run.prompt || state.pendingPrompt || "");
    if (text) box.append(message("user", text));
    if (run.status === "PROCESSING") {
      const waiting = message("assistant", "");
      waiting.querySelector(".message-copy").replaceWith(pendingPanel(run));
      box.append(waiting);
    } else if (run.status === "REVIEW_REQUIRED") {
      const rendered = window.OFCResults?.renderRun(run, state.mode, message);
      if (rendered) box.append(rendered);
      else box.append(message("assistant", safeStatus("REVIEW_REQUIRED")));
    } else {
      const promptedClarification = run.status === "CLARIFICATION_REQUIRED" &&
        ["TASTE_SEED_REQUIRED", "ARTIST_NAME_REQUIRED", "DECISIONS_MISSING"].includes(run.reason_code);
      const info = promptedClarification
        ? "I can continue with your original question. Please fill in the missing detail below."
        : run.question && typeof run.question === "string"
          ? run.question + "\n\n" + safeReason(run) : safeReason(run);
      const assistant = message("assistant", info);
      const tag = node("span", "message-state" + (run.status === "BLOCKED" ? " blocked" : ""),
        String(run.status || "UNKNOWN").replaceAll("_", " "));
      assistant.append(tag);
      if (run.status === "BLOCKED" &&
          ["MODEL_TIMEOUT", "MODEL_NETWORK_UNAVAILABLE",
           "MODEL_PROVIDER_UNAVAILABLE"].includes(run.reason_code) &&
          typeof run.prompt === "string" && run.prompt.length <= 4000) {
        const prepare = node("button", "result-action", "Prepare a new attempt");
        prepare.type = "button";
        prepare.disabled = state.busy || state.runs.length >= state.sessionMessageLimit;
        prepare.addEventListener("click", () => {
          if (state.busy || state.runs.length >= state.sessionMessageLimit) return;
          el("chat-input").value = run.prompt;
          resizeComposer();
          el("chat-input").focus();
          statusText("Prompt restored only. No provider call was sent. Press Send and confirm quota to start a new review.");
        });
        assistant.append(prepare);
        const recovery = node("section", "intake-recovery");
        recovery.append(
          node("strong", "qloo-selection-title", "Continue with confirmed details"),
          node("p", "qloo-selection-help",
            "If the planning model was unavailable, enter the artist, two brands and your original choice. " +
            "These names are not verified until Qloo checks them. No provider request is sent until confirmation.")
        );
        const addField = (label, placeholder) => {
          const field = node("label", "intake-recovery-field");
          field.append(node("span", "", label));
          const input = node("input");
          input.type = "text";
          input.maxLength = 160;
          input.autocomplete = "off";
          input.placeholder = placeholder;
          field.append(input);
          recovery.append(field);
          return input;
        };
        const artist = addField("Artist", "Exact artist name");
        const first = addField("First brand", "First candidate");
        const second = addField("Second brand", "Second candidate");
        const choiceLabel = node("label", "intake-recovery-field");
        choiceLabel.append(node("span", "", "My original choice"));
        const choice = node("select");
        for (const [value, title] of [["0", "First brand"], ["1", "Second brand"]]) {
          const option = node("option", "", title);
          option.value = value;
          choice.append(option);
        }
        choiceLabel.append(choice);
        recovery.append(choiceLabel);
        const continueButton = node("button", "result-action", "Review confirmed details");
        continueButton.type = "button";
        continueButton.addEventListener("click", () => {
          if (state.busy || state.runs.length >= state.sessionMessageLimit ||
              !/^cp-run-[a-f0-9]{32}$/.test(run.run_id || "") ||
              typeof run.prompt !== "string" || run.prompt.length > 4000) return;
          const names = [artist.value.trim(), first.value.trim(), second.value.trim()];
          if (names.some(x => !x || x.length > 160) || names[1] === names[2]) {
            statusText("Enter one artist and two different brand names. No request submitted.");
            return;
          }
          const remaining = state.capability?.capacity;
          if (Number.isInteger(remaining?.qloo_requests_remaining) &&
              remaining.qloo_requests_remaining < 5) {
            statusText("At least 5 Qloo requests are required. No request submitted.");
            return;
          }
          if (Number.isInteger(remaining?.llm_requests_remaining) &&
              remaining.llm_requests_remaining < 2) {
            statusText("At least 2 model requests are required. No request submitted.");
            return;
          }
          state.pendingPrompt = run.prompt;
          state.pendingSelection = {
            parent_run_id: run.run_id,
            confirmed_plan: {
              artist: names[0], brands: names.slice(1),
              selected: names[Number(choice.value) + 1],
            },
          };
          el("run-confirm-budget").textContent =
            "User-confirmed intake (not model extracted). This new linked review may use " +
            "5 Qloo and 2 narration model calls; verification or clarification may still be required. " +
            "No request is made until you confirm.";
          if (!el("run-confirm").open) el("run-confirm").showModal();
        });
        recovery.append(continueButton);
        assistant.append(recovery);
      }
      const missingDetail = clarificationPanel(run);
      if (missingDetail) assistant.append(missingDetail);
      if (run.status === "CLARIFICATION_REQUIRED" &&
          Array.isArray(run.suggestions) && run.suggestions.length) {
        const panel = node("div", "qloo-selection");
        const searched = run.entity_request?.name;
        panel.append(node("strong", "qloo-selection-title",
          "Select the intended Qloo match for " +
          (typeof searched === "string" ? searched.slice(0, 160) : "this name")));
        panel.append(node("p", "qloo-selection-help",
          "Search suggestions are not independently verified. Select the correct entity to prepare " +
          "a new review. Confirmation is required before any provider quota is used."));
        for (const option of run.suggestions.slice(0, 8)) {
          if (!option || typeof option.name !== "string" ||
              !option.name.trim() || option.name.length > 160 ||
              !/^[a-f0-9]{16}$/.test(option.option_ref || "")) continue;
          const action = node("button", "qloo-option");
          action.type = "button";
          action.append(node("strong", "", option.name));
          action.append(node("span", "", String(option.category || "entity").slice(0, 32)));
          action.disabled = state.runs.length >= state.sessionMessageLimit;
          action.addEventListener("click", () => requestSelection(run, option));
          panel.append(action);
        }
        assistant.append(panel);
      }
      if (run._sessionReadConfirmed && run.run_id &&
          run.status !== "PROCESSING") {
        const actions = node("div", "result-actions");
        const proof = node("button", "result-action", "Download saved status (.json)");
        proof.type = "button";
        proof.addEventListener("click", () => window.OFCResults?.downloadJSON?.());
        actions.append(proof);
        assistant.append(actions);
      }
      box.append(assistant);
    }
    box.scrollTop = box.scrollHeight;
  };

  const render = () => {
    renderHistory();
    el("composer-session-info").textContent = state.runs.length +
      " of " + state.sessionMessageLimit + " submitted requests in this private visitor session." +
      (state.runs.length >= state.sessionMessageLimit ? " Limit reached; no new provider requests will be started." : "");
    resizeComposer();
    const run = selectedRun();
    const visible = state.localNotice || run || (state.busy && state.pendingPrompt);
    el("chat-empty").hidden = Boolean(visible);
    el("chat-transcript").hidden = !visible;
    if (state.localNotice) {
      el("chat-transcript").replaceChildren(
        message("user", state.localNotice.prompt),
        message("assistant", state.localNotice.detail)
      );
    } else if (run) renderRun(run);
    else if (state.busy && state.pendingPrompt) {
      const box = el("chat-transcript");
      const waiting = message("assistant", "");
      waiting.querySelector(".message-copy").replaceWith(pendingPanel(null));
      box.replaceChildren(message("user", state.pendingPrompt), waiting);
    } else {
      el("chat-transcript").replaceChildren();
    }
    const proof = el("proof-entry");
    window.OFCResults?.syncProof(state.localNotice ? null : run);
    window.OFCActivity?.render((state.localNotice ? null : run) || (state.busy && state.pendingPrompt
      ? {local_pending: true} : null));
    syncElapsedTimer();
  };

  const updateModeExplanation = () => {
    el("status-mode-explanation").textContent = state.mode === "OFF"
      ? "Selected view: OFC OFF (LLM + Qloo baseline). Server availability is separate; new submissions still compute both views."
      : "Selected view: OFC ON (reviewed result). Server availability is separate; switching views never makes a provider call.";
  };

  const loadCapabilities = async () => {
    try {
      const caps = await jsonRequest("/api/capabilities");
      state.capability = caps;
      if (Number.isInteger(caps.session_message_limit) &&
          caps.session_message_limit >= 1 && caps.session_message_limit <= 12)
        state.sessionMessageLimit = caps.session_message_limit;
      render();
      const raw = String(caps.product || "UNKNOWN");
      const dot = el("status-dot");
      dot.className = "status-dot " +
        (raw === "ACTIVE" ? "good" : raw === "NOT_CONFIGURED" || raw === "DEGRADED" ? "bad" : "warn");
      el("status-heading").textContent = raw === "ACTIVE" ? "Review ready" :
        raw === "CHECK_REQUIRED" ? "Check required" :
        raw === "NOT_CONFIGURED" ? "Not configured" : "Status";
      const rows = el("status-rows");
      rows.replaceChildren();
      updateModeExplanation();
      for (const [label, key] of [["OFC engine available", "ofc"],["Qloo provider check", "qloo"],["Model provider check", "llm"],["Saved-run storage", "storage"]]) {
        const row = node("div", "status-row");
        row.append(node("strong", "", label), node("span", "", String(caps[key] || "UNKNOWN").replaceAll("_", " ")));
        rows.append(row);
      }
      const note = caps.capacity;
      if (note && typeof note === "object") {
        for (const [label, key] of [["Qloo remaining", "qloo_requests_remaining"],["LLM remaining", "llm_requests_remaining"]]) {
          if (Number.isInteger(note[key])) {
            const row = node("div", "status-row");
            row.append(node("strong", "", label), node("span", "", String(note[key])));
            rows.append(row);
          }
        }
      }
    } catch {
      el("status-heading").textContent = "Unavailable";
      el("status-dot").className = "status-dot bad";
      el("status-rows").replaceChildren(node("p", "secondary-copy", "Capability endpoint unavailable. No provider test was performed."));
    }
  };

  const stopPolling = () => {
    if (state.pollTimer !== null) clearTimeout(state.pollTimer);
    state.pollTimer = null;
  };

  const restore = async () => {
    if (!state.sessionId || state.syncing) return;
    state.syncing = true;
    try {
      const saved = await jsonRequest("/api/copilot/sessions/" + state.sessionId);
      if (Number.isInteger(saved.session_message_limit) &&
          saved.session_message_limit >= 1 && saved.session_message_limit <= 12)
        state.sessionMessageLimit = saved.session_message_limit;
      state.runs = Array.isArray(saved.messages)
        ? saved.messages.map(item => ({...item, _sessionReadConfirmed: true})) : [];
      if (!state.selected && state.busy && state.pendingPrompt) {
        const admitted = [...state.runs].reverse().find(run =>
          run.prompt === state.pendingPrompt &&
          !state.pendingBaselineIds?.has(run.run_id));
        if (admitted) state.selected = admitted.run_id;
      } else if (!state.selected && state.runs.length) {
        state.selected = state.runs.at(-1).run_id;
      }
      if (state.selected && !state.runs.some(x => x.run_id === state.selected))
        state.selected = state.runs.at(-1)?.run_id || null;
      if (state.selected && selectedRun()?.status !== "PROCESSING") {
        state.activeRequestId = null;
        stopPolling();
      }
      render();
    } catch (err) {
      if (err.message === "ACCESS_DENIED") {
        state.sessionId = null;
        state.runs = [];
        state.selected = null;
        saveId(null);
        stopPolling();
        statusText("This private session has expired. Start a new chat to continue.");
        render();
      } else if (err.name === "TimeoutError" || err.name === "AbortError") {
        statusText("Saved status read timed out; no new review was submitted. Check saved result again.");
      } else statusText(safeStatus(err.message));
    } finally {
      state.syncing = false;
    }
  };

  const monitor = () => {
    stopPolling();
    const requestPending = state.busy && Boolean(state.pendingPrompt);
    if (!state.sessionId || (!requestPending &&
        (!state.selected || selectedRun()?.status !== "PROCESSING"))) return;
    state.pollTimer = window.setTimeout(async () => {
      try {
        if (document.visibilityState !== "hidden") await restore();
      } finally {
        monitor();
      }
    }, 3000);
  };

  const ensureSession = async () => {
    if (state.sessionId) return state.sessionId;
    const created = await jsonRequest("/api/copilot/sessions",
      {confirm: "CREATE_COPILOT_SESSION"});
    if (!/^cp-[a-f0-9]{32}$/.test(String(created.session_id || "")))
      throw Error("INVALID_SERVER_SESSION");
    state.sessionId = created.session_id;
    saveId(state.sessionId);
    return state.sessionId;
  };

  const submit = async () => {
    const prompt = state.pendingPrompt;
    if (!prompt || state.busy) return;
    const draftInput = el("chat-input").value;
    state.pendingBaselineIds = new Set(state.runs.map(run => run.run_id));
    state.localNotice = null;
    state.pendingStartedAt = Date.now();
    setBusy(true);
    state.selected = null;
    el("chat-input").value = "";
    render();
    statusText("Submitting one bounded request…");
    const requestId = "ui-" + crypto.randomUUID();
    state.activeRequestId = requestId;
    try {
      const id = await ensureSession();
      monitor(); // Read-only GETs while the admitted POST is in flight.
      const payload = {request_id: requestId, prompt};
      if (state.pendingSelection) Object.assign(payload, state.pendingSelection);
      const reply = await jsonRequest("/api/copilot/sessions/" + id + "/messages", payload);
      state.pendingPrompt = "";
      el("chat-input").value = "";
      clearAttachment();
      resizeComposer();
      await restore();
      if (reply.run_id && !state.runs.some(x => x.run_id === reply.run_id))
        state.runs.push({prompt, ...reply});
      state.selected = reply.run_id || state.runs.at(-1)?.run_id || null;
      render();
      monitor();
      await loadCapabilities(); // Read-only refresh after durable provider admission.
      statusText(reply.status === "PROCESSING" ? safeStatus("PROCESSING") :
        "Saved response. Switching modes and opening history do not use provider quota.");
    } catch (err) {
      statusText(safeStatus(err.message) + " Do not submit again until you check saved history.");
      if (state.sessionId) await restore(); // Safe GET only. Never re-POST.
      if (!state.runs.some(run => run.prompt === prompt)) {
        el("chat-input").value = draftInput;
      }
      const code = String(err.message || "");
      if (code.includes("QLOO_DAILY_QUOTA_EXHAUSTED") ||
          code.includes("DAILY_QLOO_BUDGET_EXHAUSTED")) {
        state.selected = null;
        state.localNotice = {
          prompt,
          detail: "NOT SUBMITTED — Qloo daily capacity is insufficient. " +
            "No new decision was saved. The previous result remains in Saved conversations. " +
            "Do not retry until sufficient quota is available.",
        };
        render();
      }
      await loadCapabilities(); // No new provider calls; never replay POST.
    } finally {
      state.pendingPrompt = "";
      state.pendingStartedAt = null;
      state.pendingSelection = null;
      state.activeRequestId = null;
      state.pendingBaselineIds = null;
      setBusy(false); // Re-enable saved follow-ups without clearing typed details.
      monitor(); // Continue only if the saved run is still processing.
    }
  };

  const requestSend = () => {
    if (state.busy) return;
    state.pendingSelection = null;
    if (state.runs.length >= state.sessionMessageLimit) {
      statusText("This private visitor session has reached its " +
        state.sessionMessageLimit + "-request limit. No provider call made.");
      return;
    }
    const prompt = composedPrompt();
    if (!prompt) return;
    if (prompt.length > 4000) {
      statusText("Maximum 4,000 characters including attached notes. No provider call made.");
      return;
    }
    const available = state.capability?.capacity?.qloo_requests_remaining;
    if (Number.isInteger(available) && available < 5) {
      const detail = "NOT SUBMITTED — only " + available +
        " Qloo calls remain; a qualified artist/brand review requires at least 5. " +
        "No Gemini or Qloo request was made. The last saved review is still available " +
        "in the conversation list. Wait for verified quota before submitting."; 
      state.selected = null;
      state.localNotice = {prompt, detail};
      render();
      statusText("Not submitted: insufficient Qloo quota. No provider call made.");
      return;
    }
    state.pendingPrompt = prompt;
    const limits = state.capability?.capacity;
    el("run-confirm-budget").textContent = limits &&
      Number.isInteger(limits.qloo_requests_remaining) &&
      Number.isInteger(limits.llm_requests_remaining)
      ? "Last known daily allowance: Qloo " + limits.qloo_requests_remaining +
        ", model " + limits.llm_requests_remaining +
        ". A qualifying run may require several calls; this does not reserve quota."
      : "Live daily provider allowance could not be confirmed. Submitting may spend quota.";
    const modal = el("run-confirm");
    if (!modal.open) modal.showModal();
  };

  el("chat-input").addEventListener("input", resizeComposer);
  el("attachment-add").addEventListener("click", () => {
    if (!state.busy) el("attachment-file").click();
  });
  el("attachment-file").addEventListener("change", event => {
    void attachText(event.target.files?.[0]);
  });
  el("attachment-remove").addEventListener("click", clearAttachment);
  el("chat-input").addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      requestSend();
    }
  });
  el("chat-send").addEventListener("click", requestSend);
  el("run-cancel").addEventListener("click", () => {
    state.pendingPrompt = "";
    state.pendingSelection = null;
    el("run-confirm").close();
    el("chat-input").focus();
  });
  el("run-approve").addEventListener("click", () => {
    el("run-confirm").close();
    void submit();
  });
  el("run-confirm").addEventListener("cancel", () => {
    state.pendingPrompt = "";
    state.pendingSelection = null;
  });

  el("new-chat").addEventListener("click", () => {
    if (state.busy) return;
    state.selected = null;
    state.localNotice = null;
    el("chat-input").value = "";
    clearAttachment();
    render();
    resizeComposer();
    statusText(state.runs.length >= state.sessionMessageLimit
      ? "Visitor-session limit reached. New chat only resets the draft; it does not reset quota."
      : "New draft. No provider request made; the private session retains its remaining allowance.");
    el("chat-input").focus();
    if (window.innerWidth <= 768) setSidebar(false);
  });

  el("conversation-search").addEventListener("input", event => {
    state.search = event.target.value.trim().toLowerCase();
    renderHistory();
  });

  el("sidebar-toggle").addEventListener("click", () => setSidebar(!state.sidebarOpen));
  el("sidebar-collapse").addEventListener("click", () => setSidebar(!state.sidebarOpen));
  el("sidebar-bottom-toggle").addEventListener("click", () => setSidebar(!state.sidebarOpen));
  el("sidebar-backdrop").addEventListener("click", () => setSidebar(false));
  window.addEventListener("resize", updateShell);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" &&
        state.sessionId && (selectedRun()?.status === "PROCESSING" || state.busy)) {
      void restore().finally(monitor); // Read-only reconciliation; never POST again.
    }
  });

  el("mode-toggle").addEventListener("click", () => {
    state.mode = state.mode === "ON" ? "OFF" : "ON";
    el("mode-toggle").classList.toggle("on", state.mode === "ON");
    el("mode-toggle").setAttribute("aria-pressed", String(state.mode === "ON"));
    el("mode-toggle").setAttribute("aria-label",
      state.mode === "ON" ? "OFC ON — show reviewed result" : "OFC OFF — show Qloo baseline");
    el("mode-label").textContent = state.mode;
    updateModeExplanation();
    render(); // Presentation projection only. No provider call or DB write.
  });

  el("status-toggle").addEventListener("click", () => {
    const open = el("status-popover").hidden;
    el("status-popover").hidden = !open;
    el("status-toggle").setAttribute("aria-expanded", String(open));
    if (open) void loadCapabilities();
  });
  el("status-close").addEventListener("click", () => {
    el("status-popover").hidden = true;
    el("status-toggle").setAttribute("aria-expanded", "false");
    el("status-toggle").focus();
  });
  el("activity-hide").addEventListener("click", () => setActivity(false));
  el("activity-open").addEventListener("click", () => setActivity(true));
  window.addEventListener("keydown", event => {
    if (event.key === "Escape" && !el("status-popover").hidden) {
      el("status-popover").hidden = true;
      el("status-toggle").setAttribute("aria-expanded", "false");
      el("status-toggle").focus();
    }
  });

  window.OFCActivity?.mount();
  window.OFCResults?.mount({getRun: selectedRun, getMode: () => state.mode});
  window.OFCTools?.mount({
    sessionId: () => state.sessionId,
    clearSession: () => {
      state.sessionId = null;
      state.runs = [];
      state.selected = null;
      state.pendingPrompt = "";
      stopPolling();
      saveId(null);
      render();
      statusText("Private visitor session deleted. New reviews require a new session.");
    },
  });
  const scope = el("scope-guide");
  const scopeVersion = "ofc.copilot.scope-reviewed.v1";
  const scopeAcknowledged = () => {
    try { return localStorage.getItem(scopeVersion) === "yes"; }
    catch { return false; }
  };
  const showScope = () => {
    if (scope && !scope.open && !document.querySelector("#ofc-intro") &&
        !document.querySelector("body > .shell[inert]")) scope.showModal();
  };
  const intro = el("ofc-intro");
  if (scope && !scopeAcknowledged()) {
    if (intro) {
      const observer = new MutationObserver(() => {
        if (!intro.isConnected) {
          observer.disconnect();
          showScope();
        }
      });
      observer.observe(document.body, {childList: true, subtree: true});
    } else showScope();
  }
  scope?.addEventListener("close", () => {
    try { localStorage.setItem(scopeVersion, "yes"); } catch {}
  });
  el("scope-done")?.addEventListener("click", () => scope.close());
  el("scope-reopen")?.addEventListener("click", showScope);
  el("scope-example")?.addEventListener("click", () => {
    const example = "Artist: Taylor Swift. Candidate brands: Nike and Adidas. " +
      "Original choice: Nike. Review exactly one hypothetical cultural brand partnership " +
      "decision with Qloo evidence; no contract, payment, booking or external action.";
    if (!state.busy) {
      state.selected = null;
      el("chat-input").value = example;
      clearAttachment();
      render();
      resizeComposer();
    }
    scope.close(); // Only drafts text. Sending still requires separate approval.
    el("chat-input").focus();
  });
  state.sessionId = readId();
  updateModeExplanation();
  updateShell();
  setActivity(window.innerWidth > 1180);
  resizeComposer();
  render();
  void loadCapabilities(); // Read-only; no provider request.
  if (state.sessionId) void restore(); // Cookie-gated saved history, no provider request.
})();
