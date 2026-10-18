from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import json
from pathlib import Path

from .canonical import digest, strict_json
from itertools import product
from time import monotonic
from .graph import claim_fingerprint, constraints, impact, recheck_state
from .models import Patch, Plan, ReviewRequest, instant
from .policies import (
    Authority,
    MANIFEST,
    POLICY_DIGEST,
    POLICY_VERSION,
    evaluate,
    required_claims,
)


class ProofRejected(ValueError):
    pass


def _verify_repair_objective(
    baseline, current, before, optimization, evidence, evaluation, authorized_nodes
):
    from .engine import decision_options, apply_patches

    options = decision_options(
        baseline,
        before,
        evidence=evidence,
        evaluation=evaluation,
        authorized_nodes=authorized_nodes,
    )
    nodes = {n.id: n for n in baseline.nodes}
    keys = sorted(options)
    alternatives = {
        k: options[k].get(
            "alternatives",
            [
                {
                    "changes": {}
                    if v == nodes[k].candidate_id
                    else {"candidate_id": v},
                    "evidence_ids": options[k]["evidence_ids"],
                }
                for v in options[k]["order"]
            ],
        )
        for k in keys
    }

    def score(plan):
        by_id = {n.id: n for n in plan.nodes}
        changed = {
            k: {
                f
                for f in nodes[k].mutable_fields
                if getattr(nodes[k], f) != getattr(by_id[k], f)
            }
            for k in keys
        }
        changed = {k: v for k, v in changed.items() if v}
        utility = sum(
            next(
                (
                    a["utility"]
                    for a in options[k]["alternatives"]
                    if all(getattr(by_id[k], f) == v for f, v in a["selection"].items())
                ),
                10**13,
            )
            if "alternatives" in options[k]
            else int(by_id[k].candidate_id != options[k]["order"][0])
            for k in keys
        )
        return (
            sum(c["status"] != "PASS" for c in constraints(plan)),
            utility,
            len(changed),
            len(impact(baseline, changed)["invalidated"]),
        )

    best, winners, expansions = None, [], 0
    deadline = monotonic() + 5
    for choices in product(*(alternatives[k] for k in keys)):
        if monotonic() > deadline:
            raise ProofRejected("repair verification deadline exceeded")
        expansions += 1
        if expansions > baseline.limits.max_expansions:
            raise ProofRejected("repair optimum exceeds declared bound")
        patches = tuple(
            Patch(
                node_id=k, changes=a["changes"], evidence_ids=tuple(a["evidence_ids"])
            )
            for k, a in zip(keys, choices)
            if a["changes"]
        )
        candidate = (
            apply_patches(baseline, patches, authorized_nodes=authorized_nodes)
            if patches
            else baseline
        )
        if patches:
            joint = evaluate(candidate, evidence, **evaluation)
            admissible = True
            for patch in patches:
                node = next(n for n in candidate.nodes if n.id == patch.node_id)
                claims = joint[node.id]["claims"]
                if (
                    node.domain == "brand"
                    and not baseline.budgets
                    and evaluation["synthetic"]
                ):
                    claims = [c for c in claims if c["claim"] == "cultural_brand_rank"]
                if (
                    not claims
                    or any(c["status"] != "QUALIFIED" for c in claims)
                    or joint[node.id]["upstream_blockers"]
                ):
                    admissible = False
            if not admissible:
                continue
        value = score(candidate)
        if value[0]:
            continue
        if best is None or value < best:
            best, winners = value, [candidate.digest]
        elif value == best:
            winners.append(candidate.digest)
    if (
        not keys
        or best is None
        or winners != [current.digest]
        or best >= score(baseline)
        or optimization.get("status") != "REPAIR_PROPOSED"
        or optimization.get("objective") != list(best)
        or optimization.get("expansions") != expansions
    ):
        raise ProofRejected("repair is not the unique minimum qualified change")


def verify(
    proof: dict,
    *,
    expected_digest: str | None = None,
    trusted_authorities: dict[str, Authority] | None = None,
    trusted_delegations=(),
    trusted_collection_keys=None,
) -> dict:
    try:
        if proof.get("schema") == "ofc.collected-review/1":
            from .collection_contracts import verify_collection

            fields = deepcopy(proof)
            claimed = fields.pop("proof_digest")
            if (
                digest(fields) != claimed
                or expected_digest is not None
                and expected_digest != claimed
            ):
                raise ProofRejected("collected proof digest mismatch")
            control = proof["control"]
            verify(
                control,
                trusted_authorities=trusted_authorities,
                trusted_delegations=trusted_delegations,
            )
            verify_collection(
                proof["collection"],
                trusted_keys=trusted_collection_keys or {},
                plan=Plan.parse(control["baseline"]),
                expected_policy=POLICY_DIGEST,
            )
            expected = dict(control)
            expected.update(
                schema="ofc.collected-review/1",
                control=control,
                collection=proof["collection"],
                provider_calls=proof["collection"]["provider_calls"],
                spend_minor=0,
            )
            expected.pop("proof_digest", None)
            expected["proof_digest"] = digest(expected)
            if expected != proof:
                raise ProofRejected("collected summary mismatch")
            return {
                "valid": True,
                "proof_digest": claimed,
                "action_status": "WITHHELD",
                "provider_calls": proof["provider_calls"],
                "spend_minor": 0,
                "external_action_executed": False,
            }
        if proof.get("schema") == "ofc.control-cascade/1":
            from .engine import cascade_record

            request = ReviewRequest.parse(
                {
                    "plan": proof["baseline"],
                    "evidence": proof["evidence"],
                    "mode": proof["mode"],
                    "as_of": proof["as_of"],
                }
            )
            rounds = proof.get("rounds", [])
            if not 2 <= len(rounds) <= request.plan.limits.max_rounds:
                raise ProofRejected("invalid cascade round bound")
            previous = request.plan.digest
            evidence_before = {
                e["id"]: e for e in proof.get("initial_evidence", proof["evidence"])
            }
            clock = proof.get("frozen_at", proof["as_of"])
            for step in rounds:
                evidence_now = {e["id"]: e for e in step["evidence"]}
                if (
                    any(evidence_now.get(k) != v for k, v in evidence_before.items())
                    or instant(step["as_of"]) < instant(clock)
                    or step["mode"] != request.mode
                ):
                    raise ProofRejected("cascade evidence or clock regression")
                if (
                    step["baseline_digest"] != previous
                    or step.get("evidence_anchor") != request.plan.digest
                ):
                    raise ProofRejected("cascade parent mismatch")
                verify(
                    step,
                    trusted_authorities=trusted_authorities,
                    trusted_delegations=trusted_delegations,
                )
                previous, clock, evidence_before = (
                    step["current_digest"],
                    step["as_of"],
                    evidence_now,
                )
            if rounds[-1]["evidence"] != proof["evidence"]:
                raise ProofRejected("cascade evidence tail mismatch")
            request = ReviewRequest.parse(
                {
                    "plan": proof["baseline"],
                    "evidence": proof.get("initial_evidence", proof["evidence"]),
                    "mode": proof["mode"],
                    "as_of": proof.get("frozen_at", proof["as_of"]),
                }
            )
            regenerated = cascade_record(
                request, rounds, authorities=trusted_authorities
            )
            if (
                proof != regenerated
                or expected_digest is not None
                and expected_digest != proof["proof_digest"]
            ):
                raise ProofRejected("cascade envelope mismatch")
            return {
                "valid": True,
                "proof_digest": proof["proof_digest"],
                "mode": request.mode,
                "action_status": "WITHHELD",
                "external_action_executed": False,
            }
        document = __import__("copy").deepcopy(proof)
        claimed = document.pop("proof_digest")
        if (
            digest(document) != claimed
            or expected_digest is not None
            and expected_digest != claimed
        ):
            raise ProofRejected("proof digest mismatch")
        if (
            document["schema"] != "ofc.control-proof/1"
            or document["action_status"] != "WITHHELD"
            or document["external_action_executed"] is not False
            or document["release_authorized"] is not False
            or document["provider_calls"] != 0
            or document["spend_minor"] != 0
            or document["issuer_signature"] is not None
            or document["signature_status"] != "UNSIGNED_CONTROL_RECORD"
        ):
            raise ProofRejected("invalid control boundary")
        if (
            document["policy_manifest"] != MANIFEST
            or document["policy_digest"] != POLICY_DIGEST
            or document["policy_version"] != POLICY_VERSION
        ):
            raise ProofRejected("policy drift")
        request = ReviewRequest.parse(
            {
                "plan": document["baseline"],
                "evidence": document["evidence"],
                "mode": document["mode"],
                "as_of": document["as_of"],
            }
        )
        baseline, current = request.plan, Plan.parse(document["current"])
        if (
            baseline.digest != document["baseline_digest"]
            or current.digest != document["current_digest"]
        ):
            raise ProofRejected("plan digest mismatch")
        if digest(document["evidence"]) != document["evidence_root"]:
            raise ProofRejected("evidence root mismatch")
        if request.mode != "SYNTHETIC" and trusted_authorities is None:
            raise ProofRejected(
                "private proof requires independently supplied authority keys"
            )
        authorities = {
            key: value
            for key, value in (trusted_authorities or {}).items()
            if value.tenant_id == baseline.tenant_id
        }
        embedded = sorted(document["authority_keys"], key=lambda a: a["issuer"])
        if embedded != [
            a.data() for a in sorted(authorities.values(), key=lambda a: a.issuer)
        ]:
            raise ProofRejected("authority anchor mismatch")
        old, new = (
            {n.id: n.data() for n in baseline.nodes},
            {n.id: n.data() for n in current.nodes},
        )
        if old.keys() != new.keys():
            raise ProofRejected("node set changed")
        envelope_before, envelope_after = baseline.data(), current.data()
        envelope_before.pop("nodes")
        envelope_after.pop("nodes")
        if envelope_before != envelope_after:
            raise ProofRejected("frozen envelope changed")
        patches = {
            p["node_id"]: Patch.model_validate_json(json.dumps(p))
            for p in document["patches"]
        }
        if len(patches) != len(document["patches"]):
            raise ProofRejected("duplicate patch")
        changed = {}
        for key in old:
            fields = {field for field in old[key] if old[key][field] != new[key][field]}
            if fields:
                patch = patches.get(key)
                if (
                    patch is None
                    or not fields <= set(old[key]["mutable_fields"])
                    or fields != set(patch.changes)
                ):
                    raise ProofRejected("unauthorized field mutation")
                if any(
                    new[key][field] != value for field, value in patch.changes.items()
                ):
                    raise ProofRejected("patch result mismatch")
                changed[key] = fields
        if set(changed) != set(patches) or document["revision"] != int(bool(changed)):
            raise ProofRejected("revision mismatch")
        if (
            impact(baseline, changed) != document["impact"]
            or constraints(current) != document["constraints"]
        ):
            raise ProofRejected("impact or constraint mismatch")
        if changed and any(c["status"] != "PASS" for c in constraints(current)):
            raise ProofRejected("unsafe repair constraints")
        kwargs = {
            "baseline_digest": document.get("evidence_anchor", baseline.digest),
            "as_of": request.as_of,
            "synthetic": request.mode == "SYNTHETIC",
            "authorities": authorities,
        }
        before = evaluate(baseline, request.evidence, **kwargs)
        after = evaluate(current, request.evidence, **kwargs)
        if before != document["baseline_verdicts"] or after != document["verdicts"]:
            raise ProofRejected("claim evaluation mismatch")
        from .ownership import authorized

        authorized_nodes = document.get("authorized_nodes", [])
        expected_nodes = sorted(
            n.id
            for n in baseline.nodes
            if n.owner != baseline.actor.id
            and authorized(
                trusted_delegations,
                tenant_id=baseline.tenant_id,
                owner=n.owner,
                delegate=baseline.actor.id,
                plan_digest=document.get("evidence_anchor", baseline.digest),
                nodes=(n.id,),
                permission="patch",
                at=request.as_of,
            )
        )
        if authorized_nodes != expected_nodes:
            raise ProofRejected("delegation anchor mismatch")
        for key in patches:
            if old[key]["owner"] != baseline.actor.id and key not in authorized_nodes:
                raise ProofRejected("unauthorized node owner mutation")
        if changed:
            _verify_repair_objective(
                baseline,
                current,
                before,
                document["optimization"],
                request.evidence,
                kwargs,
                authorized_nodes,
            )
        expected_unresolved = [
            {"node_id": n.id, "reasons": after[n.id]["reasons"]}
            for n in current.nodes
            if n.required and after[n.id]["status"] != "QUALIFIED"
        ]
        if expected_unresolved != document["unresolved"]:
            raise ProofRejected("unresolved obligations hidden")
        expected_rechecks = []
        for key in document["impact"]["recheck_order"]:
            node = next(n for n in current.nodes if n.id == key)
            changed_claims = [
                claim
                for claim in required_claims(current, node)
                if claim_fingerprint(baseline, key, claim)
                != claim_fingerprint(current, key, claim)
            ]
            expected_rechecks.append(
                {
                    "node_id": key,
                    "status": after[key]["status"],
                    "invalidated_claims": changed_claims,
                    "evidence_state": recheck_state(
                        after[key]["status"], changed_claims
                    ),
                    "acquisition": "UNAVAILABLE"
                    if after[key]["status"] != "QUALIFIED"
                    else "RECHECK_COMPLETED",
                    "provider_calls": 0,
                    "reason": "LOCAL_POLICY_RECHECK",
                }
            )
        if expected_rechecks != document["rechecks"]:
            raise ProofRejected("recheck result mismatch")
        prior = "0" * 64
        events = document["events"]
        required = (
            ["FROZEN", "EVALUATED"]
            + (["PATCH_COMMITTED", "INVALIDATED", "RECHECKED"] if changed else [])
            + ["HANDOFF_WITHHELD"]
        )
        if [e["kind"] for e in events] != required:
            raise ProofRejected("journal transition missing")
        for sequence, event in enumerate(events, 1):
            fields = {key: value for key, value in event.items() if key != "hash"}
            if (
                event["sequence"] != sequence
                or event["previous"] != prior
                or digest(fields) != event["hash"]
            ):
                raise ProofRejected("journal chain mismatch")
            prior = event["hash"]
        if prior != document["journal_head"]:
            raise ProofRejected("journal head mismatch")
        if events[0]["payload"] != {
            "digest": baseline.digest,
            "revision": 0,
            "policy_digest": POLICY_DIGEST,
        }:
            raise ProofRejected("freeze journal mismatch")
        if events[1]["payload"] != {
            "verdict_digest": digest(before),
            "evidence_root": document["evidence_root"],
        }:
            raise ProofRejected("evidence journal mismatch")
        if changed:
            if events[2]["payload"] != {
                "parent_digest": baseline.digest,
                "digest": current.digest,
                "revision": document["revision"],
                "patches": document["patches"],
                "policy_digest": POLICY_DIGEST,
            }:
                raise ProofRejected("patch journal mismatch")
            if events[3]["payload"] != {
                "nodes": document["impact"]["invalidated"],
                "paths": document["impact"]["paths"],
            }:
                raise ProofRejected("invalidation journal mismatch")
            if events[4]["payload"] != {
                "checks": expected_rechecks,
                "verdict_digest": digest(after),
            }:
                raise ProofRejected("recheck journal mismatch")
        blockers = [c for c in document["constraints"] if c["status"] != "PASS"]
        if events[-1]["payload"] != {
            "unresolved": expected_unresolved,
            "constraints": blockers,
            "external_action_executed": False,
        }:
            raise ProofRejected("handoff journal mismatch")
        status = (
            "REVIEW_REQUIRED"
            if expected_unresolved or blockers
            else "EVIDENCE_COMPLETE_ACTION_WITHHELD"
        )
        if document["status"] != status:
            raise ProofRejected("review status mismatch")
        comparison = document["comparison"]
        if (
            comparison["shared_plan_digest"] != baseline.digest
            or comparison["shared_evidence_root"] != document["evidence_root"]
            or comparison["original"] != {n.id: n.candidate_id for n in baseline.nodes}
            or comparison["controlled"] != {n.id: n.candidate_id for n in current.nodes}
            or comparison["additional_provider_calls"] != 0
            or comparison["real_world_causal_claim"] is not False
        ):
            raise ProofRejected("comparison scope mismatch")
        static = {n.id: n.candidate_id for n in baseline.nodes}
        for node in baseline.nodes:
            for claim in before[node.id]["claims"]:
                if (
                    claim["claim"]
                    in ("cultural_brand_rank", "place_cultural_relevance")
                    and claim["status"] == "QUALIFIED"
                    and "candidate_id" in node.mutable_fields
                    and node.owner == baseline.actor.id
                ):
                    static[node.id] = claim["ranked_candidates"][0]
        if comparison["static_rank_rule"] != static:
            raise ProofRejected("static comparator mismatch")
        return {
            "valid": True,
            "proof_digest": claimed,
            "mode": request.mode,
            "action_status": "WITHHELD",
            "provider_authenticity": (
                "TRUSTED_ISSUER_ATTESTATION"
                if authorities
                else "SYNTHETIC_ONLY"
                if request.mode == "SYNTHETIC"
                else "NO_QUALIFIED_ISSUER"
            ),
            "signature_status": "UNSIGNED_CONTROL_RECORD",
            "external_action_executed": False,
        }
    except (
        KeyError,
        TypeError,
        ValueError,
        IndexError,
        RecursionError,
        OverflowError,
    ) as exc:
        if isinstance(exc, ProofRejected):
            raise
        raise ProofRejected("malformed control proof") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify a saved OFC review without network access"
    )
    parser.add_argument("proof", type=Path)
    parser.add_argument("--expected-digest")
    parser.add_argument("--authorities", type=Path)
    parser.add_argument("--collection-keys", type=Path)
    parser.add_argument("--delegations", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.proof.stat().st_size > 8_000_000:
            raise ProofRejected("proof exceeds size limit")
        authorities = None
        if args.authorities:
            authorities = {}
            for item in strict_json(args.authorities.read_bytes()):
                public_key = base64.b64decode(item.pop("public_key"), validate=True)
                item["claims"] = frozenset(item["claims"])
                item["revoked_entitlements"] = frozenset(
                    item.get("revoked_entitlements", [])
                )
                authority = Authority(public_key=public_key, **item)
                authorities[authority.issuer] = authority
        collection_keys = {}
        if args.collection_keys:
            collection_keys = {
                name: base64.b64decode(key, validate=True)
                for name, key in strict_json(args.collection_keys.read_bytes()).items()
            }
        delegations = []
        if args.delegations:
            from .ownership import Delegation

            for item in strict_json(args.delegations.read_bytes()):
                item["node_ids"] = frozenset(item["node_ids"])
                item["permissions"] = frozenset(item["permissions"])
                delegations.append(Delegation(**item))
        print(
            json.dumps(
                verify(
                    strict_json(args.proof.read_bytes()),
                    expected_digest=args.expected_digest,
                    trusted_authorities=authorities,
                    trusted_collection_keys=collection_keys,
                    trusted_delegations=delegations,
                ),
                sort_keys=True,
            )
        )
    except (OSError, ValueError, RecursionError, OverflowError) as exc:
        print(json.dumps({"valid": False, "reason": str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
