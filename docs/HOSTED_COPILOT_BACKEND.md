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

The user's API keys are never exposed in HTML, JavaScript, localStorage, URL parameters, application logs or JSON responses. Replacing a credential clears the last test status. The test button is never automatically executed on page load or status refresh. A configured provider is NOT_TESTED until a successful explicit test; an ACTIVE test status expires after 30 minutes.

The owner secret is not a Qloo/LLM key and must not be shared with public visitors. Rotate credentials and cryptographic master key through a controlled private operation.

## Copilot HTTP API (visitor-scoped)

- `POST /api/copilot/sessions` with `{"confirm":"CREATE_COPILOT_SESSION"}`: bounded anonymous session creation; sets an opaque `HttpOnly` cookie for 24h and returns a session ID.
- `POST /api/copilot/sessions/{session_id}/messages` with `{"request_id":"opaque-unique-id","prompt":"..."}`: idempotent prompt admission, LLM extraction, budgeted Qloo name resolution + insights, OFC evaluation and paired response.
- `GET /api/copilot/sessions/{session_id}`: read own saved results; cross-session cookies are denied.
- `GET /api/capabilities`: non-spending storage/OFC/Qloo/LLM status suitable for the future top connection indicator.

The OFF and ON answers share the same language-model identity, frozen baseline and original Qloo observations. **OFF = LLM + Qloo. ON = LLM + Qloo + OFC.** The OFF/ON toggle only reads stored output and cannot consume inference quota.

Each new session is rate-limited by a private global UTC-day budget and supports at most four independent prompts. Two model calls and several Qloo search/insights requests may be required for a prompt; quotas count calls, not sessions. Missing keys, exhausted quotas, unavailable external services and unsupported cultural decision categories return explicit blocked/uncertain states. No client can authorize bookings, payments, contracts or external actions through this API.

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
