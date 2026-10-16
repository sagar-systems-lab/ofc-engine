"""One immutable Qloo evidence set, compared with OFC disabled and enabled."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re

from .contracts import PlanSnapshot
from .durable import StorageIntegrityError
from .revisions import plan_digest

_HEX64 = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class CopilotPair:
    """Stored product projection; no provider calls or policy verdicts from the LLM."""

    run_id: str
    prompt_digest: str
    model_id: str
    baseline_digest: str
    reviewed_digest: str
    qloo_evidence_digest: str
    off: dict
    on: dict
    verification_status: str

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "prompt_digest": self.prompt_digest,
            "model_id": self.model_id,
            "baseline_digest": self.baseline_digest,
            "reviewed_digest": self.reviewed_digest,
            "qloo_evidence_digest": self.qloo_evidence_digest,
            "off": self.off,
            "on": self.on,
            "verification_status": self.verification_status,
            "external_action_executed": False,
        }


def build_copilot_pair(
    *, prompt: str, model_id: str, baseline: PlanSnapshot,
    reviewed: PlanSnapshot, evaluated: dict,
) -> CopilotPair:
    """Use the one validated Qloo run in BOTH arms; toggle costs zero requests."""
    if (
        not isinstance(prompt, str) or not prompt.strip()
        or not isinstance(model_id, str) or not model_id.strip()
    ):
        raise ValueError("model and prompt provenance required")
    base_digest = plan_digest(baseline)
    current_digest = plan_digest(reviewed)
    if (
        baseline.plan_id != reviewed.plan_id
        or baseline.revision != 0
        or evaluated.get("run_id") != baseline.plan_id
        or evaluated.get("baseline_plan_digest") != base_digest
        or evaluated.get("plan_digest") != current_digest
        or evaluated.get("revision") != reviewed.revision
        or evaluated.get("release_authorized") is not False
        or evaluated.get("external_action_executed") is not False
    ):
        raise StorageIntegrityError("comparison is not bound to the evaluated run")

    original = {d.decision_id: d for d in baseline.decisions}
    current = {d.decision_id: d for d in reviewed.decisions}
    if set(original) != set(current):
        raise StorageIntegrityError("decision IDs changed between arms")
    changed = [
        d.decision_id for d in baseline.decisions
        if current[d.decision_id].candidate_id != d.candidate_id
    ]
    if changed != evaluated.get("changed_ids"):
        raise StorageIntegrityError("comparison revision or repair mismatch")

    supplied = evaluated.get("decisions")
    if not isinstance(supplied, list) or len(supplied) != len(original):
        raise StorageIntegrityError("missing evaluated decision records")
    if {x.get("decision_id") for x in supplied if isinstance(x, dict)} != set(original):
        raise StorageIntegrityError("evaluation decision IDs are not scope-bound")

    observations = []
    off_decisions = []
    on_decisions = []
    for item in supplied:
        decision_id = item["decision_id"]
        old = original[decision_id]
        new = current[decision_id]
        if (
            item.get("selected_candidate_id") != new.candidate_id
            or item.get("verdict") not in ("PASS", "UNCERTAIN")
            or not isinstance(item.get("reason_code"), str)
        ):
            raise StorageIntegrityError("invalid OFC evaluation projection")
        note = {
            "decision_id": decision_id,
            "domain": old.domain,
            "candidate_id": old.candidate_id,
            "candidate_pool": list(old.candidate_pool),
        }
        rank = item.get("qloo_rank_evidence")
        if rank is not None:
            if not isinstance(rank, dict) or (
                rank.get("source_mode") != "LIVE_HTTP_UNVERIFIED"
                or not _HEX64.fullmatch(rank.get("query_fingerprint", ""))
                or not _HEX64.fullmatch(rank.get("capture_digest", ""))
                or not isinstance(rank.get("collected_at"), str)
                or not isinstance(rank.get("ranked_candidate_ids"), list)
                or set(rank["ranked_candidate_ids"]) != set(old.candidate_pool)
                or len(rank["ranked_candidate_ids"]) != len(old.candidate_pool)
            ):
                raise StorageIntegrityError("Qloo evidence not bound to frozen candidate pool")
            observation = {
                "decision_id": decision_id,
                "query_fingerprint": rank["query_fingerprint"],
                "capture_digest": rank["capture_digest"],
                "ranked_candidate_ids": list(rank["ranked_candidate_ids"]),
                "collected_at": rank["collected_at"],
                "source_mode": "LIVE_HTTP_UNVERIFIED",
            }
            observations.append(observation)
            note["qloo_observation"] = observation
        else:
            note["qloo_observation"] = None
        off_decisions.append({**note, "cultural_rank_observed": rank is not None})
        on_decisions.append({
            **note,
            "candidate_id": new.candidate_id,
            "ofc_verdict": item["verdict"],
            "ofc_reason": item["reason_code"],
            "rank_recheck_completed": item.get("rank_recheck_completed") is True,
            "changed": old.candidate_id != new.candidate_id,
            "pending_recheck": decision_id in evaluated.get("pending_rechecks", []),
        })

    # Canonical hash binds the identical Qloo observations in BOTH views.
    from .revisions import digest_obj
    evidence_digest = digest_obj(observations)
    shared = {
        "provider": "QLOO",
        "source_mode": (
            "LIVE_HTTP_UNVERIFIED" if observations else "EVIDENCE_UNAVAILABLE"
        ),
        "observations": observations,
        "evidence_digest": evidence_digest,
    }
    off = {
        "ofc_enabled": False,
        "llm_enabled": True,
        "qloo": shared,
        "decisions": off_decisions,
        "plan_digest": base_digest,
        "verification_status": "NOT_EVALUATED_BY_OFC",
    }
    on = {
        "ofc_enabled": True,
        "llm_enabled": True,
        "qloo": shared,
        "decisions": on_decisions,
        "plan_digest": current_digest,
        "verification_status": "REVIEW_REQUIRED",
        "changed_ids": changed,
        "pending_rechecks": list(evaluated.get("pending_rechecks", [])),
        "release_authorized": False,
    }
    return CopilotPair(
        run_id=baseline.plan_id,
        prompt_digest=sha256(prompt.strip().encode()).hexdigest(),
        model_id=model_id,
        baseline_digest=base_digest,
        reviewed_digest=current_digest,
        qloo_evidence_digest=evidence_digest,
        off=off,
        on=on,
        verification_status="REVIEW_REQUIRED",
    )
