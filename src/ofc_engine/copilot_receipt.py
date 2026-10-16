"""Deterministic partner receipt for the OFC-only decision-control delta."""
from __future__ import annotations
from .durable import StorageIntegrityError
from .revisions import digest_obj


def decision_receipt(projection: dict, *, business_review: dict) -> dict:
    off = projection.get("off")
    on = projection.get("on")
    if (
        not isinstance(off, dict) or not isinstance(on, dict)
        or off.get("ofc_enabled") is not False
        or on.get("ofc_enabled") is not True
        or off.get("qloo") != on.get("qloo")
        or not isinstance(off.get("decisions"), list)
        or not isinstance(on.get("decisions"), list)
        or not isinstance(business_review, dict)
    ):
        raise StorageIntegrityError("comparison receipt is not evidence-bound")
    before = {d["decision_id"]: d for d in off["decisions"]}
    after = {d["decision_id"]: d for d in on["decisions"]}
    if not before or before.keys() != after.keys():
        raise StorageIntegrityError("receipt decision scope changed")
    changes = []
    checks = []
    for key, original in before.items():
        verified = after[key]
        changed = original["candidate_id"] != verified["candidate_id"]
        if changed != verified.get("changed"):
            raise StorageIntegrityError("repair receipt changed flag mismatch")
        checks.append({
            "decision_id": key,
            "verdict": verified.get("ofc_verdict", "UNCERTAIN"),
            "reason": verified.get("ofc_reason", "NO_QUALIFIED_EVIDENCE"),
            "rank_rechecked": verified.get("rank_recheck_completed") is True,
            "candidate_changed": changed,
        })
        if changed:
            changes.append({
                "decision_id": key,
                "before": original["candidate_id"],
                "after": verified["candidate_id"],
                "reason": verified.get("ofc_reason", "UNKNOWN"),
            })
    receipt = {
        "schema_version": "ofc-value.v1",
        "run_id": projection["run_id"],
        "baseline_digest": projection["baseline_digest"],
        "reviewed_digest": projection["reviewed_digest"],
        "shared_qloo_evidence_digest": projection["qloo_evidence_digest"],
        "model_id": projection["model_id"],
        "comparison": "SAME_QLOO_OFF_VS_ON",
        "decision_count": len(checks),
        "changed_count": len(changes),
        "verified_rank_count": sum(x["rank_rechecked"] for x in checks),
        "changes": changes,
        "checks": checks,
        "business_evidence_satisfied": business_review.get(
            "business_evidence_satisfied", False
        ) is True,
        "missing_business_requirements": business_review.get(
            "missing_requirements", {}
        ),
        "release_authorized": False,
        "external_action_executed": False,
    }
    receipt["receipt_digest"] = digest_obj(receipt)
    return receipt


def grounded_explanations(receipt: dict) -> dict:
    if receipt.get("schema_version") != "ofc-value.v1":
        raise ValueError("typed OFC receipt required")
    checks = receipt["checks"]
    comparisons = [
        f"{x['decision_id']}: {x['verdict']} ({x['reason']})"
        for x in checks
    ]
    n = receipt["changed_count"]
    off = (
        "LLM + Qloo baseline. This view shows the original choices and "
        "the same recorded Qloo cultural evidence. OFC decision controls "
        "were not applied."
    )
    on = (
        f"LLM + Qloo + OFC. {len(checks)} decision(s) reviewed; "
        f"{n} scoped selection(s) changed; "
        f"{receipt['verified_rank_count']} independent rank recheck(s) matched. "
        + ("Checks: " + "; ".join(comparisons) + ". " if comparisons else "")
        + "Commercial suitability and external actions are not automatically approved."
    )
    return {
        "off": off,
        "on": on,
        "source": "SERVER_VALIDATED_EVIDENCE_ONLY",
        "model_narrative_trusted": False,
    }
