"use strict";

(() => {
  const byId = id => document.getElementById(id);
  const element = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const word = value => String(value || "").replaceAll("_", " ").toLowerCase();
  const short = value => String(value || "").slice(0, 180);
  const stageNames = ["Agent plan", "Qloo evidence", "OFC review", "Results"];

  const statusFor = (run, key) => {
    if (!run) return ["idle", "Not started"];
    if (run.local_pending) return ["pending", "Awaiting saved server status"];
    const activity = run.activity;
    if (!activity || activity.source !== "PERSISTED_ATTEMPTS")
      return ["unknown", "No persisted stage projection"];
    if (key === 0) {
      if (activity.agent_plan === "OBSERVED") return ["observed", "Typed plan saved"];
      if (activity.agent_plan === "ADMITTED") return ["pending", "Request admitted; draft unconfirmed"];
      return ["unknown", "Typed plan not observed"];
    }
    if (key === 1) {
      const evidence = activity.qloo_evidence;
      const requests = activity.requests || {};
      const count = Number.isInteger(requests.search_complete) ? requests.search_complete : 0;
      if (evidence === "RANK_OBSERVED") return ["observed", "Saved rank observations present"];
      if (evidence === "PARTIAL") return ["partial", "Some ranks saved; more still reserved"];
      if (evidence === "RANK_PENDING_OR_INTERRUPTED")
        return ["partial", "Search completed; rank attempt unresolved"];
      if (evidence === "SEARCH_ONLY") return ["partial", count + " entity search(es) saved; no rank"];
      if (evidence === "SEARCH_PENDING_OR_INTERRUPTED")
        return ["pending", "Entity search reserved, not completed"];
      return ["unknown", "No Qloo observation recorded"];
    }
    if (key === 2) {
      if (activity.ofc_review === "OBSERVED") return ["observed", "Review saved · further checks required"];
      return ["unknown", "No saved OFC review"];
    }
    if (activity.results === "REVIEW_REQUIRED") return ["observed", "Saved · review required"];
    if (activity.results === "CLARIFICATION_REQUIRED") return ["partial", "Saved · needs clarification"];
    if (activity.results === "CAPABILITY_UNAVAILABLE") return ["partial", "Saved · outside qualified scope"];
    if (activity.results === "BLOCKED") return ["blocked", "Saved · blocked"];
    if (activity.results === "PROCESSING") return ["pending", "Processing · not yet saved"];
    return ["unknown", "Status unavailable"];
  };

  const line = (title, value) => {
    const row = element("div", "plan-line");
    row.append(element("span", "", title), element("strong", "", value));
    return row;
  };

  const choicesFor = run => {
    if (!run || !run.plan || !Array.isArray(run.plan.choices)) return [];
    return run.plan.choices.filter(x => x && typeof x === "object" &&
      typeof x.decision_id === "string").slice(0, 6);
  };

  const renderPlan = run => {
    const section = byId("activity-detail"), target = byId("plan-content");
    const plan = run?.plan;
    const choices = choicesFor(run);
    section.hidden = !(plan && Array.isArray(plan.choices));
    target.replaceChildren();
    if (section.hidden) return;
    if (plan.objective) target.append(element("p", "plan-objective", short(plan.objective)));
    if (plan.artist_name) target.append(line("Taste anchor", short(plan.artist_name)));
    if (Number.isFinite(plan.budget_usd)) target.append(line("Budget", "$" + plan.budget_usd.toLocaleString("en-US")));
    if (Array.isArray(plan.cities) && plan.cities.length)
      target.append(line("Cities", plan.cities.slice(0, 6).map(short).join(", ")));
    target.append(element("p", "plan-subtitle", choices.length + " proposed decision(s)"));
    for (const choice of choices) {
      const card = element("article", "plan-choice");
      card.append(element("span", "plan-choice-domain", short(choice.domain || "Decision")));
      card.append(element("strong", "", short(choice.selected_name || "Unspecified")));
      if (Array.isArray(choice.candidate_names) && choice.candidate_names.length > 1)
        card.append(element("small", "", "Candidates: " + choice.candidate_names.slice(0, 5).map(short).join(" · ")));
      card.append(element("small", "plan-unverified", "Agent proposal · not independently verified"));
      target.append(card);
    }
    if (!choices.length) target.append(element("p", "activity-minor", "No decision choices saved."));
  };

  const renderMap = run => {
    const section = byId("map-detail"), target = byId("map-content");
    const choices = choicesFor(run);
    section.hidden = !choices.length;
    target.replaceChildren();
    if (!choices.length) return;
    const known = new Map(choices.map(choice => [choice.decision_id, choice]));
    target.append(element("p", "activity-minor", "Dependencies from the saved agent plan; arrows indicate required prior decisions."));
    for (const choice of choices) {
      const card = element("article", "map-node");
      const dependencyIds = Array.isArray(choice.depends_on)
        ? [...new Set(choice.depends_on.filter(id => typeof id === "string"))].slice(0, 6) : [];
      if (dependencyIds.length) {
        const dependencies = element("div", "map-parents");
        dependencies.append(element("span", "map-link-label", "Depends on"));
        for (const id of dependencyIds) {
          const parent = known.get(id);
          dependencies.append(element("span", "map-parent", short(parent?.selected_name || id)));
        }
        card.append(dependencies);
        card.append(element("span", "map-edge", "↓"));
      } else {
        card.append(element("span", "map-origin", "Independent decision"));
      }
      const main = element("div", "map-node-main");
      main.append(element("span", "map-node-id", short(choice.decision_id)),
        element("strong", "", short(choice.selected_name || "Unspecified")));
      const on = run.on?.decisions;
      const checked = Array.isArray(on) ? on.find(item => item.decision_id === choice.decision_id) : null;
      if (checked?.changed === true) main.append(element("small", "map-change", "Revised by OFC · review required"));
      else if (checked?.ofc_verdict === "UNCERTAIN")
        main.append(element("small", "map-uncertain", "Review uncertain"));
      else main.append(element("small", "map-unknown", "Change not established"));
      card.append(main);
      target.append(card);
    }
  };

  const render = (run) => {
    const container = byId("activity-steps");
    if (!container) return;
    const rows = [...container.querySelectorAll("li")];
    rows.forEach((row, i) => {
      const [state, message] = statusFor(run, i);
      row.dataset.stage = state;
      row.querySelector(".step-marker").textContent = state === "observed" ? "✓" : String(i + 1);
      row.querySelector("strong").textContent = stageNames[i];
      row.querySelector("small").textContent = message;
    });
    const note = byId("activity-honesty");
    if (!run) note.textContent = "No selected run. Activity is shown only from persisted server evidence.";
    else if (run.local_pending) note.textContent = "Awaiting admission. No provider activity is asserted yet.";
    else if (!run.activity) note.textContent = "Stage ledger is unavailable; no step is presumed complete.";
    else {
      const q = run.activity.requests || {};
      const completed = Number.isInteger(q.rank_complete) ? q.rank_complete : 0;
      const reserved = Number.isInteger(q.rank_reserved) ? q.rank_reserved : 0;
      note.textContent = "Saved activity · rank completed " + completed + ", unresolved " + reserved +
        ". Request reservations are not proof of valid Qloo evidence.";
    }
    renderPlan(run);
    renderMap(run);
  };

  const mount = () => {
    for (const [buttonId, sectionId] of [
      ["plan-expand", "plan-content"], ["map-expand", "map-content"],
    ]) {
      const button = byId(buttonId), panel = byId(sectionId);
      if (!button || !panel) continue;
      button.addEventListener("click", () => {
        panel.hidden = !panel.hidden;
        button.textContent = panel.hidden ? "Show" : "Hide";
        button.setAttribute("aria-expanded", String(!panel.hidden));
      });
    }
  };

  window.OFCActivity = Object.freeze({render, mount});
})();
