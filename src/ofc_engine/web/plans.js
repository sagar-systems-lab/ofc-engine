"use strict";

import {ControlClient, checkEnvelope} from "/control-sdk.js";

const $ = id => document.getElementById(id);
const client = new ControlClient();
const state = {request: null, proof: null, selected: null, busy: false, view: "impact", verified: false};
const causal = new Set(["REQUIRES", "APPROVAL_REQUIRES", "TEMPORAL_BEFORE"]);
const pretty = value => value.toLowerCase().replaceAll("_", " ").replace(/^./, c => c.toUpperCase());
const make = (tag, className = "", text = "") => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== "") element.textContent = String(text);
  return element;
};
const candidate = (node, id) => node.candidate_pool.find(c => c.id === id)?.label || id;
const nodeById = id => state.proof.current.nodes.find(node => node.id === id);

function notice(message, error = false) {
  $("notice").hidden = !message;
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
}

function busy(value) {
  state.busy = value;
  $("run-review").disabled = value;
  $("edit-plan").disabled = value;
  $("apply-plan").disabled = value;
  $("scenario").disabled = value;
  $("original").disabled = value;
  $("run-review").textContent = value ? "Reviewing…" : "Review plan ↗";
  $("main").setAttribute("aria-busy", String(value));
}

function card(node, compact = false) {
  const proof = state.proof;
  const changed = proof.impact.changed.includes(node.id);
  const reopened = proof.impact.invalidated.includes(node.id);
  const button = make("button", "decision" + (changed ? " changed" : reopened ? " reopened" : ""));
  button.type = "button";
  button.dataset.nodeId = node.id;
  button.setAttribute("aria-label", `${node.id}: ${node.label}. ${changed ? "Changed" : reopened ? "Dependent check reopened" : "Choice untouched"}`);
  const top = make("span", "node-top");
  top.append(make("span", "", node.id), make("span", "", changed ? "CHANGED" : reopened ? "RECHECK" : "UNCHANGED"));
  button.append(top, make("span", "node-label", node.label));
  const choice = make("span", "node-choice");
  if (changed) {
    const before = proof.baseline.nodes.find(n => n.id === node.id);
    choice.append(make("span", "node-old", candidate(before, before.candidate_id)), make("span", "", "→ " + candidate(node, node.candidate_id)));
  } else choice.textContent = compact ? node.locality || node.domain : candidate(node, node.candidate_id);
  if (compact || changed || candidate(node, node.candidate_id) !== node.label) button.append(choice);
  if (!compact && node.locality) button.append(make("span", "node-city", node.locality));
  button.addEventListener("click", () => select(node.id));
  return button;
}

function renderGraph() {
  const proof = state.proof;
  const graph = $("graph");
  graph.replaceChildren();
  const connected = new Set(proof.current.edges.flatMap(edge => [edge.source, edge.target]));
  proof.current.nodes.filter(node => node.domain === "brand").forEach(node => connected.add(node.id));
  if (!connected.size) connected.add(proof.current.nodes[0].id);
  const active = proof.current.nodes.filter(node => connected.has(node.id));
  const levels = new Map(active.map(node => [node.id, 0]));
  for (let i = 0; i < active.length; i++) {
    let changed = false;
    for (const edge of proof.current.edges) {
      if (causal.has(edge.type) && levels.has(edge.source) && levels.has(edge.target) && levels.get(edge.target) <= levels.get(edge.source)) {
        levels.set(edge.target, levels.get(edge.source) + 1);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const maxLevel = Math.min(4, Math.max(...levels.values()));
  graph.style.gridTemplateColumns = `repeat(${maxLevel + 1}, minmax(145px, 1fr))`;
  graph.style.minWidth = `${Math.max(590, (maxLevel + 1) * 190)}px`;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "graph-lines");
  svg.setAttribute("aria-hidden", "true");
  graph.append(svg);
  for (let depth = 0; depth <= maxLevel; depth++) {
    const column = make("div", "graph-column");
    column.append(make("span", "graph-column-label", depth === 0 ? "PROPOSED DECISIONS" : depth === 1 ? "DIRECT DEPENDENCIES" : "DOWNSTREAM OBLIGATIONS"));
    for (const node of active.filter(node => Math.min(maxLevel, levels.get(node.id)) === depth)) column.append(card(node));
    graph.append(column);
  }
  const independents = proof.current.nodes.filter(node => !connected.has(node.id));
  $("independent-title").textContent = `${independents.length} decisions outside this dependency component`;
  $("independent-grid").replaceChildren(...independents.map(node => card(node, true)));
  document.querySelector(".independent").hidden = !independents.length;
  requestAnimationFrame(drawEdges);
}

function drawEdges() {
  if (!state.proof || state.view !== "impact") return;
  const graph = $("graph"), svg = graph.querySelector("svg");
  if (!svg) return;
  svg.replaceChildren();
  const bounds = graph.getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${bounds.width} ${bounds.height}`);
  const path = state.proof.impact.paths[state.selected] || [];
  for (const edge of state.proof.current.edges) {
    const left = Array.from(graph.querySelectorAll(".decision")).find(n => n.dataset.nodeId === edge.source);
    const right = Array.from(graph.querySelectorAll(".decision")).find(n => n.dataset.nodeId === edge.target);
    if (!left || !right) continue;
    const a = left.getBoundingClientRect(), b = right.getBoundingClientRect();
    const x1 = a.right - bounds.left, y1 = a.top + a.height / 2 - bounds.top;
    const x2 = b.left - bounds.left, y2 = b.top + b.height / 2 - bounds.top;
    const line = document.createElementNS(svg.namespaceURI, "path");
    const active = path.some((key, index) => key === edge.source && path[index + 1] === edge.target);
    line.setAttribute("d", `M ${x1} ${y1} C ${x1 + 22} ${y1}, ${x2 - 22} ${y2}, ${x2} ${y2}`);
    line.setAttribute("class", "edge-path" + (state.proof.impact.invalidated.includes(edge.target) ? " affected" : "") + (active ? " active" : ""));
    if (!causal.has(edge.type)) line.setAttribute("stroke-dasharray", "3 4");
    svg.append(line);
  }
}

function select(id) {
  state.selected = id;
  for (const element of document.querySelectorAll(".decision")) {
    element.classList.toggle("selected", element.dataset.nodeId === id);
    element.setAttribute("aria-pressed", String(element.dataset.nodeId === id));
  }
  const node = nodeById(id), verdict = state.proof.verdicts[id];
  const inspector = $("inspector");
  inspector.replaceChildren(make("div", "inspector-label", "DECISION " + id), make("h2", "", node.label), make("p", "inspector-sub", [node.domain, node.locality].filter(Boolean).join(" · ")));
  const choice = make("div", "selected-choice", "Current selection");
  choice.append(make("strong", "", candidate(node, node.candidate_id)));
  inspector.append(choice, make("h3", "", "Evidence & obligations"));
  if (!verdict.claims.length) inspector.append(make("p", "claim-reason", "No claim policy is defined for this decision."));
  for (const claim of verdict.claims) {
    const block = make("div", "claim-card");
    const status = claim.status === "QUALIFIED" ? (state.proof.mode === "SYNTHETIC" ? "Qualified in synthetic policy test" : "Qualified by scoped policy") : pretty(claim.status);
    block.append(make("strong", "", pretty(claim.claim)), make("span", "claim-state" + (claim.status === "QUALIFIED" ? " qualified" : ""), status), make("p", "claim-reason", pretty(claim.reason)));
    inspector.append(block);
  }
  const path = state.proof.impact.paths[id];
  inspector.append(make("h3", "", path ? "Why this decision is affected" : "Dependency scope"));
  inspector.append(make("p", "dependency-path", path ? path.join(" → ") : "No changed upstream input reaches this decision."));
  if (verdict.upstream_blockers.length) inspector.append(make("p", "claim-reason", "Unresolved parents: " + verdict.upstream_blockers.join(", ")));
  const warning = make("div", "action-warning");
  warning.append(make("strong", "", "Action withheld"), make("span", "", "Selections do not grant booking, payment, or contractual authority."));
  inspector.append(warning);
  drawEdges();
}

function renderCompare() {
  const proof = state.proof;
  const rows = proof.current.nodes.map(node => {
    const row = make("tr", proof.impact.changed.includes(node.id) ? "changed-row" : "");
    const name = make("td");
    name.append(make("strong", "", node.label), make("small", "", node.id));
    row.append(name);
    for (const arm of ["original", "static_rank_rule", "controlled"]) row.append(make("td", "", candidate(node, proof.comparison[arm][node.id])));
    row.append(make("td", "", proof.impact.changed.includes(node.id) ? "Changed · action withheld" : proof.impact.invalidated.includes(node.id) ? "Reopened · unresolved" : "Selection untouched"));
    return row;
  });
  $("comparison-body").replaceChildren(...rows);
}

function renderEvidence() {
  const proof = state.proof;
  $("evidence-note").textContent = proof.mode === "SYNTHETIC" ? "Synthetic observations test the policy. Repeated observations from one source do not constitute independent source validation." : "Private normalized observations are replayed from saved captures or signed attestations. They are not new provider calls and must not be redistributed without permission.";
  const records = proof.evidence.map(item => {
    const block = make("article", "ledger-card");
    block.append(make("h3", "", pretty(item.claim) + " · " + item.node_id), make("p", "", `Source: ${item.source} · ${item.mode} · ${item.license_scope.replaceAll("_", " ")}`));
    if (item.ranks.length) {
      const list = make("ol", "rank-list");
      const node = nodeById(item.node_id);
      item.ranks.forEach((key, index) => list.append(make("li", "", `${index + 1}. ${candidate(node, key)}`)));
      block.append(list);
    }
    const meta = make("div", "ledger-meta");
    meta.append(make("span", "", "Attempt: " + item.attempt_id), make("span", "", "Observed: " + item.observed_at), make("span", "", "Expires: " + item.expires_at));
    block.append(meta, make("p", "", "Read-set fingerprint: " + item.input_digest));
    return block;
  });
  $("evidence-ledger").replaceChildren(...(records.length ? records : [make("p", "muted", "No evidence was supplied. The engine cannot qualify the requested claims.")]));
}

function renderProof() {
  const proof = state.proof;
  const facts = [["Control boundary", "WITHHELD · no external action"], ["Source mode", proof.mode], ["Assessed at", proof.as_of], ["Revision", "0 → " + proof.revision], ["Baseline digest", proof.baseline_digest], ["Reviewed digest", proof.current_digest], ["Policy digest", proof.policy_digest], ["Evidence root", proof.evidence_root], ["Journal head", proof.journal_head], ["Proof digest", proof.proof_digest], ["Browser check", state.verified ? "Hash and journal consistency checked" : "Not checked"], ["Signature", "Unsigned control record"], ["Unresolved decisions", proof.unresolved.length]];
  $("proof-facts").replaceChildren(...facts.map(([label, value]) => {
    const row = make("div", "proof-row");
    row.append(make("span", "", label), make("code", "", value));
    return row;
  }));
  $("verify-command").textContent = "python -m ofc_engine.control.verifier saved-proof.json --expected-digest " + proof.proof_digest + (proof.mode === "PRIVATE" ? " --authorities trusted-authorities.json" : "");
}

function renderTrace() {
  const proof = state.proof;
  const descriptions = {FROZEN: ["Plan frozen", `${proof.current.nodes.length} choices and exact candidate pools`], EVALUATED: ["Evidence assessed", `${proof.evidence.length} scoped observations`], PATCH_COMMITTED: ["Patch committed", `${proof.impact.changed.length} allowed candidate change${proof.impact.changed.length === 1 ? "" : "s"}`], INVALIDATED: ["Checks reopened", `${proof.impact.invalidated.length} dependent obligations`], RECHECKED: ["Policy rechecked", "Unsupported claims remain unresolved"], HANDOFF_WITHHELD: ["Action withheld", "No payment, booking, or release"]};
  $("trace").replaceChildren(...proof.events.map(event => {
    const line = make("li");
    const [title, description] = descriptions[event.kind] || [pretty(event.kind), ""];
    line.append(make("strong", "", title), make("span", "", description));
    return line;
  }));
  const findings = proof.constraints.filter(item => item.status !== "PASS").map(item => ({item, prefix: "Current plan"}));
  for (const rejected of proof.optimization.rejected_alternatives || []) {
    for (const item of rejected.constraints) findings.push({item, prefix: "Alternative " + Object.values(rejected.selections).join(", ")});
  }
  $("constraint-panel").hidden = findings.length === 0;
  $("constraint-list").replaceChildren(...findings.slice(0, 8).map(({item, prefix}) => make("li", "", `${prefix}: ${pretty(item.type)} · ${pretty(item.reason || item.status)}${item.currency ? " (" + item.currency + ")" : ""}`)));
}

async function show(proof, elapsed) {
  await checkEnvelope(proof);
  state.proof = proof;
  state.verified = true;
  const changed = proof.impact.changed.length;
  $("changed-count").textContent = changed;
  $("affected-count").textContent = proof.impact.invalidated.length;
  $("preserved-count").textContent = proof.impact.preserved.length;
  $("node-count").textContent = proof.current.nodes.length;
  $("request-count").textContent = proof.provider_calls;
  $("revision-label").textContent = "REVISION 0 → " + proof.revision;
  $("source-badge").textContent = proof.mode === "SYNTHETIC" ? "Synthetic sandbox" : "Private capture replay";
  $("headline").textContent = changed ? `${changed === 1 ? "One change." : changed + " changes."} Every consequence.` : proof.optimization.status === "PRESERVED" ? "A correction must earn its place." : "Uncertain evidence. A clear boundary.";
  $("subtitle").textContent = changed ? `${changed} evidence-supported change${changed === 1 ? "" : "s"}. ${proof.impact.invalidated.length} dependent checks reopened. ${proof.impact.preserved.length} candidate choices untouched. External actions withheld.` : "The original choices remain unchanged. Missing or conflicting evidence cannot become permission to act.";
  $("duration").textContent = `${Math.round(elapsed)} ms local round trip · 0 provider calls`;
  $("footer-source").textContent = `${proof.mode === "SYNTHETIC" ? "Synthetic policy test" : "Saved capture replay"} · no provider requests`;
  $("download-proof").disabled = false;
  renderGraph(); renderCompare(); renderEvidence(); renderTrace();
  select(proof.impact.changed[0] || proof.current.nodes[0].id);
  renderProof();
}

async function run(request) {
  if (state.busy) return;
  busy(true);
  notice("Validating the plan, evaluating evidence, and checking dependency impact…");
  const started = performance.now();
  try {
    const proof = await client.request("/api/control/simulate", request);
    state.request = request;
    await show(proof, performance.now() - started);
    notice("");
  } catch (error) {
    const details = error.fields?.map(field => field.field + ": " + field.code).join("; ");
    const message = "Not submitted. The previous valid review remains displayed. " + (details || error.message);
    notice(message, true);
    $("editor-error").textContent = message;
    $("editor-error").hidden = false;
  } finally { busy(false); }
}

async function loadFixture() {
  if (state.busy) return;
  busy(true);
  try {
    const request = await client.request(`/api/control/examples/${$("scenario").value}/${$("original").value}`);
    busy(false);
    await run(request);
  } catch (error) { notice("Not submitted. " + error.message, true); }
  finally { busy(false); }
}

$("run-review").addEventListener("click", loadFixture);
$("scenario").addEventListener("change", loadFixture);
$("original").addEventListener("change", loadFixture);
$("edit-plan").addEventListener("click", () => {
  $("plan-json").value = JSON.stringify(state.request, null, 2);
  $("editor-error").hidden = true;
  $("editor").showModal();
});
$("apply-plan").addEventListener("click", async () => {
  try {
    const payload = JSON.parse($("plan-json").value);
    await run(payload);
    if (!$("notice").classList.contains("error")) $("editor").close();
  } catch (error) {
    $("editor-error").textContent = "Not submitted. Enter valid JSON before starting a review.";
    $("editor-error").hidden = false;
  }
});
$("download-proof").addEventListener("click", () => {
  if (!state.proof) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(state.proof, null, 2) + "\n"], {type: "application/json"}));
  const link = make("a");
  link.href = url;
  link.download = `ofc-review-${state.proof.baseline_digest.slice(0, 12)}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

const views = ["impact", "compare", "evidence", "proof"];
function view(name) {
  state.view = name;
  for (const item of views) {
    $("panel-" + item).hidden = item !== name;
    $("tab-" + item).setAttribute("aria-selected", String(item === name));
    $("tab-" + item).tabIndex = item === name ? 0 : -1;
  }
  if (name === "impact") requestAnimationFrame(drawEdges);
}
for (const [index, name] of views.entries()) {
  $("tab-" + name).addEventListener("click", () => view(name));
  $("tab-" + name).addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? views.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + views.length) % views.length;
    view(views[next]);
    $("tab-" + views[next]).focus();
  });
}

$("saved-reviews").addEventListener("click", async () => {
  $("saved-dialog").showModal();
  $("saved-list").replaceChildren(make("p", "muted", "Loading saved review locators…"));
  try {
    const locator = JSON.parse(localStorage.getItem("ofc.copilot.visitor.session.v1") || "null");
    if (!locator || !/^cp-[a-f0-9]{32}$/.test(locator.id) || Date.now() - locator.saved > 86400000) throw Error("NO_SESSION");
    const response = await fetch("/api/copilot/sessions/" + locator.id, {credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(10000)});
    if (!response.ok) throw Error("SESSION_UNAVAILABLE");
    const session = await response.json();
    const reviews = session.messages.filter(message => /^pr-[a-f0-9]{32}$/.test(message.control_review?.review_id));
    if (!reviews.length) throw Error("NO_SAVED_GRAPH_REVIEWS");
    $("saved-list").replaceChildren(...reviews.map(message => {
      const button = make("button", "secondary", message.plan?.objective || message.run_id);
      button.type = "button";
      button.addEventListener("click", async () => {
        const privateClient = new ControlClient({sessionId: locator.id});
        const id = message.control_review.review_id;
        try {
          const status = await privateClient.status(id);
          if (status.state !== "FINISHED") {
            notice(`Saved review state: ${pretty(status.state)}. Reopen this list to refresh. No provider request was made.`);
            $("saved-dialog").close();
            return;
          }
          await show(await privateClient.proof(id), 0);
          $("saved-dialog").close();
          notice("Private saved evidence replay. No new provider request was made.");
        } catch (error) { notice("Saved review unavailable: " + error.message, true); }
      });
      return button;
    }));
  } catch {
    $("saved-list").replaceChildren(make("p", "muted", "No graph reviews are available in this visitor session. New Copilot results can include a graph review when the private control runtime is enabled."));
  }
});

new ResizeObserver(() => requestAnimationFrame(drawEdges)).observe($("graph"));
if (innerWidth <= 760) document.querySelector(".input-details").open = false;
await loadFixture();
