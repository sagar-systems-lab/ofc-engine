import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {runInNewContext} from "node:vm";

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.className = "";
    this.textContent = "";
    this.attributes = {};
    this.disabled = false;
  }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener() {}
  querySelector(selector) {
    return selector === ".message-copy"
      ? this.children.find(node => node.className === "message-copy") || null : null;
  }
}
const document = {
  createElement: tag => new Element(tag),
  getElementById: () => new Element("div"),
  body: new Element("body"),
};
const window = {};
const source = readFileSync(new URL("../src/ofc_engine/web/copilot-proof.js", import.meta.url), "utf8");
runInNewContext(source, {document, window, getComputedStyle: () => ({display: "block"})});

const hash = char => char.repeat(64);
const shared = {evidence_digest: hash("c")};
const original = {
  decision_id: "brand_partnership_selection", domain: "brand",
  candidate_id: "Adidas", candidate_pool: ["Adidas", "Nike"],
  cultural_rank_observed: true,
  qloo_observation: {
    ranked_candidate_ids: ["Adidas", "Nike"], source_mode: "LIVE_HTTP_UNVERIFIED",
    query_fingerprint: hash("b"), capture_digest: hash("d"),
    collected_at: "2026-10-23T09:01:13Z",
  },
};
const seed = {
  _sessionReadConfirmed: true,
  run_id: "cp-run-" + "1".repeat(32), status: "REVIEW_REQUIRED",
  model_id: "mock-model", qloo_evidence_digest: hash("c"),
  baseline_digest: hash("a"), reviewed_digest: hash("a"),
  plan: {artist_name: "Billie Eilish"},
  original_answer: "Original selection: Adidas.",
  ofc_answer: "Adidas leads in the saved ranked shortlist.",
  business_verification: {
    missing_requirements: {
      brand_partnership_selection: ["contract_rights", "budget_feasibility"],
    },
  },
  off: {ofc_enabled: false, decisions: [original], qloo: shared, plan_digest: hash("a")},
  on: {
    ofc_enabled: true,
    decisions: [{
      ...original, ofc_verdict: "PASS", ofc_reason: "QLOO_RANK_RECHECK_MATCHED",
      rank_recheck_completed: true, changed: false,
    }],
    qloo: shared, plan_digest: hash("a"), changed_ids: [],
  },
};
const message = () => {
  const outer = new Element("article"), copy = new Element("div");
  copy.className = "message-copy";
  outer.append(copy);
  return outer;
};
const textOf = node => [node.textContent || "", ...node.children.flatMap(textOf)]
  .filter(Boolean).join("|");
const show = (run, mode) => {
  const node = window.OFCResults.renderRun(run, mode, message);
  assert.ok(node, "expected a saved paired result");
  return textOf(node);
};
assert.ok(window.OFCResults.pairing(seed));
const on = show(seed, "ON"), off = show(seed, "OFF");
assert.ok(on.includes("Keep original choice: Adidas"));
assert.ok(on.includes("Verify partnership rights"));
assert.ok(on.includes("Other brands could still win"));
assert.ok(on.includes("No deal, booking or payment is authorized"));
assert.ok(on.indexOf("Model explanation · not independently verified") <
  on.indexOf("How the decision was reviewed"));
assert.ok(off.includes("Starting choice: Adidas"));
assert.ok(!off.includes("Keep original choice: Adidas"));

const uncertain = JSON.parse(JSON.stringify(seed));
uncertain.on.decisions[0].ofc_reason = "RECHECK_UNAVAILABLE";
uncertain.on.decisions[0].rank_recheck_completed = false;
const unresolved = show(uncertain, "ON");
assert.ok(unresolved.includes("No qualified brand recommendation yet"));
assert.ok(!unresolved.includes("Keep original choice: Adidas"));

const revised = JSON.parse(JSON.stringify(seed));
revised.reviewed_digest = hash("e");
revised.on.plan_digest = hash("e");
revised.on.changed_ids = ["brand_partnership_selection"];
revised.on.decisions[0].candidate_id = "Nike";
revised.on.decisions[0].changed = true;
revised.off.decisions[0].qloo_observation.ranked_candidate_ids = ["Nike", "Adidas"];
revised.on.decisions[0].qloo_observation.ranked_candidate_ids = ["Nike", "Adidas"];
assert.ok(show(revised, "ON").includes("Revise choice to Nike"));

const forged = JSON.parse(JSON.stringify(seed));
forged.on.qloo.evidence_digest = hash("9");
assert.equal(window.OFCResults.pairing(forged), null);
assert.ok(!show(forged, "ON").includes("Keep original choice: Adidas"));

console.log("Copilot advisory offline smoke PASS: keep, revise, OFF, uncertainty, mismatch, ordering");
