# Try OFC Engine

## Live Copilot — visitor-defined cultural decisions

Open https://ofc-fixture-preview.onrender.com/. Complete the first-visit scope notice and start a private visitor session.

Write your own natural-language cultural decision request. For a live-qualified policy decision, name an actual artist, an original brand selection and two to five brand candidates. This is not a general-purpose chatbot. Your phrasing is not restricted to a specific example; the system extracts a typed proposed plan and may request clarification if identities or decision scope are ambiguous.

After explicitly approving a live run, inspect the saved activity ledger, Qloo rank/recheck, OFF (unchanged proposal), ON (OFC-reviewed selection), dependency map and JSON/CSV proof. A choice may remain unchanged if Qloo evidence is absent, inconclusive or inconsistent. Even a successful brand-ranking review remains REVIEW_REQUIRED for commercial action.

A multi-city or multi-choice plan is retained in the structured proposal, but venue/locality and business claims are not currently live-qualified. Under tight quota, non-qualified optional observations can be marked NOT_EVALUATED while the qualified brand check proceeds. No bookings, endorsements or payments are made.

Live requests have shared daily Qloo/model budgets and a per-session limit. The Send confirmation and read-only capabilities view display capacity; do not retry on timeout. For no-provider-cost evaluation, use the flask icon's explicitly SYNTHETIC examples.

## Historical recorded tour

The separate legacy Studio at https://ofc-fixture-preview.onrender.com/studio.html#plan offers an interactive, read-only $60,000 Chicago–Austin–Seattle tour with one historical brand-affinity observation. It is not a newly generated live result, and its single historical observation is not the same as the Copilot's live independent rank recheck.

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

## Correctness boundaries

For the recorded tour, only the brand changes; other choices and dependent checks remain unverified. For a live request, no specific brand must be presumed to win: inspect the actual saved Qloo evidence, rank recheck and decision record. OFF never applies OFC repair; ON can revise only a qualified mutable decision. Reloading a saved run never makes a new provider call. Any missing or mismatched proof must fail closed.

See [Judge guide](JUDGE_GUIDE.md), [Product contract](PROJECT.md) and [Hosted operations](HOSTED_QLOO_TOUR.md).
