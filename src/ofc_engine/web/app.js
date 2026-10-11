"use strict";

const $ = (id) => document.getElementById(id);
const setText = (node, value) => { node.textContent = String(value); };
const make = (tag, className, value) => {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (value !== undefined) setText(el, value);
  return el;
};
const short = (value) => (value || "—").slice(0, 13) + ((value || "").length > 13 ? "…" : "");
const badge = (verdict) => make("span", "verdict-badge verdict-" + verdict, verdict);

function drawDecisions(data) {
  const baseline = $("baseline-list");
  const verdicts = $("verdict-list");
  baseline.replaceChildren();
  verdicts.replaceChildren();
  data.decisions.forEach((choice) => {
    const row = make("div", "decision-row");
    const left = make("div");
    left.append(make("span", "decision-name", choice.label), make("span", "decision-domain", choice.id.toUpperCase()));
    const right = make("div");
    right.append(make("span", "decision-choice", choice.before));
    if (!choice.changed && choice.initial_verdict === "PASS") {
      right.append(make("span", "preserved-marker", "PRESERVED"));
    }
    row.append(left, right);
    baseline.append(row);

    const verdictRow = make("div", "decision-row verdict-row");
    const verdictLeft = make("div");
    verdictLeft.append(make("span", "decision-name", choice.label));
    const detail = choice.initial_reason_codes.join(", ") || "No qualified evidence";
    verdictLeft.append(make("span", "verdict-meta", detail.replaceAll("_", " ")));
    verdictRow.append(verdictLeft, badge(choice.initial_verdict));
    verdicts.append(verdictRow);
  });
}

function drawRepair(data) {
  const area = $("repair-list");
  area.replaceChildren();
  const changed = data.decisions.filter((d) => d.changed);
  if (!changed.length) {
    const empty = make("div", "no-patch");
    empty.append(make("strong", null, "No patch applied"));
    empty.append(make("span", null, data.run_status === "USER_REVIEW" ? "Evidence unavailable. Human review is required." : "No eligible mutation in this run."));
    area.append(empty);
  }
  changed.forEach((choice) => {
    const entry = make("div", "repair-item");
    entry.append(make("div", "repair-kicker", "SELECTIVE PATCH / " + choice.id.toUpperCase()));
    entry.append(make("strong", null, choice.label));
    const diff = make("div", "repair-change");
    diff.append(make("span", "old", choice.before), make("span", "arrow", "→"), make("span", "new", choice.after));
    entry.append(diff, make("small", null, choice.pending_recheck ? "Recheck still pending · no approval" : "Rechecked at current revision · structural only"));
    area.append(entry);
  });
  const dep = data.decisions.filter((d) => d.rechecked && !d.changed);
  const review = $("recheck-panel");
  review.replaceChildren();
  const head = make("div");
  head.append(make("strong", null, "Dependent verification"), make("span", "verdict-badge " + (data.pending_rechecks.length ? "verdict-UNCERTAIN" : "verdict-PASS"), data.pending_rechecks.length ? "PENDING" : "INTEGRITY ONLY"));
  review.append(head, make("p", null, dep.length ? dep.map((d) => d.label).join(", ") + (data.pending_rechecks.length ? " · unresolved recheck" : " · rechecked at current revision") : "No additional dependencies invalidated."));
}

function drawComparison(data) {
  const c = data.comparison;
  setText($("arm-off"), c.without_evidence.replaceAll("_", " "));
  setText($("arm-on"), c.with_fixture_evidence.replaceAll("_", " "));
  if (!c.identical_baseline || c.baseline_digest !== data.baseline_digest || c.real_causal_proof !== false) {
    setText($("arm-off"), "INVALID PAIR");
    setText($("arm-on"), "INVALID PAIR");
    setText($("arm-effect"), "Comparison withheld");
    setText($("compare-validity"), "Comparison blocked: mismatched baseline or unsupported outcome claim.");
    return;
  }
  setText($("arm-effect"), c.changed_decisions + " plan choice(s) changed. No business outcomes measured.");
  setText($("compare-validity"), "Same frozen plan · fixture comparison only · no real causal proof");
}

function drawTimeline(data) {
  const parent = $("timeline-list");
  parent.replaceChildren();
  data.timeline.forEach((event) => {
    const entry = make("li", "timeline-entry");
    entry.append(make("span", "timeline-sequence", "EVENT " + String(event.sequence).padStart(2, "0")));
    entry.append(make("strong", null, event.state.replaceAll("_", " ")));
    entry.append(make("small", null, "Plan revision r" + event.revision));
    parent.append(entry);
  });
}

function inspectorRecord(data) {
  const block = make("section", "inspection-record");
  block.append(make("h3", null, data.id));
  const list = make("dl");
  const rows = [
    ["Decision", data.decision_id],
    ["Source", data.source_mode],
    ["Query", short(data.query_fingerprint)],
    ["Status", data.status],
    ["Candidates", data.query_candidates.join(", ")],
    ["Ranks", JSON.stringify(data.candidate_ranks)],
    ["Collected", data.collected_at],
  ];
  rows.forEach(([label, value]) => {
    list.append(make("dt", null, label), make("dd", null, value));
  });
  block.append(list);
  return block;
}

function drawInspector(data) {
  setText($("evidence-count"), data.evidence.length + " synthetic evidence record(s)");
  const parent = $("inspector-data");
  parent.replaceChildren();
  const meta = make("section", "inspection-record");
  meta.append(make("h3", null, "Run lineage"));
  const values = make("dl");
  [
    ["Run", data.run_id], ["Baseline", short(data.baseline_digest)],
    ["Revision", "r" + data.current_revision], ["Policy", data.policy_version],
    ["Source", data.source_mode], ["Release", data.release_state],
  ].forEach(([key, value]) => values.append(make("dt", null, key), make("dd", null, value)));
  meta.append(values);
  parent.append(meta);
  data.evidence.forEach((record) => parent.append(inspectorRecord(record)));
  if (!data.evidence.length) parent.append(make("p", "subheading", "No validated fixture evidence is available for this run."));
  data.traces.forEach((trace, i) => {
    const section = make("section", "inspection-record");
    section.append(make("h3", null, "Repair " + (i + 1) + " — " + trace.recheck_state.replaceAll("_", " ")));
    section.append(make("p", "subheading", trace.changed_decision_id + " · r" + trace.initial_revision + " → r" + trace.proposed_revision));
    if (trace.issue_codes.length) section.append(make("p", "subheading", "Blocks: " + trace.issue_codes.join(", ")));
    parent.append(section);
  });
}

function render(data) {
  $("result").hidden = false;
  setText($("run-status"), data.run_status.replaceAll("_", " "));
  $("run-status").className = "metric-value " + (data.run_status === "INTEGRITY_ONLY" ? "success-text" : "warning-text");
  setText($("run-phase"), "Lifecycle: " + data.run_phase.replaceAll("_", " "));
  setText($("revision"), "r" + data.current_revision);
  setText($("attempts"), data.attempts_used + " / " + data.max_attempts + " bounded attempts");
  setText($("change-count"), data.decisions.filter((d) => d.changed).length);
  setText($("baseline-digest"), short(data.baseline_digest));
  setText($("policy-version"), data.policy_version);
  setText($("current-revision"), "r" + data.current_revision);
  drawDecisions(data);
  drawRepair(data);
  drawTimeline(data);
  drawComparison(data);
  drawInspector(data);
  setText($("status"), data.run_status === "INTEGRITY_ONLY" ? "Synthetic structural checks complete. External release remains blocked." : "Review required: " + data.reason_codes.join(", ").replaceAll("_", " "));
}

let latestRun = 0;

function runFromUrl() {
  const params = new URLSearchParams(window.location.search);
  if (!params.has("run")) return null;
  if ([...params.keys()].some((key) => key !== "run") || params.getAll("run").length !== 1) {
    throw new Error("Invalid saved run URL");
  }
  const runId = params.get("run");
  if (!/^[0-9a-f]{32}$/.test(runId)) throw new Error("Invalid saved run ID");
  return runId;
}

async function readSavedRun(runId) {
  const response = await fetch("/api/runs/" + runId, {cache: "no-store"});
  if (!response.ok) throw new Error("Saved run unavailable");
  const body = await response.json();
  if (body.run_id !== runId || body.source_mode !== "SYNTHETIC" || !validSnapshot(body.snapshot)) {
    throw new Error("Saved run provenance invalid");
  }
  const cursor = await fetch("/api/runs/" + runId + "/events?after_sequence=-1", {cache: "no-store"});
  if (!cursor.ok) throw new Error("Run history unavailable");
  const eventView = await cursor.json();
  if (eventView.run_id !== runId || eventView.source_mode !== "SYNTHETIC" || eventView.release_authorized !== false
      || eventView.last_event_sequence !== body.snapshot.last_event_sequence
      || JSON.stringify(eventView.events) !== JSON.stringify(body.snapshot.timeline)) {
    throw new Error("Run history mismatch");
  }
  return {...body.snapshot, run_id: runId};
}

async function createOrPreview(scenario) {
  const requestId = window.crypto.randomUUID().replaceAll("-", "");
  const created = await fetch("/api/runs", {
    method: "POST", cache: "no-store",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({scenario, request_id: requestId}),
  });
  if (created.status === 405) {
    // Compatibility with the original read-only fixture viewer.
    const preview = await fetch("/api/demo?scenario=" + encodeURIComponent(scenario), {cache: "no-store"});
    if (!preview.ok) throw new Error("Preview unavailable");
    return {snapshot: await preview.json(), runId: null};
  }
  if (!created.ok) throw new Error("Could not save the run");
  const value = await created.json();
  if (!/^[0-9a-f]{32}$/.test(value.run_id) || value.source_mode !== "SYNTHETIC") {
    throw new Error("Invalid saved run ID");
  }
  return {snapshot: {...value.snapshot, run_id: value.run_id}, runId: value.run_id};
}

async function run(restore = false) {
  const token = ++latestRun;
  const button = $("run");
  const scenario = $("scenario").value;
  button.disabled = true;
  setText($("status"), restore ? "Restoring saved synthetic run…" : "Running local synthetic decision preflight…");
  try {
    const savedId = restore ? runFromUrl() : null;
    const output = savedId
      ? {snapshot: await readSavedRun(savedId), runId: savedId}
      : await createOrPreview(scenario);
    if (token !== latestRun) return;
    if (!validSnapshot(output.snapshot)) throw new Error("Invalid demo snapshot");
    $("scenario").value = output.snapshot.scenario;
    render(output.snapshot);
    if (!savedId) {
      const url = output.runId ? "?run=" + output.runId : window.location.pathname;
      window.history.replaceState(null, "", url);
    }
  } catch (error) {
    if (token !== latestRun) return;
    $("result").hidden = true;
    setText($("status"), "Saved run unavailable or incomplete. No release or external action has been authorized.");
  } finally {
    if (token === latestRun) button.disabled = false;
  }
}

function validSnapshot(data) {
  if (!data || data.demo_mode !== "SYNTHETIC" || data.source_mode !== "SYNTHETIC") return false;
  if (data.release_authorized !== false || data.external_action_executed !== false || data.provider_authenticated !== false) return false;
  if (!Number.isInteger(data.current_revision) || data.current_revision < 0) return false;
  if (!Number.isInteger(data.last_event_sequence) || !Array.isArray(data.timeline) || !Array.isArray(data.decisions)) return false;
  if (!data.timeline.length || data.timeline.at(-1).sequence !== data.last_event_sequence) return false;
  if (data.timeline.some((e, i) => e.sequence !== i || !Number.isInteger(e.revision) || e.revision > data.current_revision)) return false;
  if (!data.comparison || !Array.isArray(data.evidence) || !Array.isArray(data.traces)) return false;
  return true;
}

$("run").addEventListener("click", () => run(false));
$("inspect-toggle").addEventListener("click", () => {
  const open = $("inspector-details").hidden;
  $("inspector-details").hidden = !open;
  $("inspect-toggle").setAttribute("aria-expanded", String(open));
  $("inspect-toggle").firstChild.textContent = open ? "Close inspector " : "Open inspector ";
});
run(true);
