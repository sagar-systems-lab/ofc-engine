# Copilot integration contract

OFC Engine provides evidence-backed cultural decision review for AI agents. The hosted Copilot is one client of the same decision engine; a server-to-server API is another. Neither interface should depend on the published saved tour example.

## Execution path

1. Receive a bounded natural-language prompt with a server-owned session and idempotency key.
2. Extract a structured draft through a configured LLM adapter. Keep names and candidate suggestions marked unverified until resolved.
3. Validate and disambiguate entity IDs through qualified Qloo search capabilities. Unsupported domains remain unknown; no guessed IDs.
4. Freeze the proposal and immutable candidate pools with existing `AgentProposal` and revision digests.
5. Qloo evidence must be acquired **once** and available to both views. **OFC OFF = LLM + Qloo**: the LLM writes a Qloo-grounded baseline answer without OFC decision verification or repairs. **OFC ON = same LLM + same Qloo observation + OFC**: only the OFC policy, selective repair, uncertainty checks and controlled re-evaluation are added. The comparison must not remove Qloo from OFF or secretly spend twice. Both arms retain one original plan and the same prompt/model/evidence provenance.
6. Persist both views against one prompt/model identity, one baseline digest and identical Qloo observation digests. The OFF answer is *not* an ungrounded LLM-only response. Qloo may produce relative cultural ranks on both sides; only OFC ON enforces decision safety, selective repair and revision-bound verification. The UI toggle is a read-only projection switch, never a provider call.
7. Compose human-readable OFF and ON explanations from the same immutable cultural evidence. The LLM cannot determine evidence validity or authorize actions. Do not claim the OFF arm is missing cultural intelligence; ON must demonstrate OFC's additional value, not Qloo's value.

## Integrations

- LLM interface: provider-neutral `extract` and `explain` with validated JSON schema, bounded token/request limits, no credential exposure and explicit unavailable states. Gemini Flash-Lite is a possible configured model; Groq is an optional separate provider.
- Qloo interface: scoped `/search` entity resolution and validated `/v2/insights` query families. Only artist-to-brand rank is currently qualified by OFC. Every additional family requires schema, response and live capability tests.
- Decision interface: existing `dynamic`, `dynamic_postgres`, `repair`, `revisions` and `postgres_store` machinery, including daily provider budget reservations and CAS/idempotency.
- Public browser interface: new bounded session APIs on the server with same-origin protection, per-session/aggregate limits and a redacted evidence projection; never expose operator bearer credentials in JavaScript.
- Enterprise integration interface: private authenticated calls with stable versioned schemas, independently scoped from the browser.

## Capability and source-state contract

Distinguish `NOT_CONFIGURED`, `CHECKING`, `ACTIVE`, `DEGRADED`, `QUOTA_EXHAUSTED` and `UNAVAILABLE` per runtime component. A configured API key is not evidence of successful provider access. Status reads must not issue billable Qloo or LLM requests.

Evidence policies must distinguish:
- **PASS**: a qualified scoped decision supported by an eligible observation.
- **REPAIR**: a permitted, evidence-backed change to the frozen decision.
- **UNCERTAIN**: missing/stale/ambiguous evidence, unqualified category, unresolved dependency, provider error or exhausted budget.
- **Rank rechecked**: a distinct later scoped ranking agrees. This does not independently authenticate provider identity or certify full business suitability.
- **Review withheld**: no external actions authorized while required checks remain unresolved.

Do not infer inventory, availability, contracts or financial viability from Qloo affinity.

## Client behavior contract

One primary Copilot workspace accepts freeform prompts, displays a single conversation, and visualizes a server-generated structured plan and OFF/ON saved projections. Both modes retain LLM and Qloo. The switch controls **only OFC**. Secondary historical demo, proof and settings surfaces can be preserved behind accessible supplementary navigation. The visual design is independent of this backend contract.

## Tests required before hosted activation

- Multiple unrelated prompts and Qloo scopes; no cross-session access or evidence borrowing.
- Missing data and ambiguous entity resolution fail closed; unsupported domains are not upgraded to PASS.
- Same model, prompt, baseline and Qloo observation digests in both comparison views. Qloo evidence must be present in both OFF and ON arms.
- Duplicate messages/toggle/reload do not spend new provider requests.
- Distinct Qloo rank recheck disagreement remains uncertain; dependent decisions remain invalidated.
- Provider 401/403/429/timeout, invalid JSON, quota exhaustion, tampering, hostile prompt and replay.
- PostgreSQL persistence/restart integrity and no public secret/header/raw-capture leakage.
- Hosted exact-SHA, browser, accessibility, and no-unapproved-request qualifications.

This contract describes planned interfaces; availability on GitHub or passing fixture CI is not equivalent to enabled hosted inference, live provider authorization, or complete independent verification.

## Website-managed API connectivity (backend B2; visual design U1)

The eventual Settings icon opens an owner-only **API Connectivity** panel for Qloo and one LLM provider. Secrets are submitted over HTTPS directly to the OFC server, never to a third-party client or browser storage. This panel must be **owner-authenticated** (not merely a hidden page or a guessed URL). A public visitor cannot set or read service-wide credentials. Provider keys must never appear in HTML, JavaScript bundles, responses, traces, logs or the Git repository.

The server stores credentials only encrypted at rest with a separate server-side master encryption key (or Render-managed secret storage), returns only configured provider, masked suffix and last bounded-test status, and supports explicit replace/revoke. No raw key is returned after submission. A connection test is a **user-authorized API operation** with quota implications and recorded result; a status refresh never triggers provider spend. If an initialization trust anchor is needed, bootstrap it once through an owner-only control-plane action; do not expose an unauthenticated 'first user becomes admin' route. No client-side localStorage credentials and no client-supplied operator bearer forwarded to the public runtime.

For the free Render architecture, migrate a private Postgres credential-vault table only after key management and owner authentication have been tested; the current code is **not** yet an operational user-configurable vault. Until B2, status is NOT_CONFIGURED and Settings must not claim keys were saved.
