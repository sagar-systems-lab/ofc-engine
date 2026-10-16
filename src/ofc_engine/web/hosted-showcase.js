(() => {
  "use strict";

  const intro = document.querySelector("#view-plan > .intro");
  const steps = document.querySelector("#view-plan .state-strip");
  if (!intro || !steps) return;

  const card = document.createElement("section");
  card.id = "published-qloo-review";
  card.className = "editor-panel";
  card.hidden = true;
  card.style.marginBottom = "18px";
  card.style.padding = "22px";
  card.setAttribute("aria-label", "Qloo-backed tour walkthrough");
  steps.insertAdjacentElement("beforebegin", card);

  const element = (tag, value, cls) => {
    const node = document.createElement(tag);
    node.textContent = value;
    if (cls) node.className = cls;
    return node;
  };

  function valid(proof) {
    const tour = proof?.tour;
    const decisions = tour?.decisions;
    return (
      proof?.journey_version === 1 &&
      proof.status === "OBSERVED_QLOO_REVIEW_REQUIRED" &&
      proof.source_mode === "CAPTURED_LIVE_QLOO_UNVERIFIED" &&
      proof.revision === 1 &&
      proof.decision_count === 6 &&
      proof.release_authorized === false &&
      proof.external_action_executed === false &&
      proof.provider_authenticated === false &&
      proof.independent_recheck_completed === false &&
      proof.additional_provider_requests === 0 &&
      proof.handoff === "WITHHELD" &&
      proof.off_on?.same_baseline === true &&
      proof.off_on?.off_brand === "UNCERTAIN" &&
      proof.off_on?.on_brand === "REPAIR" &&
      proof.off_on?.real_causal_proof === false &&
      Array.isArray(proof.changed_ids) &&
      proof.changed_ids.length === 1 &&
      proof.changed_ids[0] === "brand" &&
      Array.isArray(proof.invalidated_ids) &&
      proof.invalidated_ids.includes("brand") &&
      proof.invalidated_ids.includes("afterparty") &&
      Array.isArray(proof.pending_rechecks) &&
      proof.pending_rechecks.includes("brand") &&
      proof.pending_rechecks.includes("afterparty") &&
      Array.isArray(proof.blockers) &&
      tour?.budget_usd === 60000 &&
      JSON.stringify(tour.cities) === JSON.stringify(["Chicago", "Austin", "Seattle"]) &&
      tour.audience === "Indie music audiences" &&
      tour.operator_constructed_unverified_decisions === 5 &&
      Array.isArray(decisions) &&
      decisions.length === 6 &&
      decisions.map(d => d.decision_id).join(",") ===
        "brand,opener,venue-1,venue-2,venue-3,afterparty" &&
      decisions.every(d =>
        Number.isInteger(d.candidate_count) && d.candidate_count >= 1 &&
        Number.isInteger(d.baseline_option) &&
        Number.isInteger(d.reviewed_option) &&
        d.baseline_option >= 1 && d.baseline_option <= d.candidate_count &&
        d.reviewed_option >= 1 && d.reviewed_option <= d.candidate_count &&
        d.changed === (d.baseline_option !== d.reviewed_option) &&
        d.pending_recheck === proof.pending_rechecks.includes(d.decision_id)) &&
      decisions[0].changed === true &&
      decisions.slice(1).every(d => d.changed === false) &&
      /^[a-f0-9]{64}$/.test(proof.evidence?.baseline_plan_digest || "") &&
      /^[a-f0-9]{64}$/.test(proof.evidence?.current_plan_digest || "") &&
      /^[a-f0-9]{64}$/.test(proof.evidence?.journal_head || "") &&
      /^[a-f0-9]{64}$/.test(proof.evidence?.query_fingerprint || "") &&
      /^[a-f0-9]{64}$/.test(proof.evidence?.capture_digest || "")
    );
  }

  const labels = {
    brand: "Brand partner",
    opener: "Opening act",
    "venue-1": "Chicago venue",
    "venue-2": "Austin venue",
    "venue-3": "Seattle venue",
    afterparty: "Afterparty"
  };
  const stages = [
    "01 / PROPOSE", "02 / QLOO CHECK", "03 / REPAIR",
    "04 / VERIFY", "05 / HANDOFF"
  ];

  function addList(target, lines) {
    const list = document.createElement("ul");
    for (const line of lines) list.append(element("li", line));
    target.append(list);
  }

  function load(proof) {
    const tour = proof.tour;
    const brand = tour.decisions[0];

    const actions = document.querySelector(".hosted-tour-actions");
    if (actions) actions.style.display = "none";

    const topStatus = document.querySelector(".topbar .status-label");
    if (topStatus) topStatus.textContent = "●  QLOO RANK OBSERVED · REVIEW REQUIRED";
    const description = intro.querySelector("p");
    if (description) description.textContent =
      "Review a previously acquired Qloo-backed tour. " +
      "The separate editable draft below does not inherit this evidence.";

    const checkStep = steps.querySelector(".state-item:nth-child(2) small");
    if (checkStep) checkStep.textContent = "RANK OBSERVED";
    const releaseStep = steps.querySelector(".state-item:nth-child(3) small");
    if (releaseStep) releaseStep.textContent = "HANDOFF BLOCKED";
    const boundary = document.querySelector("#view-plan .boundary-panel");
    if (boundary) {
      const heading = boundary.querySelector("strong");
      const copy = boundary.querySelector("p");
      if (heading) heading.textContent = "Rank observed · independent verification pending";
      if (copy) copy.textContent =
        "This published run repaired a brand choice. The editable draft is independent. " +
        "Unsupported choices, dependent rechecks and external action remain blocked.";
    }

    const launchRow = document.createElement("div");
    launchRow.className = "hosted-tour-actions";
    launchRow.style.display = "flex";
    launchRow.style.flexDirection = "column";
    launchRow.style.alignItems = "center";
    launchRow.style.gap = "6px";
    launchRow.style.marginBottom = "16px";
    const launch = element("button", "Review the Qloo-backed tour →", "primary");
    launch.type = "button";
    launch.id = "start-qloo-journey";
    launch.style.width = "min(100%, 320px)";
    const hint = element("p",
      "Five steps · frozen original tour · previously acquired genuine Qloo rank · no new API request",
      "small-note");
    hint.style.margin = "0";
    hint.style.textAlign = "center";
    launchRow.append(launch, hint);
    intro.insertAdjacentElement("afterend", launchRow);

    const heading = element("h2", "", "");
    heading.id = "qloo-journey-heading";
    heading.tabIndex = -1;
    const progress = element("p", "", "small-note");
    progress.id = "qloo-journey-progress";
    progress.setAttribute("aria-live", "polite");
    const body = document.createElement("div");
    body.id = "qloo-journey-body";
    body.setAttribute("aria-live", "polite");
    const navigation = document.createElement("div");
    navigation.className = "editor-actions";
    navigation.style.display = "flex";
    navigation.style.flexWrap = "wrap";
    navigation.style.alignItems = "center";
    navigation.style.gap = "10px";
    const previous = element("button", "← Previous", "export-button");
    previous.type = "button";
    previous.id = "qloo-journey-previous";
    const next = element("button", "Next →", "primary");
    next.type = "button";
    next.id = "qloo-journey-next";
    navigation.append(previous, next);
    card.append(progress, heading, body, navigation);

    const put = (message) => body.append(element("p", message));
    let position = 0;
    function show(index) {
      position = index;
      card.hidden = false;
      progress.textContent = "STEP " + (position + 1) + " / 5 · " +
        "READ-ONLY REAL-EVIDENCE WALKTHROUGH";
      heading.textContent = stages[position];
      body.replaceChildren();

      if (position === 0) {
        put(tour.objective);
        put("Frozen before Qloo · user-specified budget $" +
          tour.budget_usd.toLocaleString("en-US") +
          " · " + tour.cities.join(" / ") + " · audience: " + tour.audience + ".");
        put("Original plan: one artist-to-brand shortlist and five " +
          "operator-constructed, unverified choices. Option numbers are redacted " +
          "positions in frozen candidate pools, not invented brand names.");
        addList(body, tour.decisions.map(d =>
          labels[d.decision_id] + ": option " + d.baseline_option +
          " of " + d.candidate_count + " · " +
          (d.required ? "required" : "optional") +
          (d.depends_on.length ? " · depends on " + d.depends_on.join(", ") : "")
        ));
      } else if (position === 1) {
        put("One actual Qloo artist-to-brand affinity rank was acquired earlier for " +
          "the frozen shortlist. The current visitor makes zero provider requests.");
        put("Same baseline · Qloo OFF: " + proof.off_on.off_brand +
          " → Qloo ON: " + proof.off_on.on_brand +
          ". This is an evidence-conditioned decision difference, not a controlled " +
          "business-outcome experiment or independent provider attestation.");
        put("Observation: " + (proof.evidence.collected_at || "not available") +
          " · Query fingerprint: " + proof.evidence.query_fingerprint.slice(0, 16) +
          "… · Capture digest: " + proof.evidence.capture_digest.slice(0, 16) + "…");
        put("The rank only addresses the brand pool; it does not verify venues, " +
          "opening acts, ticket demand, contract suitability or the $60,000 budget.");
      } else if (position === 2) {
        put("One bounded, allowlisted candidate-only repair was committed:");
        addList(body, [
          "Brand partner: option " + brand.baseline_option + " of " +
          brand.candidate_count + " → option " + brand.reviewed_option +
          " of " + brand.candidate_count + " · REPAIRED",
          "Independent choices preserved: " +
            proof.preserved_ids.map(id => labels[id] || id).join(", "),
          "Affected decisions invalidated: " +
            proof.invalidated_ids.map(id => labels[id] || id).join(", "),
          "Revision 0 → " + proof.revision +
          "; the original constraints and candidate pools remain frozen."
        ]);
        put("The corrected brand has not been independently rechecked; " +
          "its dependent afterparty cannot inherit a PASS.");
      } else if (position === 3) {
        put("The revised plan is inspected at revision " + proof.revision +
          ". Rank-only evidence does not clear full-plan verification.");
        addList(body, tour.decisions.map(d =>
          labels[d.decision_id] + ": " +
          (d.decision_id === "brand" && d.verdict === "PASS" ?
            "RANK PASS" : d.verdict) +
          (d.changed ? " · REPAIRED" : " · UNCHANGED") +
          (d.pending_recheck ? " · RECHECK PENDING" : "")
        ));
        put("Pending: " + proof.pending_rechecks.join(", ") +
          " · Provider authenticated: NO · Independent recheck: NOT ESTABLISHED.");
        put("Original digest: " + proof.evidence.baseline_plan_digest.slice(0, 16) +
          "… · Current digest: " + proof.evidence.current_plan_digest.slice(0, 16) +
          "… · Journal head: " + proof.evidence.journal_head.slice(0, 16) + "…");
      } else {
        put("HANDOFF WITHHELD · REVIEW REQUIRED");
        put("The stored tour cannot be executed or released because required " +
          "choices remain uncertain and current-revision independent checks " +
          "are incomplete. Safety means refusing unsupported permission.");
        addList(body, proof.blockers);
        put("Released plan: NO · Booking/payment: NONE · External action: NONE.");
        put("Qloo rank was genuinely observed once. This published walkthrough " +
          "reuses that evidence and never spends provider quota.");
      }

      previous.disabled = position === 0;
      next.textContent = position === 4 ? "Review again ↻" : "Next: " +
        stages[position + 1].slice(5) + " →";
    }

    launch.addEventListener("click", () => {
      show(0);
      heading.focus({preventScroll: true});
      card.scrollIntoView({block: "start", behavior: "auto"});
    });
    previous.addEventListener("click", () => {
      show(position - 1);
      heading.focus({preventScroll: true});
    });
    next.addEventListener("click", () => {
      show(position === 4 ? 0 : position + 1);
      heading.focus({preventScroll: true});
    });
  }

  fetch("/api/showcase", {cache: "no-store"})
    .then(response => {
      if (!response.ok) throw new Error("Published review unavailable");
      return response.json();
    })
    .then(proof => {
      if (!valid(proof)) throw new Error("Unqualified Qloo tour");
      load(proof);
    })
    .catch(() => {
      // Missing or mismatched evidence never activates a guided live-proof journey.
    });
})();
