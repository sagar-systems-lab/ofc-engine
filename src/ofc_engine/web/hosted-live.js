(() => {
  "use strict";

  const provider = document.getElementById("view-provider");
  if (!provider) return;

  const element = (tag, value) => {
    const node = document.createElement(tag);
    if (value !== undefined) node.textContent = value;
    return node;
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
      "One server-side Qloo affinity request can inspect a fixed artist and brand pool. " +
      "It cannot certify the entire tour or authorize an external action.";
    if (eyebrow) eyebrow.textContent = "QLOO / LIVE RANK INSPECTION";
    if (ribbon) ribbon.textContent = "● QLOO REQUEST BY CONSENT · RELEASE LOCKED";
    if (banner) {
      const name = banner.querySelector("strong");
      const description = banner.querySelector("p");
      if (name) name.textContent = "One bounded provider observation";
      if (description) description.textContent =
        "No automatic calls. A live result remains a rank observation, not a verified plan.";
    }

    const old = provider.querySelector(":scope > .provider-steps");
    if (old) old.hidden = true;
    const panel = element("section");
    panel.className = "white-panel";
    panel.id = "hosted-live-inspection";
    panel.append(element("div", "mini-eyebrow", "EXPLICIT PROVIDER REQUEST"));
    panel.append(element("h2", "Check the frozen brand choice"));
    panel.append(element("p",
      "This check uses one preconfigured artist and brand shortlist. " +
      "No names, API credentials or raw provider responses are sent to your browser."));
    const action = element("button",
      status.status === "LIVE_RANK_OBSERVED"
        ? "View saved observation · no new request"
        : "Use one Qloo request to inspect rank");
    action.type = "button";
    action.className = "primary";
    if (status.status !== "READY" && status.status !== "LIVE_RANK_OBSERVED") {
      action.disabled = true;
    }
    const output = element("p", "No provider request has been made from this page.");
    output.setAttribute("role", "status");
    output.setAttribute("aria-live", "polite");
    panel.append(action, output);
    const destination = provider.querySelector(".provider-link");
    if (destination) provider.insertBefore(panel, destination);
    else provider.append(panel);

    action.addEventListener("click", async () => {
      if (action.disabled) return;
      const first = status.status !== "LIVE_RANK_OBSERVED";
      if (first && !window.confirm(
        "Use the one approved server-side Qloo request? This is a live provider call, " +
        "not verified execution. The attempt can consume quota even if it fails."
      )) return;
      action.disabled = true;
      output.textContent = "Checking the fixed brand pool…";
      try {
        const result = await fetch("/api/qloo/live-rank", {
          method: "POST",
          credentials: "same-origin",
          cache: "no-store",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({confirm: "SPEND_ONE_HOSTED_QLOO_REQUEST"})
        });
        const data = await result.json();
        if (
          !result.ok || data.status !== "LIVE_RANK_OBSERVED" ||
          data.release_authorized !== false ||
          data.provider_authenticated !== false
        ) throw new Error("The provider rank is unavailable. No action is authorized.");
        output.textContent =
          (data.cached ? "Previously observed" : "Newly observed") +
          " Qloo rank: original selection rank " + data.baseline_rank +
          " of " + data.candidate_count + ". " +
          (data.selected_candidate_is_top
            ? "Original choice ranks first."
            : "Another candidate ranks higher; repair suggested, not applied.") +
          " Independent verification and execution remain blocked.";
        action.textContent = "Observation already used";
      } catch {
        output.textContent =
          "Unable to qualify a Qloo rank. The attempt may have consumed quota; no retry is allowed.";
      }
    });
  }

  start().catch(() => {});
})();
