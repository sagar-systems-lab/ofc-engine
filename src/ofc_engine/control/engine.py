from __future__ import annotations

from itertools import product
from copy import deepcopy
import json
from time import monotonic_ns

from .canonical import digest
from .graph import claim_fingerprint, constraints, impact
from .models import Patch, Plan, ReviewRequest
from .policies import Authority, MANIFEST, POLICY_DIGEST, POLICY_VERSION, evaluate, required_claims


def apply_patches(plan: Plan, patches: tuple[Patch, ...]) -> Plan:
    if len({p.node_id for p in patches}) != len(patches):
        raise ValueError("duplicate patch node")
    by_id = {n.id: n for n in plan.nodes}
    for patch in patches:
        node = by_id.get(patch.node_id)
        if node is None or not set(patch.changes) <= set(node.mutable_fields):
            raise ValueError("patch outside mutable field mask")
        if all(getattr(node, field) == value for field, value in patch.changes.items()):
            raise ValueError("no-op patch")
        if "candidate_id" in patch.changes and patch.changes["candidate_id"] not in {c.id for c in node.candidate_pool}:
            raise ValueError("patch outside frozen candidate pool")
        by_id[node.id] = node.data() | patch.changes
    payload = plan.data()
    payload["nodes"] = [by_id[n.id].data() if hasattr(by_id[n.id], "data") else by_id[n.id] for n in plan.nodes]
    return Plan.parse(payload)


def _rank_options(plan: Plan, verdicts: dict) -> dict:
    options = {}
    for node in plan.nodes:
        if "candidate_id" not in node.mutable_fields:
            continue
        ranks = [c for c in verdicts[node.id]["claims"] if c["claim"] == "cultural_brand_rank" and c["status"] == "QUALIFIED"]
        if ranks:
            claim = ranks[0]
            options[node.id] = {"order": claim["ranked_candidates"], "evidence_ids": claim["evidence_ids"]}
    return options


def optimize(plan: Plan, verdicts: dict, *, clock=monotonic_ns) -> dict:
    started = clock()
    options = _rank_options(plan, verdicts)
    nodes = {n.id: n for n in plan.nodes}
    keys = sorted(options)
    if not keys:
        return {"status": "NO_QUALIFIED_ALTERNATIVE", "patches": [], "expansions": 0}

    def objective(candidate: Plan):
        selected = {n.id: n.candidate_id for n in candidate.nodes}
        changed = {key: {"candidate_id"} for key in keys if selected[key] != nodes[key].candidate_id}
        unresolved = sum(selected[key] != options[key]["order"][0] for key in keys)
        hard_failures = sum(c["status"] != "PASS" for c in constraints(candidate))
        return (hard_failures, unresolved, len(changed), len(impact(plan, changed)["invalidated"]))

    baseline_objective = objective(plan)
    best, best_score, best_patches, ambiguous, expansions = None, None, (), False, 0
    rejected = []
    for choices in product(*(options[key]["order"] for key in keys)):
        if expansions >= plan.limits.max_expansions or (clock() - started) / 1_000_000 > plan.limits.deadline_ms:
            return {"status": "OPTIMIZATION_UNDECIDED", "patches": [], "expansions": expansions}
        expansions += 1
        patches = tuple(Patch(node_id=key, changes={"candidate_id": value}, evidence_ids=tuple(options[key]["evidence_ids"]))
                        for key, value in zip(keys, choices) if value != nodes[key].candidate_id)
        candidate = apply_patches(plan, patches) if patches else plan
        failures = [c for c in constraints(candidate) if c["status"] != "PASS"]
        if failures:
            if len(rejected) < 8:
                rejected.append({"selections": {key: value for key, value in zip(keys, choices)}, "constraints": failures})
            continue
        score = objective(candidate)
        if best_score is None or score < best_score:
            best, best_score, best_patches, ambiguous = candidate, score, patches, False
        elif score == best_score and candidate.digest != best.digest:
            ambiguous = True
    if (clock() - started) / 1_000_000 > plan.limits.deadline_ms:
        return {"status": "OPTIMIZATION_UNDECIDED", "patches": [], "expansions": expansions}
    if best is None:
        return {"status": "HARD_CONSTRAINTS_UNRESOLVED", "patches": [], "expansions": expansions, "rejected_alternatives": rejected}
    if best_score >= baseline_objective or not best_patches:
        return {"status": "PRESERVED", "patches": [], "expansions": expansions,
                "reason": "ORIGINAL_ALREADY_TOP_RANKED" if baseline_objective[1] == 0 else "NO_SAFE_IMPROVEMENT",
                "rejected_alternatives": rejected}
    if ambiguous:
        return {"status": "AMBIGUOUS_OPTIMUM", "patches": [], "expansions": expansions}
    return {"status": "REPAIR_PROPOSED", "patches": [p.data() for p in best_patches],
            "expansions": expansions, "objective": list(best_score),
            "objective_order": ["hard_constraint_failures", "unmet_rank_obligations", "edits", "downstream_rechecks"]}


def _event(events: list[dict], kind: str, payload: dict):
    item = {"sequence": len(events) + 1, "kind": kind, "payload": payload,
            "previous": events[-1]["hash"] if events else "0" * 64}
    events.append({**item, "hash": digest(item)})


def review(request: ReviewRequest, *, authorities: dict[str, Authority] | None = None,
           optimizer=optimize) -> dict:
    request = ReviewRequest.parse(request.data())
    plan, evidence = request.plan, request.evidence
    authorities = {key: value for key, value in (authorities or {}).items() if value.tenant_id == plan.tenant_id}
    if request.as_of is None:
        raise ValueError("review clock must be frozen by the coordinator")
    synthetic = request.mode == "SYNTHETIC"
    if any(e.node_id not in {n.id for n in plan.nodes} for e in evidence):
        raise ValueError("evidence refers to unknown node")
    kwargs = {"baseline_digest": plan.digest, "as_of": request.as_of,
              "synthetic": synthetic, "authorities": authorities}
    events = []
    _event(events, "FROZEN", {"digest": plan.digest, "revision": 0, "policy_digest": POLICY_DIGEST})
    original_verdicts = evaluate(plan, evidence, **kwargs)
    _event(events, "EVALUATED", {"verdict_digest": digest(original_verdicts), "evidence_root": digest([e.data() for e in evidence])})
    optimization = optimizer(plan, original_verdicts)
    patches = tuple(Patch.model_validate_json(json.dumps(p)) for p in optimization["patches"])
    current = apply_patches(plan, patches) if patches else plan
    affected = impact(plan, {p.node_id: set(p.changes) for p in patches})
    revision = 1 if patches else 0
    if patches:
        _event(events, "PATCH_COMMITTED", {"parent_digest": plan.digest, "digest": current.digest,
                                          "revision": revision, "patches": [p.data() for p in patches],
                                          "policy_digest": POLICY_DIGEST})
        _event(events, "INVALIDATED", {"nodes": affected["invalidated"], "paths": affected["paths"]})
    verdicts = evaluate(current, evidence, **kwargs)
    rechecks = []
    for key in affected["recheck_order"]:
        node = next(n for n in current.nodes if n.id == key)
        reads_changed = [claim for claim in required_claims(current, node)
                         if claim_fingerprint(plan, key, claim) != claim_fingerprint(current, key, claim)]
        rechecks.append({"node_id": key, "status": verdicts[key]["status"],
                         "invalidated_claims": reads_changed,
                         "provider_calls": 0, "reason": "LOCAL_POLICY_RECHECK"})
    if rechecks:
        _event(events, "RECHECKED", {"checks": rechecks, "verdict_digest": digest(verdicts)})
    constraint_results = constraints(current)
    unresolved = [{"node_id": n.id, "reasons": verdicts[n.id]["reasons"]}
                  for n in current.nodes if n.required and verdicts[n.id]["status"] != "QUALIFIED"]
    blockers = [c for c in constraint_results if c["status"] != "PASS"]
    _event(events, "HANDOFF_WITHHELD", {"unresolved": unresolved, "constraints": blockers,
                                       "external_action_executed": False})
    selected_before = {n.id: n.candidate_id for n in plan.nodes}
    selected_after = {n.id: n.candidate_id for n in current.nodes}
    static = dict(selected_before)
    for key, rank in _rank_options(plan, original_verdicts).items():
        static[key] = rank["order"][0]
    comparison = {"shared_plan_digest": plan.digest, "shared_evidence_root": digest([e.data() for e in evidence]),
                  "original": selected_before, "static_rank_rule": static, "controlled": selected_after,
                  "additional_provider_calls": 0, "real_world_causal_claim": False}
    result = {
        "schema": "ofc.control-proof/1", "baseline": plan.data(), "current": current.data(),
        "baseline_digest": plan.digest, "current_digest": current.digest, "revision": revision,
        "as_of": request.as_of, "mode": request.mode, "policy_manifest": deepcopy(MANIFEST),
        "policy_version": POLICY_VERSION, "policy_digest": POLICY_DIGEST,
        "evidence": [e.data() for e in evidence], "evidence_root": digest([e.data() for e in evidence]),
        "authority_keys": [a.data() for a in sorted((authorities or {}).values(), key=lambda a: a.issuer)],
        "baseline_verdicts": original_verdicts, "verdicts": verdicts,
        "patches": [p.data() for p in patches], "impact": affected, "rechecks": rechecks,
        "constraints": constraint_results, "optimization": optimization,
        "unresolved": unresolved, "comparison": comparison, "events": events,
        "journal_head": events[-1]["hash"], "action_status": "WITHHELD",
        "release_authorized": False, "external_action_executed": False,
        "provider_calls": 0, "spend_minor": 0, "issuer_signature": None,
        "signature_status": "UNSIGNED_CONTROL_RECORD",
        "status": "REVIEW_REQUIRED" if unresolved or blockers else "EVIDENCE_COMPLETE_ACTION_WITHHELD",
    }
    result["proof_digest"] = digest(result)
    return result
