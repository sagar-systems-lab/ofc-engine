from __future__ import annotations

from itertools import product
from copy import deepcopy
import json
from time import monotonic_ns

from .canonical import digest
from .graph import claim_fingerprint, constraints, impact, recheck_state
from .models import Patch, Plan, ReviewRequest
from .policies import (
    Authority,
    MANIFEST,
    POLICY_DIGEST,
    POLICY_VERSION,
    evaluate,
    required_claims,
)


def apply_patches(
    plan: Plan, patches: tuple[Patch, ...], *, authorized_nodes=()
) -> Plan:
    if len({p.node_id for p in patches}) != len(patches):
        raise ValueError("duplicate patch node")
    by_id = {n.id: n for n in plan.nodes}
    for patch in patches:
        node = by_id.get(patch.node_id)
        if (
            node is not None
            and node.owner != plan.actor.id
            and node.id not in authorized_nodes
        ):
            raise PermissionError("NODE_OWNER_MUTATION_DENIED")
        if node is None or not set(patch.changes) <= set(node.mutable_fields):
            raise ValueError("patch outside mutable field mask")
        if all(getattr(node, field) == value for field, value in patch.changes.items()):
            raise ValueError("no-op patch")
        if "candidate_id" in patch.changes and patch.changes["candidate_id"] not in {
            c.id for c in node.candidate_pool
        }:
            raise ValueError("patch outside frozen candidate pool")
        by_id[node.id] = node.data() | patch.changes
    payload = plan.data()
    payload["nodes"] = [
        by_id[n.id].data() if hasattr(by_id[n.id], "data") else by_id[n.id]
        for n in plan.nodes
    ]
    return Plan.parse(payload)


def _rank_options(plan: Plan, verdicts: dict, *, authorized_nodes=()) -> dict:
    options = {}
    for node in plan.nodes:
        if "candidate_id" not in node.mutable_fields or (
            node.owner != plan.actor.id and node.id not in authorized_nodes
        ):
            continue
        ranks = [
            c
            for c in verdicts[node.id]["claims"]
            if c["claim"] in ("cultural_brand_rank", "place_cultural_relevance")
            and c["status"] == "QUALIFIED"
        ]
        if ranks:
            claim = ranks[0]
            options[node.id] = {
                "order": claim["ranked_candidates"],
                "evidence_ids": claim["evidence_ids"],
            }
    return options


def decision_options(
    plan, verdicts, *, evidence=(), evaluation=None, authorized_nodes=()
):
    options = _rank_options(plan, verdicts, authorized_nodes=authorized_nodes)
    if evaluation is None:
        return options
    for node in plan.nodes:
        if (
            node.owner != plan.actor.id and node.id not in authorized_nodes
        ) or not node.mutable_fields:
            continue
        legal, supporting = [], set()
        ranks = options.get(node.id, {}).get("order", [])
        for candidate in node.candidate_pool:
            windows = [(node.start_at, node.end_at)]
            if {"start_at", "end_at"} <= set(node.mutable_fields):
                windows += [
                    (e.facts.get("start_at"), e.facts.get("end_at"))
                    for e in evidence
                    if e.node_id == node.id
                    and e.claim == "schedule_window"
                    and e.subject_id == candidate.id
                ]
            for start, end in dict.fromkeys(windows):
                changes = {
                    "candidate_id": candidate.id,
                    "start_at": start,
                    "end_at": end,
                }
                changes = {k: v for k, v in changes.items() if getattr(node, k) != v}
                if not set(changes) <= set(node.mutable_fields):
                    continue
                try:
                    alternative = (
                        apply_patches(
                            plan,
                            (
                                Patch(
                                    node_id=node.id,
                                    changes=changes,
                                    evidence_ids=("policy-probe",),
                                ),
                            ),
                            authorized_nodes=authorized_nodes,
                        )
                        if changes
                        else plan
                    )
                except ValueError:
                    continue
                verdict = evaluate(alternative, evidence, **evaluation)[node.id]
                required = [
                    c
                    for c in verdict["claims"]
                    if c["claim"]
                    not in ("cultural_brand_rank", "place_cultural_relevance")
                ]
                # Rank is cultural utility, not a substitute for alternative-specific money or permissions.
                needs_business = bool(plan.budgets) or node.domain != "brand"
                if needs_business and verdict["upstream_blockers"]:
                    continue
                if needs_business and (
                    not required or any(c["status"] != "QUALIFIED" for c in required)
                ):
                    continue
                if node.domain == "brand" and not ranks:
                    continue
                if node.domain not in {
                    "brand",
                    "place",
                    "venue",
                    "schedule",
                    "budget",
                    "collaborator",
                    "channel",
                    "media",
                    "experience",
                }:
                    continue
                ids = set(options.get(node.id, {}).get("evidence_ids", []))
                ids.update(
                    i
                    for c in required
                    if c["status"] == "QUALIFIED"
                    for i in c["evidence_ids"]
                )
                utility = int(candidate.id != ranks[0]) if ranks else 0
                legal.append(
                    {
                        "changes": changes,
                        "selection": {
                            "candidate_id": candidate.id,
                            "start_at": start,
                            "end_at": end,
                        },
                        "utility": utility,
                        "evidence_ids": sorted(ids),
                    }
                )
                supporting.update(ids)
        if legal:
            options[node.id] = {
                "order": ranks,
                "evidence_ids": sorted(supporting),
                "alternatives": legal,
            }
        elif node.id in options:
            del options[node.id]
    return options


def optimize(
    plan: Plan,
    verdicts: dict,
    *,
    clock=monotonic_ns,
    evidence=(),
    evaluation=None,
    authorized_nodes=(),
) -> dict:
    started = clock()
    options = decision_options(
        plan,
        verdicts,
        evidence=evidence,
        evaluation=evaluation,
        authorized_nodes=authorized_nodes,
    )
    nodes = {n.id: n for n in plan.nodes}
    keys = sorted(options)
    if not keys:
        return {"status": "NO_QUALIFIED_ALTERNATIVE", "patches": [], "expansions": 0}

    def objective(candidate: Plan):
        selected = {n.id: n.candidate_id for n in candidate.nodes}
        changed = {
            key: {
                f
                for f in nodes[key].mutable_fields
                if getattr(next(n for n in candidate.nodes if n.id == key), f)
                != getattr(nodes[key], f)
            }
            for key in keys
        }
        changed = {k: v for k, v in changed.items() if v}
        unresolved = sum(
            (
                next(
                    (
                        a["utility"]
                        for a in options[key].get("alternatives", [])
                        if all(
                            getattr(next(n for n in candidate.nodes if n.id == key), f)
                            == v
                            for f, v in a["selection"].items()
                        )
                    ),
                    10**13,
                )
                if "alternatives" in options[key]
                else int(selected[key] != options[key]["order"][0])
            )
            for key in keys
        )
        hard_failures = sum(c["status"] != "PASS" for c in constraints(candidate))
        return (
            hard_failures,
            unresolved,
            len(changed),
            len(impact(plan, changed)["invalidated"]),
        )

    baseline_objective = objective(plan)
    best, best_score, best_patches, ambiguous, expansions = None, None, (), False, 0
    rejected = []
    alternatives = {
        key: options[key].get(
            "alternatives",
            [
                {
                    "changes": {}
                    if value == nodes[key].candidate_id
                    else {"candidate_id": value},
                    "evidence_ids": options[key]["evidence_ids"],
                }
                for value in options[key]["order"]
            ],
        )
        for key in keys
    }
    for choices in product(*(alternatives[key] for key in keys)):
        if (
            expansions >= plan.limits.max_expansions
            or (clock() - started) / 1_000_000 > plan.limits.deadline_ms
        ):
            return {
                "status": "OPTIMIZATION_UNDECIDED",
                "patches": [],
                "expansions": expansions,
            }
        expansions += 1
        patches = tuple(
            Patch(
                node_id=key,
                changes=value["changes"],
                evidence_ids=tuple(value["evidence_ids"]),
            )
            for key, value in zip(keys, choices)
            if value["changes"]
        )
        candidate = (
            apply_patches(plan, patches, authorized_nodes=authorized_nodes)
            if patches
            else plan
        )
        failures = [c for c in constraints(candidate) if c["status"] != "PASS"]
        if failures:
            if len(rejected) < 8:
                rejected.append(
                    {
                        "selections": {
                            key: value["changes"] for key, value in zip(keys, choices)
                        },
                        "constraints": failures,
                    }
                )
            continue
        score = objective(candidate)
        if best_score is None or score < best_score:
            best, best_score, best_patches, ambiguous = candidate, score, patches, False
        elif score == best_score and candidate.digest != best.digest:
            ambiguous = True
    if (clock() - started) / 1_000_000 > plan.limits.deadline_ms:
        return {
            "status": "OPTIMIZATION_UNDECIDED",
            "patches": [],
            "expansions": expansions,
        }
    if best is None:
        return {
            "status": "HARD_CONSTRAINTS_UNRESOLVED",
            "patches": [],
            "expansions": expansions,
            "rejected_alternatives": rejected,
        }
    if best_score >= baseline_objective or not best_patches:
        return {
            "status": "PRESERVED",
            "patches": [],
            "expansions": expansions,
            "reason": "ORIGINAL_ALREADY_TOP_RANKED"
            if baseline_objective[1] == 0
            else "NO_SAFE_IMPROVEMENT",
            "rejected_alternatives": rejected,
        }
    if ambiguous:
        return {"status": "AMBIGUOUS_OPTIMUM", "patches": [], "expansions": expansions}
    return {
        "status": "REPAIR_PROPOSED",
        "patches": [p.data() for p in best_patches],
        "expansions": expansions,
        "objective": list(best_score),
        "objective_order": [
            "hard_constraint_failures",
            "unmet_rank_obligations",
            "edits",
            "downstream_rechecks",
        ],
    }


def _event(events: list[dict], kind: str, payload: dict):
    item = {
        "sequence": len(events) + 1,
        "kind": kind,
        "payload": payload,
        "previous": events[-1]["hash"] if events else "0" * 64,
    }
    events.append({**item, "hash": digest(item)})


def _single_review(
    request: ReviewRequest,
    *,
    anchor_digest=None,
    authorities: dict[str, Authority] | None = None,
    optimizer=optimize,
    authorized_nodes=(),
) -> dict:
    request = ReviewRequest.parse(request.data())
    plan, evidence = request.plan, request.evidence
    authorities = {
        key: value
        for key, value in (authorities or {}).items()
        if value.tenant_id == plan.tenant_id
    }
    if request.as_of is None:
        raise ValueError("review clock must be frozen by the coordinator")
    synthetic = request.mode == "SYNTHETIC"
    if any(e.node_id not in {n.id for n in plan.nodes} for e in evidence):
        raise ValueError("evidence refers to unknown node")
    kwargs = {
        "baseline_digest": anchor_digest or plan.digest,
        "as_of": request.as_of,
        "synthetic": synthetic,
        "authorities": authorities,
    }
    events = []
    _event(
        events,
        "FROZEN",
        {"digest": plan.digest, "revision": 0, "policy_digest": POLICY_DIGEST},
    )
    original_verdicts = evaluate(plan, evidence, **kwargs)
    _event(
        events,
        "EVALUATED",
        {
            "verdict_digest": digest(original_verdicts),
            "evidence_root": digest([e.data() for e in evidence]),
        },
    )
    optimization = (
        optimizer(
            plan,
            original_verdicts,
            evidence=evidence,
            evaluation=kwargs,
            authorized_nodes=authorized_nodes,
        )
        if optimizer is optimize
        else optimizer(plan, original_verdicts)
    )
    patches = tuple(
        Patch.model_validate_json(json.dumps(p)) for p in optimization["patches"]
    )
    current = (
        apply_patches(plan, patches, authorized_nodes=authorized_nodes)
        if patches
        else plan
    )
    affected = impact(plan, {p.node_id: set(p.changes) for p in patches})
    revision = 1 if patches else 0
    if patches:
        _event(
            events,
            "PATCH_COMMITTED",
            {
                "parent_digest": plan.digest,
                "digest": current.digest,
                "revision": revision,
                "patches": [p.data() for p in patches],
                "policy_digest": POLICY_DIGEST,
            },
        )
        _event(
            events,
            "INVALIDATED",
            {"nodes": affected["invalidated"], "paths": affected["paths"]},
        )
    verdicts = evaluate(current, evidence, **kwargs)
    rechecks = []
    for key in affected["recheck_order"]:
        node = next(n for n in current.nodes if n.id == key)
        reads_changed = [
            claim
            for claim in required_claims(current, node)
            if claim_fingerprint(plan, key, claim)
            != claim_fingerprint(current, key, claim)
        ]
        rechecks.append(
            {
                "node_id": key,
                "status": verdicts[key]["status"],
                "invalidated_claims": reads_changed,
                "evidence_state": recheck_state(verdicts[key]["status"], reads_changed),
                "acquisition": "UNAVAILABLE"
                if verdicts[key]["status"] != "QUALIFIED"
                else "RECHECK_COMPLETED",
                "provider_calls": 0,
                "reason": "LOCAL_POLICY_RECHECK",
            }
        )
    if rechecks:
        _event(
            events,
            "RECHECKED",
            {"checks": rechecks, "verdict_digest": digest(verdicts)},
        )
    constraint_results = constraints(current)
    unresolved = [
        {"node_id": n.id, "reasons": verdicts[n.id]["reasons"]}
        for n in current.nodes
        if n.required and verdicts[n.id]["status"] != "QUALIFIED"
    ]
    blockers = [c for c in constraint_results if c["status"] != "PASS"]
    _event(
        events,
        "HANDOFF_WITHHELD",
        {
            "unresolved": unresolved,
            "constraints": blockers,
            "external_action_executed": False,
        },
    )
    selected_before = {n.id: n.candidate_id for n in plan.nodes}
    selected_after = {n.id: n.candidate_id for n in current.nodes}
    static = dict(selected_before)
    for key, rank in _rank_options(plan, original_verdicts).items():
        static[key] = rank["order"][0]
    comparison = {
        "shared_plan_digest": plan.digest,
        "shared_evidence_root": digest([e.data() for e in evidence]),
        "original": selected_before,
        "static_rank_rule": static,
        "controlled": selected_after,
        "additional_provider_calls": 0,
        "real_world_causal_claim": False,
    }
    result = {
        "schema": "ofc.control-proof/1",
        "evidence_anchor": anchor_digest or plan.digest,
        "authorized_nodes": sorted(authorized_nodes),
        "baseline": plan.data(),
        "current": current.data(),
        "baseline_digest": plan.digest,
        "current_digest": current.digest,
        "revision": revision,
        "as_of": request.as_of,
        "mode": request.mode,
        "policy_manifest": deepcopy(MANIFEST),
        "policy_version": POLICY_VERSION,
        "policy_digest": POLICY_DIGEST,
        "evidence": [e.data() for e in evidence],
        "evidence_root": digest([e.data() for e in evidence]),
        "authority_keys": [
            a.data()
            for a in sorted((authorities or {}).values(), key=lambda a: a.issuer)
        ],
        "baseline_verdicts": original_verdicts,
        "verdicts": verdicts,
        "patches": [p.data() for p in patches],
        "impact": affected,
        "rechecks": rechecks,
        "constraints": constraint_results,
        "optimization": optimization,
        "unresolved": unresolved,
        "comparison": comparison,
        "events": events,
        "journal_head": events[-1]["hash"],
        "action_status": "WITHHELD",
        "release_authorized": False,
        "external_action_executed": False,
        "provider_calls": 0,
        "spend_minor": 0,
        "issuer_signature": None,
        "signature_status": "UNSIGNED_CONTROL_RECORD",
        "status": "REVIEW_REQUIRED"
        if unresolved or blockers
        else "EVIDENCE_COMPLETE_ACTION_WITHHELD",
    }
    result["proof_digest"] = digest(result)
    return result


def review(
    request: ReviewRequest,
    *,
    authorities=None,
    optimizer=optimize,
    on_round=None,
    delegations=(),
):
    request = ReviewRequest.parse(request.data())
    from .ownership import authorized

    authorized_nodes = {
        n.id
        for n in request.plan.nodes
        if n.owner != request.plan.actor.id
        and authorized(
            delegations,
            tenant_id=request.plan.tenant_id,
            owner=n.owner,
            delegate=request.plan.actor.id,
            plan_digest=request.plan.digest,
            nodes=(n.id,),
            permission="patch",
            at=request.as_of,
        )
    }
    current, rounds, seen = request.plan, [], {request.plan.digest}
    started = monotonic_ns()
    for index in range(request.plan.limits.max_rounds):
        step = _single_review(
            request.model_copy(update={"plan": current}),
            authorities=authorities,
            anchor_digest=request.plan.digest,
            optimizer=optimizer,
            authorized_nodes=authorized_nodes,
        )
        if index and not step["patches"] and len(rounds) == 1:
            break
        rounds.append(step)
        if on_round:
            on_round(index + 1, step)
        current = Plan.parse(step["current"])
        if optimizer is not optimize:
            break
        if not step["patches"] or current.digest in seen:
            break
        seen.add(current.digest)
        if (monotonic_ns() - started) // 1_000_000 >= request.plan.limits.deadline_ms:
            break
    return cascade_record(request, rounds)


def cascade_record(request, rounds):
    current = Plan.parse(rounds[-1]["current"])
    if len(rounds) == 1:
        return rounds[0]
    result = deepcopy(rounds[-1])
    result.update(
        schema="ofc.control-cascade/1",
        baseline=request.plan.data(),
        baseline_digest=request.plan.digest,
        baseline_verdicts=rounds[0]["baseline_verdicts"],
        revision=sum(bool(r["patches"]) for r in rounds),
        rounds=rounds,
        termination="FIXED_POINT"
        if not rounds[-1]["patches"]
        and rounds[-1]["optimization"]["status"] != "OPTIMIZATION_UNDECIDED"
        else "BOUND_REACHED",
    )
    changes = {
        n.id: {
            f
            for f in n.mutable_fields
            if getattr(n, f)
            != getattr(next(x for x in current.nodes if x.id == n.id), f)
        }
        for n in request.plan.nodes
    }
    changes = {k: v for k, v in changes.items() if v}
    result["impact"] = impact(request.plan, changes)
    result["patches"] = [
        {
            "node_id": k,
            "changes": {
                f: getattr(next(n for n in current.nodes if n.id == k), f) for f in v
            },
            "evidence_ids": sorted(
                {
                    e
                    for r in rounds
                    for p in r["patches"]
                    if p["node_id"] == k
                    for e in p["evidence_ids"]
                }
            ),
        }
        for k, v in sorted(changes.items())
    ]
    result["comparison"] = rounds[0]["comparison"] | {
        "controlled": {n.id: n.candidate_id for n in current.nodes}
    }
    result.pop("proof_digest", None)
    result["proof_digest"] = digest(result)
    return result
