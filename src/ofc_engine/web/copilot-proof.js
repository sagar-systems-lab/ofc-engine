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
        make("small","","Cultural check: "+id(revised.ofc_verdict||"UNCERTAIN")+
          " (not commercial approval)"));
      if(revised.ofc_reason)on.append(make("small","","Reason: "+id(revised.ofc_reason)));
      const rank=rankedEvidence(pair).find(x=>x.decision_id===key);
      if(rank){
        off.append(make("small","","Qloo pool rank: "+rank.ranked_candidates.join(" → ")));
        on.append(make("small","",rank.rank_recheck_matched
          ? "Repeat Qloo ranking matched (same provider)"
          : "Repeat ranking was not qualified as matching"));
      }
      row.append(title,off,on);
      rows.append(row);
    }
    $("comparison-footer").textContent=pair.changed.length
      ? pair.changed.length+" revised · "+(pair.before.size-pair.changed.length)+
          " preserved. Business verification and external action remain withheld."
      : "Original selection preserved. "+rankedEvidence(pair).filter(x=>x.rank_recheck_matched).length+
        " repeated Qloo ranking check(s) matched. Commercial verification and external action remain withheld.";
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
  const decisionFlow=(run,pair,mode)=>{
    if(!pair) return null;
    const section=make("section","live-decision-graph");
    section.setAttribute("aria-label","Saved evidence decision flow");
    section.append(make("h2","graph-title","How the decision was reviewed"),
      make("p","graph-scope",
        "Saved evidence flow · only recorded decisions, not a synthetic dependency graph."));
    const rankChecks=rankedEvidence(pair);
    const labels=new Map([
      ["budget_feasibility","budget"],
      ["contract_rights","contract rights"],
      ["local_audience","local audience"],
      ["venue_availability","venue"],
    ]);
    const stages=(steps)=>{
      const track=make("div","graph-track");
      for(const [heading,value,kind] of steps){
        const stage=make("div","graph-stage graph-"+kind);
        stage.append(make("span","graph-stage-label",heading),
          make("strong","graph-stage-value",value));
        track.append(stage);
      }
      return track;
    };
    for(const [decisionId,original] of [...pair.before].slice(0,4)){
      const revised=pair.after.get(decisionId);
      const rank=rankChecks.find(x=>x.decision_id===decisionId);
      const required=run.business_verification?.missing_requirements?.[decisionId];
      const missing=Array.isArray(required)
        ? [...new Set(required.filter(x=>labels.has(x)).map(x=>labels.get(x)))].slice(0,4) : [];
      const complete=run.business_verification?.business_evidence_satisfied===true &&
        run.value_receipt?.business_evidence_satisfied===true;
      const place=String(original.domain || "").toLowerCase()==="place";
      const artist=place
        ? "Place reference · no artist required"
        : typeof run.plan?.artist_name==="string" && run.plan.artist_name.trim()
          ? name(run.plan.artist_name) : "Artist not recorded";
      const ranked=rank ? rank.ranked_candidates.join(" → ") : "Repeatable rank unavailable";
      const choice=mode==="OFF"
        ? "Original: "+name(original.candidate_id)+" · OFC not applied"
        : name(revised.candidate_id)+(revised.changed
          ? " · revised from "+name(original.candidate_id)
          : " · original preserved");
      const gate=complete ? "Evidence gate complete · action withheld"
        : "HOLD · "+(missing.length ? missing.join(", ")+" pending" : "business proof pending");
      const item=make("div","graph-decision");
      item.append(make("p","graph-decision-name",name(original.domain)+" · "+id(decisionId)));
      item.append(stages([
        [place?"Taste reference / scope":"Artist / scope",artist,"source"],
        ["Qloo cultural rank",ranked,rank?"observed":"unverified"],
        [mode==="ON"?"OFC review":"Original choice",choice,"choice"],
        ["Business gate",gate,complete?"withheld":"hold"],
      ]));
      section.append(item);
    }
    if(pair.before.size>4)
      section.append(make("p","graph-scope",
        "Showing 4 of "+pair.before.size+" saved decisions; see decision map for the complete scope."));
    section.append(make("p","graph-disclaimer",
      "Arrows show review stages, not causal or contractual relationships. Qloo ranking is cultural evidence, not permission to act."));
    return section;
  };

  const decisionBrief=(run,pair,mode)=>{
    const panel=make("section","decision-brief");
    panel.setAttribute("aria-label","Evidence-grounded decision brief");
    panel.append(make("h2","brief-heading","Decision brief"));
    const row=(key,value)=>{
      const item=make("div","brief-row");
      item.append(make("strong","brief-key",key),make("p","brief-detail",value));
      panel.append(item);
    };
    if(!pair){
      row("Evidence","No complete paired comparison is saved.");
      row("Release","Not authorized. Evidence review must complete first.");
      return panel;
    }
    if(run.intake_source==="USER_CONFIRMED_UNVERIFIED")
      row("Plan source","Artist and brands entered by the user after a model outage; identities required Qloo verification.");
    const ranked=rankedEvidence(pair);
    const match=ranked.find(item=>item.rank_recheck_matched);
    row("Qloo observation",match
      ? match.ranked_candidates.join(" > ")+" — repeat Qloo rank matched. Cultural evidence only."
      : "No matching repeated Qloo rank established.");
    if(mode==="OFF"){
      row("Decision","Original plan. No OFC control review applied to this view.");
    }else{
      const changed=pair.changed.length, preserved=pair.before.size-changed;
      row("OFC intervention",changed
        ? changed+" revised · "+preserved+" preserved after control review."
        : preserved+" original choice(s) preserved after control review. No selection revision.");
    }
    const available=new Map([
      ["budget_feasibility","budget feasibility"],
      ["contract_rights","partnership/contract rights"],
      ["local_audience","local audience evidence"],
      ["venue_availability","venue availability"],
    ]);
    const missing=[];
    const items=run.business_verification?.missing_requirements;
    if(items && typeof items==="object" && !Array.isArray(items))
      for(const needs of Object.values(items))
        if(Array.isArray(needs))
          for(const requirement of needs)
            if(available.has(requirement) && !missing.includes(available.get(requirement)))
              missing.push(available.get(requirement));
    const complete=run.business_verification?.business_evidence_satisfied===true &&
      run.value_receipt?.business_evidence_satisfied===true;
    const placeOnly=[...pair.before].length>0 &&
      [...pair.before].every(([,decision])=>String(decision.domain).toLowerCase()==="place");
    row(placeOnly?"Venue / commercial readiness":"Partnership readiness",complete
      ? "Business evidence gate satisfied. External action remains unauthorized."
      : "HOLD — commercial evidence incomplete."+
        (missing.length ? " Missing: "+missing.join(", ")+"." :
          " Independent business approval was not established."));
    if((Array.isArray(run.plan?.cities) && run.plan.cities.length) ||
       (Array.isArray(run.unapplied_constraints) &&
        run.unapplied_constraints.includes("LOCATION_NOT_IN_RANK_QUERY")))
      row("City allocation","Not verified by this artist-brand Qloo ranking. No geographic or budget priority is established.");
    return panel;
  };

  const advisoryPanel=(run,pair,mode)=>{
    if(!pair)return null;
    const brands=[...pair.before].filter(([,choice])=>choice.domain==="brand");
    if(!brands.length)return null;
    const panel=make("section","answer-advisory");
    panel.setAttribute("aria-label","Evidence-grounded recommendation and next actions");
    panel.append(make("p","advisory-eyebrow",
      mode==="ON"?"OFC decision advisory · saved evidence":"Original decision · OFC controls not applied"));
    const ranked=rankedEvidence(pair);
    const requirements=new Map([
      ["budget_feasibility","Confirm negotiated cost, fees and budget fit"],
      ["contract_rights","Verify partnership rights and required approvals"],
      ["local_audience","Validate the actual target audience and geography"],
      ["venue_availability","Verify venue or campaign availability"],
    ]);
    for(const [key,old] of brands.slice(0,3)){
      const reviewed=pair.after.get(key);
      const choice=mode==="ON"?reviewed.candidate_id:old.candidate_id;
      const rank=ranked.find(item=>item.decision_id===key);
      const qualified=Boolean(rank?.rank_recheck_matched &&
        rank.ranked_candidates[0]===reviewed.candidate_id &&
        reviewed.ofc_verdict==="PASS" &&
        reviewed.ofc_reason==="QLOO_RANK_RECHECK_MATCHED");
      const changed=reviewed.candidate_id!==old.candidate_id;
      const artist=name(run.plan?.artist_name);
      const item=make("article","advisory-decision");
      item.append(make("span","advisory-context",
        (artist?artist+" · ":"")+"Brand decision"));
      const headline=mode==="OFF"?"Starting choice: "+choice
        :qualified?(changed?"Revise choice to ":"Keep original choice: ")+choice
          :"No qualified brand recommendation yet";
      item.append(make("h2","advisory-title",headline));
      const basis=mode==="OFF"
        ?"This is the unmodified proposal, not an OFC endorsement. The Qloo candidate ranking below is shared with the reviewed view."
        :qualified
          ?"The saved OFC verdict agrees with two matching Qloo rank observations for the declared shortlist. "+
            "This is a relative cultural ranking from one provider—not audience conversion, commercial availability or a confidence percentage."
          :"The saved control record does not establish a matching qualified top-ranked brand for this decision. Do not present the choice as verified.";
      item.append(make("p","advisory-basis",basis));
      const evidence=make("div","advisory-evidence");
      evidence.append(make("strong","advisory-label","Observed Qloo shortlist"));
      if(rank){
        const ordered=make("ol","advisory-ranking");
        for(const candidate of rank.ranked_candidates.slice(0,5)){
          const li=make("li","advisory-rank-item",candidate);
          ordered.append(li);
        }
        evidence.append(ordered);
        evidence.append(make("p","advisory-footnote",rank.rank_recheck_matched
          ?"Two scoped ranking observations matched · same Qloo provider"
          :"Repeat ranking was not qualified as matching"));
      }else evidence.append(make("p","advisory-footnote",
        "No qualified repeated ranking is available for this decision."));
      item.append(evidence);
      const insights=make("div","advisory-insights");
      const insight=(heading,copy)=>{
        const cell=make("div","advisory-insight");
        cell.append(make("h3","",heading),make("p","",copy));
        insights.append(cell);
      };
      insight("What the comparison means",
        rank?.rank_recheck_matched
          ?"Within this named shortlist, "+rank.ranked_candidates[0]+
            " ranked first. Other brands could still win on price, rights or campaign goals not checked by this ranking."
          :"The current saved evidence does not justify a reliable cultural winner. Unverified alternatives remain open.");
      insight("When to reconsider",
        "Reassess if the candidate pool, artist or audience target changes, a fresh scoped rank conflicts, "+
        "or independently checked commercial terms favour a different option.");
      const missing=run.business_verification?.missing_requirements?.[key];
      const checks=Array.isArray(missing)
        ? [...new Set(missing.filter(x=>requirements.has(x)))].map(x=>requirements.get(x))
        : [];
      const next=checks.length?checks.join("; "):
        "Verify contract rights, full campaign cost and audience-market fit";
      insight("Before committing",next+
        ". No deal, booking or payment is authorized by this review.");
      item.append(insights);
      panel.append(item);
    }
    if(brands.length>3)panel.append(make("p","advisory-footnote",
      "Showing 3 brand decisions; use the decision map and proof for the full recorded plan."));
    panel.append(make("p","advisory-disclaimer",
      "Advice derived from the saved decision record. Rankings do not prove real-world partnership success."));
    return panel;
  };

  // A discovery result is provider-observed, not an OFC-qualified destination.
  const tasteExplorer=(run,pair,mode,onTaste)=>{
    if(mode!=="ON" || !pair || !run._sessionReadConfirmed ||
       typeof onTaste!=="function" || !/^cp-run-[a-f0-9]{32}$/.test(run.run_id||"") ||
       ![...pair.before].some(([key,d])=>String(d.domain).toLowerCase()==="place" &&
         (pair.after.get(key)?.ofc_verdict!=="PASS" ||
          pair.after.get(key)?.ofc_reason!=="QLOO_RANK_RECHECK_MATCHED")))return null;
    const card=make("section","taste-explorer");
    card.setAttribute("aria-label","Discover alternatives based on your taste");
    card.append(make("p","taste-kicker","YOUR NEXT STEP · TASTE EXPLORER"),
      make("h2","taste-title","Want alternatives that fit your taste?"),
      make("p","taste-intro",
        "Answer a few questions. OFC can request a fresh, linked Qloo cultural exploration, " +
        "then show what was actually observed and what remains unverified. No request is sent while you explore."));
    const discovery=run.off?.qloo?.native_discovery;
    if(discovery?.status==="CULTURAL_CANDIDATES_OBSERVED" &&
       discovery?.source_mode==="LIVE_HTTP_UNVERIFIED" &&
       Array.isArray(discovery.candidates)){
      const names=new Set(), options=[];
      let excluded=0;
      const selected=[...pair.before].filter(([,d])=>d.domain==="place")
        .map(([,d])=>String(d.candidate_id||"").toLowerCase());
      for(const raw of discovery.candidates.slice(0,5)){
        const candidate=raw?.name;
        if(typeof candidate!=="string" || !candidate.trim() ||
           candidate.length>160 || names.has(candidate.toLowerCase()) ||
           selected.includes(candidate.toLowerCase()))continue;
        names.add(candidate.toLowerCase());
        // Qloo 'place' can include lodging/food businesses. A name heuristic
        // can exclude obvious mismatches but cannot certify destinations.
        if(/\b(hotel|guest\s*house|guesthouse|cafe|café|resort|hostel|homestay|home\s*stay|lodge|restaurant|motel|villa)\b/i.test(candidate)){
          excluded++;continue;
        }
        options.push(candidate.trim());
      }
      const observed=make("div","taste-observed");
      observed.append(make("strong","","Existing Qloo discovery · destination screening"));
      if(options.length){
        const list=make("ol","taste-observed-list");
        for(const candidate of options)list.append(make("li","",candidate));
        observed.append(list,make("p","taste-small",
          "These are saved Qloo place entities, not qualified hill-station alternatives. " +
          "Destination type, travel suitability and availability remain unverified."));
      }else observed.append(make("p","taste-small",
        "No destination-level alternative was established by this saved Qloo discovery. " +
        "Answer the taste questions to start a narrower exploration."));
      if(excluded)observed.append(make("p","taste-small",
        excluded+" place result(s) looked like lodging or food businesses and were excluded from the destination shortlist."));
      card.append(observed);
    }
    const open=make("button","result-action taste-open","Find alternatives for my taste");
    open.type="button";
    open.setAttribute("aria-expanded","false");
    const form=make("div","taste-form");
    form.hidden=true;
    form.setAttribute("role","group");
    form.setAttribute("aria-label","Your travel taste profile");
    const field=(title,values)=>{
      const wrapper=make("label","taste-field");
      wrapper.append(make("span","",title));
      const select=make("select","taste-select");
      for(const [value,label] of values){
        const option=make("option","",label);
        option.value=value;
        select.append(option);
      }
      wrapper.append(select);
      form.append(wrapper);
      return select;
    };
    const seedField=make("label","taste-field taste-wide");
    seedField.append(make("span","","A place whose atmosphere you like"));
    const seed=make("input","taste-input");
    seed.type="text";seed.maxLength=120;seed.autocomplete="off";
    const savedSeed=run.plan?.taste_seed_name;
    seed.value=typeof savedSeed==="string"&&savedSeed.length<=120?savedSeed:"";
    seed.placeholder="e.g., Manali";
    seedField.append(seed);form.append(seedField);
    const mood=field("What experience do you want?",[
      ["quiet_nature","Quiet mountains & nature"],
      ["snow_views","Snowy scenery"],
      ["adventure","Adventure & hiking"],
      ["cafes_culture","Cafés & local culture"],
      ["family_relaxation","Relaxed family trip"],
    ]);
    const crowds=field("Crowd preference",[
      ["avoid_crowds","Quiet / fewer crowds"],["balanced","Some activity is fine"],
      ["lively","Lively places"],
    ]);
    const budget=field("Spending preference",[
      ["budget","Budget conscious"],["midrange","Mid-range"],
      ["flexible","Flexible budget"],
    ]);
    const monthField=make("label","taste-field");
    monthField.append(make("span","","Travel month (optional)"));
    const month=make("input","taste-input");
    month.type="month";monthField.append(month);form.append(monthField);
    const originField=make("label","taste-field taste-wide");
    originField.append(make("span","","Preferred destination region (optional)"));
    const origin=make("input","taste-input");origin.type="text";origin.maxLength=80;
    origin.autocomplete="off";origin.placeholder="e.g., Delhi, or Himachal Pradesh";
    originField.append(origin);form.append(originField);
    const feedback=make("p","taste-feedback","");
    feedback.setAttribute("role","status");form.append(feedback);
    const submit=make("button","result-action taste-submit","Review my alternative options");
    submit.type="button";
    submit.addEventListener("click",()=>{
      const profile={
        seed:seed.value.trim(),mood:mood.value,crowds:crowds.value,
        budget:budget.value,month:month.value,origin:origin.value.trim()
      };
      if(!profile.seed || profile.seed.length>120 ||
         /[\u0000-\u001f]/.test(profile.seed) ||
         profile.origin.length>80 || /[\u0000-\u001f]/.test(profile.origin)){
        feedback.textContent="Enter a known place reference (maximum 120 characters). No request sent.";
        seed.focus();return;
      }
      feedback.textContent=onTaste(run,profile)
        ?"Review your provider-usage confirmation to continue. No request has been sent yet."
        :"Unable to prepare this review. Check the message near the chat box.";
    });
    form.append(submit,make("p","taste-small",
      "Qloo supports a taste-seed discovery, not verified budget, weather, crowds or lodging filters. " +
      "Preferences guide the review, but alternatives are never labeled 'best' without supporting evidence. " +
      "Only the separate confirmation can start provider calls."));
    open.addEventListener("click",()=>{
      form.hidden=!form.hidden;
      open.setAttribute("aria-expanded",String(!form.hidden));
      if(!form.hidden)seed.focus();
    });
    card.append(open,form);
    return card;
  };

  const renderRun=(run,mode,makeMessage,onTaste)=>{
    if(run.status!=="REVIEW_REQUIRED")return false;
    const answer=mode==="ON"?run.ofc_answer:run.original_answer;
    // Historical model narration may contain Qloo UUIDs; proof stays unchanged.
    const readable=typeof answer==="string" ? answer.replace(
      /\s*\([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\)/gi, ""
    ) : "";
    const assistant=makeMessage("assistant","");
    const section=make("section","result-summary");
    const heading=make("div","result-heading");
    heading.append(make("h2","",mode==="ON"?"Reviewed choices · OFC ON":"Original choices · OFC OFF"),
      make("span","review-state","REVIEW REQUIRED"));
    section.append(heading);
    const pair=pairing(run);
    const body=assistant.querySelector(".message-copy") || assistant;
    const advisory=advisoryPanel(run,pair,mode);
    if(advisory)body.append(advisory);
    const unqualifiedPlace=Boolean(pair && [...pair.before].some(([key,original]) =>
      String(original.domain).toLowerCase()==="place" &&
      (pair.after.get(key)?.ofc_verdict!=="PASS" || !pair.after.get(key)?.rank_recheck_completed)));
    if(unqualifiedPlace && mode==="ON"){
      const boundary=make("section","answer-advisory");
      boundary.append(
        make("p","advisory-eyebrow","OFC safety outcome · saved evidence"),
        make("h2","advisory-title","No verified place winner — recommendation withheld"),
        make("p","advisory-basis",
          "Qloo cultural discovery produced a candidate, but OFC found no qualified repeated ranking for this place decision. "+
          "The original choice was preserved, not endorsed. No destination, lodging availability or booking is approved."),
        make("p","advisory-disclaimer",
          "OFC contributed a documented uncertainty gate here, not a better destination or a selection revision.")
      );
      body.append(boundary);
    }
    const narrative=make("section","narrative-section");
    const placeSummary=unqualifiedPlace
      ? "The saved Qloo discovery identified a place candidate, but the OFC verdict is uncertain and the proof contains no qualified repeated rank. "+
        "The original choice remained unchanged. A discovered cafe, guest house or hotel is not necessarily a hill station. "+
        "Model-described affinity scores or shortlist positions are not verified by the exported proof, so they are not presented as factual recommendations. "+
        "Travel distance, weather, amenities, budget, availability and suitability still need independent verification."
      : readable.trim()?readable:
        "The saved model explanation is unavailable. Use the evidence and controls below.";
    narrative.append(make("h3","",unqualifiedPlace
      ? "Evidence-limited place review · unverified model claims withheld"
      : "Model explanation · not independently verified"),
      make("p","",placeSummary));
    const explorer=tasteExplorer(run,pair,mode,onTaste);
    if(explorer)body.append(explorer);
    body.append(narrative,decisionBrief(run,pair,mode));
    const flow=decisionFlow(run,pair,mode);
    if(flow)body.append(flow);
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
    if(explorer)footer.append(action("Find alternatives for my taste",()=>{
      const trigger=explorer.querySelector(".taste-open");
      if(explorer.querySelector(".taste-form")?.hidden)trigger?.click();
      explorer.scrollIntoView({block:"center"});
    }));
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