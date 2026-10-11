"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const make = (tag, cls, value) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (value !== undefined) node.textContent = String(value);
    return node;
  };
  const KEY = "ofc:browser-drafts:v1";
  const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
  const uuid = (prefix) => prefix + "-" + crypto.randomUUID();
  const split = (s) => String(s || "").split(",").map((s) => s.trim()).filter(Boolean);
  const seed = () => ({
    objective: "Choose partners and experiences for an emerging artist's three-city tour.",
    budget: "60000", cities: "Chicago, Austin, Seattle", audience: "Indie music audiences",
    decisions: [
      {decision_id:"brand",domain:"partner",candidate_id:"brand-a",candidate_pool:"brand-a, brand-b",depends_on:"",locality_ref:"Chicago",mutable:true,required:true},
      {decision_id:"afterparty",domain:"experience",candidate_id:"experience-a",candidate_pool:"experience-a, experience-b",depends_on:"brand",locality_ref:"Chicago",mutable:true,required:true},
      {decision_id:"opener",domain:"performer",candidate_id:"opener-a",candidate_pool:"opener-a, opener-b",depends_on:"",locality_ref:"Austin",mutable:true,required:true},
    ]
  });
  let draftId = uuid("draft"), runId = uuid("run"), requestId = uuid("request"), validated = null;
  let drafts = readDrafts();

  function readDrafts() {
    try {
      const v = JSON.parse(localStorage.getItem(KEY) || "[]");
      return Array.isArray(v) ? v.filter((x) => x && typeof x.id === "string" && x.form && Array.isArray(x.form.decisions)).slice(0,20) : [];
    } catch (_) { return []; }
  }
  function message(msg, kind) {
    $("editor-message").textContent = msg;
    $("editor-message").className = "editor-message" + (kind ? " " + kind : "");
  }
  function formValue() {
    return {
      objective: $("objective").value.trim(), budget: $("budget").value.trim(),
      audience: $("audience").value.trim(), cities: $("cities").value.trim(),
      decisions: Array.from($("decision-list").children, (card) => {
        const item = {};
        card.querySelectorAll("[data-key]").forEach((input) => {
          item[input.dataset.key] = input.type === "checkbox" ? input.checked : input.value.trim();
        });
        return item;
      })
    };
  }
  function invalidate() {
    validated = null;
    $("export-proposal").disabled = true;
    $("proposal-state").textContent = "Not validated";
    $("proposal-detail").textContent = "Changes need a fresh local contract check.";
    const values = formValue().decisions;
    $("decision-count").textContent = values.length;
    $("mutable-count").textContent = values.filter((d) => d.mutable).length;
  }
  function inputField(parent, title, key, value, index) {
    const box = make("div","decision-cell");
    const label = make("label",null,title), input = make("input");
    input.type = "text";
    input.dataset.key = key;
    input.maxLength = key === "candidate_pool" ? 1000 : 240;
    input.id = "decision-" + index + "-" + key;
    input.value = value || "";
    label.htmlFor = input.id;
    input.addEventListener("input", invalidate);
    box.append(label,input); parent.append(box);
  }
  function checkbox(parent, title, key, checked) {
    const label = make("label"), input = make("input");
    input.type = "checkbox"; input.dataset.key = key; input.checked = Boolean(checked);
    input.addEventListener("change", invalidate);
    label.append(input,document.createTextNode(title)); parent.append(label);
  }
  function addDecision(data = {}) {
    const list = $("decision-list");
    if(list.children.length >= 64) {message("The contract permits at most 64 decisions.","error"); return;}
    const n = list.children.length, card=make("article","decision-card");
    const head=make("div","decision-head"), grid=make("div","decision-grid"), foot=make("div","decision-footer");
    head.append(make("strong",null,"DECISION / "+String(n+1).padStart(2,"0")));
    const remove=make("button","remove-decision","Remove");
    remove.type="button";remove.setAttribute("aria-label","Remove decision "+(n+1));
    remove.addEventListener("click",()=>{card.remove();renumber();invalidate();});
    head.append(remove);
    [
      ["Decision ID","decision_id"],["Domain","domain"],
      ["Selected candidate ID","candidate_id"],
      ["Candidate pool (comma-separated)","candidate_pool"],
      ["Locality","locality_ref"],["Depends on (decision IDs)","depends_on"]
    ].forEach(([name,key])=>inputField(grid,name,key,data[key],n));
    checkbox(foot,"Can repair candidate","mutable",data.mutable !== false);
    checkbox(foot,"Required","required",data.required !== false);
    card.append(head,grid,foot); list.append(card); invalidate();
  }
  function renumber() {
    Array.from($("decision-list").children).forEach((card,index)=>{
      card.querySelector(".decision-head strong").textContent="DECISION / "+String(index+1).padStart(2,"0");
      card.querySelector(".remove-decision").setAttribute("aria-label","Remove decision "+(index+1));
    });
  }
  function populate(form) {
    ["objective","budget","cities","audience"].forEach((id)=>{$(id).value=form[id]||"";});
    $("decision-list").replaceChildren();
    (form.decisions || []).slice(0,64).forEach(addDecision);
    invalidate();
  }
  function build(form, id=runId, request=requestId) {
    if(typeof form.objective!=="string" || !form.objective.trim() || form.objective.length>2048)
      throw Error("Provide a non-empty objective of at most 2048 characters.");
    const cities=split(form.cities);
    if(!cities.length || cities.length>12 || new Set(cities).size!==cities.length) throw Error("Enter 1–12 unique cities.");
    if(!/^\d+$/.test(form.budget) || !Number.isSafeInteger(Number(form.budget)) || Number(form.budget)>1e12)
      throw Error("Budget must be a non-negative whole number.");
    if(!form.decisions.length || form.decisions.length>64) throw Error("Include 1–64 decisions.");
    const ids=new Set();
    const decisions=form.decisions.map((d,index)=>{
      if(!ID.test(d.decision_id||"") || typeof d.domain!=="string" || !d.domain.trim())
        throw Error("Decision "+(index+1)+": invalid ID or domain.");
      if(ids.has(d.decision_id)) throw Error("Duplicate decision ID: "+d.decision_id);
      ids.add(d.decision_id);
      const pool=split(d.candidate_pool);
      if(!pool.length || pool.length>64 || new Set(pool).size!==pool.length || pool.some((v)=>!ID.test(v)))
        throw Error("Decision "+d.decision_id+": provide 1–64 unique valid candidate IDs.");
      if(!pool.includes(d.candidate_id)) throw Error("Decision "+d.decision_id+": selected candidate is outside pool.");
      const deps=split(d.depends_on);
      if(new Set(deps).size!==deps.length) throw Error("Decision "+d.decision_id+": duplicate dependencies.");
      return {decision_id:d.decision_id,domain:d.domain,candidate_id:d.candidate_id,
        candidate_pool:pool,mutable_fields:d.mutable?["candidate_id"]:[],
        depends_on:deps,locality_ref:d.locality_ref||null,required:Boolean(d.required)};
    });
    const byId=new Map(decisions.map((d)=>[d.decision_id,d])),visiting=new Set(),visited=new Set();
    const check=(id)=>{
      if(visiting.has(id))throw Error("Cyclic dependency at "+id);
      if(visited.has(id))return;
      visiting.add(id);
      byId.get(id).depends_on.forEach((dep)=>{
        if(!byId.has(dep))throw Error("Unknown dependency: "+dep);
        check(dep);
      });
      visiting.delete(id);visited.add(id);
    };
    decisions.forEach((d)=>check(d.decision_id));
    const proposal={intent:{schema_version:"0.1.0",run_id:id,client_request_id:request,
      objective:form.objective,constraints:{budget_usd:Number(form.budget),cities},
      taste_context:{audience:form.audience,locality:cities.join(", ")}},decisions};
    if(JSON.stringify(proposal).length>96*1024)throw Error("Proposal exceeds 96 KiB limit.");
    return proposal;
  }
  function validate() {
    try {
      validated=build(formValue());
      $("export-proposal").disabled=false;
      $("proposal-state").textContent="Contract valid";
      $("proposal-detail").textContent="Input shape checked. Provider evidence still unavailable.";
      message("Proposal structure validated in this browser. Export for the authenticated local intake; no cultural evaluation ran.","ok");
    } catch(error) {
      invalidate();
      $("proposal-state").textContent="Needs correction";
      $("proposal-detail").textContent="Fix the contract issue before export.";
      message(error.message,"error");
    }
  }
  function displayDrafts() {
    const list=$("draft-list"),q=$("draft-search").value.trim().toLowerCase();
    list.replaceChildren();$("draft-count").textContent=drafts.length;
    const shown=drafts.filter((d)=>[d.form.objective,d.form.audience,d.form.cities].some((v)=>String(v||"").toLowerCase().includes(q)));
    if(!shown.length)list.append(make("p","empty-drafts",drafts.length?"No matching drafts.":"No saved drafts yet."));
    shown.forEach((d)=>{
      const b=make("button","draft-item"+(d.id===draftId?" selected":""));
      b.type="button";
      b.append(make("strong",null,d.form.objective||"Untitled"),make("small",null,new Date(d.savedAt).toLocaleString()));
      b.addEventListener("click",()=>{
        draftId=d.id;runId=d.runId;requestId=d.requestId;
        populate(d.form);displayDrafts();message("Saved browser-local draft opened. Validate before export.");
      });
      list.append(b);
    });
  }
  function fresh() {
    draftId=uuid("draft");runId=uuid("run");requestId=uuid("request");
    populate(seed());displayDrafts();
    message("Editable example loaded. Choices are operator-provided placeholders, not Qloo evidence.");
  }
  function save() {
    const form=formValue();
    if(!form.objective&&!form.decisions.length){message("Add an objective or decision before saving.","error");return;}
    drafts=[{id:draftId,runId,requestId,form,savedAt:new Date().toISOString()},
      ...drafts.filter((v)=>v.id!==draftId)].slice(0,20);
    try {
      localStorage.setItem(KEY,JSON.stringify(drafts));
      displayDrafts();message("Draft saved in this browser only. Nothing was sent to the server.","ok");
    } catch (_) {message("Browser storage unavailable or full; export the validated JSON instead.","error");}
  }
  function exportFile() {
    if(!validated)return;
    const url=URL.createObjectURL(new Blob([JSON.stringify(validated,null,2)+"\n"],{type:"application/json"}));
    const link=make("a");link.href=url;link.download="ofc-proposal-"+runId+".json";
    link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  async function importFile(file) {
    if(!file)return;
    try {
      if(file.size>96*1024)throw Error("Import exceeds 96 KiB.");
      const data=JSON.parse(await file.text());
      if(!data||!data.intent||!Array.isArray(data.decisions)||data.intent.schema_version!=="0.1.0"||
          !ID.test(data.intent.run_id||"")||!ID.test(data.intent.client_request_id||""))
        throw Error("Expected a typed OFC agent proposal.");
      const i=data.intent,c=i.constraints||{};
      const form={objective:i.objective,budget:String(c.budget_usd??""),
        cities:Array.isArray(c.cities)?c.cities.join(", "):"",
        audience:typeof i.taste_context?.audience==="string"?i.taste_context.audience:"",
        decisions:data.decisions.map((d)=>({decision_id:d.decision_id,domain:d.domain,
          candidate_id:d.candidate_id,candidate_pool:Array.isArray(d.candidate_pool)?d.candidate_pool.join(", "):"",
          depends_on:Array.isArray(d.depends_on)?d.depends_on.join(", "):"",
          locality_ref:d.locality_ref||"",mutable:Array.isArray(d.mutable_fields)&&d.mutable_fields.includes("candidate_id"),
          required:d.required!==false}))};
      build(form,i.run_id,i.client_request_id);
      draftId=uuid("draft");runId=i.run_id;requestId=i.client_request_id;populate(form);
      message("Proposal imported. Validate before saving or exporting.","ok");
    } catch(error){message(error.message||"Invalid import.","error");}
  }
  ["objective","budget","cities","audience"].forEach((id)=>$(id).addEventListener("input",invalidate));
  $("new-draft").addEventListener("click",()=>{fresh();$("objective").focus();});
  $("add-decision").addEventListener("click",()=>addDecision());
  $("validate").addEventListener("click",validate);
  $("save-draft").addEventListener("click",save);
  $("export-proposal").addEventListener("click",exportFile);
  $("draft-search").addEventListener("input",displayDrafts);
  $("import-file").addEventListener("change",(event)=>{importFile(event.target.files[0]);event.target.value="";});
  $("clear-drafts").addEventListener("click",()=>{
    if(!drafts.length||!confirm("Delete all drafts stored in this browser?"))return;
    try{localStorage.removeItem(KEY);}catch(_){message("Unable to clear browser storage.","error");return;}
    drafts=[];displayDrafts();message("Browser-local saved drafts deleted.");
  });
  fresh();
})();