# Hosted Copilot backend contract

This backend supplies the future single-screen Copilot. It is disabled by default and does not change the existing public saved-Qloo showcase.

## Private database migration

Apply `sql/003_dynamic_qloo.sql` (if not already applied) and `sql/004_copilot_hosted.sql` to **OFC's** private PostgreSQL database. Tables have RLS enabled and no `anon` / `authenticated` grants. Never use NOW Network's database.

## Launch gate

Set the following **server-side environment variables** in OFC's deployment dashboard, not in browser JavaScript:

- `OFC_COPILOT_MODE=PRIVATE_BETA`
- `OFC_OWNER_BOOTSTRAP_SECRET`: unique, high-entropy random secret, at least 40 characters, for initial owner-only website login
- `OFC_COPILOT_MASTER_KEY`: one Fernet encryption key, generated offline and stored separately from the database; retain for encrypted credential recovery
- `OFC_COPILOT_QLOO_DAILY_LIMIT`: 1–64 total search **and** insights requests
- `OFC_COPILOT_LLM_DAILY_LIMIT`: 1–64 total extraction, explanation and model tests
- `OFC_COPILOT_BROWSER_DAILY_LIMIT`: 1–80 new visitor sessions
- `OFC_COPILOT_SESSION_MESSAGE_LIMIT`: 1–12 messages per private session; defaults to 4
- Existing `OFC_DURABLE_RUNS_MODE=ENABLED` and `OFC_DATABASE_URL` remain required.

Unknown modes, missing migrations, invalid owner/master secrets and absent budgets fail at startup. The current service remains unaffected until this mode is explicitly set.

Generate the Fernet key locally using `from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())`; do not paste keys into chat or commit them to GitHub. This one-time deployment bootstrap is a trust root; provider credentials thereafter are configured through the authenticated website Settings API (the visible Settings control arrives in U1).

## Website Settings API (owner-only)

All writes require same-site HTTPS JSON requests. Initial authentication uses `POST /api/owner/login` with an owner secret. The response sets a short-lived `HttpOnly; SameSite=Strict; Secure` session cookie (local loopback exempt for tests). Owner Settings uses:

- `GET /api/owner/credentials`: provider states and last-four-character mask; never actual keys.
- `POST /api/owner/credentials` with `{"action":"set","provider":"qloo|gemini|groq","api_key":"..."}` to encrypt and save a key.
- `POST /api/owner/credentials` with `{"action":"revoke","provider":"..."}` to revoke.
- `POST /api/owner/test-connection` with `{"provider":"qloo|gemini|groq","confirm":"SPEND_ONE_PROVIDER_REQUEST"}` for an explicitly authorized test; counts against connection-test quota and the provider's shared global daily quota.
- `POST /api/owner/logout`: revoke the session and clear cookie.

The temporary owner setup portal at `/connect` provides this workflow using real authenticated requests. This is an operational configuration page, not the final one-screen product interface. The final UI will move the functionality to the Settings icon without changing backend security.

The user's API keys are never exposed in HTML, JavaScript, localStorage, URL parameters, application logs or JSON responses. Replacing a credential clears the last test status. The test button is never automatically executed on page load or status refresh. A configured provider is NOT_TESTED until a successful explicit test; an ACTIVE test status expires after 30 minutes. The public capability response distinguishes individual components and the derived product status without spending a provider request.

The owner secret is not a Qloo/LLM key and must not be shared with public visitors. Rotate credentials and cryptographic master key through a controlled private operation.

## Copilot HTTP API (visitor-scoped)

- `POST /api/copilot/sessions` with `{"confirm":"CREATE_COPILOT_SESSION"}`: bounded anonymous session creation; sets an opaque `HttpOnly` cookie for 24h and returns a session ID.
- `POST /api/copilot/sessions/{session_id}/messages` with `{"request_id":"opaque-unique-id","prompt":"..."}`: idempotent prompt admission, LLM extraction, budgeted Qloo name resolution + insights, OFC evaluation and paired response.
- `GET /api/copilot/sessions/{session_id}`: read own saved results; cross-session cookies are denied.
- `GET /api/capabilities`: non-spending storage/OFC/Qloo/LLM status suitable for the future top connection indicator.

The OFF and ON answers share the same language-model identity, frozen baseline and original Qloo observations. **OFF = LLM + Qloo. ON = LLM + Qloo + OFC.** The OFF/ON toggle only reads stored output and cannot consume inference quota.

Each new session requires Qloo plus a configured LLM key, is rate-limited by a private global UTC-day budget, and supports at most four independent prompts by default (configurable to 1–12). Original user prompts are encrypted in the private database and restored only to the matching visitor session. A completed response reports Qloo search requests, insights/rank requests and their combined total, plus LLM request count. Typically one artist search, two brand searches, two rank observations and three model requests (one extraction plus independent OFF and ON narration) are needed; actual counts depend on the eligible scope. Quotas count calls, not sessions. Missing keys, exhausted quotas, unavailable external services and unsupported cultural decision categories return explicit blocked/uncertain states. No client can authorize bookings, payments, contracts or external actions through this API.

The browser receives the original/reviewed choice **names** and redacted evidence summaries, not raw Qloo captures or private Qloo ID lists. Internally all decisions remain tied to scoped provider IDs, checksums, revisions and the journal.

## Qualification boundary

The deterministic GitHub tests use fake LLM/Qloo transports and an isolated PostgreSQL service. They prove API shaping, authentication, encryption, quota reservations and durable result handling; they **do not** prove that a free Gemini API key or Qloo account has working provider access.

Before enabling B2 publicly:
1. Apply private migrations to the OFC database.
2. Set the owner/master/budget environment settings privately.
3. Deploy the exact reviewed main SHA to OFC Render only.
4. Log into Settings and enter provider keys through HTTPS, not terminal.
5. Explicitly approve a bounded connection test for each provider.
6. Run a new unrelated prompt and verify one grounded OFF/ON result, same evidence, no additional calls on toggle/reload, clean cold-restart and cross-session denial.
7. Keep the product in NOT_CONFIGURED/DEGRADED rather than falsely showing ACTIVE when the above is incomplete.

Final U1 creates the user-facing Settings icon/panel, conversational one-screen UI and responsive accessibility qualification. Until then, this backend does not claim the final UI is present.


## Source-side backend closure (before Render)

The guarded public API returns a versioned `copilot.v1` projection with prompt/model/Qloo scope digests, OFC revision and journal head. LLM, Qloo name-search and insights request counts are derived from private transactional ledgers, including failed reservations. `REVIEW_REQUIRED` never implies independently verified business suitability.

An absent artist yields `CLARIFICATION_REQUIRED`; a decision domain without a qualified Qloo adapter returns `CAPABILITY_UNAVAILABLE`. Neither case invents Qloo evidence or spends a Qloo request. A Qloo HTTP 200 with invalid success/results payload does not mark the provider ACTIVE. Owner login admission is capped at 20 attempts per rolling 15-minute interval across Postgres workers.

An interrupted RESERVED conversation stays non-repeatable to prevent accidental new provider spending; after three minutes it reads as `BLOCKED / WORKFLOW_INTERRUPTED` instead of indefinitely PROCESSING. The user can explicitly submit a **new** request if needed. The original request key never silently retries.

### Preflight script for eventual hosted qualification

Prepare the exact reviewed deployment SHA. Without `--execute`, the script only reads host readiness and capability status; it creates no session and makes **zero** Qloo/LLM calls:

```bash
python scripts/qualify_copilot_hosted.py \
  --base-url https://ofc-fixture-preview.onrender.com \
  --expected-sha <full-merged-commit-sha>
```

Only **after** owner-configured website credentials, explicit one-call connection tests, confirmed active provider statuses and authorization of a live demonstration, run a single bounded new-prompt E2E:

```bash
python scripts/qualify_copilot_hosted.py \
  --base-url https://ofc-fixture-preview.onrender.com \
  --expected-sha <full-merged-commit-sha> \
  --execute \
  --approve-spend APPROVE_ONE_BOUNDED_COPILOT_E2E \
  --prompt "Compare two real brands for a real Qloo-resolvable artist."
```

This is an **example command**, not an authorization or a claim that the sample prompt is qualified. Replace the prompt with a verified, fictional/public test scope and inspect the live usage limits before executing it. A single workflow can consume multiple real Qloo and model calls. There are no hidden retries. The script validates: OFF/ON share Qloo evidence, only OFC differs, original prompt is restored, idempotent replay produces no extra usage, provider-origin fingerprints are present, and a second visitor cannot read the first visitor's history. It never reveals API credentials.

`sql/004_copilot_hosted.sql` is idempotent for existing tables and backfills the model-attempt counter if an earlier private B2 migration was applied. Run migrations in the OFC database **only**. No migrations or deployments occur merely from merging this code.

Code/test closure is not live-provider acceptance. The qualified evidence family is still scoped to artist→brand ranking; other cultural or business domains require separate real Qloo verification. Browser-level design remains the owner-directed U1 phase.

## Conversation continuity and independent comparison

A subsequent message may include an optional `parent_run_id` pointing to an existing **completed** run in the same visitor session. The server validates the parent digest and ownership, passes only the redacted structured prior plan to the LLM, and requires a full new typed plan. An unrelated run ID or an attempt to reuse the same request ID with a different parent is rejected. Each follow-up is a fresh bounded review; old Qloo captures are never silently borrowed for a new scope. This is contextual plan editing, not an unbounded long-term chat memory service.

The OFF natural-language response is generated in a separate inference request **before** ON, using the original baseline plus the shared Qloo rank only. The OFF model never receives OFC changes, verdicts, repair reasons or reviewed decisions. ON receives the identical Qloo evidence plus OFC's scoped findings. Both arms keep the same model identity; inference totals are 3 per complete task (extraction + separate OFF + ON). UI toggles and saved-message reads are provider-free. These are controlled, evidence-blinded inference arms, not an independent randomized clinical-style causal experiment.

The anonymous session limit defaults to 4 prompts for quota safety. The supported 1–12 setting does not create additional upstream allowance: raise it only after verifying global free-tier request capacity and abuse controls.


## Entity disambiguation

A Qloo /search response may contain several plausible aliases or indistinguishable names. The server returns `CLARIFICATION_REQUIRED` and bounded suggestions with opaque `option_ref` values, never silently choosing a fuzzy provider ID. The user can select a suggestion in a new Copilot message using **all** of `parent_run_id`, `entity_selection_ref`, `request_id` and `prompt`. The owner of the parent visitor session is checked, the selected reference must occur in that parent response, and a newly reserved Qloo lookup must return the same identity bound to the request fingerprint. A stale/cross-session/forged reference fails closed. The final UI can render these options as selection controls without ever sending raw Qloo IDs to the browser.

This user confirmation is **entity disambiguation**, not independent verification that the place has availability, the brand has contract rights, or a specific audience will convert.


## Cross-domain Qloo observations (policy deliberately withheld)

The hosted Copilot can now resolve artist-anchored candidates in the documented entity catalog (movie, place, destination, book, and related supported types) and inspect a **bounded relative-affinity ranking**. Every lookup and insights request enters the same shared Postgres Qloo daily budget before network I/O; saved observations carry scope fingerprints and digest-checked replay.

The original and OFC-enabled views share one immutable catalog-evidence digest and human-readable ranked candidate names. These non-brand observations are explicitly labeled `OBSERVED_NOT_POLICY_QUALIFIED`; OFC returns `UNCERTAIN / CATALOG_POLICY_NOT_LIVE_QUALIFIED` rather than committing a choice or inferring venue inventory, reservations, signed contracts, business suitability or verified audience geography.

A taste subject is still necessary for the current website flow; the initial configured Copilot schema is artist-anchored. General free-form audience demographics, tags and location filters need separately qualified request grammars and provider-backed tests. Qloo's documented category names alone do not certify that arbitrary provider keys support every combination. Real domain-specific Qloo qualification must remain a controlled user-approved test step before any new domain may be promoted from observation-only to decision policy.

## Judge-volume capacity planning (opt-in)

The server defaults remain unchanged; a code merge does **not** increase the live daily limits.
The Qloo gate supports 1–256 requests/day and the model gate 1–128/day with the same
PostgreSQL atomic reservation and no automatic retry behavior. A hypothetical 24
three-brand evaluations at six Qloo searches/ranks and three model calls each
requires approximately 144 Qloo and 72 model reservations. For this controlled
capacity envelope, the operator may configure, **only after checking provider
rate limits, available credits, and a dollar spending ceiling**:

```text
OFC_COPILOT_QLOO_DAILY_LIMIT=150
OFC_COPILOT_LLM_DAILY_LIMIT=75
OFC_COPILOT_BROWSER_DAILY_LIMIT=30
```

These are *daily request ceilings*, not promises of successful end-to-end reviews,
hard dollar limits, provider-approved quotas, or per-visitor allowances. Actual
usage varies by the number of named candidates, extra domains, clarification,
identity ambiguity and partial failures. A request admitted near a quota boundary
may still be blocked by the durable quota gate. Keep the model/Qloo keys server
side, monitor upstream credits, and do not activate these values without explicit
owner spend approval. Avoid a public unauthenticated stress-test endpoint; use
isolated CI transports for load and security regression tests.
