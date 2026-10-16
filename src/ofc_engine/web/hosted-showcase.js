(() => {
  "use strict";

  const steps = document.querySelector("#view-plan .state-strip");
  if (!steps) return;

  const card = document.createElement("section");
  card.className = "preflight-record";
  card.id = "published-qloo-review";
  card.setAttribute("aria-label", "Qloo-backed decision review");
  card.hidden = true;
  steps.insertAdjacentElement("afterend", card);

  const item = (tag, value) => {
    const el = document.createElement(tag);
    el.textContent = value;
    return el;
  };

  fetch("/api/showcase", {cache: "no-store"})
    .then(response => {
      if (!response.ok) throw new Error("Published review unavailable");
      return response.json();
    })
    .then(run => {
      if (
        run.status !== "OBSERVED_QLOO_REVIEW_REQUIRED" ||
        run.source_mode !== "CAPTURED_LIVE_QLOO_UNVERIFIED" ||
        run.release_authorized !== false ||
        run.external_action_executed !== false ||
        run.provider_authenticated !== false ||
        run.independent_recheck_completed !== false ||
        run.additional_provider_requests !== 0 ||
        run.handoff !== "WITHHELD" ||
        run.off_on?.same_baseline !== true ||
        run.off_on?.off_brand !== "UNCERTAIN" ||
        run.off_on?.on_brand !== "REPAIR" ||
        run.off_on?.real_causal_proof !== false ||
        run.changed_ids?.length !== 1 || run.changed_ids[0] !== "brand" ||
        !Array.isArray(run.decisions) || run.decisions.length !== 6 ||
        !Array.isArray(run.invalidated_ids) ||
        !Array.isArray(run.pending_rechecks) ||
        !Array.isArray(run.blockers) ||
        !/^[a-f0-9]{64}$/.test(run.evidence?.current_plan_digest || "") ||
        !/^[a-f0-9]{64}$/.test(run.evidence?.journal_head || "")
      ) throw new Error("Unqualified Qloo review");

      const badge = document.querySelector(".topbar .status-label");
      if (badge) badge.textContent = "●  QLOO RANK OBSERVED · REVIEW REQUIRED";

      const intro = document.querySelector("#view-plan > .intro p");
      if (intro) intro.textContent =
        "This published tour uses a previously acquired Qloo brand-affinity observation. " +
        "The editable draft below is separate and has no Qloo evidence.";

      const checkStep = steps.querySelector(".state-item:nth-child(2) small");
      if (checkStep) checkStep.textContent = "RANK OBSERVED";

      const handoffStep = steps.querySelector(".state-item:nth-child(3) small");
      if (handoffStep) handoffStep.textContent = "HANDOFF BLOCKED";

      const boundary = document.querySelector("#view-plan .boundary-panel");
      if (boundary) {
        const heading = boundary.querySelector("strong");
        const description = boundary.querySelector("p");
        if (heading) heading.textContent = "Qloo rank observed · independent verification pending";
        if (description) description.textContent =
          "The published tour repaired one brand choice. Unsupported decisions and " +
          "dependent rechecks remain blocked. The editable draft is separate; " +
          "no external action is authorized.";
      }

      const header = item("h2", "Qloo changed one decision. OFC kept the plan safe.");
      const context = item("p",
        "Previously acquired Qloo brand-affinity observation · " +
        (run.evidence.collected_at || "time unavailable") +
        " · Not a new API request by this visitor.");
      const comparison = item("p",
        "Same frozen baseline · Brand: UNCERTAIN (Qloo OFF) → REPAIR (Qloo ON). " +
        "The selected brand was changed within the approved candidate pool.");
      const decisions = document.createElement("ol");
      for (const decision of run.decisions) {
        const verdict = decision.decision_id === "brand" &&
          decision.verdict === "PASS" ? "RANK PASS" : decision.verdict;
        decisions.append(item("li", decision.decision_id + ": " +
          verdict + (decision.changed ? " · REPAIRED" : " · UNCHANGED") +
          (decision.pending_recheck ? " · RECHECK PENDING" : "")));
      }
      const boundary = item("p",
        "Dependent checks invalidated: " + run.invalidated_ids.join(", ") +
        ". Pending rechecks: " + run.pending_rechecks.join(", ") +
        ". Handoff WITHHELD. Independent provider verification is not established.");
      const provenance = item("p",
        "Current revision " + run.revision +
        " · Journal " + run.evidence.journal_head.slice(0, 12) +
        "… · No bookings, payments or external actions.");
      card.replaceChildren(header, context, comparison, decisions, boundary, provenance);
      card.hidden = false;
    })
    .catch(() => {
      // A missing or rejected publication must never turn into synthetic proof.
    });
})();
