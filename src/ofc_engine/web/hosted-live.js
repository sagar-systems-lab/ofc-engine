(() => {
  "use strict";

  const provider = document.getElementById("view-provider");
  if (!provider) return;

  const element = (tag, value) => {
    const node = document.createElement(tag);
    if (value !== undefined) node.textContent = value;
    return node;
  };

  const format = data => {
    if (
      data.status !== "LIVE_RANK_OBSERVED" ||
      data.release_authorized !== false ||
      data.provider_authenticated !== false ||
      data.independent_recheck_completed !== false
    ) throw new Error("Unqualified observation");
    return "Qloo rank observed: original choice rank " + data.baseline_rank +
      " of " + data.candidate_count + ". " +
      (data.selected_candidate_is_top
        ? "Original choice ranks first."
        : "A different candidate ranks higher; repair suggested, not applied.") +
      " Independent verification and execution remain blocked.";
  };

  async function start() {
    const response = await fetch("/api/qloo/status", {cache: "no-store"});
    if (!response.ok) return;
    const status = await response.json();
    if (
      status.capability !== "LIVE_QLOO_RANK_OBSERVATION_ONLY" ||
      status.release_authorized !== false ||
      status.provider_authenticated !== false
    ) return;

    const heading = provider.querySelector(".page-intro h1");
    const detail = provider.querySelector(".page-intro p");
    const eyebrow = provider.querySelector(".page-intro .eyebrow");
    const ribbon = document.querySelector(".status-label");
    const banner = provider.querySelector(".provider-banner");
    if (heading) heading.textContent = "Qloo rank inspection. Action still locked.";
    if (detail) detail.textContent =
      "A fixed artist and brand pool can be inspected server-side. " +
      "Rank evidence does not certify the entire tour or authorize an action.";
    if (eyebrow) eyebrow.textContent = "QLOO / LIVE RANK INSPECTION";
    if (ribbon) ribbon.textContent = "● QLOO RANK INSPECTION · RELEASE LOCKED";
    if (banner) {
      const name = banner.querySelector("strong");
      const description = banner.querySelector("p");
      if (name) name.textContent = "Controlled provider observation";
      if (description) description.textContent =
        "Only the operator can spend the reserved Qloo request. Visitors can review the redacted outcome.";
    }

    const old = provider.querySelector(":scope > .provider-steps");
    if (old) old.hidden = true;

    const panel = element("section");
    panel.className = "white-panel";
    panel.id = "hosted-live-inspection";
    panel.append(element("div", "mini-eyebrow", "SERVER-SIDE EVIDENCE / RANK ONLY"));
    panel.append(element("h2", "Inspect the frozen brand choice"));
    panel.append(element("p",
      "The artist and shortlist are preconfigured. Raw provider responses, " +
      "entity IDs, access credentials and execution privileges stay off this page."));

    const button = element("button", "Operator: use one Qloo request");
    button.type = "button";
    button.className = "primary";
    button.disabled = status.status !== "READY";
    const output = element("p", "No recorded rank is available yet.");
    output.setAttribute("role", "status");
    output.setAttribute("aria-live", "polite");
    panel.append(button, output);

    const destination = provider.querySelector(".provider-link");
    if (destination) provider.insertBefore(panel, destination);
    else provider.append(panel);

    if (status.status === "LIVE_RANK_OBSERVED") {
      try {
        const saved = await fetch("/api/qloo/result", {cache: "no-store"});
        if (!saved.ok) throw new Error("No saved rank");
        output.textContent = format(await saved.json());
      } catch {
        output.textContent = "Stored rank cannot be verified. No action is authorized.";
      }
    } else if (status.status !== "READY") {
      output.textContent =
        "The provider attempt is already reserved or unavailable. " +
        "No further request is permitted from this installation.";
    }

    button.addEventListener("click", async () => {
      if (button.disabled) return;
      const token = window.prompt(
        "Operator authorization token (never enter a Qloo API key):"
      );
      if (!token) return;
      if (!window.confirm(
        "Spend the one reserved server-side Qloo attempt? Failures can consume quota. " +
        "This does not verify or release the full plan."
      )) return;
      button.disabled = true;
      output.textContent = "Inspecting the fixed brand pool…";
      try {
        const result = await fetch("/api/qloo/live-rank", {
          method: "POST",
          credentials: "same-origin",
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
            "X-OFC-Operator-Token": token
          },
          body: JSON.stringify({confirm: "SPEND_ONE_HOSTED_QLOO_REQUEST"})
        });
        const data = await result.json();
        if (!result.ok) throw new Error("Provider unavailable");
        output.textContent = format(data);
      } catch {
        output.textContent =
          "No qualified rank available. If the request was reserved, retry is disabled.";
      }
    });
  }

  start().catch(() => {});
})();
