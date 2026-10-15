"""Redacted public evidence for one explicitly approved Qloo-backed tour."""
from __future__ import annotations

from hashlib import sha256
import json

from .hosted_runs import HostedRunRegistry
from .intake import freeze_proposal
from .qloo import brand_affinity_probe
from .revisions import plan_digest


class PublishedTourReview:
    def __init__(self, registry: HostedRunRegistry, run_id: str):
        self.registry = registry
        self.run_id = run_id
        self.review()

    def review(self) -> dict:
        proposal = self.registry.operator_proposal(self.run_id, allow_expired=True)
        baseline, _ = freeze_proposal(proposal)
        current = self.registry.store.get(self.run_id)
        history = self.registry.store.audit_history(self.run_id)
        brand = next((d for d in baseline.decisions if d.decision_id == "brand"), None)
        artist = proposal.intent.taste_context.get("artist_id")
        if brand is None or not isinstance(artist, str):
            raise ValueError("missing approved Qloo scope")
        spec = brand_affinity_probe(artist, brand.candidate_pool)
        scope = sha256((
            spec.fingerprint + ":" + brand.candidate_id + ":" +
            plan_digest(baseline)
        ).encode()).hexdigest()
        with self.registry.store._connect() as db:
            row = db.execute(
                "select scope,state,result from ofc_private.provider_attempt "
                "where id=1"
            ).fetchone()
        if row is None or row[0] != scope or row[1] != "COMPLETE":
            raise ValueError("no qualified Qloo observation for public showcase")
        try:
            recorded = json.loads(row[2])
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid stored Qloo observation") from exc
        if not isinstance(recorded, dict) or recorded.get("status") != "LIVE_RANK_OBSERVED":
            raise ValueError("Qloo observation unavailable")
        run = recorded.get("agent_run")
        if not isinstance(run, dict):
            raise ValueError("agent evidence missing")
        proof = run.get("provenance", {})
        observation = proof.get("qloo_rank_observation", {})
        comparison = run.get("off_on", {})
        handoff = run.get("handoff", {})
        decisions = run.get("decisions")
        if (
            current.revision != 1 or len(history) != 2
            or len(baseline.decisions) != 6
            or any(
                old != new for old, new in zip(
                    (d for d in baseline.decisions if d.decision_id != "brand"),
                    (d for d in current.decisions if d.decision_id != "brand"),
                )
            )
            or run.get("status") != "HOSTED_AGENT_REVIEW"
            or run.get("revision") != current.revision
            or run.get("source_mode") != "LIVE_HTTP_UNVERIFIED"
            or run.get("real_qloo_requests") != 1
            or run.get("release_authorized") is not False
            or run.get("provider_authenticated") is not False
            or run.get("independent_recheck_completed") is not False
            or run.get("external_action_executed") is not False
            or run.get("verification_status") != "REVIEW_REQUIRED"
            or recorded.get("release_authorized") is not False
            or recorded.get("external_action_executed") is not False
            or proof.get("baseline_plan_digest") != plan_digest(baseline)
            or proof.get("current_plan_digest") != plan_digest(current)
            or proof.get("journal_head") != history[-1].event_hash
            or observation.get("query_fingerprint") != spec.fingerprint
            or observation.get("source_mode") != "LIVE_HTTP_UNVERIFIED"
            or observation.get("provider_attestation") != "NOT_ESTABLISHED"
            or not isinstance(observation.get("capture_digest"), str)
            or len(observation["capture_digest"]) != 64
            or comparison.get("same_baseline") is not True
            or comparison.get("real_causal_proof") is not False
            or comparison.get("off_verdicts", {}).get("brand") != "UNCERTAIN"
            or comparison.get("on_verdicts", {}).get("brand") != "REPAIR"
            or run.get("changed_ids") != ["brand"]
            or handoff.get("status") != "WITHHELD"
            or handoff.get("reviewed_revision") != current.revision
            or handoff.get("reviewed_plan_digest") != plan_digest(current)
            or handoff.get("journal_head") != history[-1].event_hash
            or handoff.get("external_action_executed") is not False
            or not isinstance(decisions, list) or len(decisions) != 6
            or [d.get("decision_id") for d in decisions] != [
                d.decision_id for d in current.decisions
            ]
        ):
            raise ValueError("published Qloo review is not revision-bound")
        return {
            "status": "OBSERVED_QLOO_REVIEW_REQUIRED",
            "source_mode": "CAPTURED_LIVE_QLOO_UNVERIFIED",
            "decision_count": 6,
            "revision": current.revision,
            "decisions": [{
                "decision_id": d["decision_id"],
                "verdict": d["verdict"],
                "changed": d["changed"],
                "preserved": d["preserved"],
                "pending_recheck": d["pending_recheck"],
            } for d in decisions],
            "changed_ids": run["changed_ids"],
            "preserved_ids": run["preserved_ids"],
            "invalidated_ids": run["invalidated_ids"],
            "pending_rechecks": run["pending_rechecks"],
            "blockers": run["blockers"],
            "off_on": {
                "same_baseline": True,
                "off_brand": "UNCERTAIN",
                "on_brand": "REPAIR",
                "real_causal_proof": False,
            },
            "evidence": {
                "collected_at": observation.get("collected_at"),
                "query_fingerprint": spec.fingerprint,
                "capture_digest": observation["capture_digest"],
                "baseline_plan_digest": plan_digest(baseline),
                "current_plan_digest": plan_digest(current),
                "journal_head": history[-1].event_hash,
                "policy_version": proof.get("policy_version"),
            },
            "provider_authenticated": False,
            "independent_recheck_completed": False,
            "release_authorized": False,
            "external_action_executed": False,
            "additional_provider_requests": 0,
            "handoff": "WITHHELD",
        }
