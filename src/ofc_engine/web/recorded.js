"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const fileInput = $("proof-file");
  const dropArea = $("drop-area");
  const MAX_FILE_BYTES = 128 * 1024;
  const HEX = /^[a-f0-9]{64}$/;
  const ALIAS = /^Candidate [0-9]{2}$/;
  const RECHECK_STATES = new Set([
    "ORIGINAL_CAPTURE_REPLAY_ONLY",
    "RECORDED_LATER_CAPTURE_RANK_PASS_ONLY",
    "RECORDED_CAPTURE_NOT_LATER",
    "RECORDED_RECHECK_UNRESOLVED"
  ]);
  const isRecord = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const keysMatch = (data, allowed) =>
    isRecord(data) && Object.keys(data).sort().join("|") === allowed.slice().sort().join("|");
  const validDate = (value) => typeof value === "string" &&
    value.length >= 20 && value.length <= 40 && Number.isFinite(Date.parse(value));
  const validAlias = (value) => typeof value === "string" && ALIAS.test(value);
  const put = (id, value) => { $(id).textContent = String(value); };
  const stableJson = (value) => {
    if (Array.isArray(value)) return "[" + value.map(stableJson).join(",") + "]";
    if (isRecord(value))
      return "{" + Object.keys(value).sort().map((key) =>
        JSON.stringify(key) + ":" + stableJson(value[key])).join(",") + "}";
    return JSON.stringify(value);
  };
  const sha256 = async (value) => {
    if (!globalThis.crypto || !crypto.subtle) throw Error("Secure browser context required");
    const bytes = new TextEncoder().encode(stableJson(value));
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  };
  function verifyShape(document) {
    if (!keysMatch(document, ["payload", "sha256"]) || !HEX.test(document.sha256))
      throw Error("Invalid proof manifest envelope");
    const p = document.payload;
    if (!keysMatch(p, [
      "schema", "title", "source_mode", "historical_replay", "generated_at", "captured_at",
      "plan", "decision", "recheck", "qualifications"
    ]) || p.schema !== "ofc-private-operator-workspace-v1" ||
        p.title !== "Recorded decision proof" ||
        p.source_mode !== "RECORDED_REDACTED" ||
        typeof p.historical_replay !== "boolean" ||
        !validDate(p.generated_at) || !validDate(p.captured_at))
      throw Error("Unsupported recorded proof schema");

    const plan = p.plan, decision = p.decision, recheck = p.recheck, q = p.qualifications;
    if (!keysMatch(plan, [
      "baseline_digest", "stored_digest", "revision", "journal_events", "decision_count"
    ]) || !HEX.test(plan.baseline_digest) || !HEX.test(plan.stored_digest) ||
        plan.revision !== 1 || plan.journal_events !== 2 || plan.decision_count !== 1)
      throw Error("Invalid stored plan lineage");
    if (!keysMatch(decision, [
      "initial", "replacement", "changed", "rank_order", "evidence_off",
      "evidence_on_initial", "evidence_on_after_repair", "repair_attempts",
      "affected_decision_count", "structural_recheck"
    ]) || !validAlias(decision.initial) || !validAlias(decision.replacement) ||
        decision.initial === decision.replacement || decision.changed !== true ||
        !Array.isArray(decision.rank_order) || decision.rank_order.length < 2 ||
        decision.rank_order.length > 5 || !decision.rank_order.every(validAlias) ||
        new Set(decision.rank_order).size !== decision.rank_order.length ||
        !decision.rank_order.includes(decision.initial) ||
        decision.rank_order[0] !== decision.replacement ||
        decision.evidence_off !== "UNCERTAIN" ||
        decision.evidence_on_initial !== "REPAIR" ||
        decision.evidence_on_after_repair !== "PASS" ||
        decision.repair_attempts !== 1 ||
        decision.affected_decision_count !== 1 ||
        decision.structural_recheck !== "STRUCTURAL_PASS")
      throw Error("Invalid recorded repair trace");

    if (!keysMatch(recheck, [
      "status", "recorded_rank_verdict", "same_capture", "later_capture_observed",
      "pending_count", "pending_recheck_cleared"
    ]) || !RECHECK_STATES.has(recheck.status) ||
        !["PASS", "REPAIR", "UNCERTAIN"].includes(recheck.recorded_rank_verdict) ||
        typeof recheck.same_capture !== "boolean" ||
        typeof recheck.later_capture_observed !== "boolean" ||
        recheck.pending_count !== 1 || recheck.pending_recheck_cleared !== false ||
        (recheck.same_capture && recheck.later_capture_observed))
      throw Error("Unqualified pending recheck");

    if (!keysMatch(q, [
      "operator_supplied_capture_not_commit_attested", "provider_authenticated",
      "independent_recheck_completed", "overall_plan_verified", "release_authorized",
      "external_action_executed", "real_qloo_requests_this_export"
    ]) || q.operator_supplied_capture_not_commit_attested !== true ||
        q.provider_authenticated !== false || q.independent_recheck_completed !== false ||
        q.overall_plan_verified !== false || q.release_authorized !== false ||
        q.external_action_executed !== false || q.real_qloo_requests_this_export !== 0)
      throw Error("Proof exceeds recorded-only trust boundary");
    return p;
  }
  function render(report, checksum) {
    const plan = report.plan, d = report.decision, r = report.recheck;
    put("revision", "r" + plan.revision);
    put("events", plan.journal_events);
    put("attempts", d.repair_attempts);
    put("pending", r.pending_count + " PENDING");
    put("verdict-off", d.evidence_off);
    put("verdict-on", d.evidence_on_initial + " → " + d.evidence_on_after_repair);
    put("initial-candidate", d.initial);
    put("before-candidate", d.initial);
    put("after-candidate", d.replacement);
    put("recheck-note", r.status.replaceAll("_", " ").toLowerCase() + " · still pending");
    put("baseline-digest", plan.baseline_digest);
    put("plan-digest", plan.stored_digest);
    put("manifest-digest", checksum);
    put("recheck-status", r.status.replaceAll("_", " "));
    put("age-label", report.historical_replay ? "Historical point-in-time replay" : "Recorded evidence, not live certified");
    const ranks = $("rank-list");
    ranks.replaceChildren();
    d.rank_order.forEach((name, index) => {
      const row = document.createElement("div");
      row.className = "rank-row";
      const ordinal = document.createElement("strong");
      ordinal.textContent = "#" + String(index + 1).padStart(2, "0");
      const label = document.createElement("span");
      label.textContent = name;
      const state = document.createElement("small");
      state.textContent = index === 0 ? "LEADING RANK" : "RECORDED";
      if (index === 0) state.className = "rank-leading";
      row.append(ordinal, label, state);
      ranks.append(row);
    });
    $("proof-result").hidden = false;
    put("import-status", "Local checksum matched. Recorded-only proof loaded; pending recheck remains unresolved.");
    $("import-status").className = "import-status good";
  }
  let requested = 0;
  async function load(file) {
    const request = ++requested;
    $("proof-result").hidden = true;
    $("import-status").className = "import-status";
    put("import-status", "Validating local manifest…");
    try {
      if (!file || file.size === 0 || file.size > MAX_FILE_BYTES)
        throw Error("Choose a JSON proof manifest smaller than 128 KB");
      const document = JSON.parse(await file.text());
      const report = verifyShape(document);
      const computed = await sha256(report);
      if (computed !== document.sha256) throw Error("Proof manifest checksum mismatch");
      if (request !== requested) return;
      render(report, computed);
    } catch (error) {
      if (request !== requested) return;
      $("proof-result").hidden = true;
      $("import-status").className = "import-status bad";
      put("import-status", "Proof blocked: " + (error instanceof Error ? error.message : "Unknown file error") + ". No action authorized.");
    }
  }
  fileInput.addEventListener("change", () => {
    if (fileInput.files && fileInput.files[0]) load(fileInput.files[0]);
  });
  dropArea.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropArea.classList.add("dragging");
  });
  dropArea.addEventListener("dragleave", () => dropArea.classList.remove("dragging"));
  dropArea.addEventListener("drop", (event) => {
    event.preventDefault();
    dropArea.classList.remove("dragging");
    if (event.dataTransfer && event.dataTransfer.files.length === 1)
      load(event.dataTransfer.files[0]);
    else {
      $("proof-result").hidden = true;
      $("import-status").className = "import-status bad";
      put("import-status", "Select exactly one private JSON manifest. No action authorized.");
    }
  });
})();