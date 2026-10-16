# Dynamic Qloo Agent Review

The authenticated local Agent API supports proposals that are not tied to the published tour. The proposal carries its own run ID, selected candidates, immutable pools, constraints and Qloo artist identity.

## Enable controlled live review

The default mode is disabled. Configure these in the **private operator environment**, not the repository or browser:

- `OFC_AGENT_TOKEN`: strong random local API bearer token (32–256 ASCII characters).
- `QLOO_API_KEY`: operator-owned credential.
- `OFC_DYNAMIC_QLOO_MODE=OPERATOR_BUDGETED`
- `OFC_DYNAMIC_QLOO_DAILY_LIMIT`: integer from 1 to 64, counting individual provider requests, **not plans**.

Start `python -m ofc_engine.agent_api --database /private/ofc/agent.sqlite3 --port 8770`. The service binds only to `127.0.0.1` and requires bearer authentication. Existing hosted Studio and the saved Qloo example are unaffected.

POST an `AgentProposal` JSON document to `/api/proposals/evaluate` with `Authorization: Bearer <private token>` and `Content-Type: application/json`. A successful evaluation returns a revision-bound report. Repeating the same immutable proposal does not repeat completed provider requests.

## Decision coverage

The current qualified request family is Qloo **artist → brand-affinity ranking**, not unconstrained Qloo search. An independent `brand`, `partner`, or `brand_partner` decision can be evaluated when:

- `intent.taste_context.artist_id` is a pre-resolved Qloo artist ID; optionally override by decision through `qloo_artist_ids: {"decision-id": "artist-id"}`.
- `candidate_pool` holds two to five distinct, pre-resolved brand IDs.
- `candidate_id` belongs to that pool; a repair additionally requires `mutable_fields: ["candidate_id"]`.
- No locality or dependency changes the interpretation of the Qloo request. Unsupported categories remain UNCERTAIN.

Up to four independent supported brand decisions are considered per run. Each normally uses **two** reserved provider requests: original ranking and a separate fresh ranking check. The limit counts both requests. Reservation is committed **before** network I/O; a timeout or crash cannot silently retry a spent attempt. The fixed pools are per-proposal, not hardcoded artist, city or brand values.

If a higher-ranked candidate exists, a permitted repair commits through the existing CAS/idempotency mechanism and append-only journal. If a second, distinct request agrees, the **brand-rank decision** receives PASS with `QLOO_RANK_RECHECK_MATCHED`. Otherwise it is UNCERTAIN. Pending dependent checks remain pending.

## What the result does and does not mean

A second Qloo request verifies *repeatability of a within-query rank* only. It is **not independent provider attestation**, nor independent venue/contract/inventory/budget/business-outcome verification. The result always includes `full_plan_independently_verified: false`, `provider_authenticated: false`, `verification_status: REVIEW_REQUIRED`, `release_authorized: false` and `external_action_executed: false`.

Unsupported decisions, stale evidence, exhausted budget, mismatched stored plan, inconsistent second rank and interrupted attempts fail closed. Public visitors do not receive the operator credential and cannot invoke this loopback API.

## Tests and rollout

`python -m pytest tests/test_dynamic.py tests/test_agent_api.py`

Tests use deterministic injected Qloo transports and make no real provider requests. Existing production live-Qloo authorization remains a separately guarded single-use path. The same evaluator is also wired behind an **independently authenticated, default-disabled hosted API**. This is server-to-server infrastructure, not a browser-accessible public Qloo switch.

## Hosted agent integration

Apply `sql/003_dynamic_qloo.sql` to the **OFC PostgreSQL database only** before enabling the hosted path. It creates private, row-level-security-protected per-plan Qloo attempts and a UTC-day budget row. No migration is applied automatically and no public permission is granted.

In the private OFC service environment, in addition to its existing database connection, set:

- `OFC_DYNAMIC_QLOO_MODE=OPERATOR_BUDGETED`
- `OFC_DYNAMIC_QLOO_DAILY_LIMIT=...` (1–64 requests/day)
- `OFC_DYNAMIC_AGENT_TOKEN=...` (strong 32–256 character ASCII secret, **not** the public browser ticket)
- `QLOO_API_KEY` (already operator-protected)

POST the same immutable `AgentProposal` to `/api/agent/evaluate` with the private bearer token from a **server-side agent**, not JavaScript in a visitor's browser. Browser-origin submissions are rejected; unauthorized requests cannot start provider traffic. The endpoint is disabled without operator configuration. PostgreSQL persists reservations across restarts; replaying completed attempts does not issue a new Qloo request.

The current public hosted Studio remains unchanged. Do not enable the hosted endpoint until its isolated database migration, key quota and deploy qualification are approved. No user-facing self-service Qloo spend is authorized by this backend route.
