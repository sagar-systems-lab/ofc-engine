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
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    const request = proposal();
    const number = ++latest;
    const state = get("preflight-state");
    const description = get("preflight-description");
    const decisions = get("preflight-decisions");
    const fixture = get("open-fixture");
    if (!state || !description || !decisions || !fixture) return;
    const signature = JSON.stringify(request.decisions) +
      JSON.stringify(request.intent.constraints) + request.intent.objective;
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
      if (signature !== JSON.stringify(current.decisions) +
        JSON.stringify(current.intent.constraints) + current.intent.objective) return;
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
