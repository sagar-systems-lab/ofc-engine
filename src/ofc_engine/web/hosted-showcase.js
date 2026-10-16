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
    body.style.overflowWrap = "anywhere";
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
    const stage = element("div", "", "qloo-review-stage");
    stage.style.minWidth = "0";
    const stageTop = element("div", "", "section-head");
    stageTop.style.alignItems = "center";
    stageTop.style.marginBottom = "12px";
    const copilotToggle = element("button", "Hide Copilot", "secondary small");
    copilotToggle.id = "toggle-qloo-copilot";
    copilotToggle.type = "button";
    copilotToggle.setAttribute("aria-controls", "qloo-copilot");
    copilotToggle.setAttribute("aria-expanded", "true");
    stageTop.append(progress, copilotToggle);
    stage.append(stageTop, heading, body, navigation);

    const copilot = element("aside", "", "summary-panel");
    copilot.id = "qloo-copilot";
    copilot.setAttribute("aria-label", "OFC Copilot: recorded Qloo evidence comparison");
    copilot.style.minWidth = "0";
    copilot.style.padding = "20px";
    copilot.style.border = "1px solid #cbd9ef";
    copilot.style.borderRadius = "16px";
    copilot.style.boxShadow = "0 12px 32px #162d4c0b";
    copilot.style.background = "linear-gradient(160deg,#f8fbff,#fff 44%)";
    copilot.style.overflowWrap = "anywhere";

    const copilotTop = element("div");
    copilotTop.style.display = "flex";
    copilotTop.style.alignItems = "center";
    copilotTop.style.gap = "10px";
    copilotTop.style.marginBottom = "7px";
    const mark = element("span", "◇");
    mark.setAttribute("aria-hidden", "true");
    Object.assign(mark.style, {
      display: "grid", placeItems: "center", width: "35px", height: "35px",
      flex: "none", borderRadius: "11px", background: "#172642",
      color: "#fff", fontSize: "22px"
    });
    const identity = element("div");
    const assistantTitle = element("strong", "OFC Copilot");
    Object.assign(assistantTitle.style, {
      display: "block", fontSize: "15px", color: "#142442", letterSpacing: "-.02em"
    });
    const assistantRole = element("small", "Decision explanation · read-only");
    Object.assign(assistantRole.style, {
      display: "block", color: "#637796", fontSize: "10px", marginTop: "3px"
    });
    identity.append(assistantTitle, assistantRole);
    copilotTop.append(mark, identity);

    const recordLabel = element("p",
      "One frozen tour · compare saved evidence OFF / ON");
    Object.assign(recordLabel.style, {
      margin: "0 0 13px", fontSize: "11px", lineHeight: "1.5",
      color: "#657894"
    });

    const switcher = element("div");
    switcher.setAttribute("role", "group");
    switcher.setAttribute("aria-label", "Qloo evidence for the same frozen baseline");
    Object.assign(switcher.style, {
      display: "grid", gridTemplateColumns: "1fr 1fr", gap: "5px",
      padding: "5px", background: "#eaf0f8", borderRadius: "12px",
      marginBottom: "14px"
    });
    const off = element("button", "Qloo OFF");
    const on = element("button", "Qloo ON");
    off.id = "qloo-copilot-off";
    on.id = "qloo-copilot-on";
    for (const button of [off, on]) {
      button.type = "button";
      Object.assign(button.style, {
        minHeight: "38px", minWidth: "0", border: "1px solid transparent",
        borderRadius: "8px", fontWeight: "750", fontSize: "12px",
        padding: "8px", transition: "background .15s, box-shadow .15s"
      });
      switcher.append(button);
    }

    const decisionStatus = element("div");
    decisionStatus.id = "qloo-copilot-status";
    decisionStatus.setAttribute("role", "status");
    Object.assign(decisionStatus.style, {
      borderRadius: "11px", border: "1px solid #d8e3f2",
      padding: "13px", marginBottom: "13px"
    });
    const statusEyebrow = element("div", "BRAND DECISION");
    Object.assign(statusEyebrow.style, {
      fontSize: "10px", letterSpacing: ".09em", fontWeight: "800",
      marginBottom: "6px"
    });
    const statusValue = element("strong", "", "");
    statusValue.id = "qloo-copilot-decision";
    Object.assign(statusValue.style, {
      display: "block", fontSize: "24px", letterSpacing: "-.05em",
      lineHeight: "1.15", marginBottom: "5px"
    });
    const statusChoice = element("small");
    statusChoice.id = "qloo-copilot-choice";
    Object.assign(statusChoice.style, {
      display: "block", fontSize: "11px", lineHeight: "1.5"
    });
    decisionStatus.append(statusEyebrow, statusValue, statusChoice);

    const thread = element("div");
    thread.id = "qloo-copilot-thread";
    thread.setAttribute("aria-live", "polite");
    thread.setAttribute("aria-atomic", "true");

    const safety = element("p");
    safety.id = "qloo-copilot-safety";
    Object.assign(safety.style, {
      fontSize: "11px", lineHeight: "1.6", color: "#624c28",
      background: "#fff7e9", border: "1px solid #f3dfbd",
      padding: "11px 12px", borderRadius: "10px",
      margin: "11px 0 0"
    });
    const provenance = element("p",
      "Recorded comparison, not a live switch. No Qloo request, " +
      "booking or payment occurs here. Brand names are redacted.");
    Object.assign(provenance.style, {
      fontSize: "10px", lineHeight: "1.55", color: "#697990",
      margin: "13px 2px 0"
    });
    copilot.append(copilotTop, recordLabel, switcher, decisionStatus,
      thread, safety, provenance);

    const layout = element("div", "", "columns");
    layout.id = "qloo-review-layout";
    layout.style.gap = "20px";
    layout.append(stage, copilot);
    card.append(layout);

    let evidenceMode = "off";
    function message(label, text, assistant) {
      const box = element("div");
      Object.assign(box.style, {
        borderRadius: "12px", padding: "12px 13px", marginBottom: "9px",
        background: assistant ? "#ecf3ff" : "#f1f3f7",
        border: assistant ? "1px solid #d4e4ff" : "1px solid #e6ebf1"
      });
      const name = element("div", label);
      Object.assign(name.style, {
        color: assistant ? "#2356ad" : "#63718a",
        fontSize: "9px", fontWeight: "850", letterSpacing: ".09em",
        marginBottom: "7px"
      });
      const content = element("p", text);
      Object.assign(content.style, {
        fontSize: "12px", lineHeight: "1.65", color: "#1c2d49", margin: "0"
      });
      box.append(name, content);
      return box;
    }

    function renderCopilot() {
      const enabled = evidenceMode === "on";
      for (const [button, selected] of [[off, !enabled], [on, enabled]]) {
        button.setAttribute("aria-pressed", String(selected));
        button.style.background = selected ? "#fff" : "transparent";
        button.style.color = selected ? "#17294b" : "#62728c";
        button.style.borderColor = selected ? "#c8d6eb" : "transparent";
        button.style.boxShadow = selected ? "0 2px 6px #162d4c12" : "none";
      }
      decisionStatus.style.background = enabled ? "#eaf3ff" : "#fff7e9";
      decisionStatus.style.borderColor = enabled ? "#b9d2fa" : "#efd7ad";
      decisionStatus.style.color = enabled ? "#1b4d9e" : "#95601d";
      statusValue.textContent = enabled ? proof.off_on.on_brand : proof.off_on.off_brand;
      statusChoice.textContent = enabled
        ? "Brand option " + brand.baseline_option + " → " +
          brand.reviewed_option + " of " + brand.candidate_count +
          " · revision 0 → " + proof.revision
        : "Original option " + brand.baseline_option + " of " +
          brand.candidate_count + " · evidence excluded, not approved";
      thread.replaceChildren();
      thread.append(message("DECISION QUESTION",
        "Can we justify this tour's brand partner for indie music audiences?", false));
      if (enabled) {
        thread.append(message("OFC · WITH SAVED QLOO EVIDENCE",
          "The previously captured Qloo artist-to-brand rank favors option " +
          brand.reviewed_option + " over the original option " +
          brand.baseline_option + " in the same frozen shortlist. " +
          "I recommend a bounded brand repair. Only that choice changed; " +
          "the five other selections stay as proposed.", true));
        thread.append(message("WHAT I STILL CANNOT APPROVE",
          "The brand and its dependent afterparty need independent rechecks. " +
          "Opening act and venues remain unverified. This rank cannot predict " +
          "ticket sales, sponsor ROI or tour success.", true));
      } else {
        thread.append(message("OFC · WITHOUT QLOO EVIDENCE",
          "I can see the original brand option " + brand.baseline_option +
          " of " + brand.candidate_count +
          ", but have no cultural-affinity ranking to justify keeping " +
          "or replacing it. My decision is uncertain; I will not guess.", true));
        thread.append(message("WHAT HAPPENS NEXT",
          "The six frozen choices still lack adequate evidence. " +
          "No recommendation is authorized from this no-evidence view.", true));
      }
      safety.textContent = enabled
        ? "HANDOFF WITHHELD · Brand rank supports a repair, not permission to execute."
        : "HANDOFF WITHHELD · Missing evidence does not become permission.";
    }

    off.addEventListener("click", () => {
      evidenceMode = "off";
      renderCopilot();
    });
    on.addEventListener("click", () => {
      evidenceMode = "on";
      renderCopilot();
    });
    copilotToggle.addEventListener("click", () => {
      copilot.hidden = !copilot.hidden;
      copilotToggle.setAttribute("aria-expanded", String(!copilot.hidden));
      copilotToggle.textContent = copilot.hidden ? "Show Copilot" : "Hide Copilot";
      layout.style.gridTemplateColumns = copilot.hidden ? "minmax(0,1fr)" : "";
    });
    renderCopilot();

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
          "positions in frozen candidate pools, not invented brand names. " +
          "Artist identity and tour feasibility are scenario assumptions, not Qloo attestations.");
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
        put("The observed Qloo rank preferred the reviewed brand option over the " +
          "original option inside this fixed candidate pool. The rank does not verify venues, " +
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
