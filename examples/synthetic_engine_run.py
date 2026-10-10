"""Run the full synthetic OFF/ON decision loop without network or credentials.

Run: python examples/synthetic_engine_run.py
"""
import json
from datetime import datetime, timedelta, timezone

from ofc_engine.contracts import (
    Decision, EvidenceQuerySpec, EvidenceStatus, PlanSnapshot, QlooEvidence,
    SourceMode,
)
from ofc_engine.proof import EvidenceBinding
from ofc_engine.revisions import digest_obj
from ofc_engine.workflow import compare_fixture_arms

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


class SyntheticTourEvidence:
    def fetch(self, plan: PlanSnapshot) -> tuple[EvidenceBinding, ...]:
        records = []
        for decision in plan.decisions:
            ranks = {item: rank + 1 for rank, item in enumerate(decision.candidate_pool)}
            if decision.decision_id == 'brand':
                ranks = {'brand-a': 2, 'brand-b': 1}
            fp = digest_obj({'plan': plan.plan_id, 'decision': decision.decision_id, 'ranks': ranks})
            records.append(EvidenceBinding(
                decision_id=decision.decision_id,
                query=EvidenceQuerySpec(
                    query_id='fixture:'+decision.decision_id,
                    capability='synthetic-fixture', input_entity_ids=('invented-artist',),
                    candidate_ids=decision.candidate_pool, locality_ref=decision.locality_ref,
                    request_fingerprint=fp,
                ),
                evidence=QlooEvidence(
                    evidence_id=f'fixture:{decision.decision_id}:r{plan.revision}',
                    source_mode=SourceMode.SYNTHETIC, status=EvidenceStatus.VALID,
                    query_fingerprint=fp, collected_at=NOW-timedelta(minutes=5),
                    expires_at=NOW+timedelta(minutes=30), candidate_ranks=ranks,
                ),
            ))
        return tuple(records)


def main():
    baseline = PlanSnapshot(
        plan_id='synthetic-three-city-tour', revision=0,
        baseline_digest=digest_obj({'scenario': 'frozen-plan-1'}),
        constraints_digest=digest_obj({'budget_usd': 60000, 'cities': 3}),
        decisions=(
            Decision(decision_id='brand', domain='brand_partner', candidate_id='brand-a',
                     candidate_pool=('brand-a','brand-b'), mutable_fields=('candidate_id',)),
            Decision(decision_id='opener', domain='artist', candidate_id='opener-a',
                     candidate_pool=('opener-a',)),
            Decision(decision_id='city-1', domain='venue', candidate_id='venue-a',
                     candidate_pool=('venue-a',)),
            Decision(decision_id='city-2', domain='venue', candidate_id='venue-b',
                     candidate_pool=('venue-b',)),
            Decision(decision_id='city-3', domain='venue', candidate_id='venue-c',
                     candidate_pool=('venue-c',)),
            Decision(decision_id='afterparty', domain='experience', candidate_id='party-a',
                     candidate_pool=('party-a',), depends_on=('brand',)),
        ),
    )
    a = compare_fixture_arms(baseline, SyntheticTourEvidence(), as_of=NOW,
                             max_evidence_age=timedelta(hours=1))
    payload = {
        'data_origin': 'SYNTHETIC_FIXTURES_ONLY',
        'real_qloo_requests': 0,
        'same_frozen_baseline': a.identical_baseline,
        'baseline_digest': a.baseline_digest,
        'without_evidence': {
            'status': a.disabled.status.value,
            'revision': a.disabled.plan.revision,
            'reason_codes': a.disabled.reason_codes,
        },
        'with_fixture_evidence': {
            'status': a.enabled.status.value,
            'revision': a.enabled.plan.revision,
            'reason_codes': a.enabled.reason_codes,
            'decisions': {d.decision_id: d.candidate_id for d in a.enabled.plan.decisions},
            'repair_trace': [step.as_dict() for step in a.enabled.traces],
        },
        'real_causal_proof': False,
        'release_authorized': False,
        'external_action_executed': False,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
