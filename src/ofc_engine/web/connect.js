"use strict";

(() => {
  const login = document.getElementById("login");
  const manager = document.getElementById("manager");
  const feedback = document.getElementById("feedback");
  const ownerSecret = document.getElementById("owner-secret");
  const providers = [...document.querySelectorAll(".provider")];
  let connectionTests = null;

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
    if (report.connection_tests) connectionTests = report.connection_tests;
    const exhausted = connectionTests?.remaining === 0;
    for (const node of providers) {
      const provider = node.dataset.provider;
      const data = statuses[provider] || { status: "NOT_CONFIGURED" };
      const state = node.querySelector(".state");
      const suffix = data.masked_suffix ? " ••••" + data.masked_suffix : "";
      state.textContent = data.status
        + (data.qualification === "CONNECTIVITY_ONLY" ? " (API only)" : "")
        + suffix;
      state.dataset.status = data.status;
      const verification = node.querySelector(".verification");
      const history = data.last_test;
      if (!history || !history.checked_at) {
        verification.textContent = data.configured ? "No successful test recorded." : "No key configured.";
      } else {
        const label = history.outcome === "ACTIVE" || history.outcome === "CONNECTED"
          ? (history.outcome === "CONNECTED" ? "API connectivity verified" : "Last verification successful")
          : history.outcome === "UNAVAILABLE" ? "Last verification failed" : "Last test: " + history.outcome;
        const when = new Date(history.checked_at);
        const timestamp = Number.isNaN(when.getTime()) ? "" : " · " + when.toLocaleString();
        const freshness = history.freshness === "EXPIRED"
          ? " · Older than 30 min; not evidence of disconnection"
          : history.freshness === "RECENT" ? " · Recently checked" : "";
        verification.textContent = label + timestamp + freshness;
      }
      node.querySelector('button[data-action="test"]').disabled = exhausted;
    }
  }

  async function refresh() {
    try {
      const report = await request("/api/owner/credentials");
      manager.hidden = false;
      login.hidden = true;
      present(report);
      const reasons = Object.entries(report.providers || {})
        .filter(([, data]) => data.reason_code)
        .map(([provider, data]) => provider + ": " + data.reason_code);
      const quota = connectionTests?.remaining === 0
        ? " Connection tests exhausted (" + connectionTests.used + "/" + connectionTests.limit
          + "); reset " + connectionTests.resets_at_utc + "." : "";
      const connectivity = report.providers?.gemini?.qualification === "CONNECTIVITY_ONLY"
        ? " Gemini API was verified; full Copilot draft is NOT qualified."
        : "";
      const stale = Object.entries(report.providers || {})
        .filter(([, data]) => data.status === "CHECK_REQUIRED"
          && ["ACTIVE", "CONNECTED"].includes(data.last_test?.outcome))
        .map(([provider]) => provider);
      const freshness = stale.length
        ? " Previous successful checks now outside 30-minute freshness: "
          + stale.join(", ") + ". No disconnection inferred." : "";
      tell((reasons.length
        ? "Last failed tests: " + reasons.join(" | ") + "."
        : "Credentials are stored on the OFC server. A configured key is not yet a live connection.")
        + connectivity + freshness + quota + " Status refresh made no provider request.");
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
      if (connectionTests?.remaining === 0) {
        tell("Connection test allowance exhausted. Reset at "
          + connectionTests.resets_at_utc + ". No provider request made.");
        return;
      }
      if (!window.confirm("Make one real provider request? This consumes API quota.")) return;
      path = "/api/owner/test-connection";
      payload = { provider, confirm: "SPEND_ONE_PROVIDER_REQUEST" };
    } else return;

    button.disabled = true;
    try {
      const result = await request(path, "POST", payload);
      if (action === "save") key.value = "";
      if (action === "test") {
        const detail = result.reason_code ? " — " + result.reason_code : "";
        const scope = result.qualification === "CONNECTIVITY_ONLY"
          ? " Gemini API verified; full Copilot proposal remains unqualified." : "";
        tell(provider + ": " + result.status + detail + ". One connection test was attempted." + scope);
      }
      else tell(provider + " credential " + (action === "revoke" ? "revoked." : "encrypted and saved."));
      const current = action === "test"
        ? await request("/api/owner/credentials") : result;
      present(current);
    } catch (error) {
      if (error.message === "CONNECTION_TEST_BUDGET_EXHAUSTED") {
        connectionTests = { ...connectionTests, remaining: 0 };
        for (const item of providers) {
          item.querySelector('button[data-action="test"]').disabled = true;
        }
        tell("Connection test quota exhausted. No provider request made.");
      } else {
        tell("Request failed: " + error.message);
      }
    } finally {
      button.disabled = action === "test" && connectionTests?.remaining === 0;
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
