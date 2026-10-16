# OFC Engine — Product Integration & Execution Contract

Status: IMPLEMENTATION PLAN / NOT A CLAIM OF COMPLETION  
Base reviewed: main @ 71a579ddeeb61edcc2375bc57b93dc90a16a1e7c (PR #84 merged)  
Objective: make OFC a usable Qloo-grounded AI decision layer, demonstrated through one real interactive Copilot and also consumable by external agents.

## 1. Product invariant

**One prompt → one baseline → two comparable views → one structured plan.**

The user types a natural-language request. A real LLM extracts intent, constraints, cultural subjects and *proposed* decisions. The backend stores and freezes that baseline. OFF is the original AI result without OFC evidence. ON applies supported Qloo cultural observations, OFC verdicts, allowed selective changes, dependency invalidation and verification state to that **same** baseline. Both modes expose the same task and model identity. ON must never manufacture evidence, pass unsupported decisions or silently rewrite the OFF answer.

The core is an integration product, not a tour-specific demo: the web Copilot is its first client; future apps consume the same authenticated agent APIs. The existing published $60k six-decision tour is retained as an isolated historical example and must not be wired into arbitrary new users' requests.

Qloo supplies cultural association/evidence. A general-purpose LLM supplies natural-language understanding and explanation. OFC supplies typed scope binding, evidence admission, decision review, selective repair, replay-safe revision control, verification boundaries and action gating. Qloo is neither merely a chat provider nor interchangeable with the LLM.

## 2. Verified starting point (repo audit)

Available:
- Typed ActionIntent, Decision, PlanSnapshot with bounded intake; immutable candidate pools within a run; DAG validation.
- Qloo /search adapter limited to artist and brand entity types; one qualified artist→brand /v2/insights affinity rank grammar and normalization.
- Operator-controlled dynamic brand evaluation for arbitrary proposed plans, with SQLite or PostgreSQL, bounded daily request reservations, idempotent replays, rank recheck and hash-linked repair journal (PR #84).
- Durable PostgreSQL hosted proposal registry, private browser tickets, bounded public fixture showcase, hosted browser tests.
- Public studio at ofc-fixture-preview.onrender.com currently serves a separate historical saved review; its Copilot is a read-only explanatory widget.

NOT YET PROVIDED / NOT PROVEN:
- Real language-model backend, turn/session orchestration, machine-validated text→AgentProposal, conversation persistence, multi-turn editing.
- Prompt/brand/artist text→Qloo entity ID resolution with disambiguation; unsupported domains must not be inferred as resolved.
- General grounded natural-language reply from verified *decision* outputs; OFF/ON same-run comparison API; auto-filled plan projection.
- Public-browser authenticated, budget-controlled Copilot session APIs. The current /api/agent/evaluate is private server-to-server and must **never** receive the operator bearer token from JS.
- Broad Qloo categories, venue availability, booking, independent Qloo attestation, financial suitability, generalized full-plan verification.
- Hosted activation of PR #84 dynamic mode: source on main does not mean database migration/configuration/deployment is live.
- Scale/load/security qualification of arbitrary judge-driven inference traffic. Green static fixture/browser CI is not proof of live end-to-end AI traffic.

## 3. Minimal externally visible product contract

One primary workspace (final UI design reserved for the operator):
- Header: actual OFC status indicator. OFF / NOT_CONFIGURED / CONNECTING / ACTIVE / DEGRADED / ERROR based on server capabilities and probe results; do not label Qloo or LLM "online" only because a key is set.
- One Copilot input and conversational assistant reply; optional examples, no required manual form.
- OFC OFF/ON switch; both arms reuse the same frozen baseline, deterministic policy version and provider-model config. Switching view must not generate unapproved provider calls.
- Structured plan/evidence/changes panel automatically derives from typed backend output; OFF plan remains readable but evidence/control fields are visually disabled (not data deleted).
- Secondary Demo, Recorded Proof, Settings and diagnostic sections move behind accessible icons/drawers, not extra primary route tabs. No deletion of existing archival/fixture content without explicit operator direction.
- Desktop + 375px + 320px, keyboard, focus, reduced-motion, screen reader and clear empty/error/blocked states.
- Single page: results and reasoning near the prompt. Not a five-page slide deck.

The UI is the **last** implementation phase. This document freezes only the behavior and interfaces; actual colors, layout, typography, motion and styling await operator design approval.

## 4. Canonical backend transaction

A. POST a prompt to create a bounded browser/session-scoped conversation. Require input length, privacy classification, and access policy; assign server-owned session/run IDs.

B. LLM extraction (same pinned free-tier model for both arms):
   - Parse objective, constraints, candidate names, domains, dependencies, and uncertainties into a strict, server-validated structure.
   - Distinguish known facts from proposed candidates and missing required slots. Reject or ask clarification instead of hallucinating Qloo IDs or fabricating budgets.
   - Store original LLM proposal, model/version, normalized input digest and extraction provenance.
   - Server validates schema with existing Pydantic contracts and keeps original baseline immutable.

C. Entity resolution:
   - Resolve submitted names to Qloo IDs by supported search grammar; disambiguate ambiguous/missing identities with a clarification step.
   - Bound candidates and Qloo lookups before reserving spend; no blind queries or unqualified categories/locality.
   - Maintain capability matrix: artist↔brand affinity qualified; further artist/venue/dining/film/fashion/lookalike queries require official schema + controlled live qualification **per family**.
   - Never pretend a restaurant is open, has inventory, or a contract is signed because Qloo affinity is high.

D. Persistent evidence workflow:
   - Create a new immutable proposal and evidence query specs per run; reserve Qloo requests before network I/O in private Postgres.
   - Run validated Qloo adapter only with explicit bounded provider quotas and spend policy; no unlimited self-service Qloo access.
   - Evaluate per decision, produce PASS / REPAIR / UNCERTAIN and stable reason codes; apply only allowlisted fields via CAS and idempotency.
   - Recheck repaired ranks as separate evidence events; mark third-party attestation and full business independence false until actually established.
   - Preserve unchanged decisions, invalidate dependents, retain revision/digest/journal and separate "rank checked" from "full-plan verified".
   - No external action until an explicit later release policy and actual authorized independent checks exist.

E. OFF/ON comparison:
   - OFF response references the stored baseline model and plan without Qloo/OFC intervention.
   - ON references the same baseline plus verified Qloo-bound evidence, current revision, changed IDs, risk and pending rechecks.
   - Both responses grounded in a typed AnswerProjection assembled **before** verbalization; LLM can write a human summary but never authorizes a PASS, invents confidence or changes the evidence source.
   - Toggle reads saved comparison, never triggers a new LLM or Qloo request. Saved responses share run_id, baseline_digest, model identity, prompt fingerprint.
   - A fallback model changes comparison identity: show invalid/mismatched until the entire baseline is re-established, no cherry-picked arms.

F. Explanation and API response:
   - Status, conversation messages, baseline_plan, reviewed_plan, decisions, changes, Qloo evidence summaries, uncertainty/reason_codes, cost/quota accounting, provenance and release state.
   - No private raw Qloo captures, tokens, full provider headers or full confidential prompts in public responses or logs.
   - Return explicit partial failure states on provider unavailability, caps, ambiguous identity or stale evidence; no silent synthetic fallback.

## 5. Provider decision (2026-10-16 researched)

Primary: Gemini 3.1 Flash-Lite (model gemini-3.1-flash-lite), conditional on a confirmed available API free tier for this account. Supports structured outputs. Use server-side REST adapter, no SDK requirement if a small bounded HTTPS client suffices. Set maximum output tokens, temperature and request quota.

Backup: Groq Free Plan with supported schema-capable model (for example openai/gpt-oss-20b), **separate configured mode**, not a silent per-arm replacement. Exact per-account limits must be checked. No consumer chat scraping.

DeepSeek developer API is billed by tokens, notwithstanding free use of its consumer chat app. Do not choose as default for zero-spend development or assume promotional credits.

Provider-neutral LLM interface: extract_plan, compose_response, health/capability and deterministic injected test transport; preserve provider/model IDs and output digests. Keys only in server secrets. If neither free API key is available, implement and test the interfaces with injected deterministic transports; show LLM NOT_CONFIGURED and **do not claim live Copilot completed**.

Pricing and policies:
- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite
- https://console.groq.com/docs/rate-limits
- https://api-docs.deepseek.com/quick_start/pricing
- https://qloo.devpost.com/rules

Free inference tiers are quota-limited and may have provider data-use terms. Use only fictional/demo public inputs on free models until privacy/commercial arrangements are suitable. Keep separate inference and Qloo budget counters.

## 6. Private and browser API boundaries

Private integrations:
- Existing authenticated POST /api/agent/evaluate remains server-to-server only, behind bearer auth. New canonical private POST /v1/agent/runs (propose) and POST /v1/agent/runs/{run_id}/review with a scoped integration token can be added, sharing the same OFC core. Version contracts and return stable machine-readable errors.

Public Copilot:
- POST /api/copilot/sessions: bounded anonymous visitor token, CSRF/same-origin policy, consent/usage notice as needed.
- POST /api/copilot/sessions/{id}/messages: prompt, selected conversation revision and idempotency key; server runs bounded, model-backed extraction then policy.
- GET /api/copilot/sessions/{id}: ticketed persisted messages and comparison projection.
- GET /api/capabilities: safe, non-sensitive status by component and evidence freshness.
- No write via GET, no credentials in web assets, no browser-callable private operator endpoints.
- Browser identities from server-owned session token; budget admission by session/IP aggregate/day, concurrency limits, payload bounds and rate limits; request starts cannot multiply Qloo requests through refresh/retry.

Responses are bounded, schema-versioned. Separate public redacted projection from private operator audit. The public website may remain read-only or degraded when an upstream budget or provider is unavailable.

## 7. Exact verification boundaries

"Rank rechecked" = two distinct Qloo requests produce a consistent result within the frozen candidate pool. It is not verification that the provider owns an entity, that a recommendation will succeed or that every dependency is independently satisfied.

"Full-plan independently verified" requires **policy-specific authoritative evidence** for *every required decision and dependent*, freshness, a revision match, rights/permissions, and no open blockers. Most tour business constraints have no such verifier yet and therefore remain REVIEW_REQUIRED. Do not simply flip existing false flags. Do not declare all-six PASS for lack of a missing provider.

Future independent verifiers should be an adapter registry with explicit scope and authority, not a generic LLM "verified" label. External actions remain unavailable until independently implemented and approved.

## 8. Failure and status design

Component statuses: OFF, NOT_CONFIGURED, CHECKING, ACTIVE, DEGRADED, QUOTA_EXHAUSTED, UNAVAILABLE. Top OFC status derives from **real server capability** and active run evidence, not a static green dot. Distinguish:
- OFC core ready, Qloo missing, LLM missing.
- Provider configured but live validation unproven.
- LLM available but Qloo down → show base answer and OFC UNCERTAIN, not synthetic repair.
- Qloo available but LLM down → no fabricated conversation; saved typed reviews still viewable.
- Free Render cold-start → CHECKING/RECONNECTING until server responds; not indefinite offline verdict.
- Timeout, 429 and interrupted reservation → safe error with quota consumed where applicable; no hidden auto-retries.

Status endpoint must not spend Qloo or LLM quota just to refresh a badge. Use recent bounded probe timestamps and conservative "unknown" on absent evidence.

## 9. Minimal grouped PR plan

**PR B1 — Engine: Language → frozen plan → verified evidence → answer projection**
- Add provider-neutral Gemini primary / Groq optional adapter, strict JSON extraction schema, bounded timeouts and transport injection.
- Typed conversation/plan and prompt provenance, Qloo name/entity resolution for supported categories, quota admission and replay safety.
- Reuse dynamic Qloo evaluator; fix discoverable concurrency/scope/journal edge cases before opening public API.
- Build one same-run OFF/ON AnswerProjection with no LLM authority over verdicts, persisted run and tests.
- Integrate this contract document. Test schema failures, ambiguous identities, unqualified domains, provider failures, repeat/cost, tampered evidence, prompt injection and changes to model identity.
- **Exit:** real-agent service protocol end-to-end under deterministic provider transports; no real paid API call; CI all green. Real free-key integration remains separately gated.

**PR B2 — Hosted backend: Secure public Copilot + real-provider qualification**
- Only once B1 merged: private Postgres migrations, browser session/quotas/CSRF/rate limits, status endpoint, protected APIs; keep private server-to-server path separate.
- Render OFC-only deployment staging switch, Gemini free-tier actual request validation, Qloo capability queries only within separate operator-approved spending budget.
- End-to-end one prompt generates and persists two comparable projections, then refresh/restart replays zero provider traffic.
- Test unauthorized requests, deliberate cross-session access, free-tier exhaustion, cold restart, mobile-client JSON paths and real hosted boundaries.
- **Exit:** hosted endpoint returns genuine saved LLM+Qloo+OFC results; exact deployed SHA checks green; zero credentials/browser leakage.

**PR U1 — Final single-screen UI/UX (last)**
- Operator provides visual direction. One Copilot conversation and integrated auto-filled workspace; ON/OFF controls; icon drawers for supporting sections and the red/green factual header indicator.
- Responsive/accessibility qualification and human user test. Remove/de-emphasize confusing legacy primary tabs only with explicit UI approval.
- **Exit:** hosted end-to-end judge can type, compare, inspect changed decisions/evidence/reasons, refresh and understand every state without a guide.

**No micro PRs** for individual tests, labels, copy tweaks or small submodules. Changes requiring revised provider budget, legal/privacy terms, or a new evidence domain may need explicit gated follow-up; do not hide them in the UI PR.

Merge protocol: one branch/PR at a time, all relevant checks pass, owner reviews/merges/deletes branch, advance to next. Do not auto-merge. Main is the engineering source of truth; deployed SHA and actual runtime probes are the hosting source of truth. Do not touch NOW Network.

## 10. Acceptance matrix and real completion definition

B1 and B2 are NOT "done" merely when a PR passes fixture CI.

| Scenario | Expected behavior |
| --- | --- |
| Two unrelated users ask different questions | Distinct sessions/plans; separate Qloo scopes; no cross-leakage |
| Natural language with missing city/budget | Clarification or explicitly unknown slots, not fictional completed fields |
| User asks about unsupported taste category | Explicit UNCERTAIN + accurate capability explanation |
| Same prompt + OFF/ON | Same frozen baseline/model; OFF untouched; ON only supported changes |
| User flips OFF/ON repeatedly | Zero new provider calls |
| Provider budget exhausted | Safe limited answer; no hidden spend; honest status |
| Qloo unavailable | Original LLM answer remains, OFC withholds unsupported conclusions |
| LLM unavailable | No fabricated chatbot text or "AI active" badge |
| Concurrency / repeated POST | Idempotent session message and durable request quota; no double spend |
| Dependent decision after brand repair | Dependent marked pending until independently rechecked |
| Tampered journal/scope | Fail closed; no release; no public raw evidence |
| Reload / Render cold restart | Same stored history/comparison, no additional model/Qloo spend |
| Malicious prompt tries to override OFC verdict | Untrusted LLM prose cannot change server policy or reveal secrets |
| Hosted public browser | Can request bounded authorized product tasks; cannot reach private operator token |
| Operator-free public visit | Working zero-cost tier within allotted caps or clearly marked unavailable; no fake demo |
| Published repo and live Render version | Exact SHA matched; runtime smoke and security gates pass |

Backend DONE means: both real external integrations (LLM + Qloo) succeed on the hosted OFC instance with approved quota, a new arbitrary user task receives same-baseline OFF/ON results, the auto-filled typed plan is accurate, provenance and replay are durable, and failure/security/restart tests pass. If no genuine free LLM key or permitted Qloo requests exist, backend is **CODE READY / LIVE UNVERIFIED**, not complete.

## 11. Constraints and non-goals

- Current target is a **production-oriented integration product** and genuinely functional public evaluation experience. A zero-spend hackathon stack cannot promise unlimited enterprise inference capacity, SLAs or a comprehensive compliance certification.
- Preserve origin and freshness of Qloo observations; never treat one historical artist/brand test as universal cultural coverage.
- Server owns keys and policy; LLM and Qloo responses are untrusted data, never code/commands.
- No automatic booking, payout, contract signing or third-party account writes.
- Final visual design and any tab removal are intentionally not preselected by the backend plan.
- No silent external spend, no unapproved deployment/configuration changes, no NOW Network modification.

## 12. Current execution pointer

Latest reviewed main: 71a579ddeeb61edcc2375bc57b93dc90a16a1e7c.
Next action: implement PR B1 according to this document. Before claiming B2 live, separately verify active provider credentials, explicit user-approved quotas, migrations and deployed SHA. Then and only then begin PR U1 with operator-authored design.

Hackathon deadline and testability: https://qloo.devpost.com/rules — public working app + public open-source repo + English description; demo video optional in the 2026 Agentic event. This is a submission requirement, not evidence of product completeness.
