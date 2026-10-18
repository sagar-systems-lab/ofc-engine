from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import json
from itertools import product
from math import prod
from pathlib import Path
from time import monotonic

from .canonical import digest, strict_json
from .graph import claim_fingerprint, constraints, impact
from .models import Patch, Plan, ReviewRequest
from .policies import Authority, MANIFEST, POLICY_DIGEST, POLICY_VERSION, evaluate, required_claims


class ProofRejected(ValueError):
    pass


def _verify_repair_objective(baseline, current, before, optimization):
    ranks = {}
    for node in baseline.nodes:
        for claim in before[node.id]["claims"]:
            if "candidate_id" in node.mutable_fields and claim["claim"] == "cultural_brand_rank" and claim["status"] == "QUALIFIED":
                ranks[node.id] = claim["ranked_candidates"]
    keys = sorted(ranks)
    combinations = prod(len(ranks[key]) for key in keys)
    if not keys or combinations > baseline.limits.max_expansions:
        raise ProofRejected("repair optimum exceeds declared search bound")
    original = {node.id: node.candidate_id for node in baseline.nodes}

    def score(plan):
        choices = {node.id: node.candidate_id for node in plan.nodes}
        changed = {key: {"candidate_id"} for key in keys if choices[key] != original[key]}
        return (sum(c["status"] != "PASS" for c in constraints(plan)),
                sum(choices[key] != ranks[key][0] for key in keys), len(changed),
                len(impact(baseline, changed)["invalidated"]))

    best, optimum_digest, ties = None, None, 0
    deadline = monotonic() + 5
    payload = baseline.data()
    for choices in product(*(ranks[key] for key in keys)):
        if monotonic() > deadline:
            raise ProofRejected("repair optimum verification budget exceeded")
        selection = dict(zip(keys, choices))
        candidate_data = payload | {"nodes": [node | {"candidate_id": selection.get(node["id"], node["candidate_id"])} for node in payload["nodes"]]}
        candidate = Plan.parse(candidate_data)
        value = score(candidate)
        if value[0] != 0:
            continue
        if best is None or value < best:
            best, optimum_digest, ties = value, candidate.digest, 1
        elif value == best:
            ties += 1
    if (best is None or ties != 1 or optimum_digest != current.digest or best >= score(baseline)
            or optimization.get("status") != "REPAIR_PROPOSED" or optimization.get("objective") != list(best)
            or optimization.get("expansions") != combinations
            or optimization.get("objective_order") != ["hard_constraint_failures", "unmet_rank_obligations", "edits", "downstream_rechecks"]):
        raise ProofRejected("repair is not the unique minimum qualified change")


def verify(proof: dict, *, expected_digest: str | None = None,
           trusted_authorities: dict[str, Authority] | None = None) -> dict:
    try:
        document = deepcopy(proof)
        claimed = document.pop("proof_digest")
        if digest(document) != claimed or expected_digest is not None and expected_digest != claimed:
            raise ProofRejected("proof digest mismatch")
        if (document["schema"] != "ofc.control-proof/1" or document["action_status"] != "WITHHELD"
                or document["external_action_executed"] is not False or document["release_authorized"] is not False
                or document["provider_calls"] != 0 or document["spend_minor"] != 0
                or document["issuer_signature"] is not None or document["signature_status"] != "UNSIGNED_CONTROL_RECORD"):
            raise ProofRejected("invalid control boundary")
        if document["policy_manifest"] != MANIFEST or document["policy_digest"] != POLICY_DIGEST or document["policy_version"] != POLICY_VERSION:
            raise ProofRejected("policy drift")
        request = ReviewRequest.parse({"plan": document["baseline"], "evidence": document["evidence"],
                                       "mode": document["mode"], "as_of": document["as_of"]})
        baseline, current = request.plan, Plan.parse(document["current"])
        if baseline.digest != document["baseline_digest"] or current.digest != document["current_digest"]:
            raise ProofRejected("plan digest mismatch")
        if digest(document["evidence"]) != document["evidence_root"]:
            raise ProofRejected("evidence root mismatch")
        if request.mode != "SYNTHETIC" and trusted_authorities is None:
            raise ProofRejected("private proof requires independently supplied authority keys")
        authorities = {key: value for key, value in (trusted_authorities or {}).items() if value.tenant_id == baseline.tenant_id}
        embedded = sorted(document["authority_keys"], key=lambda a: a["issuer"])
        if embedded != [a.data() for a in sorted(authorities.values(), key=lambda a: a.issuer)]:
            raise ProofRejected("authority anchor mismatch")
        old, new = {n.id: n.data() for n in baseline.nodes}, {n.id: n.data() for n in current.nodes}
        if old.keys() != new.keys():
            raise ProofRejected("node set changed")
        envelope_before, envelope_after = baseline.data(), current.data()
        envelope_before.pop("nodes"); envelope_after.pop("nodes")
        if envelope_before != envelope_after:
            raise ProofRejected("frozen envelope changed")
        patches = {p["node_id"]: Patch.model_validate_json(json.dumps(p)) for p in document["patches"]}
        if len(patches) != len(document["patches"]):
            raise ProofRejected("duplicate patch")
        changed = {}
        for key in old:
            fields = {field for field in old[key] if old[key][field] != new[key][field]}
            if fields:
                patch = patches.get(key)
                if patch is None or not fields <= set(old[key]["mutable_fields"]) or fields != set(patch.changes):
                    raise ProofRejected("unauthorized field mutation")
                if any(new[key][field] != value for field, value in patch.changes.items()):
                    raise ProofRejected("patch result mismatch")
                changed[key] = fields
        if set(changed) != set(patches) or document["revision"] != int(bool(changed)):
            raise ProofRejected("revision mismatch")
        if impact(baseline, changed) != document["impact"] or constraints(current) != document["constraints"]:
            raise ProofRejected("impact or constraint mismatch")
        if changed and any(c["status"] != "PASS" for c in constraints(current)):
            raise ProofRejected("unsafe repair constraints")
        kwargs = {"baseline_digest": baseline.digest, "as_of": request.as_of,
                  "synthetic": request.mode == "SYNTHETIC", "authorities": authorities}
        before = evaluate(baseline, request.evidence, **kwargs)
        after = evaluate(current, request.evidence, **kwargs)
        if before != document["baseline_verdicts"] or after != document["verdicts"]:
            raise ProofRejected("claim evaluation mismatch")
        for key, patch in patches.items():
            ranks = [c for c in before[key]["claims"] if c["claim"] == "cultural_brand_rank" and c["status"] == "QUALIFIED"]
            if (set(patch.changes) != {"candidate_id"} or not ranks
                    or set(patch.evidence_ids) != set(ranks[0]["evidence_ids"])):
                raise ProofRejected("patch is not evidence licensed")
        if changed:
            _verify_repair_objective(baseline, current, before, document["optimization"])
        expected_unresolved = [{"node_id": n.id, "reasons": after[n.id]["reasons"]}
                               for n in current.nodes if n.required and after[n.id]["status"] != "QUALIFIED"]
        if expected_unresolved != document["unresolved"]:
            raise ProofRejected("unresolved obligations hidden")
        expected_rechecks = []
        for key in document["impact"]["recheck_order"]:
            node = next(n for n in current.nodes if n.id == key)
            changed_claims = [claim for claim in required_claims(current, node) if claim_fingerprint(baseline, key, claim) != claim_fingerprint(current, key, claim)]
            expected_rechecks.append({"node_id": key, "status": after[key]["status"],
                                      "invalidated_claims": changed_claims, "provider_calls": 0,
                                      "reason": "LOCAL_POLICY_RECHECK"})
        if expected_rechecks != document["rechecks"]:
            raise ProofRejected("recheck result mismatch")
        prior = "0" * 64
        events = document["events"]
        required = ["FROZEN", "EVALUATED"] + (["PATCH_COMMITTED", "INVALIDATED", "RECHECKED"] if changed else []) + ["HANDOFF_WITHHELD"]
        if [e["kind"] for e in events] != required:
            raise ProofRejected("journal transition missing")
        for sequence, event in enumerate(events, 1):
            fields = {key: value for key, value in event.items() if key != "hash"}
            if event["sequence"] != sequence or event["previous"] != prior or digest(fields) != event["hash"]:
                raise ProofRejected("journal chain mismatch")
            prior = event["hash"]
        if prior != document["journal_head"]:
            raise ProofRejected("journal head mismatch")
        if events[0]["payload"] != {"digest": baseline.digest, "revision": 0, "policy_digest": POLICY_DIGEST}:
            raise ProofRejected("freeze journal mismatch")
        if events[1]["payload"] != {"verdict_digest": digest(before), "evidence_root": document["evidence_root"]}:
            raise ProofRejected("evidence journal mismatch")
        if changed:
            if events[2]["payload"] != {"parent_digest": baseline.digest, "digest": current.digest,
                                         "revision": document["revision"], "patches": document["patches"], "policy_digest": POLICY_DIGEST}:
                raise ProofRejected("patch journal mismatch")
            if events[3]["payload"] != {"nodes": document["impact"]["invalidated"], "paths": document["impact"]["paths"]}:
                raise ProofRejected("invalidation journal mismatch")
            if events[4]["payload"] != {"checks": expected_rechecks, "verdict_digest": digest(after)}:
                raise ProofRejected("recheck journal mismatch")
        blockers = [c for c in document["constraints"] if c["status"] != "PASS"]
        if events[-1]["payload"] != {"unresolved": expected_unresolved, "constraints": blockers, "external_action_executed": False}:
            raise ProofRejected("handoff journal mismatch")
        status = "REVIEW_REQUIRED" if expected_unresolved or blockers else "EVIDENCE_COMPLETE_ACTION_WITHHELD"
        if document["status"] != status:
            raise ProofRejected("review status mismatch")
        comparison = document["comparison"]
        if (comparison["shared_plan_digest"] != baseline.digest or comparison["shared_evidence_root"] != document["evidence_root"]
                or comparison["original"] != {n.id: n.candidate_id for n in baseline.nodes}
                or comparison["controlled"] != {n.id: n.candidate_id for n in current.nodes}
                or comparison["additional_provider_calls"] != 0 or comparison["real_world_causal_claim"] is not False):
            raise ProofRejected("comparison scope mismatch")
        static = {n.id: n.candidate_id for n in baseline.nodes}
        for node in baseline.nodes:
            for claim in before[node.id]["claims"]:
                if claim["claim"] == "cultural_brand_rank" and claim["status"] == "QUALIFIED" and "candidate_id" in node.mutable_fields:
                    static[node.id] = claim["ranked_candidates"][0]
        if comparison["static_rank_rule"] != static:
            raise ProofRejected("static comparator mismatch")
        return {"valid": True, "proof_digest": claimed, "mode": request.mode,
                "action_status": "WITHHELD", "provider_authenticity": ("TRUSTED_ISSUER_ATTESTATION" if authorities else "SYNTHETIC_ONLY" if request.mode == "SYNTHETIC" else "NO_QUALIFIED_ISSUER"),
                "signature_status": "UNSIGNED_CONTROL_RECORD", "external_action_executed": False}
    except (KeyError, TypeError, ValueError, IndexError, RecursionError, OverflowError) as exc:
        if isinstance(exc, ProofRejected):
            raise
        raise ProofRejected("malformed control proof") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify a saved OFC review without network access")
    parser.add_argument("proof", type=Path)
    parser.add_argument("--expected-digest")
    parser.add_argument("--authorities", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.proof.stat().st_size > 2_000_000:
            raise ProofRejected("proof exceeds size limit")
        authorities = None
        if args.authorities:
            authorities = {}
            for item in strict_json(args.authorities.read_bytes()):
                public_key = base64.b64decode(item.pop("public_key"), validate=True)
                item["claims"] = frozenset(item["claims"])
                authority = Authority(public_key=public_key, **item)
                authorities[authority.issuer] = authority
        print(json.dumps(verify(strict_json(args.proof.read_bytes()), expected_digest=args.expected_digest,
                                trusted_authorities=authorities), sort_keys=True))
    except (OSError, ValueError, RecursionError, OverflowError) as exc:
        print(json.dumps({"valid": False, "reason": str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
