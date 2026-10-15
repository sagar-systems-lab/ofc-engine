(() => {
  "use strict";

  const intro = document.querySelector("#view-plan > .intro");
  const panel = document.getElementById("preflight-panel");
  if (!intro || !panel) return;

  const actions = document.createElement("div");
  actions.className = "hosted-tour-actions";
  actions.style.display = "flex";
  actions.style.flexDirection = "column";
  actions.style.alignItems = "center";
  actions.style.gap = "6px";
  actions.style.marginBottom = "16px";
  intro.style.marginBottom = "10px";
  const start = document.createElement("button");
  start.id = "start-hosted-tour";
  start.type = "button";
  start.className = "primary";
  start.style.width = "min(100%, 320px)";
  start.textContent = "Start six-decision tour review →";
  const status = document.createElement("p");
  status.className = "small-note";
  status.style.margin = "0";
  status.style.textAlign = "center";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  actions.append(start, status);
  intro.insertAdjacentElement("afterend", actions);

  start.addEventListener("click", async () => {
    if (start.disabled) return;
    start.disabled = true;
    start.textContent = "Saving tour review…";
    try {
      const response = await fetch("/api/runs/tour", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({confirm: "SAVE_SIX_DECISION_TOUR"})
      });
      if (!response.ok) throw new Error("Tour review unavailable");
      const saved = await response.json();
      const review = saved.review;
      if (
        !/^web-[a-f0-9]{32}$/.test(saved.run_id) ||
        typeof saved.ticket !== "string" || saved.ticket.length < 32 ||
        review?.run_id !== saved.run_id ||
        review.revision !== 0 ||
        review.source_mode !== "EVIDENCE_UNAVAILABLE" ||
        review.release_authorized !== false ||
        review.external_action_executed !== false ||
        review.real_qloo_requests !== 0 ||
        !/^[a-f0-9]{64}$/.test(review.plan_digest) ||
        !Array.isArray(review.decisions) ||
        review.decisions.length !== 6 ||
        review.decisions.some(d => d.verdict !== "UNCERTAIN")
      ) throw new Error("Unqualified tour review");

      sessionStorage.setItem("ofc-last-review", JSON.stringify({
        run_id: saved.run_id, ticket: saved.ticket
      }));
      panel.hidden = false;
      document.getElementById("preflight-state").textContent =
        "SIX-DECISION TOUR SAVED · REVIEW REQUIRED";
      document.getElementById("preflight-description").textContent =
        "Server-frozen tour " + saved.run_id +
        " is saved for 24 hours. No Qloo request was made. " +
        "Provider evidence, independent verification and action release remain blocked.";
      const list = document.getElementById("preflight-decisions");
      list.replaceChildren();
      for (const decision of review.decisions) {
        const row = document.createElement("li");
        const name = document.createElement("strong");
        const verdict = document.createElement("span");
        row.className = "preflight-record";
        name.textContent = decision.decision_id;
        verdict.textContent = "UNCERTAIN · EVIDENCE UNAVAILABLE";
        row.append(name, verdict);
        list.append(row);
      }
      location.hash = "#plan";
      panel.scrollIntoView({block: "nearest", behavior: "auto"});
      start.textContent = "Tour saved · review required";
      status.textContent = "Six decisions saved. Provider evidence and handoff remain blocked.";
    } catch {
      start.disabled = false;
      start.textContent = "Retry six-decision tour review";
      status.textContent =
        "Tour save unavailable. No provider request or external action occurred.";
    }
  });
})();
