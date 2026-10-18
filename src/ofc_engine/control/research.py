from __future__ import annotations

import hashlib
import hmac
from typing import Literal
from pydantic import Field

from .canonical import digest
from .models import Hash, Id, Strict


class Feedback(Strict):
    schema_version: Literal["ofc.feedback/1"] = Field(
        default="ofc.feedback/1", alias="schema"
    )
    study_id: Id
    assignment_id: Hash
    consent: Literal[True]
    source_id: Id
    scorer_id: Id
    proof_digest: Hash
    outcome: Literal["ACCEPT", "REJECT", "ABSTAIN"]
    review_seconds: int = Field(ge=0, le=86400)
    avoided_loss_minor: int | None = Field(default=None, ge=0, le=10**12)
    operator_cost_per_hour_minor: int | None = Field(default=None, ge=0, le=10**9)


def assign(study_id, participant_id, *, secret):
    if len(secret) < 32:
        raise ValueError("study assignment requires a private key")
    token = hmac.new(
        secret, (study_id + ":" + participant_id).encode(), hashlib.sha256
    ).hexdigest()
    return {
        "study_id": study_id,
        "assignment_id": token,
        "variant": "CONTROLLED" if int(token[-1], 16) % 2 else "BASELINE",
    }


def study_report(feedback, *, assumptions):
    rows = [Feedback.model_validate_json(__import__("json").dumps(v)) for v in feedback]
    if len({r.assignment_id for r in rows}) != len(rows):
        raise ValueError("duplicate study outcome")
    return {
        "schema": "ofc.study-report/1",
        "status": "RECORDED_OUTCOMES" if rows else "NOT_RUN",
        "sample_size": len(rows),
        "accepted": sum(r.outcome == "ACCEPT" for r in rows),
        "abstained": sum(r.outcome == "ABSTAIN" for r in rows),
        "assumptions": assumptions,
        "record_digest": digest([r.data() for r in rows]),
        "independent_validation": False,
        "reported_avoided_loss_minor": sum(r.avoided_loss_minor or 0 for r in rows),
        "causal_roi_claim": False,
    }
