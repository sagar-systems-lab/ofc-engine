(() => {
  "use strict";

  const provider = document.getElementById("view-provider");
  if (!provider) return;

  const element = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  };

  async function show() {
    const response = await fetch("/api/local-run", {
      cache: "no-store",
      credentials: "same-origin"
    });
    if (!response.ok) throw new Error("Local recorded review unavailable");
    const state = await response.json();
    if (
      state.status !== "RECORDED_AGENT_REVIEW" ||
      state.source_mode !== "RECORDED_REDACTED" ||
      state.release_authorized !== false ||
      state.external_action_executed !== false
    ) throw new Error("Unqualified local review");

    const hypothetical = state.operator_constructed_unverified_decisions;
    if (
      !Number.isInteger(hypothetical) || hypothetical < 0 ||
      hypothetical > state.decision_count - 1 ||
      state.off_on?.same_baseline !== true ||
      !Array.isArray(state.decisions) ||
      state.decisions.length !== state.decision_count ||
      !Array.isArray(state.pending_rechecks) ||
      !Array.isArray(state.blockers) ||
      state.release_state !== "REVIEW_REQUIRED" ||
      state.provider_authenticated !== false ||
      state.independent_recheck_completed !== false
    ) throw new Error("Inconsistent recorded evidence state");

    const rank = state.recorded_rank_recheck_pass;
    const changed = state.changed_ids.length;
    const status = document.querySelector(".status-label");
    if (status) status.textContent = state.historical_replay
      ? "● RECORDED QLOO · HISTORICAL · RELEASE LOCKED"
      : "● RECORDED QLOO · REVIEW REQUIRED";
    const rail = document.querySelector(".rail-status");
    if (rail) {
      const name = rail.querySelector("strong");
      const detail = rail.querySelector("small");
      if (name) name.textContent = "Recorded agent evidence";
      if (detail) detail.textContent = state.historical_replay
        ? "Historical rank only · not live"
        : "Saved Qloo rank · review pending";
    }
    const eyebrow = provider.querySelector(".page-intro .eyebrow");
    if (eyebrow) eyebrow.textContent = state.historical_replay
      ? "RECORDED QLOO / HISTORICAL REVIEW"
      : "RECORDED QLOO / REVIEW REQUIRED";

    const stages = [...provider.querySelectorAll(":scope > .provider-steps > article")];
    if (stages.length !== 4) throw new Error("Missing decision stages");
    const updates = [
      ["FROZEN", "Agent choices and immutable candidate pools were recorded."],
      [rank ? "RANK PASS ONLY" : "RANK UNRESOLVED",
       "Saved Qloo affinity was assessed; independent provider verification is not established."],
      [changed === 1 ? "ONE CHANGE RECORDED" : "NO REPAIR",
       "Only the evidence-supported brand choice was changed; other choices remain unverified."],
      ["REVIEW REQUIRED",
       state.pending_rechecks.length + " pending decisions. No verified-plan handoff or external action."]
    ];
    stages.forEach((card, index) => {
      const description = card.querySelector("p");
      const badge = card.querySelector("b");
      if (description) description.textContent = updates[index][1];
      if (badge) badge.textContent = updates[index][0];
    });

    const intro = provider.querySelector(".page-intro");
    intro.querySelector("h1").textContent = "An evidenced repair. Execution still locked.";
    intro.querySelector("p").textContent =
      "Saved Qloo ranking was reviewed against a frozen agent plan. Recorded rank is not provider-certified cultural approval.";
    const banner = provider.querySelector(".provider-banner");
    banner.querySelector("strong").textContent = "Recorded evidence · Review required";
    banner.querySelector("p").textContent =
      "The current plan remains blocked while independent verification and required decisions are unresolved.";

    const sheet = element("section", "white-panel");
    sheet.id = "local-agent-run";
    sheet.setAttribute("aria-label", "Recorded agent decision review");
    sheet.append(element("div", "mini-eyebrow", "SAVED AGENT RUN / REDACTED EVIDENCE"));
    sheet.append(element("h2", "", state.decision_count + " decisions · revision " + state.revision));
    sheet.append(element("p", "", state.historical_replay
      ? "Historical point-in-time replay. Not a current live approval."
      : "Recorded Qloo evidence. No new provider call or action."));
    if (state.operator_constructed_unverified_decisions > 0) {
      sheet.append(element("p", "privacy-caption",
        state.operator_constructed_unverified_decisions +
        " additional tour choices are unverified operator hypotheses. " +
        "Only the original brand decision was imported from the saved agent proposal."));
    }
    sheet.append(element("p", "privacy-caption",
      "Qloo OFF / ON share the same frozen baseline: " +
      (state.off_on.same_baseline ? "yes" : "no") +
      ". Evidence-backed changed decisions: " + state.off_on.changed_ids.length +
      ". Recorded later ranking: " +
      (state.recorded_rank_recheck_pass ? "rank PASS only" : "not qualified") + "."));

    const rows = element("div", "provider-steps");
    for (const item of state.decisions) {
      const card = element("article");
      const unverified = hypothetical > 0 && item.decision_id !== "brand";
      card.append(element("span", "", item.changed ? "CHANGED" : unverified
        ? "UNCHANGED · UNVERIFIED" : item.preserved ? "UNCHANGED" : "REVIEW"));
      card.append(element("h3", "", item.decision_id.replaceAll("_", " ")));
      card.append(element("p", "",
        item.domain + " · " + item.verdict +
        (item.pending_recheck ? " · recheck pending" : "") +
        (item.reason_codes.length ? " · " + item.reason_codes.join(", ").replaceAll("_", " ").toLowerCase() : "")
      ));
      rows.append(card);
    }
    sheet.append(rows);
    sheet.append(element("p", "privacy-caption",
      "Why handoff is blocked: " +
      state.blockers.join(" · ").replaceAll("_", " ").toLowerCase()));
    sheet.append(element("p", "privacy-caption",
      "No raw Qloo response, candidate entity IDs, private credentials or execution permission are exposed here."));
    const syntheticLink = provider.querySelector(".provider-link");
    if (syntheticLink) {
      syntheticLink.textContent = "Explore the separate synthetic proof ↗";
      provider.insertBefore(sheet, syntheticLink);
    } else {
      provider.append(sheet);
    }
  }

  show().catch(() => {
    const intro = provider.querySelector(".page-intro p");
    if (intro) intro.textContent =
      "The local evidence review could not be validated. No action is authorized.";
  });
})();
