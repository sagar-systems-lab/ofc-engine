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
    row.append(left, make("span", "decision-choice", choice.before));
    baseline.append(row);

    const verdictRow = make("div", "decision-row verdict-row");
    const verdictLeft = make("div");
    verdictLeft.append(make("span", "decision-name", choice.label));
    const detail = choice.reason_codes.join(", ") || "No qualified evidence";
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
    entry.append(diff, make("small", null, "Plan revision updated. Original constraints preserved."));
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
  setText($("arm-effect"), c.changed_decisions + " plan choice(s) changed. No actual outcomes measured.");
  setText($("compare-validity"), c.identical_baseline ? "✓ Same frozen plan and constraints in both arms · Structural comparison only" : "⚠ Comparison invalid: baselines differ");
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
  drawComparison(data);
  drawInspector(data);
  setText($("status"), data.run_status === "INTEGRITY_ONLY" ? "Synthetic structural checks complete. External release remains blocked." : "Review required: " + data.reason_codes.join(", ").replaceAll("_", " "));
}

async function run() {
  const runButton = $("run");
  const scenario = $("scenario").value;
  runButton.disabled = true;
  setText($("status"), "Running local synthetic decision preflight…");
  try {
    const response = await fetch("/api/demo?scenario=" + encodeURIComponent(scenario), {cache: "no-store"});
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    if (data.demo_mode !== "SYNTHETIC" || data.release_authorized !== false || data.external_action_executed !== false || data.source_mode !== "SYNTHETIC") throw new Error("Invalid demo provenance");
    render(data);
  } catch (error) {
    $("result").hidden = true;
    setText($("status"), "Unable to load the demo run. Nothing has been approved or executed.");
  } finally {
    runButton.disabled = false;
  }
}

$("run").addEventListener("click", run);
$("inspect-toggle").addEventListener("click", () => {
  const open = $("inspector-details").hidden;
  $("inspector-details").hidden = !open;
  $("inspect-toggle").setAttribute("aria-expanded", String(open));
  $("inspect-toggle").firstChild.textContent = open ? "Close inspector " : "Open inspector ";
});
run();
