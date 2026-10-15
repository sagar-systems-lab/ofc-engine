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
    sheet.append(element("p", "privacy-caption",
      "Qloo OFF / ON share the same frozen baseline: " +
      (state.off_on.same_baseline ? "yes" : "no") +
      ". Evidence-backed changed decisions: " + state.off_on.changed_ids.length +
      ". Recorded later ranking: " +
      (state.recorded_rank_recheck_pass ? "rank PASS only" : "not qualified") + "."));

    const rows = element("div", "provider-steps");
    for (const item of state.decisions) {
      const card = element("article");
      card.append(element("span", "", item.changed ? "CHANGED" : item.preserved ? "PRESERVED" : "REVIEW"));
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
    provider.append(sheet);
  }

  show().catch(() => {
    const intro = provider.querySelector(".page-intro p");
    if (intro) intro.textContent =
      "The local evidence review could not be validated. No action is authorized.";
  });
})();
