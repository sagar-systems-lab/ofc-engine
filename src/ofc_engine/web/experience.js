"use strict";

let selectedDecision = null;
let previousRevision = null;

function labelStage(name, state, detail) {
  const node = document.querySelector('[data-stage="' + name + '"]');
  if (!node) return;
  node.dataset.state = state;
  node.querySelector(".stage-state").textContent = detail;
}

function presentLifecycle(snapshot) {
  const events = snapshot.timeline.map((event) => event.state);
  labelStage("PROPOSE", events.includes("BASELINE_FROZEN") ? "done" : "blocked", "SEALED");
  labelStage("CHECK", events.includes("EVALUATED") ? "done" : "blocked", "CHECKED");
  const patched = events.includes("PATCH_APPLIED");
  labelStage("REPAIR", patched ? "done" : "skipped", patched ? "APPLIED" : "NONE");
  const complete = snapshot.run_status === "INTEGRITY_ONLY" && snapshot.pending_rechecks.length === 0;
  labelStage("VERIFY", complete ? "done" : "blocked", complete ? "INTEGRITY" : "BLOCKED");
  labelStage("ACT", "blocked", "LOCKED");
}

function presentInsight(snapshot) {
  const pending = snapshot.pending_rechecks;
  const patches = snapshot.decisions.filter((choice) => choice.changed);
  let message;
  if (pending.length) {
    message = pending.length + " dependent decision(s) need evidence at revision r" + snapshot.current_revision
      + ". Prior approvals are invalidated; action release remains blocked.";
  } else if (snapshot.run_status === "REPAIR_EXHAUSTED") {
    message = "The bounded repair budget is exhausted at revision r" + snapshot.current_revision
      + ". Further changes need review, not an automatic retry.";
  } else if (snapshot.run_status !== "INTEGRITY_ONLY") {
    message = "Required fixture evidence is missing or unusable. Human review is required; no cultural approval is established.";
  } else if (patches.length) {
    message = patches.length + " candidate choice(s) changed under the fixture policy. Other choices were preserved and dependent checks rerun. This is not live cultural proof.";
  } else {
    message = "The fixture plan required no change. Structural integrity does not establish cultural suitability or action permission.";
  }
  document.getElementById("insight-text").textContent = message;
}

function selectEvidence(snapshot, decisionId) {
  document.querySelectorAll("#verdict-list .verdict-trigger").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.decisionId === decisionId));
  });
  const details = document.getElementById("inspector-details");
  const toggle = document.getElementById("inspect-toggle");
  details.hidden = false;
  toggle.setAttribute("aria-expanded", "true");
  toggle.firstChild.textContent = "Close inspector ";
  const choice = snapshot.decisions.find((item) => item.id === decisionId);
  document.querySelectorAll("#inspector-data .inspection-record").forEach((record) => {
    record.classList.remove("selected-evidence");
  });
  const record = Array.from(document.querySelectorAll("#inspector-data .inspection-record"))
    .find((item) => choice?.evidence_ids.includes(item.querySelector("h3")?.textContent));
  if (record) record.classList.add("selected-evidence");
  document.getElementById("evidence-count").textContent = record
    ? "Focused: " + choice.label + " · synthetic evidence"
    : "No current evidence for " + choice.label + " · review required";
  document.getElementById("inspector").scrollIntoView({
    behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
    block: "start",
  });
}

function interactiveVerdicts(snapshot) {
  document.querySelectorAll("#verdict-list .verdict-row").forEach((row, index) => {
    const choice = snapshot.decisions[index];
    if (!choice) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "verdict-trigger";
    button.dataset.decisionId = choice.id;
    button.setAttribute("aria-label", "Inspect " + choice.label + ": " + choice.initial_verdict + "; source synthetic");
    button.setAttribute("aria-pressed", String(selectedDecision === choice.id));
    Array.from(row.childNodes).forEach((node) => button.append(node));
    const hint = document.createElement("span");
    hint.className = "trace-hint";
    hint.textContent = "Inspect evidence ↗";
    const name = button.querySelector(".decision-name");
    if (name) name.parentElement.append(hint);
    row.replaceChildren(button);
    button.addEventListener("click", () => {
      selectedDecision = choice.id;
      selectEvidence(snapshot, choice.id);
    });
  });
}

function presentTimeline(snapshot) {
  document.getElementById("timeline-meta").textContent =
    snapshot.timeline.length + " completed events · revision r" + snapshot.current_revision;
  const entries = document.querySelectorAll("#timeline-list .timeline-entry");
  entries.forEach((entry, index) => {
    entry.dataset.last = String(index === entries.length - 1);
  });
}

function onSnapshot(event) {
  const snapshot = event.detail;
  if (!snapshot || snapshot.source_mode !== "SYNTHETIC"
      || snapshot.release_authorized !== false || snapshot.external_action_executed !== false) return;
  presentLifecycle(snapshot);
  presentInsight(snapshot);
  interactiveVerdicts(snapshot);
  presentTimeline(snapshot);
  if (previousRevision !== null && snapshot.current_revision !== previousRevision) {
    document.querySelectorAll("#repair-list .repair-item").forEach((item) => {
      item.classList.add("motion-change");
    });
  }
  previousRevision = snapshot.current_revision;
  if (selectedDecision && snapshot.decisions.some((choice) => choice.id === selectedDecision)) {
    selectEvidence(snapshot, selectedDecision);
  }
}

document.addEventListener("ofc:snapshot", onSnapshot);
