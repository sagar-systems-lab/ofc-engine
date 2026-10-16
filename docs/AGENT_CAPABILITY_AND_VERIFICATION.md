# OFC agent capability and authority boundaries

OFC is an evidence-control layer. The LLM writes proposals and explanations, Qloo contributes cultural affinity, and OFC checks scoped decisions, repairs and release constraints. External booking, contracting and payments are not supported.

## Multi-turn Copilot

Visitors can submit a new prompt with an explicit completed parent run ID belonging to the same session. The backend loads the saved typed plan and asks the model to produce a **complete revised proposal** while preserving unchanged fields. Prior runs remain immutable and replayable. Four independent messages per 24-hour visitor session are the current quota-oriented boundary, not an unlimited chat history guarantee.

A comparative response uses three separately admitted inference requests: extraction, OFF narrative and ON narrative. OFF receives the original plan and the same Qloo cultural evidence but **never** the OFC-reviewed plan/verdict. ON receives the same evidence plus policy conclusions and bounded repairs. Switching saved views makes no new provider calls. This is a controlled context-isolated comparison, not a claim of randomized causal attribution.

## Qloo catalog scope

The documented Qloo entity categories exposed by the strict lookup/insights adapters are artist, book, brand, destination, movie, person, place, podcast, TV show and videogame. Aliases map venue/restaurant to place and film to movie; location/venue **availability** is not established by taste affinity.

- Supported public schema: bounded entity search and typed fixed-candidate relative-affinity requests.
- Syntactic policy validators: query fingerprint, expected source type, exact candidate coverage, deterministic ranking, finite scores, freshness and observed response structure.
- Name resolution: Unicode-equivalent exact names can resolve; fuzzy or duplicate results require user clarification with safe, non-reversible candidate references. Neither a suggestion nor a model-proposed ID is trusted as verified.
- Currently production-scope-qualified in the project: artist→brand rank recheck policy, subject to a real provider quota and account-specific live qualification before hosted activation.
- Other categories: **observation-only until a separately authorized live Qloo request proves their provider behavior for that family**. Do not promote schema support to a safe commercial verdict. Unsupported audience, demographic and localized predicates remain independently unqualified; they require their own typed adapters and observed fixtures.

Published Qloo contract: https://github.com/qloo/docs-public/blob/main/reference/insights-api-deep-dive.md

## Repair and business authority

OFC now commits a scoped candidate change only after two distinct Qloo rank responses with matching complete order, bounded freshness and matching query fingerprints. Missing quota, mismatch, conflict or stale evidence leaves the original candidate intact. A matching taste rank only qualifies **that rank**, not commercial readiness.

Business assurance is a separate server-owned verifier: required budget feasibility, rights/contracts, venue inventory and audience-locality evidence must be bound to a specific plan ID, revision, digest, decision and expiry, signed by an explicitly trusted source authorized for that requirement. Proofs from the LLM, Qloo ranking, an unknown issuer, a mismatched revision or an expired signature cannot satisfy the gate. Every decision must have a non-empty verification policy. Until actual independent authorities/connectors and signed records are configured, the result remains `REVIEW_REQUIRED`, `full_plan_independently_verified=false`, and `release_authorized=false`.

The signable evidence envelope and pure policy evaluator are implemented/tested, but there are no automatically configured authoritative contracting, inventory, budget approval or local-audience providers. Real contracts/availability cannot be asserted from invented inputs or fixtures. Enterprise trust-anchor enrollment, source revocation and authority-specific verifiers require explicit future authorization before a public release workflow is possible.

## Qualification and change management

These three grouped change sets must be merged in sequence:
1. Session context and independent OFF/ON inference.
2. Typed Qloo category registry and safe name disambiguation.
3. Repair-before-commit and independent business proof gates.

All source tests use injected model/Qloo transports, deterministic provider-shaped responses, and isolated local databases. Passing CI demonstrates source integration, not that every Qloo category or external authority is live qualified.

No UI refactor, Render activation, server secret handling, unapproved Qloo call, real model spend or unrelated NOW Network deployment is part of these changes. Hosted product acceptance remains gated on owner-provided credentials, approved small-budget connection tests, exact deployed SHA, cold restart and isolated session E2E.
