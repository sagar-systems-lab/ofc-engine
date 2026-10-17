"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const make = (tag, cls = "", value) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (value !== undefined && value !== null) el.textContent = String(value);
    return el;
  };
  const api = async (path, body) => {
    const opts = {credentials: "same-origin", cache: "no-store", headers: {"Accept":"application/json"}};
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    let value;
    try { value = await res.json(); } catch {throw Error("INVALID_SERVER_RESPONSE");}
    if (!res.ok) throw Error(typeof value.error === "string" ? value.error : "HTTP_" + res.status);
    return value;
  };
  const close = dialog => {if (dialog.open) dialog.close();};
  const show = (dialog, invoker) => {
    if (dialog.open) return;
    dialog._invoker = invoker;
    dialog.showModal();
  };
  const labels = {
    repair: ["Selective repair", "One constrained choice is eligible for review"],
    outage: ["Provider outage", "Unverified evidence remains withheld"],
    dependent_missing: ["Missing dependency", "Review depends on required evidence"],
    budget_exhausted: ["Evidence budget", "Maximum recheck attempts constrain the result"],
  };
  const showFeedback = message => {$("settings-owner-feedback").textContent = message;};
  const owner = {connected: false, tests: null, report: null, readVersion: 0};
  const callbacks = {clearSession: () => {}, sessionId: () => null};

  const runDemo = async scenario => {
    const target = $("demo-result");
    target.replaceChildren(make("p", "settings-muted", "Loading a synthetic fixture…"));
    for (const button of document.querySelectorAll("[data-demo]"))
      button.setAttribute("aria-pressed", String(button.dataset.demo === scenario));
    try {
      const record = await api("/api/demo?scenario=" + encodeURIComponent(scenario));
      if (record.scenario !== scenario || record.demo_mode !== "SYNTHETIC" ||
          record.source_mode !== "SYNTHETIC" || record.real_qloo_requests > 0 ||
          record.provider_authenticated === true) throw Error("INVALID_FIXTURE");
      target.replaceChildren();
      target.append(make("span", "demo-label", "SYNTHETIC · NO PROVIDER CALLS"));
      target.append(make("h3", "", labels[scenario][0]));
      target.append(make("p", "", labels[scenario][1]));
      const rows = make("dl", "demo-facts");
      for (const [title, value] of [
        ["Recorded status", record.run_status || "UNAVAILABLE"],
        ["Selection revisions", Array.isArray(record.decisions) ?
          record.decisions.filter(x => x.changed === true).length : "Unavailable"],
        ["Pending rechecks", Array.isArray(record.pending_rechecks) ?
          record.pending_rechecks.length : "Unavailable"],
        ["Real external calls", "0"],
      ]) {
        const row = make("div");
        row.append(make("dt", "", title),make("dd", "", value));
        rows.append(row);
      }
      target.append(rows);
      target.append(make("p", "demo-warning",
        "This is a recorded simulation. It is not a provider-authenticated Qloo result or a live Copilot run."));
    } catch {
      target.replaceChildren(make("p", "settings-error", "Synthetic example is temporarily unavailable."));
    }
  };

  const showSection = name => {
    for (const panel of document.querySelectorAll(".settings-panel"))
      panel.hidden = panel.id !== "settings-" + name;
    for (const btn of document.querySelectorAll("[data-settings-tab]")) {
      const active = btn.dataset.settingsTab === name;
      if (active) btn.setAttribute("aria-current", "page");
      else btn.removeAttribute("aria-current");
    }
    if (name === "connections") void loadOwnerStatus();
  };

  const publicStatus = async () => {
    try {
      const caps = await api("/api/capabilities");
      const target = $("settings-public-status");
      target.replaceChildren();
      for (const [label,key] of [["Qloo Taste AI","qloo"],["Selected LLM","llm"],
        ["OFC policy","ofc"],["Storage","storage"]]) {
        const row=make("div","settings-public-row");
        row.append(make("span","",label),make("strong","",String(caps[key] || "UNKNOWN").replaceAll("_"," ")));
        target.append(row);
      }
    } catch {
      $("settings-public-status").textContent = "Read-only capability status is unavailable.";
    }
  };

  const renderOwner = report => {
    owner.report=report;
    owner.tests=report.connection_tests || null;
    owner.connected=true;
    $("settings-owner-gate").hidden=true;
    $("settings-owner-manager").hidden=false;
    const budget=owner.tests;
    $("settings-test-quota").textContent = budget && Number.isInteger(budget.remaining)
      ? "Connection tests remaining: " + budget.remaining + " / " + budget.limit +
        ". Resets " + (budget.resets_at_utc || "next UTC day") + "."
      : "Connection-test budget could not be verified; tests are disabled.";
    const place=$("settings-owner-providers");
    place.replaceChildren();
    for (const [key,label] of [["qloo","Qloo Taste AI"],["gemini","Gemini"],["groq","Groq fallback"]]) {
      const value=report.providers?.[key] || {};
      const card=make("article","settings-provider");
      card.dataset.provider=key;
      const head=make("div","settings-provider-head");
      head.append(make("strong","",label),make("span","settings-provider-state",
        String(value.status || "NOT_CONFIGURED").replaceAll("_"," ") +
          (value.qualification==="CONNECTIVITY_ONLY"?" · API only":"")));
      card.append(head);
      if(value.masked_suffix) card.append(make("p","settings-muted","Saved key: ••••"+String(value.masked_suffix).slice(0,8)));
      const last=value.last_test;
      if(last?.checked_at) card.append(make("p","settings-muted",
        "Last test: "+String(last.outcome || "unknown")+" · "+String(last.freshness || "UNKNOWN") +
        " · "+new Date(last.checked_at).toLocaleString()));
      const input=make("input","settings-key");
      input.type="password";input.autocomplete="new-password";input.spellcheck=false;
      input.placeholder="Paste new API key";input.setAttribute("aria-label",label+" API key");
      card.append(input);
      const actions=make("div","settings-provider-actions");
      for (const [type,caption] of [["save","Save encrypted"],["test","Test connection"],["revoke","Revoke"]]) {
        const b=make("button","secondary-button",caption);
        b.type="button";b.dataset.ownerAction=type;b.dataset.provider=key;
        if(type==="test")b.disabled=!(budget && Number.isInteger(budget.remaining) && budget.remaining>0 && value.configured);
        if(type==="revoke")b.disabled=!value.configured;
        actions.append(b);
      }
      card.append(actions);
      place.append(card);
    }
  };
  const lockOwner = (clearInput = true) => {
    owner.connected=false;owner.report=null;owner.tests=null;
    $("settings-owner-manager").hidden=true;
    $("settings-owner-gate").hidden=false;
    $("settings-owner-providers").replaceChildren();
    if(clearInput)$("settings-owner-secret").value="";
  };
  const loadOwnerStatus=async()=>{
    const version=++owner.readVersion;
    try {
      const report=await api("/api/owner/credentials");
      if(version===owner.readVersion)renderOwner(report);
    } catch {
      if(version===owner.readVersion)lockOwner(false);
    }
  };
  const ownerAction=async(button)=>{
    const provider=button.dataset.provider,action=button.dataset.ownerAction;
    const card=button.closest(".settings-provider");
    const input=card.querySelector(".settings-key");
    let body,path;
    if(action==="save") {
      if(!input.value.trim()) {showFeedback("Paste an API key first.");return;}
      path="/api/owner/credentials";
      body={action:"set",provider,api_key:input.value};
    } else if(action==="revoke") {
      if(!window.confirm("Revoke the stored "+provider+" key? This cannot be undone."))return;
      path="/api/owner/credentials";body={action:"revoke",provider};
    } else if(action==="test") {
      if(!owner.tests || !Number.isInteger(owner.tests.remaining) || owner.tests.remaining<=0){
        showFeedback("Connection test allowance unavailable or exhausted. No provider request made.");return;
      }
      if(!window.confirm("Authorize ONE real "+provider+
        " provider request? This spends API quota and one of "+
        owner.tests.remaining+" remaining connection tests. No automatic retry."))return;
      path="/api/owner/test-connection";
      body={provider,confirm:"SPEND_ONE_PROVIDER_REQUEST"};
    } else return;
    button.disabled=true;
    try {
      const result=await api(path,body);
      if(action==="save"||action==="revoke"){
        renderOwner(result);
        showFeedback(provider+" credentials updated on server. No provider test performed.");
      } else {
        showFeedback(provider+" connection test returned: "+String(result.status || "RESULT_RECORDED")+
          ". See saved verification details below.");
        await loadOwnerStatus();
      }
    } catch(err) {
      showFeedback(String(err.message || "Operation unavailable").slice(0,85)+
        ". No automatic retry performed.");
      await loadOwnerStatus();
    } finally {
      input.value="";
      button.disabled=false;
    }
  };

  const reduceMotion=()=>document.body.classList.toggle("workspace-reduced-motion",$("settings-reduce-motion").checked);
  const applyStoredMotion=()=>{
    try {$("settings-reduce-motion").checked=localStorage.getItem("ofc.workspace.motion.reduce")==="1";}catch{}
    reduceMotion();
  };
  const mount=config=>{
    callbacks.clearSession=config.clearSession;
    callbacks.sessionId=config.sessionId;
    $("demo-entry").addEventListener("click",e=>{
      show($("demo-dialog"),e.currentTarget);
    });
    $("settings-entry").addEventListener("click",e=>{
      show($("settings-dialog"),e.currentTarget);
      showSection("connections");
      void publicStatus();
    });
    for(const [dialog,closeBtn] of [["demo-dialog","demo-close"],["settings-dialog","settings-close"]]) {
      const element=$(dialog);
      $(closeBtn).addEventListener("click",()=>close(element));
      element.addEventListener("close",()=>{
        if(element._invoker?.isConnected)element._invoker.focus();
        if(dialog==="settings-dialog")$("settings-owner-secret").value="";
      });
    }
    for(const b of document.querySelectorAll("[data-demo]"))
      b.addEventListener("click",()=>void runDemo(b.dataset.demo));
    for(const b of document.querySelectorAll("[data-settings-tab]"))
      b.addEventListener("click",()=>showSection(b.dataset.settingsTab));
    const signIn=async()=>{
      const input=$("settings-owner-secret"),secret=input.value;
      input.value="";
      if(secret.length<40){
        showFeedback("Owner secret must contain at least 40 characters.");
        return;
      }
      try {
        owner.readVersion++;
        await api("/api/owner/login",{owner_secret:secret});
        await loadOwnerStatus();
        if(owner.connected)showFeedback("Owner connection controls unlocked.");
        else showFeedback("Owner login response received, but connection status could not be verified.");
      } catch {showFeedback("Owner sign-in failed. Check your private owner secret.");}
    };
    $("settings-owner-form").addEventListener("submit",e=>{
      e.preventDefault();
      void signIn();
    });
    $("settings-owner-unlock").addEventListener("click",()=>void signIn());
    $("settings-owner-providers").addEventListener("click",event=>{
      const b=event.target.closest("[data-owner-action]");
      if(b && !b.disabled)void ownerAction(b);
    });
    $("settings-owner-logout").addEventListener("click",async()=>{
      try {await api("/api/owner/logout",{});} catch {}
      lockOwner();showFeedback("Owner connections locked.");
    });
    $("settings-reduce-motion").addEventListener("change",()=>{
      reduceMotion();
      try{localStorage.setItem("ofc.workspace.motion.reduce",$("settings-reduce-motion").checked?"1":"0");}catch{}
    });
    $("settings-delete-session").addEventListener("click",async()=>{
      const id=callbacks.sessionId();
      if(!id){showFeedback("No active visitor session to delete.");return;}
      if(!window.confirm("Permanently delete this visitor session and all its saved runs?"))return;
      try{
        await api("/api/copilot/sessions/"+id,{confirm:"DELETE_MY_SESSION"});
        callbacks.clearSession();
        showFeedback("Private visitor session deleted.");
      }catch(err){showFeedback("Deletion unavailable: "+String(err.message).slice(0,65));}
    });
    applyStoredMotion();
  };
  window.OFCTools=Object.freeze({mount});
})();
