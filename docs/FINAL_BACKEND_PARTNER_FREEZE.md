# Backend freeze — partner integration and safety

This document defines the **last intended feature group** after the Qloo taste-intelligence PR. The original hosted UI and all unrelated NOW Network services remain unchanged.

## Partner value receipt
A completed Copilot response contains `value_receipt` (`ofc-value.v1`) with original/repaired choices, scoped reason codes, matching rank counts, the **same shared Qloo evidence digest** for OFF and ON, and missing independent business requirements. `grounded_explanations` is deterministic server text based on this receipt; `original_answer` and `ofc_answer` remain natural model narratives explicitly tagged `MODEL_NARRATIVE_NOT_INDEPENDENTLY_VERIFIED`. The LLM cannot independently certify its own factual claims. Consumers should use the grounded text when an authoritative explanation is required. A saved toggle does not spend provider calls.

## Owner-registered business proof
The owner-only `POST /api/owner/business` route requires the existing authenticated, same-origin owner cookie. Actions:
- `register`: `issuer`, `public_key_b64` (Ed25519 raw public key), `requirements` allowlist.
- `revoke`: `issuer`; removes that source from active trust decisions.
- `proof`: `run_id` and a signed `SignedBusinessProof` object bound to the exact frozen plan ID/revision/digest, decision, requirement, issued and expiration times.
- `assess`: `run_id`; recomputes the report from only currently trusted authorities and stored authenticated proofs, with no Qloo/LLM calls.

Proofs are encrypted at rest. Registration is an **owner trust decision**, not independent confirmation that a real business institution controls the asserted signing key. Missing, untrusted, tampered or expired proofs remain unverified; no booking, contract or external action is authorized. Initial Copilot reviews remain immutable; owner assessments are a separate live projection.

## Budgets and model selection
The request path performs a read-only capacity check before committing the three-call LLM workflow and a conservative Qloo request estimate before searches. Every actual call still has its own transactional reservation. Simultaneous requests may exhaust shared budgets after preflight; no hidden retry follows. When both LLM providers are configured, an explicitly tested ACTIVE provider is preferred and no unapproved automatic provider retry occurs. `/api/capabilities` reports safe read-only capacity and chosen provider.

## Private visitor data lifecycle
Copilot prompts **and saved result projections** are encrypted at rest with the owner-controlled server key; historic unencrypted result rows remain readable for migration but new results are written encrypted. Original plan audit tables still hold plaintext snapshots and are only accessible to the private database role.

A visitor can delete their own session via an authenticated, same-origin `POST /api/copilot/sessions/{id}` with `{"confirm":"DELETE_MY_SESSION"}`. This atomically deletes that session's prompts, saved responses, scoped Qloo captures, revision journal, business proofs and session; it does not reset shared daily spend counters. Other visitor records are outside this action. This deletes current database rows, not provider-side logs or backups—those are governed separately.

## Partner-facing minimal API contract
Public Copilot:
`POST /api/copilot/sessions` -> HttpOnly session cookie;
`POST /api/copilot/sessions/{id}/messages` with unique request ID, prompt and optional parent run/selection ref;
`GET /api/copilot/sessions/{id}` -> saved, authorized conversation;
`POST /api/copilot/sessions/{id}` with exact deletion confirmation -> private purge;
`GET /api/capabilities` -> component and budget health.

Server-to-server: separately guarded `POST /api/agent/evaluate` requires operator bearer and remains default-disabled. Do not put that token in browser JS. This is an evidence-control contract, not a universal business execution SDK.

## Final qualification boundaries
These endpoints and source integrations must pass isolated PostgreSQL, contracts, container and merged-main CI. No real Qloo/Gemini/Groq request is authorized by merging. Controlled Render migration/deployment and actual provider identity/parameter qualification follow with owner permission. Backend feature scope is frozen after those CI and live gates; later changes are corrective only.
