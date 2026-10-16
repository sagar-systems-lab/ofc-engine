# Product and evidence contract

OFC Engine sits between an autonomous agent's proposal and external action: **PROPOSE → CHECK → REPAIR → VERIFY → HANDOFF**.

## Hosted, bounded tour

The published tour is a fictional $60,000 Chicago–Austin–Seattle emerging-artist scenario. Six decisions are frozen: one brand, opener, three venues and a brand-dependent afterparty. The five non-brand choices are **operator-constructed, unverified hypotheses**.

One previously acquired genuine Qloo /v2/insights brand-affinity observation changed the top-ranked choice within the fixed brand pool. OFC atomically committed a single allowed brand repair at revision 1, preserved independent decisions and invalidated the brand and afterparty checks. Without rank evidence the brand verdict is UNCERTAIN; with it the frozen decision is REPAIR.

The PostgreSQL audit journal, baseline/current digests, Qloo request fingerprint and capture digest bind the published read-only review to that exact saved run. Public viewers cannot spend Qloo quota, select their own Qloo evidence or attach the stored result to an unrelated browser draft.

## Verification is deliberately narrow

RANK PASS means only that the revised brand is top ranked inside the frozen shortlist; it does not certify the opener, venues, artist identity, ticket demand, contracts, budget or cultural suitability. The provider response is **not cryptographically attested or independently rechecked**. The other five decisions remain UNCERTAIN. The revision-bound handoff is REVIEW_REQUIRED / WITHHELD. No real action occurs.

Qloo OFF/ON is a same-baseline evidence-conditioned decision comparison, not business-outcome causal proof.

## Engineering boundaries

- Contracts and typed intake: contracts, intake.
- Scoped Qloo client and rank policy: qloo, qloo_affinity.
- Selective patch, CAS, replay prevention and journal: agent_run, postgres_store.
- Private operator ledger and one-request gating: hosted_live, postgres_attempt.
- Redacted public proof and browser workflow: published_tour, hosted_preview, web/hosted-showcase.js.

Synthetic fixture results, private historical replay, and the real saved hosted observation must never be presented as equivalent sources. See [Try it](TRY_IT.md) and [Hosted operations](HOSTED_QLOO_TOUR.md).

**Out of scope:** provider cryptographic attestation, independent full-tour verification, automatic bookings/payments, unrestricted public live-Qloo queries, or an all-six PASS.
