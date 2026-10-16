"use strict";

(() => {
  const login = document.getElementById("login");
  const manager = document.getElementById("manager");
  const feedback = document.getElementById("feedback");
  const ownerSecret = document.getElementById("owner-secret");
  const providers = [...document.querySelectorAll(".provider")];

  const tell = (message) => { feedback.textContent = message; };

  async function request(path, method = "GET", value) {
    const opts = {
      method, credentials: "same-origin", cache: "no-store",
      headers: method === "POST" ? { "Content-Type": "application/json" } : {},
    };
    if (method === "POST") opts.body = JSON.stringify(value);
    const response = await fetch(path, opts);
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "Request unavailable");
    return result;
  }

  function present(report) {
    const statuses = report.providers || {};
    for (const node of providers) {
      const provider = node.dataset.provider;
      const data = statuses[provider] || { status: "NOT_CONFIGURED" };
      const state = node.querySelector(".state");
      const suffix = data.masked_suffix ? " ••••" + data.masked_suffix : "";
      state.textContent = data.status + suffix;
      state.dataset.status = data.status;
    }
  }

  async function refresh() {
    try {
      const report = await request("/api/owner/credentials");
      manager.hidden = false;
      login.hidden = true;
      present(report);
      tell("Credentials are stored on the OFC server. A configured key is not yet a live connection.");
    } catch {
      manager.hidden = true;
      login.hidden = false;
      tell("Owner sign-in required.");
    }
  }

  document.getElementById("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const secret = ownerSecret.value;
    ownerSecret.value = "";
    try {
      await request("/api/owner/login", "POST", { owner_secret: secret });
      await refresh();
    } catch {
      tell("Owner sign-in failed. Check the private OFC owner secret.");
    }
  });

  manager.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button || button.disabled) return;
    const node = button.closest(".provider");
    const provider = node?.dataset.provider;
    if (!provider) return;
    const action = button.dataset.action;
    const key = node.querySelector("input");
    let path, payload;
    if (action === "save") {
      if (!key.value.trim()) { tell("Paste a provider key first."); return; }
      path = "/api/owner/credentials";
      payload = { action: "set", provider, api_key: key.value };
    } else if (action === "revoke") {
      if (!window.confirm("Revoke this server-side API key?")) return;
      path = "/api/owner/credentials";
      payload = { action: "revoke", provider };
    } else if (action === "test") {
      if (!window.confirm("Make one real provider request? This consumes API quota.")) return;
      path = "/api/owner/test-connection";
      payload = { provider, confirm: "SPEND_ONE_PROVIDER_REQUEST" };
    } else return;

    button.disabled = true;
    try {
      const result = await request(path, "POST", payload);
      if (action === "save") key.value = "";
      if (action === "test") tell(provider + ": " + result.status + ". One connection test was attempted.");
      else tell(provider + " credential " + (action === "revoke" ? "revoked." : "encrypted and saved."));
      const current = action === "test"
        ? await request("/api/owner/credentials") : result;
      present(current);
    } catch (error) {
      tell("Request failed: " + error.message);
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById("logout").addEventListener("click", async () => {
    try { await request("/api/owner/logout", "POST", {}); }
    catch { /* Local view still locks. */ }
    manager.hidden = true;
    login.hidden = false;
    tell("Owner session locked.");
  });

  refresh();
})();
