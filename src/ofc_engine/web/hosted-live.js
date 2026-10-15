(() => {
  "use strict";

  const provider = document.getElementById("view-provider");
  if (!provider) return;

  const node = (tag, value, className) => {
    const item = document.createElement(tag);
    if (value !== undefined) item.textContent = value;
    if (className) item.className = className;
    return item;
  };

  function showRun(run, panel) {
    if (
      run.status !== "HOSTED_AGENT_REVIEW" ||
      run.source_mode !== "LIVE_HTTP_UNVERIFIED" ||
      run.release_authorized !== false ||
      run.provider_authenticated !== false ||
      run.independent_recheck_completed !== false ||
      run.verification_status !== "REVIEW_REQUIRED" ||
      !Array.isArray(run.decisions) ||
      run.decision_count !== run.decisions.length ||
      run.off_on?.same_baseline !== true ||
      !run.provenance || !run.handoff ||
      run.handoff.status !== "WITHHELD" ||
      run.handoff.reviewed_revision !== run.revision ||
      run.handoff.reviewed_plan_digest !== run.provenance.current_plan_digest ||
      run.handoff.journal_head !== run.provenance.journal_head ||
      run.handoff.external_action_executed !== false ||
      !Array.isArray(run.handoff.blockers) ||
      !Array.isArray(run.handoff.pending_rechecks) ||
      !/^[a-f0-9]{64}$/.test(run.provenance.baseline_plan_digest) ||
      !/^[a-f0-9]{64}$/.test(run.provenance.current_plan_digest) ||
      !/^[a-f0-9]{64}$/.test(run.provenance.journal_head) ||
      run.provenance.qloo_rank_observation?.source_mode !== "LIVE_HTTP_UNVERIFIED" ||
      run.provenance.qloo_rank_observation?.provider_attestation !== "NOT_ESTABLISHED" ||
      !/^[a-f0-9]{64}$/.test(run.provenance.qloo_rank_observation?.capture_digest || "")
    ) throw new Error("Unqualified agent run");

    const stages = [...provider.querySelectorAll(":scope > .provider-steps > article")];
    if (stages.length === 4) {
      const data = [
        ["FROZEN", "Original agent choices and constraints were locked before Qloo evaluation."],
        ["QLOO OBSERVED", "Live HTTP rank was scoped to one frozen artist and brand shortlist."],
        [run.changed_ids.length ? "ONE CHANGE" : "UNCHANGED",
          "Only the evidenced brand choice can be repaired. Peers remain unverified."],
        ["REVIEW REQUIRED",
          run.pending_rechecks.length + " pending rechecks; no verified-plan handoff."]
      ];
      stages.forEach((stage, i) => {
        const p = stage.querySelector("p");
        const badge = stage.querySelector("b");
        if (p) p.textContent = data[i][1];
        if (badge) badge.textContent = data[i][0];
      });
    }
    const rail = document.querySelector(".status-label");
    if (rail) rail.textContent = "● QLOO OBSERVED · REVIEW REQUIRED";

    const sheet = node("section", undefined, "white-panel");
    sheet.id = "hosted-agent-decision";
    sheet.append(node("div", "QLOO-GROUNDED AGENT REVIEW", "mini-eyebrow"));
    sheet.append(node("h2", run.decision_count + " decisions · revision " + run.revision));
    sheet.append(node("p",
      "The original choice and shortlist were frozen before the server-side Qloo request. " +
      "The ranking affected the brand decision only; the full tour remains unverified."));
    sheet.append(node("p",
      "Qloo OFF / ON: same baseline · " + run.off_on.changed_ids.length +
      " evidenced change. Provider authentication and independent recheck remain unproven.",
      "privacy-caption"));

    const decisions = node("div", undefined, "provider-steps");
    for (const d of run.decisions) {
      const card = node("article");
      card.append(node("span", d.changed ? "REPAIRED" :
        d.pending_recheck ? "RECHECK PENDING" : "UNCHANGED · UNVERIFIED"));
      card.append(node("h3", d.decision_id.replaceAll("_", " ")));
      card.append(node("p", d.domain + " · " + d.verdict +
        (d.reason_codes.length
          ? " · " + d.reason_codes.join(", ").replaceAll("_", " ").toLowerCase()
          : "")));
      decisions.append(card);
    }
    sheet.append(decisions);
    const proof = run.provenance;
    sheet.append(node("p",
      "Revision " + proof.reviewed_revision + " · policy " + proof.policy_version +
      " · journal " + proof.journal_head.slice(0, 12) + "… · capture " +
      proof.qloo_rank_observation.capture_digest.slice(0, 12) + "…",
      "privacy-caption"));
    sheet.append(node("p",
      "Handoff withheld: " + run.handoff.blockers.join(" · ").replaceAll("_", " ").toLowerCase(),
      "privacy-caption"));
    sheet.append(node("p",
      "No booking, payment, raw entity identifiers, or action permission is supplied.",
      "privacy-caption"));
    panel.append(sheet);
  }

  async function start() {
    const statusResponse = await fetch("/api/qloo/status", {cache: "no-store"});
    if (!statusResponse.ok) return;
    const state = await statusResponse.json();
    if (
      state.capability !== "LIVE_QLOO_RANK_OBSERVATION_ONLY" ||
      state.release_authorized !== false ||
      state.provider_authenticated !== false
    ) return;

    const heading = provider.querySelector(".page-intro h1");
    const detail = provider.querySelector(".page-intro p");
    const eyebrow = provider.querySelector(".page-intro .eyebrow");
    const banner = provider.querySelector(".provider-banner");
    if (heading) heading.textContent = "One agent plan. One evidenced correction.";
    if (detail) detail.textContent =
      "OFC checks a frozen tour plan with server-side Qloo ranking, repairs only supported " +
      "choices and withholds the action while required checks remain open.";
    if (eyebrow) eyebrow.textContent = "QLOO / AGENT DECISION RUN";
    if (banner) {
      const title = banner.querySelector("strong");
      const p = banner.querySelector("p");
      if (title) title.textContent = "Real evidence · controlled execution";
      if (p) p.textContent =
        "Provider acquisition is operator-controlled. Review the stored, redacted plan below.";
    }

    const stages = provider.querySelector(":scope > .provider-steps");
    if (stages) stages.hidden = true;
    const panel = node("section", undefined, "white-panel");
    panel.id = "hosted-live-inspection";
    panel.append(node("div", "HOSTED AGENT EVIDENCE", "mini-eyebrow"));
    panel.append(node("h2", "Review the pre-execution decision"));
    const message = node("p",
      "No Qloo-backed agent decision has been published for this installation. " +
      "Provider requests cannot be spent by visitors.");
    message.setAttribute("role", "status");
    message.setAttribute("aria-live", "polite");
    panel.append(message);
    const link = provider.querySelector(".provider-link");
    if (link) provider.insertBefore(panel, link);
    else provider.append(panel);

    if (state.status !== "LIVE_RANK_OBSERVED") return;
    try {
      const response = await fetch("/api/qloo/result", {cache: "no-store"});
      if (!response.ok) throw new Error("No stored agent result");
      const report = await response.json();
      if (!report.agent_run) throw new Error("No qualified agent projection");
      message.textContent = report.cached
        ? "Saved Qloo observation · no additional request."
        : "Qloo observation received.";
      if (stages) stages.hidden = false;
      showRun(report.agent_run, panel);
    } catch {
      message.textContent =
        "The stored observation cannot qualify a current agent run. Execution remains blocked.";
    }
  }

  start().catch(() => {});
})();
