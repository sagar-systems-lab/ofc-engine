"use strict";

(() => {
  const el = id => document.getElementById(id);
  if (!el("chat-input") || !el("conversation-list")) return;
  const shell = document.querySelector("body.copilot-page > .shell");
  const storageKey = "ofc.copilot.visitor.session.v1";
  const state = {
    sessionId: null, runs: [], selected: null, mode: "ON", busy: false,
    pendingPrompt: "", sidebarOpen: window.innerWidth > 1180, search: "",
    capability: null, activeRequestId: null, pollTimer: null,
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
    };
    return labels[code] || "This review is unavailable (" + String(code || "UNKNOWN").slice(0, 64) + ").";
  };

  const statusText = label => {
    el("composer-status").textContent = label;
  };

  const setBusy = busy => {
    state.busy = busy;
    el("chat-input").readOnly = busy;
    el("chat-send").disabled = busy || !el("chat-input").value.trim();
    if (!busy) resizeComposer();
  };

  const resizeComposer = () => {
    const input = el("chat-input");
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 138) + "px";
    if (!state.busy) el("chat-send").disabled = !input.value.trim();
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
    const reason = value?.reason_code ? " Reason: " + String(value.reason_code).slice(0, 80) + "." : "";
    return safeStatus(status) + reason;
  };

  const renderRun = (run) => {
    const box = el("chat-transcript");
    box.replaceChildren();
    const text = String(run.prompt || state.pendingPrompt || "");
    if (text) box.append(message("user", text));
    if (run.status === "PROCESSING") {
      box.append(message("assistant", safeStatus("PROCESSING")));
    } else if (run.status === "REVIEW_REQUIRED") {
      const answer = state.mode === "ON" ? run.ofc_answer : run.original_answer;
      const assistant = message("assistant",
        typeof answer === "string" && answer.trim() ? answer : safeStatus("REVIEW_REQUIRED"));
      const card = node("section", "message-card");
      card.append(node("h2", "", state.mode === "ON" ? "OFC review · saved" : "Qloo baseline · saved"));
      card.append(node("p", "",
        "Review required. Qloo cultural evidence is not a guarantee of commercial suitability. External actions are withheld."));
      assistant.append(card);
      box.append(assistant);
    } else {
      const info = run.question && typeof run.question === "string"
        ? run.question + "\n\n" + safeReason(run) : safeReason(run);
      const assistant = message("assistant", info);
      const tag = node("span", "message-state" + (run.status === "BLOCKED" ? " blocked" : ""),
        String(run.status || "UNKNOWN").replaceAll("_", " "));
      assistant.append(tag);
      box.append(assistant);
    }
    box.scrollTop = box.scrollHeight;
  };

  const render = () => {
    renderHistory();
    const run = selectedRun();
    const visible = run || (state.busy && state.pendingPrompt);
    el("chat-empty").hidden = Boolean(visible);
    el("chat-transcript").hidden = !visible;
    if (run) renderRun(run);
    else if (state.busy && state.pendingPrompt) {
      const box = el("chat-transcript");
      box.replaceChildren(message("user", state.pendingPrompt),
        message("assistant", "Waiting for a saved server result. No automatic retry will be made."));
    } else {
      el("chat-transcript").replaceChildren();
    }
    const proof = el("proof-entry");
    proof.disabled = true;
    proof.title = run ? "Saved run available. Proof export is not available in this view."
      : "Proof available after a saved response";
    proof.setAttribute("aria-label", proof.title);
    window.OFCActivity?.render(run || (state.busy && state.pendingPrompt
      ? {local_pending: true} : null));
  };

  const loadCapabilities = async () => {
    try {
      const caps = await jsonRequest("/api/capabilities");
      state.capability = caps;
      const raw = String(caps.product || "UNKNOWN");
      const dot = el("status-dot");
      dot.className = "status-dot " +
        (raw === "ACTIVE" ? "good" : raw === "NOT_CONFIGURED" || raw === "DEGRADED" ? "bad" : "warn");
      el("status-heading").textContent = raw === "ACTIVE" ? "Review ready" :
        raw === "CHECK_REQUIRED" ? "Check required" :
        raw === "NOT_CONFIGURED" ? "Not configured" : "Status";
      const rows = el("status-rows");
      rows.replaceChildren();
      for (const [label, key] of [["OFC", "ofc"],["Qloo", "qloo"],["Model", "llm"],["Storage", "storage"]]) {
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
    if (!state.sessionId) return;
    try {
      const saved = await jsonRequest("/api/copilot/sessions/" + state.sessionId);
      state.runs = Array.isArray(saved.messages) ? saved.messages : [];
      if (!state.selected && state.runs.length) state.selected = state.runs.at(-1).run_id;
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
      } else statusText(safeStatus(err.message));
    }
  };

  const monitor = () => {
    stopPolling();
    if (!state.selected || !state.sessionId || selectedRun()?.status !== "PROCESSING") return;
    state.pollTimer = window.setTimeout(async () => {
      if (document.visibilityState !== "hidden") await restore();
      monitor();
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
    setBusy(true);
    statusText("Submitting one bounded request…");
    const requestId = "ui-" + crypto.randomUUID();
    state.activeRequestId = requestId;
    try {
      const id = await ensureSession();
      const reply = await jsonRequest("/api/copilot/sessions/" + id + "/messages",
        {request_id: requestId, prompt});
      state.pendingPrompt = "";
      el("chat-input").value = "";
      resizeComposer();
      await restore();
      if (reply.run_id && !state.runs.some(x => x.run_id === reply.run_id))
        state.runs.push({prompt, ...reply});
      state.selected = reply.run_id || state.runs.at(-1)?.run_id || null;
      render();
      monitor();
      statusText(reply.status === "PROCESSING" ? safeStatus("PROCESSING") :
        "Saved response. Switching modes and opening history do not use provider quota.");
    } catch (err) {
      statusText(safeStatus(err.message) + " Do not submit again until you check saved history.");
      if (state.sessionId) await restore(); // Safe GET only. Never re-POST.
    } finally {
      state.pendingPrompt = "";
      state.activeRequestId = null;
      setBusy(false);
    }
  };

  const requestSend = () => {
    if (state.busy || !el("chat-input").value.trim()) return;
    state.pendingPrompt = el("chat-input").value.trim();
    const modal = el("run-confirm");
    if (!modal.open) modal.showModal();
  };

  el("chat-input").addEventListener("input", resizeComposer);
  el("chat-input").addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      requestSend();
    }
  });
  el("chat-send").addEventListener("click", requestSend);
  el("run-cancel").addEventListener("click", () => {
    state.pendingPrompt = "";
    el("run-confirm").close();
    el("chat-input").focus();
  });
  el("run-approve").addEventListener("click", () => {
    el("run-confirm").close();
    void submit();
  });
  el("run-confirm").addEventListener("cancel", () => {
    state.pendingPrompt = "";
  });

  el("new-chat").addEventListener("click", () => {
    if (state.busy) return;
    state.selected = null;
    el("chat-input").value = "";
    render();
    resizeComposer();
    statusText("New conversation. No provider request made.");
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

  el("mode-toggle").addEventListener("click", () => {
    state.mode = state.mode === "ON" ? "OFF" : "ON";
    el("mode-toggle").classList.toggle("on", state.mode === "ON");
    el("mode-toggle").setAttribute("aria-pressed", String(state.mode === "ON"));
    el("mode-toggle").setAttribute("aria-label",
      state.mode === "ON" ? "OFC ON — show reviewed result" : "OFC OFF — show Qloo baseline");
    el("mode-label").textContent = state.mode;
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
  state.sessionId = readId();
  updateShell();
  setActivity(window.innerWidth > 1180);
  resizeComposer();
  render();
  void loadCapabilities(); // Read-only; no provider request.
  if (state.sessionId) void restore(); // Cookie-gated saved history, no provider request.
})();
