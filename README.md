# OFC Engine

**Pre-execution decision control for AI agents.** OFC freezes a proposed plan, checks bounded evidence, repairs only an allowed choice, records the revision, and refuses to release an inadequately verified action.

**Live Copilot:** https://ofc-fixture-preview.onrender.com/

**Start here:** [Self-service evaluation guide](docs/JUDGE_GUIDE.md) — zero-provider-cost first look, live testing scope, and saved decision proof.

The hosted Copilot accepts the visitor's own natural-language cultural decision proposal. The **live-qualified scope** is an artist comparing two to five named brand candidates, including an original choice. Gemini/Groq extracts the proposed plan; Qloo resolves exact entities, ranks the bounded candidate pool and supplies a second scoped rank observation; OFC may revise the brand choice. The saved OFF/ON comparison uses the same Qloo evidence and shows what the control layer changed. Live requests consume finite provider quotas and require confirmation.

The Copilot also retains structured multi-city and dependent choices. Non-brand or locality evidence is **not policy-qualified**: such decisions remain unverified, never receive a fabricated PASS, and may be skipped when conserving daily provider capacity. Business readiness, bookings and other external actions remain withheld. Use the on-screen scope guide and synthetic scenarios to explore without spending provider quota.

A separate **recorded tour** at [the legacy Studio](https://ofc-fixture-preview.onrender.com/studio.html#plan) displays one historical Qloo observation. It is read-only and must not be confused with the newly submitted live Copilot or its independent rank recheck.

## Recorded tour — historical evidence

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

**Historical-tour limitation:** The recorded-tour Qloo HTTP response is not cryptographically provider-attested or independently rechecked. Live Copilot brand ranking has a separate two-observation repeatability check; neither path establishes real-world business fitness. The relative brand rank does not prove tour suitability, valid contracts, venue readiness, budget feasibility or business outcomes. The same-baseline OFF/ON comparison is a *decision-policy contrast*, not an experiment establishing business causality. Full-plan release remains blocked.

## Hosted live Copilot operations

The current hosted service has its private beta Copilot **enabled**. Visitor sessions, encrypted owner-managed credentials, PostgreSQL evidence and quota reservations support independent OFF/ON model narratives over shared Qloo observations. Owner credentials remain server-side, configured through `/connect`. A fresh local installation is **disabled by default** until private PostgreSQL, credentials and runtime configuration are explicitly provisioned.

Read [hosted setup and qualification](docs/HOSTED_COPILOT_BACKEND.md) for deployment requirements, and [Try it](docs/TRY_IT.md) for current supported tests. Merely loading settings, previous history or the capability endpoint makes no provider call. The browser's proof file is a saved-session export, not a digitally signed Qloo attestation.

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

## Typed plan review

An opt-in control service reviews up to 64 decisions, typed dependencies, scoped evidence, currency limits, and masked repair batches. The graph explorer shows exactly which choices changed, which obligations reopened, and which choices remained untouched. Agent clients receive durable review IDs, ordered events, and independently checkable control records.

Connected reviews can collect fresh claim evidence between bounded repair rounds. Optional Qloo and signed business-feed adapters enforce account, entitlement, locality, quota, and deadline scopes; their durable tickets prevent blind retries after a crash. Source collection requires separate operator configuration and a confirmed zero-price entitlement. The default sandbox makes no provider requests.

Run `OFC_CONTROL_MODE=SANDBOX python -m ofc_engine.hosted_preview` and open `/plans` for a keyless, synthetic 30-decision example. Private PostgreSQL admission, signed input attestations, saved Copilot capture replay, and the optional expanded natural-language intake require separate configuration. The hosted default remains unchanged. See [plan review operations and contracts](docs/CONTROL_PLANE.md).
