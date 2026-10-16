# OFC Engine

**Pre-execution decision control for AI agents.** OFC freezes a proposed plan, checks bounded evidence, repairs only an allowed choice, records the revision, and refuses to release an inadequately verified action.

**Hosted Studio:** https://ofc-fixture-preview.onrender.com/#plan

The published Qloo-backed example is a **read-only review of one previously acquired genuine brand-affinity ranking**. It is not an API playground, a venue-booking agent, or a provider-authenticated cultural approval.

## Walk through the product

Open **Create a plan → Review the Qloo-backed tour** to follow one frozen, $60,000 Chicago–Austin–Seattle tour:

1. **PROPOSE:** Original objective, budget, cities, brand and five operator-constructed, unverified supporting choices.
2. **QLOO CHECK:** The previously acquired Qloo observation from a frozen artist/brand shortlist. Same-baseline Qloo OFF is UNCERTAIN; Qloo ON supports brand REPAIR.
3. **REPAIR:** Only the brand candidate changes within its allowlist. Other choices remain unchanged; brand and dependent afterparty checks are invalidated.
4. **VERIFY:** Six outcomes, revision-bound evidence, pending rechecks and the hash-linked PostgreSQL journal.
5. **HANDOFF:** REVIEW_REQUIRED / WITHHELD. No bookings, payments or external actions occur.

Qloo entity IDs and the private raw capture are not published. Option numbers describe positions inside the fixed pool. The five supporting choices are scenario assumptions, not Qloo recommendations.

The editable Plan Builder beneath the walkthrough is **separate**. Visitors can validate and save a draft but cannot inherit Qloo evidence or trigger provider calls. **Try the demo** provides clearly labeled synthetic scenarios; **Recorded proof** is historical and separate.

## Architecture and trust boundary

- Typed, frozen proposals, candidate allowlists and dependency validation.
- Scoped Qloo /v2/insights brand-ranking client, private operator approval and a durable one-request reservation.
- Single-field selective repair, revision/digest CAS, idempotency and a hash-linked PostgreSQL journal.
- Dependency invalidation, independent decision preservation and review-required withheld handoff.
- Private browser tickets, bounded publication approval and a redacted, read-only public evidence projection.
- Fail-closed behavior on missing evidence, wrong scope, corrupted history, or mismatched revision.

**Limitation:** The real Qloo HTTP response is not cryptographically provider-attested or independently rechecked. The relative brand rank does not prove tour suitability, valid contracts, venue readiness, budget feasibility or business outcomes. The same-baseline OFF/ON comparison is a *decision-policy contrast*, not an experiment establishing business causality. Full-plan release remains blocked.

## Optional hosted Copilot backend

The B2 backend supports an opt-in, authenticated Copilot service with visitor sessions, an encrypted owner-managed Qloo/Gemini/Groq credential vault, PostgreSQL evidence and request reservations, and a shared-evidence **LLM + Qloo** (OFC OFF) versus **LLM + Qloo + OFC** (OFC ON) decision comparison. Provider keys are configured privately through the temporary website owner portal at `/connect` after the one-time server trust anchor is provisioned.

**This feature is disabled by default**; the existing hosted saved-review page does not become a live Copilot merely by merging the code. Review [hosted setup and qualification](docs/HOSTED_COPILOT_BACKEND.md) for private migration and provider-test gates. Unsupported Qloo domains remain unverified. No model or Qloo request is made by loading the settings or status endpoints.

## Develop and test

Python 3.11+:

    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -e '.[dev]'
    python -m pytest
    python -m ofc_engine.hosted_preview --bind 127.0.0.1 --port 18080

Open http://127.0.0.1:18080/. With no private PostgreSQL/operator setup this runs synthetic-only; it cannot reproduce the published real-Qloo capture. GitHub CI provisions an isolated PostgreSQL database and a **test-only Qloo transport**, then exercises the full five-stage browser review without contacting Qloo.

Related: [Try it](docs/TRY_IT.md) · [Product contract](docs/PROJECT.md) · [Hosted operations](docs/HOSTED_QLOO_TOUR.md) · [Judge guide](docs/JUDGE_GUIDE.md).

## Privacy and release

MIT license. Never commit Qloo credentials, PostgreSQL URLs, browser tickets, operator tokens, or raw provider captures. Provider-derived publication is subject to applicable event/account rights. A Git-history regex scan does not replace manual source/history and licensing review.

OFC does not execute bookings, transfers or payments. The demonstrated outcome is **rank-grounded selective repair plus safe refusal**.
