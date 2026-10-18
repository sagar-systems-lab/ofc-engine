"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const make = (tag, cls = "", value) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (value !== undefined && value !== null) n.textContent = String(value);
    return n;
  };
  const name = x => typeof x === "string" ? x.slice(0, 300) : "";
  const id = x => typeof x === "string" ? x.slice(0, 128) : "";
  const recorded = run => Boolean(run?._sessionReadConfirmed &&
    /^cp-run-[a-f0-9]{32}$/.test(run.run_id || "") &&
    ["REVIEW_REQUIRED", "BLOCKED", "CLARIFICATION_REQUIRED", "CAPABILITY_UNAVAILABLE"].includes(run.status));

  const pairing = run => {
    if (!recorded(run) || run.status !== "REVIEW_REQUIRED") return null;
    const off = run.off, on = run.on, digest = run.qloo_evidence_digest;
    if (!off || !on || off.ofc_enabled !== false || on.ofc_enabled !== true ||
        !run.model_id || !/^[a-f0-9]{64}$/.test(digest || "") ||
        !Array.isArray(off.decisions) || !Array.isArray(on.decisions) ||
        !off.decisions.length || off.qloo?.evidence_digest !== digest ||
        on.qloo?.evidence_digest !== digest ||
        JSON.stringify(off.qloo) !== JSON.stringify(on.qloo)) return null;
    if (run.baseline_digest && run.baseline_digest !== off.plan_digest) return null;
    if (run.reviewed_digest && run.reviewed_digest !== on.plan_digest) return null;
    const before = new Map(), after = new Map();
    for (const d of off.decisions) {
      if (!d || typeof d.decision_id !== "string" || !d.decision_id ||
          typeof d.candidate_id !== "string" || before.has(d.decision_id)) return null;
      before.set(d.decision_id, d);
    }
    for (const d of on.decisions) {
      if (!d || !before.has(d.decision_id) || after.has(d.decision_id) ||
          typeof d.candidate_id !== "string" ||
          d.changed !== (before.get(d.decision_id).candidate_id !== d.candidate_id)) return null;
      after.set(d.decision_id, d);
    }
    if (before.size !== after.size) return null;
    const changed = [...before].filter(([key, old]) => old.candidate_id !== after.get(key).candidate_id).map(([key]) => key);
    if (Array.isArray(on.changed_ids) && JSON.stringify(changed) !== JSON.stringify(on.changed_ids)) return null;
    const receipt = run.value_receipt;
    if (receipt && (receipt.schema_version !== "ofc-value.v1" ||
        receipt.run_id !== run.run_id ||
        receipt.shared_qloo_evidence_digest !== digest ||
        receipt.model_id !== run.model_id ||
        receipt.decision_count !== before.size ||
        receipt.changed_count !== changed.length)) return null;
    return {before, after, changed, digest};
  };

  const decisions = (run, pair) => pair ? [...pair.before].map(([key, old]) => {
    const revised = pair.after.get(key);
    return {
      decision_id: id(key), domain: name(old.domain),
      off_choice: name(old.candidate_id), on_choice: name(revised.candidate_id),
      changed: old.candidate_id !== revised.candidate_id,
      verdict: id(revised.ofc_verdict) || null, reason: id(revised.ofc_reason) || null,
      pending_recheck: revised.pending_recheck === true,
      rank_observed: old.cultural_rank_observed === true,
      rank_rechecked: revised.rank_recheck_completed === true,
    };
  }) : [];

  const rankedEvidence = pair => {
    if (!pair) return [];
    const observations = [];
    for (const [key, original] of pair.before) {
      const reviewed = pair.after.get(key);
      const proof = original.qloo_observation;
      const order = proof?.ranked_candidate_ids;
      const pool = original.candidate_pool;
      if (!original.cultural_rank_observed || !reviewed?.rank_recheck_completed ||
          !proof || proof.source_mode !== "LIVE_HTTP_UNVERIFIED" ||
          !/^[a-f0-9]{64}$/.test(proof.query_fingerprint || "") ||
          !/^[a-f0-9]{64}$/.test(proof.capture_digest || "") ||
          !Array.isArray(order) || !Array.isArray(pool) ||
          order.length < 2 || order.length !== pool.length ||
          !order.every(x => typeof x === "string" && x.length > 0 && x.length <= 300) ||
          new Set(order).size !== order.length ||
          !order.every(x => pool.includes(x))) continue;
      observations.push({
        decision_id: id(key),
        ranked_candidates: order.map(name),
        rank_recheck_matched: reviewed.ofc_reason === "QLOO_RANK_RECHECK_MATCHED",
        source_mode: proof.source_mode,
        observation_time_utc: name(proof.collected_at) || null,
        query_fingerprint: proof.query_fingerprint,
        capture_digest: proof.capture_digest,
      });
    }
    return observations;
  };

  const buildProof = (run, mode, generatedAt = new Date().toISOString()) => {
    if (!recorded(run)) return null;
    const pair = pairing(run), usage = {};
    for (const key of ["qloo_requests_this_run", "qloo_search_requests", "qloo_rank_requests",
      "qloo_catalog_requests", "qloo_discovery_requests", "llm_requests_this_run"])
      if (Number.isInteger(run[key]) && run[key] >= 0) usage[key] = run[key];
    if (run.activity?.requests) {
      usage.saved_attempt_states = {};
      for (const key of ["search_complete", "search_reserved", "rank_complete", "rank_reserved"])
        if (Number.isInteger(run.activity.requests[key]) && run.activity.requests[key] >= 0)
          usage.saved_attempt_states[key] = run.activity.requests[key];
    }
    return {
      schema_version: "ofc-browser-proof.v1",
      kind: pair ? "paired-saved-run-export" : "saved-status-export",
      source: "OFC_HOSTED_SESSION_REPLAY",
      export_generated_at_utc: generatedAt,
      run_id: run.run_id, run_status: run.status, selected_view: mode === "OFF" ? "OFF" : "ON",
      model_id: name(run.model_id) || null, prompt_digest: id(run.prompt_digest) || null,
      baseline_digest: id(run.baseline_digest) || null, reviewed_digest: id(run.reviewed_digest) || null,
      qloo_evidence_digest: pair?.digest || null,
      verification_status: id(run.verification_status || run.status),
      reason_code: id(run.reason_code) || null,
      decisions: decisions(run, pair), ranked_evidence: rankedEvidence(pair), usage,
      receipt: pair && run.value_receipt ? {
        schema_version: "ofc-value.v1",
        receipt_digest: id(run.value_receipt.receipt_digest) || null,
        changed_count: pair.changed.length,
        business_evidence_satisfied: run.value_receipt.business_evidence_satisfied === true,
      } : null,
      comparison_available: Boolean(pair),
      release_authorized: false, external_action_executed: false,
      limitations: [
        "Browser export of saved session data; not a cryptographically signed provider attestation.",
        "Qloo cultural ranking does not establish business, geographic or contract suitability.",
        "No payment, booking, endorsement or other external action is authorized.",
      ],
    };
  };

  const csvCell = value => {
    let v = String(value ?? "").replace(/\u0000/g, "");
    if (/^[\s\uFEFF]*[=+\-@]/u.test(v) || /^[\t\r\n]/u.test(v)) v = "'" + v;
    return '"' + v.replace(/"/g, '""') + '"';
  };
  const csv = proof => {
    if (!proof?.comparison_available) return null;
    const fields = ["run_id", "decision_id", "domain", "off_choice", "on_choice",
      "changed", "verdict", "reason", "pending_recheck", "evidence_digest",
      "observed_rank_order", "rank_recheck_matched"];
    const lines = [fields.join(",")];
    for (const decision of proof.decisions) {
      const evidence = proof.ranked_evidence?.find(x => x.decision_id === decision.decision_id);
      const row = {...decision, run_id: proof.run_id, evidence_digest: proof.qloo_evidence_digest,
        observed_rank_order: evidence?.ranked_candidates.join(" > ") || "",
        rank_recheck_matched: evidence?.rank_recheck_matched ?? false};
      lines.push(fields.map(key => csvCell(row[key])).join(","));
    }
    return "\uFEFF" + lines.join("\r\n") + "\r\n";
  };
  const saveFile = (body, mediaType, ext, run, mode) => {
    const url = URL.createObjectURL(new Blob([body], {type:mediaType}));
    const a = document.createElement("a");
    a.href = url;
    a.download = "ofc-" + run.run_id.slice(-12) + "-" + mode.toLowerCase() + "." + ext;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const state = {getRun:()=>null, getMode:()=> "ON"};
  const downloadJSON = () => {
    const run = state.getRun(), mode = state.getMode(), proof = buildProof(run, mode);
    if (proof) saveFile(JSON.stringify(proof,null,2)+"\n","application/json;charset=utf-8","json",run,mode);
  };
  const downloadCSV = () => {
    const run = state.getRun(), mode = state.getMode(), proof = buildProof(run, mode), data = csv(proof);
    if (data) saveFile(data,"text/csv;charset=utf-8","csv",run,mode);
  };
  const field = (key,value) => {
    const r = make("div","proof-field");
    r.append(make("dt","",key),make("dd","",value === null ? "Not recorded" : value));
    return r;
  };
  const openDialog = (dialog, invoker) => {
    if (dialog.open) return;
    dialog._invoker = invoker || document.activeElement;
    dialog.showModal();
  };
  const closeDialog = dialog => {
    if (dialog.open) dialog.close();
  };

  const openProof = (run=state.getRun(), invoker=document.activeElement) => {
    const proof=buildProof(run,state.getMode());
    if (!proof) return;
    $("proof-summary").textContent = proof.comparison_available
      ? "Saved decision record. Browser export is not independently signed."
      : "Saved partial or blocked record; no completed OFF/ON comparison is claimed.";
    const fields=$("proof-fields");
    fields.replaceChildren();
    for(const [label,value] of [
      ["Run ID",proof.run_id],["Status",proof.run_status],
      ["Selected view",proof.selected_view],["Model",proof.model_id],
      ["Decisions",proof.comparison_available ? proof.decisions.length : null],
      ["Qloo ranked attempts",proof.usage.saved_attempt_states?.rank_complete ?? null],
      ["Saved pool ranking",proof.ranked_evidence.length
        ? proof.ranked_evidence.map(x => x.ranked_candidates.join(" → ")).join(" | ")
        : "Not available or not rechecked"],
      ["Changed selections",proof.comparison_available ? proof.decisions.filter(d=>d.changed).length : null],
      ["Shared evidence digest",proof.qloo_evidence_digest],
      ["Receipt digest",proof.receipt?.receipt_digest ?? null],
    ]) fields.append(field(label,value));
    $("proof-json-download").disabled=false;
    $("proof-csv-download").disabled=!proof.comparison_available;
    openDialog($("proof-dialog"),invoker);
  };

  const openCompare = (run=state.getRun(), invoker=document.activeElement) => {
    const pair=pairing(run);
    if (!pair) return;
    const ident=$("comparison-identity"), rows=$("comparison-rows");
    ident.replaceChildren();rows.replaceChildren();
    ident.append(field("Same model",name(run.model_id)),
      field("Shared Qloo evidence",pair.digest.slice(0,18)+"…"),
      field("Verification","REVIEW_REQUIRED"));
    for(const [key,old] of pair.before) {
      const revised=pair.after.get(key), changed=old.candidate_id!==revised.candidate_id;
      const row=make("div","comparison-row");
      const title=make("div","comparison-row-title");
      title.append(make("strong","",name(old.domain)||"Decision"),make("small","",id(key)));
      const off=make("div","comparison-choice comparison-off");
      off.append(make("span","comparison-overline","OFC OFF · LLM + Qloo"),
        make("strong","",name(old.candidate_id)),make("small","",
          old.cultural_rank_observed ? "Saved Qloo rank observed" : "Qloo rank not observed"));
      const on=make("div","comparison-choice comparison-on");
      on.append(make("span","comparison-overline","OFC ON · reviewed"),
        make("strong","",name(revised.candidate_id)),
        make("small",changed?"comparison-delta":"comparison-preserved",
          changed?"Selection revised; further checks required":"Original selection preserved"),
        make("small","","Verdict: "+id(revised.ofc_verdict||"UNCERTAIN")));
      if(revised.ofc_reason)on.append(make("small","","Reason: "+id(revised.ofc_reason)));
      const rank=rankedEvidence(pair).find(x=>x.decision_id===key);
      if(rank){
        off.append(make("small","","Qloo pool rank: "+rank.ranked_candidates.join(" → ")));
        on.append(make("small","",rank.rank_recheck_matched
          ? "Independent ranking recheck matched"
          : "Ranking replay recorded; independent match not verified"));
      }
      row.append(title,off,on);
      rows.append(row);
    }
    $("comparison-footer").textContent=pair.changed.length
      ? pair.changed.length+" revised · "+(pair.before.size-pair.changed.length)+
          " preserved. Business verification and external action remain withheld."
      : "No qualified change demonstrated. Commercial verification and external action remain withheld.";
    openDialog($("comparison-dialog"),invoker);
  };
  const action = (caption,fn,disabled=false) => {
    const button=make("button","result-action",caption);
    button.type="button";button.disabled=disabled;
    if(!disabled)button.addEventListener("click",()=>fn(button));
    return button;
  };
  const mapView = () => {
    if($("map-detail").hidden)return;
    if(getComputedStyle($("activity-rail")).display==="none")$("activity-open").click();
    if($("map-content").hidden)$("map-expand").click();
    $("map-detail").scrollIntoView({block:"nearest"});
  };
  const renderRun=(run,mode,makeMessage)=>{
    if(run.status!=="REVIEW_REQUIRED")return false;
    const answer=mode==="ON"?run.ofc_answer:run.original_answer;
    // Historical model narration may contain Qloo UUIDs; proof stays unchanged.
    const readable=typeof answer==="string" ? answer.replace(
      /\s*\([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\)/gi, ""
    ) : "";
    const assistant=makeMessage("assistant",readable.trim()?readable:
      "A saved cultural review is available. Further verification is required.");
    const section=make("section","result-summary");
    const heading=make("div","result-heading");
    heading.append(make("h2","",mode==="ON"?"Reviewed choices · OFC ON":"Original choices · OFC OFF"),
      make("span","review-state","REVIEW REQUIRED"));
    section.append(heading);
    const pair=pairing(run);
    if(pair){
      const grid=make("div","result-grid");
      for(const [key,before] of pair.before){
        const after=pair.after.get(key),selected=mode==="ON"?after:before;
        const tile=make("article","decision-card");
        tile.append(make("span","decision-domain",name(before.domain)||"Decision"),
          make("h3","",name(selected.candidate_id)));
        if(mode==="ON"){
          const changed=before.candidate_id!==after.candidate_id;
          tile.append(make("small",changed?"decision-change":"decision-preserved",
            changed?"Changed from "+name(before.candidate_id):"Original choice preserved"));
          tile.append(make("p","",after.ofc_verdict==="UNCERTAIN"?
            "Uncertain; more evidence or business checks required.":
            after.ofc_verdict==="PASS"?"Cultural policy review recorded; commercial checks separate.":
              "Review verdict not available."));
        }else tile.append(make("p","",before.cultural_rank_observed?
          "Qloo cultural ranking recorded. OFC controls not applied.":
          "No scoped Qloo rank recorded for this choice."));
        grid.append(tile);
      }
      section.append(grid);
      const summary = mode==="ON"
        ? pair.changed.length+" revised · "+(pair.before.size-pair.changed.length)+" preserved."
        : pair.before.size+" original choice"+(pair.before.size===1?"":"s")+". OFC controls not applied.";
      section.append(make("p","result-caveat",summary+
        " Cultural affinity is not commercial approval."));
    }else section.append(make("p","result-caveat",
      "Saved evidence-bound OFF/ON comparison is unavailable. No improvement is inferred."));
    const footer=make("div","result-actions");
    footer.append(action("View decision map",mapView,!Array.isArray(run.plan?.choices)||
      !run.plan.choices.length),
      action("Download proof (.json)",downloadJSON,!recorded(run)),
      action("Compare OFF vs ON",invoker=>openCompare(run,invoker),!pair));
    section.append(footer,make("p","result-legal",
      "Saved cultural review · further checks required · external action withheld"));
    assistant.append(section);
    return assistant;
  };
  const syncProof=run=>{
    const button=$("proof-entry"),enabled=recorded(run);
    button.disabled=!enabled;
    button.title=enabled?"Inspect saved record and export":"Proof available after a saved response";
    button.setAttribute("aria-label",button.title);
    button.dataset.hasComparison=String(Boolean(pairing(run)));
  };
  const mount=config=>{
    state.getRun=config.getRun;state.getMode=config.getMode;
    $("proof-entry").addEventListener("click",()=>openProof());
    $("proof-json-download").addEventListener("click",downloadJSON);
    $("proof-csv-download").addEventListener("click",downloadCSV);
    $("comparison-open-proof").addEventListener("click",()=>{
      closeDialog($("comparison-dialog"));openProof();
    });
    for(const [d,c] of [["proof-dialog","proof-close"],["comparison-dialog","comparison-close"]]){
      const dialog=$(d);
      $(c).addEventListener("click",()=>closeDialog(dialog));
      dialog.addEventListener("close",()=>{if(dialog._invoker?.isConnected)dialog._invoker.focus();});
    }
  };
  window.OFCResults=Object.freeze({mount,renderRun,syncProof,buildProof,pairing,csv,downloadJSON,downloadCSV});
})();