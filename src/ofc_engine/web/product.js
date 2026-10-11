"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const label = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const paths = new Set(["home", "plan", "map", "evidence", "history", "provider", "settings"]);
  const names = {home:"Overview",plan:"Plan builder",map:"Decision map",evidence:"Evidence",history:"Draft history",provider:"Provider status",settings:"Settings"};
  const storageKey = "ofc:browser-drafts:v1";
  let current = "";
  let activeDecision = null;
  let lastScenario = "repair";

  const localDrafts = () => {
    try {
      const items = JSON.parse(localStorage.getItem(storageKey) || "[]");
      return Array.isArray(items) ? items.filter(v => v && typeof v.id === "string" && v.form && Array.isArray(v.form.decisions)).slice(0,20) : [];
    } catch (_) { return []; }
  };
  const set = (node, value) => { if (node) node.textContent = String(value); };
  const previewDecisions = () => Array.from($("decision-list").children).map(card => {
    const values = {};
    card.querySelectorAll("[data-key]").forEach(input => {
      values[input.dataset.key] = input.type === "checkbox" ? input.checked : input.value.trim();
    });
    return values;
  });

  function view(name, focus = false) {
    const selected = paths.has(name) ? name : "home";
    current = selected;
    for (const id of paths) {
      const el = $("view-" + id);
      if (el) el.hidden = id !== selected;
    }
    document.querySelectorAll("[data-view-link]").forEach(a => {
      const active = a.dataset.viewLink === selected;
      a.classList.toggle("active", active);
      if (active) a.setAttribute("aria-current","page");
      else a.removeAttribute("aria-current");
    });
    set($("breadcrumb-title"), names[selected]);
    document.title = "OFC Engine · " + names[selected];
    if (selected === "map") refreshMap();
    if (selected === "history") refreshHistory();
    if (selected === "home" || selected === "settings") refreshCounts();
    if (selected === "evidence") loadScenario(lastScenario);
    if (focus) {
      const heading = $("view-" + selected)?.querySelector("h1");
      heading?.setAttribute("tabindex","-1");
      heading?.focus({preventScroll:true});
    }
    window.scrollTo({top:0,behavior:"instant"});
  }
  function navigate(name) {
    location.hash = name;
    view(name, true);
  }
  function refreshCounts() {
    const count = localDrafts().length;
    set($("home-draft-count"),count);
    set($("settings-drafts"),count + (count === 1 ? " draft" : " drafts") + " stored locally");
    set($("history-count"), count);
  }
  function renderMapDetails(decision) {
    const panel = $("map-selected-pool");
    panel.replaceChildren();
    set($("map-selected-title"), decision.decision_id || "Untitled decision");
    set($("map-selected-description"),
      "Selected: " + (decision.candidate_id || "None") +
      ". Dependencies: " + (decision.depends_on || "none") +
      ". All candidates are operator-provided, not ranked by Qloo.");
    const candidates = (decision.candidate_pool || "").split(",").map(s => s.trim()).filter(Boolean);
    const headline=label("div","mini-eyebrow","FROZEN CANDIDATE POOL");
    const list=label("div","candidate-pool");
    for(const candidate of candidates){
      const row=label("span",candidate===decision.candidate_id?"pool-chip selected":"pool-chip",candidate);
      list.append(row);
    }
    panel.append(headline,list);
  }
  function refreshMap() {
    const root=$("map-nodes");
    root.replaceChildren();
    const decisions=previewDecisions();
    if (!decisions.length) {
      const empty=label("div","map-empty");
      empty.append(label("span","empty-symbol","◇"),
        label("strong",null,"Your decisions will connect here."),
        label("p",null,"Start with a plan and candidate choices. No graph is fabricated before you create one."));
      const button=label("button","primary","Create a plan ↗");
      button.type="button";
      button.addEventListener("click",()=>navigate("plan"));
      empty.append(button); root.append(empty);
      set($("map-selected-title"),"No decisions yet");
      set($("map-selected-description"),"Create a plan first.");
      $("map-selected-pool").replaceChildren();return;
    }
    if(!activeDecision || !decisions.some(d=>d.decision_id===activeDecision)) activeDecision=decisions[0].decision_id;
    decisions.forEach((d,index)=>{
      const card=label("button","map-node"+(d.decision_id===activeDecision?" selected":""));
      card.type="button";card.setAttribute("aria-pressed",String(d.decision_id===activeDecision));
      const top=label("div","map-node-head");
      top.append(label("span","map-node-index",String(index+1).padStart(2,"0")),
        label("span","map-node-type",d.domain||"DECISION"),
        label("span","map-node-indicator",d.mutable?"MUTABLE":"LOCKED"));
      card.append(top,label("strong",null,d.decision_id||"Untitled"),label("small",null,d.candidate_id||"No candidate"));
      if (d.depends_on) card.append(label("span","dependency-chip","↳ after " + d.depends_on));
      card.addEventListener("click",()=>{activeDecision=d.decision_id;refreshMap();});
      root.append(card);
    });
    const selected=decisions.find(d=>d.decision_id===activeDecision)||decisions[0];
    renderMapDetails(selected);
  }
  function openDraft(id) {
    const list=localDrafts();
    const index=list.findIndex(d=>d.id===id);
    if(index<0)return;
    $("draft-search").value="";
    $("draft-search").dispatchEvent(new Event("input",{bubbles:true}));
    const buttons=$("draft-list").querySelectorAll(".draft-item");
    if(buttons[index]){buttons[index].click();navigate("plan");}
  }
  function refreshHistory() {
    refreshCounts();
    const q=$("history-search").value.trim().toLowerCase();
    const list=$("history-rows");
    list.replaceChildren();
    const items=localDrafts().filter(d=>[d.form.objective,d.form.audience,d.form.cities]
      .some(s=>String(s||"").toLowerCase().includes(q)));
    if (!items.length) {
      list.append(label("p","history-empty",q?"No matching drafts.":"No saved drafts. Use Plan builder → Save draft to create one."));
      return;
    }
    const heading=label("div","history-row history-row-head");
    ["PLAN / OBJECTIVE","DECISIONS","STORAGE","LAST SAVED","ACTION"].forEach(t=>heading.append(label("span",null,t)));
    list.append(heading);
    for(const d of items){
      const row=label("div","history-row");
      const name=label("strong",null,d.form.objective||"Untitled");
      const sub=label("small",null,d.form.cities||"No locality");
      const nameBox=label("div","history-name");
      nameBox.append(name,sub);
      const btn=label("button","secondary small","Open ↗");
      btn.type="button";btn.addEventListener("click",()=>openDraft(d.id));
      row.append(nameBox,label("span",null,d.form.decisions.length),label("span","history-local","Browser only"),
        label("span",null,d.savedAt&&Number.isFinite(Date.parse(d.savedAt))?new Date(d.savedAt).toLocaleDateString():"—"),btn);
      list.append(row);
    }
  }
  async function loadScenario(scenario) {
    const root=$("evidence-preview");
    lastScenario=scenario;
    document.querySelectorAll(".scenario-choice").forEach(btn=>{
      const active=btn.dataset.scenario===scenario;
      btn.classList.toggle("is-active",active);
      btn.setAttribute("aria-pressed",String(active));
    });
    root.replaceChildren(label("p","evidence-loading","Loading selected fixture…"));
    try {
      const response=await fetch("/api/demo?scenario="+encodeURIComponent(scenario),{cache:"no-store"});
      if(!response.ok) throw Error("Fixture unavailable");
      const data=await response.json();
      if (scenario!==lastScenario)return;
      if(data.scenario!==scenario || data.source_mode!=="SYNTHETIC"||
         data.release_authorized!==false || data.external_action_executed!==false ||
         data.provider_authenticated!==false || data.comparison?.real_causal_proof!==false)
        throw Error("Fixture provenance invalid");
      root.replaceChildren();
      const header=label("div","evidence-result-head");
      header.append(label("span","evidence-badge","SYNTHETIC FIXTURE"),
        label("strong",null,data.run_status.replaceAll("_"," ")));
      const explanation=label("p",null,
        "Evidence OFF: " + data.comparison.without_evidence.replaceAll("_"," ") +
        "  →  Fixture ON: " + data.comparison.with_fixture_evidence.replaceAll("_"," "));
      const cards=label("div","evidence-facts");
      [
        ["DECISIONS",data.decisions.length],
        ["CHANGED",data.comparison.changed_decisions],
        ["CURRENT REVISION","r"+data.current_revision],
        ["ACTION RELEASE","BLOCKED"]
      ].forEach(([name,value])=>{
        const cell=label("div");cell.append(label("small",null,name),label("strong",null,value));cards.append(cell);
      });
      root.append(header,explanation,cards,
        label("p","evidence-fine","Fixture-only structural demonstration. No real provider claim or authorization."));
    } catch (_) {
      root.replaceChildren(label("p","evidence-error","Fixture unavailable or provenance invalid. No verdict is issued."));
    }
  }
  function init() {
    document.querySelectorAll("[data-view-link]").forEach(a=>{
      a.addEventListener("click",e=>{e.preventDefault();navigate(a.dataset.viewLink);});
    });
    for (const id of ["home-create","home-build"]) $(id).addEventListener("click",()=>{
      $("new-draft").click();navigate("plan");$("objective").focus();
    });
    $("home-example").addEventListener("click",()=>{
      $("load-example").click();navigate("plan");
    });
    $("map-edit").addEventListener("click",()=>navigate("plan"));
    $("history-search").addEventListener("input",refreshHistory);
    document.querySelectorAll(".scenario-choice").forEach(button=>
      button.addEventListener("click",()=>loadScenario(button.dataset.scenario)));
    $("global-search").addEventListener("input",e=>{
      $("history-search").value=e.target.value;
      navigate("history");
    });
    document.addEventListener("keydown",e=>{
      if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="k"){
        e.preventDefault();$("global-search").focus();$("global-search").select();
      }
      if(e.key==="Escape"&&document.activeElement===$("global-search"))$("global-search").blur();
    });
    const preference=localStorageSafeGet("ofc:ui-motion");
    $("motion-enabled").checked=preference!=="off";
    motion($("motion-enabled").checked);
    $("motion-enabled").addEventListener("change",()=>{
      const enabled=$("motion-enabled").checked;
      motion(enabled);
      try{localStorage.setItem("ofc:ui-motion",enabled?"on":"off");}catch(_){}
    });
    $("settings-delete").addEventListener("click",()=>{
      $("clear-drafts").click();refreshCounts();refreshHistory();
    });
    $("clear-drafts").addEventListener("click",()=>{queueMicrotask(()=>{refreshCounts();if(current==="history")refreshHistory();});});
    $("save-draft").addEventListener("click",()=>queueMicrotask(refreshCounts));
    $("new-draft").addEventListener("click",()=>{
      if(current!=="plan")navigate("plan");
    });
    $("load-example").addEventListener("click",()=>{
      if(current!=="plan")navigate("plan");
    });
    $("decision-list").addEventListener("input",()=>{if(current==="map")refreshMap();});
    addEventListener("hashchange",()=>view(location.hash.slice(1)));
    view(location.hash.slice(1));
  }
  function localStorageSafeGet(key){try{return localStorage.getItem(key);}catch(_){return null;}}
  function motion(enabled){document.documentElement.classList.toggle("motion-off",!enabled);}
  init();
})();