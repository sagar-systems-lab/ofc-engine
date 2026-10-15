"""Provisional, provider-independent plan and evidence contracts."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "0.1.0"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourceMode(str, Enum):
    SYNTHETIC = "SYNTHETIC"
    RECORDED_REDACTED = "RECORDED_REDACTED"
    LIVE_HTTP_UNVERIFIED = "LIVE_HTTP_UNVERIFIED"
    VERIFIED_LIVE = "VERIFIED_LIVE"


class EvidenceStatus(str, Enum):
    VALID = "VALID"
    INSUFFICIENT = "INSUFFICIENT"
    STALE = "STALE"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


class Verdict(str, Enum):
    PASS = "PASS"
    REPAIR = "REPAIR"
    UNCERTAIN = "UNCERTAIN"


class RunPhase(str, Enum):
    PROPOSED = "PROPOSED"
    BASELINE_FROZEN = "BASELINE_FROZEN"
    EVIDENCE_LOADING = "EVIDENCE_LOADING"
    EVALUATED = "EVALUATED"
    REPAIRING = "REPAIRING"
    VERIFYING = "VERIFYING"
    RELEASE_READY = "RELEASE_READY"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


class ActionIntent(StrictModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str = Field(min_length=1)
    client_request_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    constraints: dict[str, Any]
    taste_context: dict[str, Any]


class Decision(StrictModel):
    decision_id: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    candidate_pool: tuple[str, ...] = Field(min_length=1)
    mutable_fields: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    locality_ref: str | None = None
    required: bool = True

    @model_validator(mode="after")
    def validate_fields(self) -> Decision:
        if not all(x for x in self.candidate_pool):
            raise ValueError("candidate pool cannot contain empty IDs")
        if len(set(self.candidate_pool)) != len(self.candidate_pool):
            raise ValueError("candidate pool has duplicates")
        if self.candidate_id not in self.candidate_pool:
            raise ValueError("selected candidate is outside fixed pool")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("dependency list has duplicates")
        if len(set(self.mutable_fields)) != len(self.mutable_fields):
            raise ValueError("field allowlist has duplicates")
        if any(x != "candidate_id" for x in self.mutable_fields):
            raise ValueError("current contract only supports candidate_id repair; other fields are not supported by this contract")
        return self


class PlanSnapshot(StrictModel):
    schema_version: str = SCHEMA_VERSION
    plan_id: str = Field(min_length=1)
    revision: int = Field(ge=0)
    baseline_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    constraints_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    decisions: tuple[Decision, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dag(self) -> PlanSnapshot:
        by_id = {d.decision_id: d for d in self.decisions}
        if len(by_id) != len(self.decisions):
            raise ValueError("duplicate decision_id")
        for d in self.decisions:
            if any(x not in by_id for x in d.depends_on):
                raise ValueError("dependency refers to unknown decision")
            if d.decision_id in d.depends_on:
                raise ValueError("self dependency")
        active: set[str] = set()
        done: set[str] = set()

        def visit(node: str) -> None:
            if node in active:
                raise ValueError("cyclic dependencies")
            if node in done:
                return
            active.add(node)
            for dep in by_id[node].depends_on:
                visit(dep)
            active.remove(node)
            done.add(node)

        for d in self.decisions:
            visit(d.decision_id)
        return self


class EvidenceQuerySpec(StrictModel):
    query_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    input_entity_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    locality_ref: str | None = None
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class QlooEvidence(StrictModel):
    evidence_id: str = Field(min_length=1)
    source_mode: SourceMode
    status: EvidenceStatus
    query_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    collected_at: datetime
    expires_at: datetime | None = None
    candidate_ranks: dict[str, int]
    explainability: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_times(self) -> QlooEvidence:
        if self.collected_at.utcoffset() is None:
            raise ValueError("evidence collected_at must be timezone-aware")
        if self.expires_at is not None:
            if self.expires_at.utcoffset() is None:
                raise ValueError("evidence expires_at must be timezone-aware")
            if self.expires_at <= self.collected_at:
                raise ValueError("evidence expires_at must follow collected_at")
        if any(v < 1 for v in self.candidate_ranks.values()):
            raise ValueError("ranks start at 1")
        return self


class DecisionVerdict(StrictModel):
    decision_id: str = Field(min_length=1)
    plan_revision: int = Field(ge=0)
    verdict: Verdict
    reason_codes: tuple[str, ...] = Field(min_length=1)
    evidence_refs: tuple[str, ...]
    policy_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_provenance(self) -> DecisionVerdict:
        if self.verdict != Verdict.UNCERTAIN and not self.evidence_refs:
            raise ValueError("PASS/REPAIR requires provenance")
        return self


class RepairPatch(StrictModel):
    patch_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    base_revision: int = Field(ge=0)
    old_candidate_id: str = Field(min_length=1)
    new_candidate_id: str = Field(min_length=1)
    field_mask: tuple[str, ...] = ("candidate_id",)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    precondition_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_mask(self) -> RepairPatch:
        if self.field_mask != ("candidate_id",):
            raise ValueError("patch may only change candidate_id")
        if self.old_candidate_id == self.new_candidate_id:
            raise ValueError("no-op patch forbidden")
        return self


class VerificationResult(StrictModel):
    verified_revision: int = Field(ge=0)
    verified_plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    checked_decision_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    policy_version: str = Field(min_length=1)
    status: str


class ReleaseArtifact(StrictModel):
    plan_id: str = Field(min_length=1)
    revision: int = Field(ge=0)
    verified_plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    verification_status: str
    source_mode: SourceMode
    external_action_executed: bool = False

    @model_validator(mode="after")
    def no_real_execution(self) -> ReleaseArtifact:
        if self.external_action_executed:
            raise ValueError("external execution is not supported")
        if self.source_mode != SourceMode.VERIFIED_LIVE and self.verification_status == "VERIFIED":
            raise ValueError("synthetic or recorded data cannot claim live verification")
        return self
