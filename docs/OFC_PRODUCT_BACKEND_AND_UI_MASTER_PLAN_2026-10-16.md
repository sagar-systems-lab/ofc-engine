# Copilot integration contract

OFC Engine provides evidence-backed cultural decision review for AI agents. The hosted Copilot is one client of the same decision engine; a server-to-server API is another. Neither interface should depend on the published saved tour example.

## Execution path

1. Receive a bounded natural-language prompt with a server-owned session and idempotency key.
2. Extract a structured draft through a configured LLM adapter. Keep names and candidate suggestions marked unverified until resolved.
3. Validate and disambiguate entity IDs through qualified Qloo search capabilities. Unsupported domains remain unknown; no guessed IDs.
4. Freeze the proposal and immutable candidate pools with existing \`AgentProposal\` and revision digests.
5. Preserve the baseline as the OFC OFF view. The OFC ON view uses supported Qloo evidence, decision verdicts, allowed selective patches, dependency invalidation and rechecks.
6. Persist both views against one prompt/model identity and one baseline digest. The UI toggle is a read-only projection switch, never a provider call.
7. Render human-readable explanation from stored typed verdicts; the LLM cannot determine evidence validity or authorize actions.

## Integrations

- LLM interface: provider-neutral \`extract\` and \`explain\` with validated JSON schema, bounded token/request limits, no credential exposure and explicit unavailable states. Gemini Flash-Lite is a possible configured model; Groq is an optional separate provider.
- Qloo interface: scoped \`/search\` entity resolution and validated \`/v2/insights\` query families. Only artist-to-brand rank is currently qualified by OFC. Every additional family requires schema, response and live capability tests.
- Decision interface: existing \`dynamic\`, \`dynamic_postgres\`, \`repair\`, \`revisions\` and \`postgres_store\` machinery, including daily provider budget reservations and CAS/idempotency.
- Public browser interface: new bounded session APIs on the server with same-origin protection, per-session/aggregate limits and a redacted evidence projection; never expose operator bearer credentials in JavaScript.
- Enterprise integration interface: private authenticated calls with stable versioned schemas, independently scoped from the browser.

## Capability and source-state contract

Distinguish \`NOT_CONFIGURED\`, \`CHECKING\`, \`ACTIVE\`, \`DEGRADED\`, \`QUOTA_EXHAUSTED\` and \`UNAVAILABLE\` per runtime component. A configured API key is not evidence of successful provider access. Status reads must not issue billable Qloo or LLM requests.

Evidence policies must distinguish:
- **PASS**: a qualified scoped decision supported by an eligible observation.
- **REPAIR**: a permitted, evidence-backed change to the frozen decision.
- **UNCERTAIN**: missing/stale/ambiguous evidence, unqualified category, unresolved dependency, provider error or exhausted budget.
- **Rank rechecked**: a distinct later scoped ranking agrees. This does not independently authenticate provider identity or certify full business suitability.
- **Review withheld**: no external actions authorized while required checks remain unresolved.

Do not infer inventory, availability, contracts or financial viability from Qloo affinity.

## Client behavior contract

One primary Copilot workspace accepts freeform prompts, displays a single conversation, and visualizes a server-generated structured plan and OFF/ON saved projections. Secondary historical demo, proof and settings surfaces can be preserved behind accessible supplementary navigation. The visual design is independent of this backend contract.

## Tests required before hosted activation

- Multiple unrelated prompts and Qloo scopes; no cross-session access or evidence borrowing.
- Missing data and ambiguous entity resolution fail closed; unsupported domains are not upgraded to PASS.
- Same model, prompt and baseline digests in both comparison views.
- Duplicate messages/toggle/reload do not spend new provider requests.
- Distinct Qloo rank recheck disagreement remains uncertain; dependent decisions remain invalidated.
- Provider 401/403/429/timeout, invalid JSON, quota exhaustion, tampering, hostile prompt and replay.
- PostgreSQL persistence/restart integrity and no public secret/header/raw-capture leakage.
- Hosted exact-SHA, browser, accessibility, and no-unapproved-request qualifications.

This contract describes planned interfaces; availability on GitHub or passing fixture CI is not equivalent to enabled hosted inference, live provider authorization, or complete independent verification.
