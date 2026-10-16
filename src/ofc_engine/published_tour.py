"""Redacted public evidence for one explicitly approved Qloo-backed tour."""
from __future__ import annotations

from hashlib import sha256
import json

from .hosted_runs import HostedRunRegistry
from .intake import freeze_proposal
from .qloo import brand_affinity_probe
from .revisions import plan_digest
from .webdemo import canonical_proposal


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
        if not isinstance(proof, dict):
            raise ValueError("invalid evidence provenance")
        observation = proof.get("qloo_rank_observation", {})
        comparison = run.get("off_on", {})
        handoff = run.get("handoff", {})
        decisions = run.get("decisions")
        if any(not isinstance(value, dict) for value in (
            observation, comparison, handoff,
        )):
            raise ValueError("invalid evidence boundary")
        if not isinstance(decisions, list) or any(
            not isinstance(value, dict) for value in decisions
        ):
            raise ValueError("invalid decision reviews")
        canonical = canonical_proposal()
        if (
            proposal.intent.objective != canonical.intent.objective
            or proposal.intent.constraints.get("budget_usd") != 60000
            or proposal.intent.constraints.get("cities") != ["Chicago", "Austin", "Seattle"]
            or proposal.intent.constraints.get("external_action") is not False
            or proposal.intent.taste_context.get("audience") != "Indie music audiences"
            or len(proposal.decisions) != 6
        ):
            raise ValueError("public tour must match the approved canonical scope")
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
            or any(c not in "0123456789abcdef" for c in observation["capture_digest"])
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
        baseline_brand = baseline.decisions[0]
        repaired_brand = current.decisions[0]
        if (
            baseline_brand.decision_id != "brand"
            or repaired_brand.decision_id != "brand"
            or baseline_brand.candidate_id == repaired_brand.candidate_id
            or baseline_brand.candidate_pool != repaired_brand.candidate_pool
            or repaired_brand.candidate_id not in baseline_brand.candidate_pool
            or any(
                d.get("pending_recheck") != (d["decision_id"] in run["pending_rechecks"])
                for d in decisions
            )
            or set(run["pending_rechecks"]) != {"brand", "afterparty"}
            or set(run["invalidated_ids"]) != {"brand", "afterparty"}
        ):
            raise ValueError("unqualified tour transition")
        tour_decisions = []
        for previous, revised, verdict in zip(
            baseline.decisions, current.decisions, decisions
        ):
            if (
                previous.candidate_pool != revised.candidate_pool
                or previous.candidate_id not in previous.candidate_pool
                or revised.candidate_id not in previous.candidate_pool
                or previous.decision_id != revised.decision_id
                or previous.depends_on != revised.depends_on
                or verdict["changed"] != (previous.candidate_id != revised.candidate_id)
                or verdict["pending_recheck"] != (
                    previous.decision_id in run["pending_rechecks"]
                )
            ):
                raise ValueError("published decision does not match frozen plan")
            tour_decisions.append({
                "decision_id": previous.decision_id,
                "domain": previous.domain,
                "required": previous.required,
                "depends_on": list(previous.depends_on),
                "candidate_count": len(previous.candidate_pool),
                "baseline_option": previous.candidate_pool.index(previous.candidate_id) + 1,
                "reviewed_option": previous.candidate_pool.index(revised.candidate_id) + 1,
                "verdict": verdict["verdict"],
                "changed": verdict["changed"],
                "pending_recheck": verdict["pending_recheck"],
            })
        return {
            "journey_version": 1,
            "tour": {
                "objective": canonical.intent.objective,
                "budget_usd": 60000,
                "cities": ["Chicago", "Austin", "Seattle"],
                "audience": "Indie music audiences",
                "candidate_labels": "Redacted option indices from frozen candidate pools",
                "operator_constructed_unverified_decisions": 5,
                "decisions": tour_decisions,
            },
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
