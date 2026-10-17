"""Read-only local demo server backed by the synthetic decision workflow."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
from urllib.parse import parse_qs, urlsplit

from .contracts import ActionIntent, Decision, EvidenceQuerySpec, EvidenceStatus, PlanSnapshot, QlooEvidence, SourceMode
from .intake import AgentProposal, freeze_proposal
from .proof import EvidenceBinding
from .revisions import digest_obj, plan_digest
from .workflow import compare_fixture_arms

AS_OF = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
SCENARIOS = ("repair", "outage", "dependent_missing", "budget_exhausted")
_NAMES = {
    "brand": ("Brand partner", "Brand A", "Brand B"),
    "opener": ("Opening act", "Opener A", "Opener A"),
    "venue-1": ("Chicago venue", "Venue A", "Venue A"),
    "venue-2": ("Austin venue", "Venue B", "Venue B"),
    "venue-3": ("Seattle venue", "Venue C", "Venue C"),
    "afterparty": ("Afterparty", "Experience A", "Experience A"),
}


def canonical_proposal() -> AgentProposal:
    return AgentProposal(
        intent=ActionIntent(
            run_id="tour-demo", client_request_id="tour-fixture",
            objective="Select culturally suitable partners and experiences for an emerging artist's three-city tour.",
            constraints={"budget_usd": 60000, "cities": ["Chicago", "Austin", "Seattle"]},
            taste_context={"audience": "Indie music audiences", "locality": "Chicago, Austin, Seattle"},
        ),
        decisions=(
            Decision(decision_id="brand", domain="partner", candidate_id="brand-a",
                     candidate_pool=("brand-a", "brand-b"), mutable_fields=("candidate_id",)),
            Decision(decision_id="opener", domain="artist", candidate_id="opener-a",
                     candidate_pool=("opener-a",)),
            Decision(decision_id="venue-1", domain="venue", candidate_id="venue-a",
                     candidate_pool=("venue-a",)),
            Decision(decision_id="venue-2", domain="venue", candidate_id="venue-b",
                     candidate_pool=("venue-b",)),
            Decision(decision_id="venue-3", domain="venue", candidate_id="venue-c",
                     candidate_pool=("venue-c",)),
            Decision(decision_id="afterparty", domain="experience", candidate_id="experience-a",
                     candidate_pool=("experience-a",), depends_on=("brand",)),
        ),
    )


def baseline_plan() -> PlanSnapshot:
    return freeze_proposal(canonical_proposal())[0]


def example_payload() -> dict:
    return {
        "source_mode": "SYNTHETIC",
        "release_authorized": False,
        "external_action_executed": False,
        "provider_authenticated": False,
        "real_qloo_requests": 0,
        "baseline_digest": plan_digest(baseline_plan()),
        "example": canonical_proposal().model_dump(mode="json"),
    }


class DemoEvidence:
    def __init__(self, scenario: str):
        self.scenario = scenario

    def fetch(self, plan: PlanSnapshot) -> tuple[EvidenceBinding, ...]:
        if self.scenario == "outage":
            raise TimeoutError("synthetic outage")
        records = []
        for decision in plan.decisions:
            if self.scenario == "dependent_missing" and plan.revision > 0 and decision.decision_id == "afterparty":
                continue
            ranks = {candidate: i + 1 for i, candidate in enumerate(decision.candidate_pool)}
            if decision.decision_id == "brand":
                if self.scenario == "budget_exhausted" and plan.revision > 0:
                    ranks = {"brand-a": 1, "brand-b": 2}
                else:
                    ranks = {"brand-a": 2, "brand-b": 1}
            fp = digest_obj({"decision": decision.decision_id, "ranks": ranks})
            records.append(EvidenceBinding(
                decision_id=decision.decision_id,
                query=EvidenceQuerySpec(
                    query_id="fixture:" + decision.decision_id,
                    capability="synthetic-demo", input_entity_ids=("invented-artist",),
                    candidate_ids=decision.candidate_pool, locality_ref=decision.locality_ref,
                    request_fingerprint=fp,
                ),
                evidence=QlooEvidence(
                    evidence_id=f"fixture:{decision.decision_id}:r{plan.revision}",
                    source_mode=SourceMode.SYNTHETIC, status=EvidenceStatus.VALID,
                    query_fingerprint=fp,
                    collected_at=AS_OF - timedelta(minutes=5),
                    expires_at=AS_OF + timedelta(minutes=30), candidate_ranks=ranks,
                ),
            ))
        return tuple(records)


def demo_snapshot(scenario: str) -> dict:
    if scenario not in SCENARIOS:
        raise ValueError("unknown scenario")
    baseline = baseline_plan()
    result = compare_fixture_arms(
        baseline, DemoEvidence(scenario), as_of=AS_OF,
        max_evidence_age=timedelta(hours=1),
        max_attempts=1 if scenario == "budget_exhausted" else 2,
    )
    on, off = result.enabled, result.disabled
    before = {d.decision_id: d for d in baseline.decisions}
    after = {d.decision_id: d for d in on.plan.decisions}
    original_verdicts = {v.decision_id: v for v in on.reviews[0].verdicts}
    latest_verdicts = {v.decision_id: v for v in on.reviews[-1].verdicts}
    affected = {item for trace in on.traces for item in trace.affected_decision_ids}
    decisions = []
    for decision in baseline.decisions:
        item_id = decision.decision_id
        first = original_verdicts[item_id]
        last = latest_verdicts[item_id]
        choices = _NAMES[item_id]
        current = after[item_id].candidate_id
        selected_label = choices[2] if item_id == "brand" and current == "brand-b" else choices[1]
        decisions.append({
            "id": item_id, "label": choices[0],
            "before": choices[1], "after": selected_label,
            "changed": before[item_id].candidate_id != current,
            "initial_verdict": first.verdict.value,
            "latest_verdict": last.verdict.value,
            "initial_reason_codes": list(first.reason_codes),
            "initial_evidence_ids": list(first.evidence_refs),
            "depends_on": list(decision.depends_on),
            "rechecked": item_id in affected,
            "pending_recheck": item_id in on.ledger.pending_recheck_ids,
            "evidence_ids": list(last.evidence_refs),
            "reason_codes": list(last.reason_codes),
        })
    evidence = [
        {
            "id": binding.evidence.evidence_id,
            "decision_id": binding.decision_id,
            "capability": binding.query.capability,
            "query_fingerprint": binding.query.request_fingerprint,
            "query_candidates": list(binding.query.candidate_ids),
            "candidate_ranks": binding.evidence.candidate_ranks,
            "collected_at": binding.evidence.collected_at.isoformat(),
            "source_mode": binding.evidence.source_mode.value,
            "status": binding.evidence.status.value,
        }
        for binding in on.reviews[-1].bindings
    ]
    changed_count = sum(d["changed"] for d in decisions)
    timeline = [("BASELINE_FROZEN", baseline.revision)]
    for index, review in enumerate(on.reviews):
        if index:
            timeline.append(("PATCH_APPLIED", on.traces[index - 1].proposed_revision))
        timeline.append(("EVALUATED", review.plan_revision))
    timeline.append((on.status.value, on.plan.revision))
    events = [
        {"sequence": index, "state": state, "revision": revision}
        for index, (state, revision) in enumerate(timeline)
    ]
    return {
        "scenario": scenario,
        "demo_mode": "SYNTHETIC",
        "source_mode": "SYNTHETIC",
        "run_id": baseline.plan_id,
        "objective": "Select culturally suitable partners and experiences for an emerging artist's three-city tour.",
        "budget_usd": 60000,
        "cities": ["Chicago", "Austin", "Seattle"],
        "run_phase": "EVALUATED" if on.status.value == "INTEGRITY_ONLY" else "REVIEW_REQUIRED",
        "run_status": on.status.value,
        "release_state": "USER_REVIEW",
        "release_authorized": False,
        "external_action_executed": False,
        "provider_authenticated": False,
        "baseline_digest": result.baseline_digest,
        "baseline_revision": baseline.revision,
        "current_revision": on.plan.revision,
        "policy_version": "fixture-rank-v1",
        "last_event_sequence": events[-1]["sequence"],
        "timeline": events,
        "attempts_used": on.ledger.attempts_used,
        "max_attempts": on.ledger.max_attempts,
        "reason_codes": list(on.reason_codes),
        "pending_rechecks": list(on.ledger.pending_recheck_ids),
        "decisions": decisions,
        "evidence": evidence,
        "traces": [trace.as_dict() for trace in on.traces],
        "comparison": {
            "identical_baseline": result.identical_baseline,
            "baseline_digest": result.baseline_digest,
            "without_evidence": off.status.value,
            "with_fixture_evidence": on.status.value,
            "changed_decisions": changed_count,
            "business_outcomes_measured": False,
            "real_causal_proof": False,
        },
    }


_ASSETS = {
    "/": ("studio.html", "text/html; charset=utf-8"),
    "/proof": ("index.html", "text/html; charset=utf-8"),
    "/recorded": ("recorded.html", "text/html; charset=utf-8"),
    "/recorded.html": ("recorded.html", "text/html; charset=utf-8"),
    "/recorded.js": ("recorded.js", "text/javascript; charset=utf-8"),
    "/recorded.css": ("recorded.css", "text/css; charset=utf-8"),
    "/studio.html": ("studio.html", "text/html; charset=utf-8"),
    "/studio.css": ("studio.css", "text/css; charset=utf-8"),
    "/copilot-workspace.css": ("copilot-workspace.css", "text/css; charset=utf-8"),
    "/copilot-workspace.js": ("copilot-workspace.js", "text/javascript; charset=utf-8"),
    "/copilot-activity.js": ("copilot-activity.js", "text/javascript; charset=utf-8"),
    "/copilot-proof.js": ("copilot-proof.js", "text/javascript; charset=utf-8"),
    "/intro.css": ("intro.css", "text/css; charset=utf-8"),
    "/intro.js": ("intro.js", "text/javascript; charset=utf-8"),
    "/studio.js": ("studio.js", "text/javascript; charset=utf-8"),
    "/product.js": ("product.js", "text/javascript; charset=utf-8"),
    "/proof-premium.css": ("proof-premium.css", "text/css; charset=utf-8"),
    "/optical.css": ("optical.css", "text/css; charset=utf-8"),
    "/calm.css": ("calm.css", "text/css; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/experience.js": ("experience.js", "text/javascript; charset=utf-8"),
}


class DemoHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/api/example":
            if parts.query:
                self._send(400, b'{"error":"INVALID_QUERY"}', "application/json")
                return
            body = json.dumps(example_payload(), separators=(",", ":"), allow_nan=False).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if parts.path == "/api/demo":
            query = parse_qs(parts.query, keep_blank_values=True)
            if set(query) != {"scenario"} or len(query["scenario"]) != 1:
                self._send(400, b'{"error":"INVALID_SCENARIO"}', "application/json")
                return
            try:
                data = demo_snapshot(query["scenario"][0])
            except ValueError:
                self._send(400, b'{"error":"INVALID_SCENARIO"}', "application/json")
                return
            body = json.dumps(data, separators=(",", ":"), allow_nan=False).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if parts.path in _ASSETS and not parts.query:
            filename, content_type = _ASSETS[parts.path]
            self._send(200, files("ofc_engine").joinpath("web", filename).read_bytes(), content_type)
            return
        self._send(404, b'{"error":"NOT_FOUND"}', "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; font-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self._send(405, b'{"error":"READ_ONLY"}', "application/json")

    def log_message(self, format: str, *args: object) -> None:
        pass


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local-only synthetic OFC proof viewer")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    with ThreadingHTTPServer(("127.0.0.1", args.port), DemoHandler) as server:
        print(f"Synthetic demo: http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
