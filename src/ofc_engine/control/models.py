from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical import canonical_bytes, digest, strict_json

Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Claim = Literal[
    "cultural_brand_rank",
    "place_cultural_observation",
    "budget_quote",
    "venue_capacity",
    "contract_rights",
    "schedule_window",
    "place_cultural_relevance",
    "channel_suitability",
    "media_suitability",
    "collaborator_suitability",
    "audience_suitability",
]
EdgeType = Literal[
    "REQUIRES",
    "COMPATIBLE_WITH",
    "CONFLICTS_WITH",
    "CONSUMES_BUDGET",
    "SHARES_RESOURCE",
    "TEMPORAL_BEFORE",
    "APPROVAL_REQUIRES",
]
CAUSAL_EDGES = frozenset({"REQUIRES", "APPROVAL_REQUIRES", "TEMPORAL_BEFORE"})
MutableField = Literal["candidate_id", "start_at", "end_at"]


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("UTC timestamp required")
    return parsed


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def data(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)


class Actor(Strict):
    id: Id
    kind: Literal["agent", "human"] = "agent"


class Candidate(Strict):
    id: Id
    label: str = Field(min_length=1, max_length=160)
    cost_minor: int | None = Field(default=None, ge=0, le=10**12)
    currency: Literal["USD", "EUR", "INR"] | None = None
    resource: Id | None = None

    @model_validator(mode="after")
    def currency_pair(self):
        if not self.label.strip():
            raise ValueError("candidate label must not be blank")
        if (self.cost_minor is None) != (self.currency is None):
            raise ValueError("cost and currency must be supplied together")
        return self


class Node(Strict):
    id: Id
    domain: str = Field(min_length=1, max_length=48, pattern=r"^[a-z][a-z_]*$")
    label: str = Field(min_length=1, max_length=160)
    candidate_id: Id
    candidate_pool: tuple[Candidate, ...] = Field(min_length=1, max_length=8)
    mutable_fields: tuple[MutableField, ...] = ()
    owner: Id
    required: bool = True
    locality: str | None = Field(default=None, max_length=96)
    subject_id: Id | None = None
    required_capacity: int | None = Field(default=None, ge=1, le=10**7)
    claims: tuple[Claim, ...] = ()
    start_at: str | None = Field(default=None, max_length=32)
    end_at: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def valid_node(self):
        if not self.label.strip():
            raise ValueError("decision label must not be blank")
        ids = [c.id for c in self.candidate_pool]
        if len(ids) != len(set(ids)) or self.candidate_id not in ids:
            raise ValueError("candidate pool must be unique and contain the selection")
        if len(self.mutable_fields) != len(set(self.mutable_fields)):
            raise ValueError("duplicate mutable field")
        if len(self.claims) != len(set(self.claims)):
            raise ValueError("duplicate claim")
        if (self.start_at is None) != (self.end_at is None):
            raise ValueError("incomplete schedule window")
        if self.start_at is not None and instant(self.start_at) >= instant(self.end_at):
            raise ValueError("schedule must end after start")
        return self


class Edge(Strict):
    source: Id
    target: Id
    type: EdgeType = "REQUIRES"
    read_set: tuple[str, ...] = Field(default=(), max_length=32)
    candidate_pairs: tuple[tuple[Id, Id], ...] = Field(default=(), max_length=64)
    buffer_seconds: int = Field(default=0, ge=0, le=7 * 86400)
    budget_currency: Literal["USD", "EUR", "INR"] | None = None
    budget_limit_minor: int | None = Field(default=None, ge=0, le=10**12)


class Budget(Strict):
    currency: Literal["USD", "EUR", "INR"]
    limit_minor: int = Field(ge=0, le=10**12)


class Limits(Strict):
    max_expansions: int = Field(default=4096, ge=1, le=65536)
    max_rounds: int = Field(default=3, ge=1, le=3)
    deadline_ms: int = Field(default=200, ge=1, le=2000)
    provider_calls: int = Field(default=0, ge=0, le=128)
    collection_deadline_ms: int = Field(default=30000, ge=1, le=120000)
    spend_minor: Literal[0] = 0


class Plan(Strict):
    schema_version: Literal["ofc.plan/1"] = Field(default="ofc.plan/1", alias="schema")
    tenant_id: Id
    actor: Actor
    idempotency_key: Id
    objective: str = Field(min_length=1, max_length=2000)
    nodes: tuple[Node, ...] = Field(min_length=1, max_length=64)
    edges: tuple[Edge, ...] = Field(default=(), max_length=512)
    budgets: tuple[Budget, ...] = Field(default=(), max_length=3)
    limits: Limits = Limits()
    external_action: Literal[False] = False

    @model_validator(mode="after")
    def valid_graph(self):
        if not self.objective.strip():
            raise ValueError("objective must not be blank")
        nodes = {n.id: n for n in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("duplicate node id")
        if len({b.currency for b in self.budgets}) != len(self.budgets):
            raise ValueError("duplicate budget currency")
        budget_scopes = {}
        for edge in self.edges:
            if edge.type == "CONSUMES_BUDGET":
                key = (edge.target, edge.budget_currency)
                if (
                    key in budget_scopes
                    and budget_scopes[key] != edge.budget_limit_minor
                ):
                    raise ValueError("budget edges must share one bucket limit")
                budget_scopes[key] = edge.budget_limit_minor
            if edge.buffer_seconds and edge.type != "TEMPORAL_BEFORE":
                raise ValueError("temporal buffer only applies to temporal edges")
        seen, adjacency, indegree = (
            set(),
            {k: [] for k in nodes},
            dict.fromkeys(nodes, 0),
        )
        for edge in self.edges:
            if (
                edge.source not in nodes
                or edge.target not in nodes
                or edge.source == edge.target
            ):
                raise ValueError("invalid edge endpoint")
            key = (edge.source, edge.target, edge.type)
            if key in seen:
                raise ValueError("duplicate edge")
            seen.add(key)
            allowed_reads = {
                f"{edge.source}.{name}"
                for name in ("candidate_id", "start_at", "end_at", "locality")
            }
            if (
                len(edge.read_set) != len(set(edge.read_set))
                or not set(edge.read_set) <= allowed_reads
            ):
                raise ValueError("invalid dependency read set")
            if edge.type == "CONSUMES_BUDGET":
                if (
                    nodes[edge.target].domain != "budget"
                    or edge.budget_currency is None
                    or edge.budget_limit_minor is None
                ):
                    raise ValueError(
                        "budget edge requires a budget target, currency and limit"
                    )
            elif (
                edge.budget_currency is not None or edge.budget_limit_minor is not None
            ):
                raise ValueError("budget fields require CONSUMES_BUDGET")
            if (
                edge.type == "APPROVAL_REQUIRES"
                and nodes[edge.source].domain != "approval"
            ):
                raise ValueError("approval dependency requires an approval source")
            if edge.type in CAUSAL_EDGES:
                adjacency[edge.source].append(edge.target)
                indegree[edge.target] += 1
            if (
                edge.type in ("CONFLICTS_WITH", "COMPATIBLE_WITH")
                and not edge.candidate_pairs
            ):
                raise ValueError("candidate constraint needs explicit pairs")
            for left, right in edge.candidate_pairs:
                if left not in {
                    c.id for c in nodes[edge.source].candidate_pool
                } or right not in {c.id for c in nodes[edge.target].candidate_pool}:
                    raise ValueError("constraint candidate outside pool")
        queue = sorted(k for k, count in indegree.items() if count == 0)
        visited = 0
        while queue:
            current = queue.pop()
            visited += 1
            for target in adjacency[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited != len(nodes):
            raise ValueError("cyclic causal dependency")
        canonical_bytes(self.data())
        return self

    @property
    def digest(self) -> str:
        return digest(self.data())

    @classmethod
    def parse(cls, value: dict | str | bytes):
        raw = value if isinstance(value, (str, bytes)) else canonical_bytes(value)
        return cls.model_validate_json(canonical_bytes(strict_json(raw)))


class Evidence(Strict):
    schema_version: Literal["ofc.evidence/1"] = Field(
        default="ofc.evidence/1", alias="schema"
    )
    id: Id
    tenant_id: Id
    plan_digest: Hash
    node_id: Id
    claim: Claim
    subject_id: Id
    candidate_pool: tuple[Id, ...] = Field(min_length=1, max_length=8)
    locality: str | None = Field(default=None, max_length=96)
    input_digest: Hash
    source: Literal["qloo", "operator", "fixture"]
    mode: Literal["SYNTHETIC", "CAPTURED", "LIVE", "ATTESTED"]
    attempt_id: Id
    query_fingerprint: Hash | None = None
    source_digest: Hash | None = None
    observed_at: str = Field(max_length=32)
    expires_at: str = Field(max_length=32)
    policy_version: str = Field(min_length=1, max_length=64)
    license_scope: Literal["private_review", "synthetic_public"]
    ranks: tuple[Id, ...] = Field(default=(), max_length=8)
    facts: dict = Field(default_factory=dict)
    parent_ids: tuple[Id, ...] = Field(default=(), max_length=16)
    issuer: Id | None = None
    signature: str | None = Field(default=None, max_length=128)
    provider_account_scope: Id | None = None
    extraction_rule_version: Id | None = None
    entitlement_id: Id | None = None
    raw_payload_ref: Hash | None = None

    @model_validator(mode="after")
    def valid_evidence(self):
        if instant(self.observed_at) >= instant(self.expires_at):
            raise ValueError("invalid evidence lifetime")
        if len(set(self.candidate_pool)) != len(self.candidate_pool):
            raise ValueError("duplicate evidence candidate")
        if len(set(self.ranks)) != len(self.ranks) or not set(self.ranks) <= set(
            self.candidate_pool
        ):
            raise ValueError("invalid rank order")
        if (
            len(self.parent_ids) != len(set(self.parent_ids))
            or self.id in self.parent_ids
        ):
            raise ValueError("invalid provenance parents")
        canonical_bytes(self.facts)
        return self

    def signing_fields(self):
        return self.model_dump(mode="json", by_alias=True, exclude={"signature"})


class Patch(Strict):
    node_id: Id
    changes: dict[MutableField, str] = Field(min_length=1, max_length=3)
    evidence_ids: tuple[Id, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_evidence(self):
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("duplicate patch evidence")
        return self


class ReviewRequest(Strict):
    plan: Plan
    evidence: tuple[Evidence, ...] = Field(default=(), max_length=512)
    mode: Literal["SYNTHETIC", "PRIVATE"] = "PRIVATE"
    as_of: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def valid_request(self):
        if self.as_of is not None:
            instant(self.as_of)
        ids = {e.id for e in self.evidence}
        if len(ids) != len(self.evidence):
            raise ValueError("duplicate evidence id")
        parents = {e.id: e.parent_ids for e in self.evidence}
        active, done = set(), set()

        def visit(key):
            if key in active:
                raise ValueError("cyclic evidence provenance")
            if key in done:
                return
            active.add(key)
            for parent in parents[key]:
                if parent not in parents:
                    raise ValueError("missing evidence parent")
                visit(parent)
            active.remove(key)
            done.add(key)

        for key in parents:
            visit(key)
        return self

    @classmethod
    def parse(cls, value):
        raw = value if isinstance(value, (str, bytes)) else canonical_bytes(value)
        return cls.model_validate_json(canonical_bytes(strict_json(raw)))
