# Try OFC Engine

## Published Qloo-backed tour

Open https://ofc-fixture-preview.onrender.com/#plan.

Choose **Review the Qloo-backed tour**. Click through PROPOSE, QLOO CHECK, REPAIR, VERIFY and HANDOFF to see the original $60,000 tour, frozen candidates, observed brand rank, narrow repair, pending dependent checks, revision evidence and WITHHELD outcome.

This is an interactive review of **one previously acquired genuine Qloo rank observation**, not a new live query. The browser does not receive provider keys or execute bookings. Entity IDs are redacted; option numbers refer to frozen candidate-list positions.

The separate blank editor validates new proposals with EVIDENCE_UNAVAILABLE; it cannot inherit Qloo results. Try the demo uses synthetic fixtures; Recorded proof shows separate historical evidence.

## Local development

Requires Python 3.11+:

    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -e '.[dev]'
    python -m pytest
    python -m ofc_engine.hosted_preview --port 18080

Open http://127.0.0.1:18080/. Without a private operator authorization and database configuration this is a synthetic-only preview.

Focused evidence-policy tests (external API calls are not required):

    python -m pytest tests/test_agent_run.py tests/test_qloo_decision_gate.py

PostgreSQL integration requires a dedicated isolated OFC_TEST_POSTGRES_URL; the database tests skip when it is absent. The CI browser workflow creates a separate local PostgreSQL service, uses a **test-only Qloo transport** and checks the real stored-review HTTP/Chromium journey without external Qloo spend.

## What should be visible

Only the brand changes. The opener and venues stay unchanged, while the dependent afterparty remains unverified. The final state must read REVIEW_REQUIRED / WITHHELD. A reload must not fetch a new provider response. Any missing or mismatched stored proof must fail closed.

See [Judge guide](JUDGE_GUIDE.md), [Product contract](PROJECT.md) and [Hosted operations](HOSTED_QLOO_TOUR.md).
