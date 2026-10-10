"""Fully synthetic, reproducible illustration; no external service is queried.

Run: python examples/synthetic_three_city_trace.py
"""
import json
from datetime import datetime, timedelta, timezone

from ofc_engine.contracts import (
    Decision, DecisionVerdict, EvidenceQuerySpec, EvidenceStatus,
    PlanSnapshot, QlooEvidence, RepairPatch, SourceMode, Verdict,
)
from ofc_engine.proof import EvidenceBinding, ReviewEnvelope
from ofc_engine.repair import begin_repair_ledger, preview_repair
from ofc_engine.revisions import digest_obj, plan_digest
from ofc_engine.trace import build_repair_trace

AS_OF = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
AGE = timedelta(hours=1)


def fixture_review(plan, *, repair_brand):
    bindings, verdicts = [], []
    for i, decision in enumerate(plan.decisions):
        fp = format(i + 1, 'x') * 64
        eid = f'synthetic:{decision.decision_id}:rev{plan.revision}'
        bindings.append(EvidenceBinding(
            decision_id=decision.decision_id,
            query=EvidenceQuerySpec(
                query_id=f'synthetic-query-{decision.decision_id}',
                capability='fixture-illustration',
                input_entity_ids=('invented-artist',),
                candidate_ids=decision.candidate_pool,
                request_fingerprint=fp,
            ),
            evidence=QlooEvidence(
                evidence_id=eid,
                source_mode=SourceMode.SYNTHETIC,
                status=EvidenceStatus.VALID,
                query_fingerprint=fp,
                collected_at=AS_OF-timedelta(minutes=3),
                expires_at=AS_OF+timedelta(minutes=30),
                candidate_ranks={c: i + 1 for i, c in enumerate(decision.candidate_pool)},
            ),
        ))
        verdicts.append(DecisionVerdict(
            decision_id=decision.decision_id,
            plan_revision=plan.revision,
            verdict=Verdict.REPAIR if repair_brand and decision.decision_id == 'brand' else Verdict.PASS,
            reason_codes=('SYNTHETIC_FIXTURE_ONLY',),
            evidence_refs=(eid,), policy_version='synthetic-policy',
        ))
    return ReviewEnvelope(
        plan_id=plan.plan_id, plan_revision=plan.revision,
        plan_digest=plan_digest(plan), source_mode=SourceMode.SYNTHETIC,
        policy_version='synthetic-policy', verdicts=tuple(verdicts),
        bindings=tuple(bindings),
    )


def main():
    plan = PlanSnapshot(
        plan_id='synthetic-tour-three-cities', revision=0,
        baseline_digest=digest_obj({'fixture': 'frozen three-city tour'}),
        constraints_digest=digest_obj({'budget_usd': 60000, 'city_count': 3}),
        decisions=(
            Decision(decision_id='brand', domain='partner', candidate_id='brand-a',
                     candidate_pool=('brand-a', 'brand-b'), mutable_fields=('candidate_id',)),
            Decision(decision_id='opener', domain='artist', candidate_id='opener-a',
                     candidate_pool=('opener-a',)),
            Decision(decision_id='venue-city-1', domain='venue', candidate_id='venue-a',
                     candidate_pool=('venue-a',)),
            Decision(decision_id='venue-city-2', domain='venue', candidate_id='venue-b',
                     candidate_pool=('venue-b',)),
            Decision(decision_id='venue-city-3', domain='venue', candidate_id='venue-c',
                     candidate_pool=('venue-c',)),
            Decision(decision_id='afterparty', domain='experience', candidate_id='event-a',
                     candidate_pool=('event-a',), depends_on=('brand',)),
        ),
    )
    before = fixture_review(plan, repair_brand=True)
    ledger = begin_repair_ledger(plan, max_attempts=2)
    patch = RepairPatch(
        patch_id='synthetic-patch-1', decision_id='brand', base_revision=0,
        old_candidate_id='brand-a', new_candidate_id='brand-b',
        precondition_digest=plan_digest(plan),
        evidence_refs=('synthetic:brand:rev0',),
    )
    proposed = preview_repair(plan, ledger, before, patch, as_of=AS_OF, max_evidence_age=AGE)
    after = fixture_review(proposed.proposed_plan, repair_brand=False)
    trace = build_repair_trace(
        plan, ledger, before, patch, proposed, after_review=after,
        as_of=AS_OF, max_evidence_age=AGE,
    )
    print(json.dumps({
        'scenario': 'Illustrative three-city tour, 60000 USD budget',
        'data_provenance': 'ALL SYNTHETIC; NO LIVE QLOO DATA',
        'trace': trace.as_dict(),
    }, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
