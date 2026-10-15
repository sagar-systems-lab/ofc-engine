(() => {
  "use strict";

  const get = id => document.getElementById(id);
  const split = value => String(value || "").split(",").map(x => x.trim()).filter(Boolean);
  const button = get("check-preflight");
  if (!button || !get("decision-list")) return;

  function proposal() {
    const decisions = Array.from(get("decision-list").children, card => {
      const values = {};
      for (const input of card.querySelectorAll("[data-key]")) {
        values[input.dataset.key] = input.type === "checkbox"
          ? input.checked : input.value.trim();
      }
      return {
        decision_id: values.decision_id,
        domain: values.domain,
        candidate_id: values.candidate_id,
        candidate_pool: split(values.candidate_pool),
        mutable_fields: values.mutable ? ["candidate_id"] : [],
        depends_on: split(values.depends_on),
        locality_ref: values.locality_ref || null,
        required: Boolean(values.required)
      };
    });
    const cities = split(get("cities").value);
    return {
      intent: {
        schema_version: "0.1.0",
        run_id: "review-" + crypto.randomUUID(),
        client_request_id: "review-" + crypto.randomUUID(),
        objective: get("objective").value.trim(),
        constraints: {budget_usd: Number(get("budget").value), cities},
        taste_context: {
          audience: get("audience").value.trim(),
          locality: get("taste-locality").value.trim() || cities.join(", ")
        }
      },
      decisions
    };
  }

  let latest = 0;
  let validatedRequest = null;
  let validatedSignature = null;
  let storageAvailable = false;

  const savedButton = document.createElement("button");
  savedButton.type = "button";
  savedButton.className = "export-button";
  savedButton.textContent = "Save this review for 24 hours ↗";
  savedButton.hidden = true;
  const panel = get("preflight-panel");
  if (panel) panel.append(savedButton);
  const proof = document.createElement("section");
  proof.id = "hosted-qloo-proof";
  proof.className = "preflight-record";
  proof.hidden = true;
  if (panel) panel.append(proof);

  function identity(request) {
    return JSON.stringify(request.decisions) +
      JSON.stringify(request.intent.constraints) + request.intent.objective;
  }

  function validateStored(review) {
    const live = review.source_mode === "LIVE_HTTP_UNVERIFIED";
    if (
      review.verification_status !== "REVIEW_REQUIRED" ||
      review.release_authorized !== false ||
      review.external_action_executed !== false ||
      review.provider_authenticated !== false ||
      review.independent_recheck_completed !== false ||
      !/^[0-9a-f]{64}$/.test(review.plan_digest) ||
      !Array.isArray(review.decisions) ||
      (live ? review.real_qloo_requests !== 1 :
        review.source_mode !== "EVIDENCE_UNAVAILABLE" ||
        review.real_qloo_requests !== 0 ||
        review.decisions.some(decision => decision.verdict !== "UNCERTAIN"))
    ) throw new Error("Invalid stored review");
    if (live) {
      const run = review.agent_run;
      if (
        !run || run.release_authorized !== false ||
        run.verification_status !== "REVIEW_REQUIRED" ||
        run.source_mode !== "LIVE_HTTP_UNVERIFIED" ||
        run.provenance?.baseline_digest !== review.baseline_digest ||
        run.provenance?.current_plan_digest !== review.plan_digest ||
        run.provenance?.journal_head !== review.journal_head ||
        run.handoff?.status !== "WITHHELD" ||
        run.revision !== review.revision ||
        !Array.isArray(run.changed_ids) ||
        !Array.isArray(run.pending_rechecks) ||
        !Array.isArray(run.decisions) ||
        run.decisions.length !== review.decisions.length ||
        run.off_on?.same_baseline !== true ||
        run.off_on.real_causal_proof !== false ||
        !Array.isArray(run.preserved_ids) ||
        !Array.isArray(run.invalidated_ids) ||
        !Array.isArray(run.blockers) ||
        !Array.isArray(run.handoff?.blockers)
      ) throw new Error("Unbound Qloo review");
    }
  }

  async function restore() {
    try {
      const record = JSON.parse(sessionStorage.getItem("ofc-last-review") || "null");
      if (!record || !/^web-[0-9a-f]{32}$/.test(record.run_id)) return;
      const response = await fetch("/api/runs/" + record.run_id + "/review", {
        cache: "no-store",
        headers: {"Authorization": "Bearer " + record.ticket}
      });
      if (!response.ok) return;
      const review = await response.json();
      validateStored(review);
      if (panel) panel.hidden = false;
      proof.replaceChildren();
      proof.hidden = true;
      const live = review.source_mode === "LIVE_HTTP_UNVERIFIED";
      get("preflight-state").textContent = live
        ? "QLOO RANK OBSERVED · REVIEW REQUIRED"
        : "SAVED RUN · REVIEW REQUIRED";
      get("preflight-description").textContent = live
        ? "One scoped Qloo rank observation changed " +
          review.agent_run.changed_ids.length + " decision(s). " +
          review.agent_run.pending_rechecks.length +
          " rechecks remain pending. Provider attestation and safe handoff are withheld."
        : "Restored durable review " + review.run_id + " at revision " +
          review.revision + ". No Qloo evidence or external action was approved.";
      if (live) {
        const list = get("preflight-decisions");
        list.replaceChildren();
        for (const item of review.decisions) {
          const row = document.createElement("li");
          const name = document.createElement("strong");
          const detail = document.createElement("span");
          name.textContent = item.decision_id;
          detail.textContent = item.verdict +
            (item.pending_recheck ? " · RECHECK PENDING" : " · RANK-ONLY REVIEW");
          row.className = "preflight-record";
          row.append(name, detail);
          list.append(row);
        }
        const run = review.agent_run;
        const label = document.createElement("strong");
        label.textContent = "Qloo OFF / ON · Same frozen baseline";
        const comparison = document.createElement("p");
        comparison.textContent = "Brand: " +
          (run.off_on.off_verdicts.brand || "UNCERTAIN") + " → " +
          (run.off_on.on_verdicts.brand || "UNCERTAIN") +
          ". Selective repairs: " + run.changed_ids.length +
          "; preserved decisions: " + run.preserved_ids.length +
          "; invalidated dependents: " + run.invalidated_ids.length + ".";
        const safety = document.createElement("p");
        safety.textContent = "Revision " + run.revision +
          " · Rechecks pending: " + run.pending_rechecks.length +
          " · Handoff WITHHELD · Provider attestation not established.";
        const evidence = document.createElement("p");
        evidence.textContent = "Source: LIVE_HTTP_UNVERIFIED. " +
          "One observed Qloo rank request. This is not a verified full-tour plan.";
        proof.append(label, comparison, safety, evidence);
        proof.hidden = false;
        const disclaimer = document.querySelector("#view-plan > .intro p");
        if (disclaimer) disclaimer.textContent =
          "One operator-authorized Qloo rank observation is recorded for this " +
          "tour. Independent verification and action handoff remain blocked.";
        const step = document.querySelector(".state-strip .state-item:nth-child(2) small");
        if (step) step.textContent = "RANK OBSERVED";
        const boundary = document.querySelector("#view-plan .boundary-panel");
        if (boundary) {
          const heading = boundary.querySelector("strong");
          const description = boundary.querySelector("p");
          if (heading) heading.textContent = "Rank observed · verification pending";
          if (description) description.textContent =
            "Qloo supplied one scoped rank observation. Unsupported decisions, " +
            "dependent rechecks and plan release remain blocked. " +
            "No external action has occurred.";
        }
      }
    } catch {
      // The original plan editor remains usable without session storage.
    }
  }

  window.addEventListener("ofc-review-refresh", () => { void restore(); });

  fetch("/api/runs/status", {cache: "no-store"})
    .then(response => response.ok ? response.json() : null)
    .then(result => {
      storageAvailable = result?.enabled === true &&
        result.release_authorized === false;
      if (storageAvailable) restore();
    }).catch(() => {});

  savedButton.addEventListener("click", async () => {
    if (!validatedRequest || !storageAvailable || savedButton.disabled) return;
    if (identity(proposal()) !== validatedSignature) {
      savedButton.hidden = true;
      validatedRequest = null;
      return;
    }
    savedButton.disabled = true;
    const state = get("preflight-state");
    state.textContent = "SAVING DURABLE REVIEW";
    try {
      const response = await fetch("/api/runs", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(validatedRequest)
      });
      if (!response.ok) throw new Error("Run storage unavailable");
      const stored = await response.json();
      if (!/^web-[0-9a-f]{32}$/.test(stored.run_id) ||
        typeof stored.ticket !== "string" ||
        stored.review?.run_id !== stored.run_id) {
        throw new Error("Invalid run receipt");
      }
      validateStored(stored.review);
      sessionStorage.setItem("ofc-last-review", JSON.stringify({
        run_id: stored.run_id, ticket: stored.ticket
      }));
      state.textContent = "DURABLE RUN SAVED · REVIEW REQUIRED";
      get("preflight-description").textContent =
        "Run " + stored.run_id + " is frozen for 24 hours. Its revision and " +
        "journal can be rechecked after reload. Evidence and action stay blocked.";
      savedButton.textContent = "Run saved for this browser session";
    } catch {
      state.textContent = "DURABLE SAVE UNAVAILABLE";
      get("preflight-description").textContent =
        "The plan was validated but could not be saved. No evidence or action was approved.";
      savedButton.disabled = false;
    }
  });

  button.addEventListener("click", async () => {
    if (button.disabled) return;
    const request = proposal();
    const number = ++latest;
    const state = get("preflight-state");
    const description = get("preflight-description");
    const decisions = get("preflight-decisions");
    const fixture = get("open-fixture");
    if (!state || !description || !decisions || !fixture) return;
    const signature = identity(request);
    validatedRequest = null;
    validatedSignature = null;
    savedButton.hidden = true;
    savedButton.disabled = false;
    savedButton.textContent = "Save this review for 24 hours ↗";
    state.textContent = "SERVER REVIEW IN PROGRESS";
    fixture.hidden = true;
    try {
      const response = await fetch("/api/preflight", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(request)
      });
      if (!response.ok) throw new Error("Unable to review");
      const result = await response.json();
      if (number !== latest || button.disabled) return;
      const current = proposal();
      if (signature !== identity(current)) return;
      if (
        !Array.isArray(result.decisions) ||
        result.decisions.length !== request.decisions.length ||
        result.release_authorized !== false ||
        result.external_action_executed !== false ||
        result.provider_authenticated !== false ||
        result.real_qloo_requests !== 0 ||
        result.stored_on_server !== false ||
        !/^[a-f0-9]{64}$/.test(result.plan_digest)
      ) throw new Error("Unqualified review");
      const canonical = result.synthetic_fixture_available === true;
      if (result.status !== "REVIEW_REQUIRED" ||
        result.source_mode !== "EVIDENCE_UNAVAILABLE") {
        throw new Error("Unqualified outcome");
      }

      state.textContent = canonical
        ? "SERVER VALIDATED · SYNTHETIC ONLY" : "SERVER VALIDATED · REVIEW REQUIRED";
      description.textContent = canonical
        ? "Your choices match the canonical tour template. The linked proof is a separate synthetic example, not evidence for this proposal. No Qloo call or action occurred."
        : "Server validation passed. No trusted Qloo evidence was supplied for this plan; required decisions remain uncertain and action is blocked.";
      decisions.replaceChildren();
      for (const item of result.decisions) {
        if (item.verdict !== "UNCERTAIN") throw new Error("Invalid preflight verdict");
        const row = document.createElement("li");
        row.className = "preflight-record";
        const name = document.createElement("strong");
        name.textContent = item.decision_id;
        const status = document.createElement("span");
        status.textContent = "UNCERTAIN · EVIDENCE UNAVAILABLE";
        row.append(name, status);
        decisions.append(row);
      }
      fixture.hidden = !canonical;
      validatedRequest = request;
      validatedSignature = signature;
      savedButton.hidden = !storageAvailable;
    } catch {
      if (number !== latest) return;
      state.textContent = "SERVER REVIEW UNAVAILABLE";
      description.textContent =
        "The server could not qualify this proposal. No evidence or action was approved.";
      decisions.replaceChildren();
      fixture.hidden = true;
    }
  });
})();
