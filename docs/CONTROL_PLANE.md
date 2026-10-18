# Plan review

The plan-review service accepts up to 64 typed decisions, freezes their candidate pools, evaluates claim-specific evidence, and computes bounded selective repairs. A repair reopens dependent obligations while preserving unrelated choices. Cultural ranking, business evidence, and action authority remain separate.

The existing Copilot and recorded-review APIs remain available. The control service is disabled by default. It has no payment, booking, or external-action connector.

Natural-language extraction retains the existing six-choice profile by default. `OFC_COPILOT_GRAPH_INTAKE=ENABLED` enables a separate bounded 64-choice profile only when both private Copilot and control runtimes are configured. It preserves declared dependencies, permits singleton operational choices, and rejects causal cycles before entity lookup. The same existing model/Qloo quota reservations apply. This option is not enabled by the sandbox, migration, or feature flag alone; larger extraction can consume more model tokens when an operator explicitly enables live use.

## Run the sandbox

```sh
python -m pip install -e '.[dev]'
OFC_CONTROL_MODE=SANDBOX python -m ofc_engine.hosted_preview --port 8000
```

Open `/plans`. Repair, preservation, disagreement, stale evidence, and a conflicting hard constraint use synthetic observations. The plan editor accepts the same typed contract as the agent API. It does not collect evidence. All sandbox responses identify their source mode and report zero provider calls.

## Contracts and policies

`GET /api/control/schema` returns the JSON contract. A plan includes tenant, actor, idempotency key, objective, decisions, typed edges, independent currency limits, and bounded search limits. Candidate IDs are supplied or resolved upstream; labels are not identity certificates. Unsupported legacy pools are rejected rather than truncated.

Only causal edges participate in cycle detection and invalidation. Candidate compatibility, exclusions, shared resources, temporal order, and budgets receive separate constraint checks. A read set identifies the source fields on which a downstream decision depends. Unknown prices and missing currency limits remain unknown. Currency conversion and travel-time inference are not implemented.

The optimizer compares hard-constraint feasibility, unmet qualified rank obligations, edit count, and dependent recheck count in that order. It searches only frozen pools. Equal optima, exhausted expansion limits, and deadlines produce abstention. General search is bounded, not linear-time.

| Claim | Accepted input | Scope of a successful policy check |
|---|---|---|
| Brand cultural rank | Two distinct compatible scoped observations | Repeatable rank within the frozen pool; one upstream source |
| Place cultural observation | Locality-bound source observation | Observation only; no venue or booking qualification |
| Budget quote | Pinned operator signature, cost, currency, fees, expiry | Quoted input matches the selected candidate |
| Venue capacity | Pinned operator signature, venue, required capacity, exact window | Signed capacity and availability assertion covers the declared request |
| Contract rights | Pinned operator signature and scoped contract reference | Required attestation is present; not legal certification |
| Schedule window | Pinned operator signature and exact UTC interval | Declared interval is attested; not traffic prediction |

Private evidence requires a trusted issuer key, exact tenant/subject/pool/locality, policy version, input fingerprint, origin, and freshness. Derived claims also require qualified provenance parents. Fixture evidence cannot qualify private reviews. An LLM or caller-provided signature does not establish trust.

Caller-declared claims cannot remove the required brand/venue/place policies. A declared budget also requires matching quote evidence for its cost inputs. Omitting a mandatory obligation keeps that decision unresolved.

## Agent API

Private calls require a configured bearer identity. Browser access to a saved Copilot review uses its existing HttpOnly visitor cookie plus the nonsecret `X-OFC-Session` locator. Tenant and actor must match the authenticated identity. Reads, polling, proof export, previews, and replay do not call providers.

| Route | Behavior |
|---|---|
| `POST /api/control/plans` | Durable admission; returns 202 and review/status/event/proof locators |
| `GET /api/control/reviews/{id}` | Scoped state, revision, digests, correlation ID |
| `GET /api/control/reviews/{id}/events?after=N` | Ordered cursor replay; JSON or `Accept: text/event-stream` |
| `GET /api/control/reviews/{id}/proof` | Private normalized evidence and control proof |
| `POST /api/control/reviews/{id}/proposals` | Revision/policy/evidence-bound patch preview; no commit |
| `POST /api/control/reviews/{id}/cancel` | Invalidate the worker lease; preserve the trace |
| `POST /api/control/reviews/{id}/resume` | Explicitly resume a recovered local review |
| `POST /api/control/reviews/{id}/approvals` | Human-scoped approval intent or revocation; never action authorization |

Python clients use `ofc_engine.control.sdk.ControlClient`; JavaScript clients use `/control-sdk.js`. Writes have no automatic retries. Status/event reads can be retried. SSE responses return the currently persisted cursor range; reconnect with the last sequence.

## Private deployment

Use a dedicated staging database first. `ofc_control` is an additive private schema; the existing `ofc_private` data is not migrated or rewritten.

1. Set `OFC_CONTROL_DATABASE_URL` to the intended PostgreSQL service. Remote connections require TLS.
2. Apply `python -m ofc_engine.control.manage migrate` using an authorized database role.
3. Configure `OFC_CONTROL_MASTER_KEY` with a Fernet key. Keep a recoverable, access-controlled copy. Changing this key without re-encrypting existing records makes them unreadable.
4. Configure `OFC_CONTROL_TOKENS` as a JSON mapping from strong tokens to `{tenant_id, actor_id, roles, kind}`. Allowed roles are `read`, `submit`, and `approve`; `approve` requires `kind: human`. Tokens remain server-side and are compared by digest.
5. Configure `OFC_CONTROL_AUTHORITIES` as pinned public-key records containing `issuer`, `tenant_id`, base64 `public_key`, allowed `claims`, `source`, `expires_at`, and `revoked`. A revoked or expired key cannot qualify evidence. Apply key revocation/rotation by changing this configuration and restarting the service.
6. Set `OFC_CONTROL_MODE=PRIVATE`. Do not enable a production rollout solely because the sandbox passes.

The database stores encrypted private request/result payloads. It never stores provider credentials. Review/event writes and revision CAS are transactional. Workers use bounded leases; PostgreSQL claims use `FOR UPDATE SKIP LOCKED`. An expired lease becomes `RECOVERY_REQUIRED`, not an automatic upstream retry. The default runtime performs local evaluation and capture replay. A provider-independent planner can be injected with an explicitly offline transport; no live transport is installed. No new live collector is enabled.

To attach graph reviews to new Copilot results, configure `OFC_CONTROL_CAPTURE_ISSUER` and a base64 raw Ed25519 `OFC_CONTROL_CAPTURE_SIGNER`. The corresponding pinned authority must be a Qloo-adapter issuer for the `copilot` tenant. It attests to validated saved captures, not a Qloo-issued signature. Existing completed observations are checked against their original query/baseline hashes and replayed without another Qloo request. Missing or expired captures remain unqualified.

Queue admission is bounded to 128 pending reviews per tenant and 4096 stored reviews. Capacity exhaustion returns 429. Retention/export policy, encryption-key custody, independent issuer verification, and operational monitoring must be established before sustained production use. Scoped retention cleanup is explicit: `set_retention` sets expiry/legal hold, and `prune(tenant_id)` removes expired terminal records while retaining audit tombstones and idempotency commitments. No paid scaling is configured.

## Verify and evaluate

```sh
python -m ofc_engine.control.verifier saved-proof.json --expected-digest TRUSTED_DIGEST
python -m ofc_engine.control.verifier private-proof.json --authorities trusted-authorities.json
python -m ofc_engine.control.benchmark --output evaluation.json
python scripts/control_browser_smoke.py
```

Private proofs contain normalized restricted evidence. Deliver them only to authorized reviewers; they are not public Qloo datasets. The browser checks hash/journal consistency. The Python verifier also reconstructs policies, patches, impacted nodes, obligations, comparisons, and journal semantics. Its authority keys must be independently pinned, not trusted merely because they appear in an exported record.

Control proofs are unsigned. Hash chains are tamper evidence against protected anchors, not external notarization or provider authentication. `control.release` can build a byte-reproducible verifier bundle and optionally sign its file manifest with an operator-controlled Ed25519 key. That signature establishes artifact integrity, not commercial release authority.

The policy digest binds both the manifest and the source implementing canonicalization, contracts, graph rules, policy, repair, and verification. Keep the corresponding verifier bundle with retained records. A changed implementation must not silently reinterpret a saved proof under its previous policy digest.

The evaluation harness accepts paired recorded comparator arms only when plan and evidence hashes match. Missing arms are reported as not measured. Built-in cases are development fixtures, not a blinded holdout, independent audit, adopter pilot, live latency certification, or measured business ROI.

## Rollback

Unset `OFC_CONTROL_MODE` and restart to remove the graph/API entry points. Existing Copilot behavior remains available. Preserve the control database and encryption key for audit/replay. No destructive rollback migration is required. Do not deploy this feature to the current hosted demo until isolated PostgreSQL, recovery, access, and browser checks pass in the target environment.


## Connected repairs and ownership

`cascading_campaign()` is a 30-decision fixture with independently scoped synthetic partner and creative rights. Its first batch changes the partner; its second batch revisits and changes a dependent creative choice. A terminal evaluation records the fixed point. Independent choices remain unchanged. `max_rounds`, expansion and controller deadlines bound work; missing evidence leaves claims uncertain. Each batch has a durable revision CAS and a replayable per-round proof. Recovery reuses committed batches without incrementing their revision twice.

Venue, schedule, collaborator, media and channel alternatives require their declared business evidence. Budgeted alternatives additionally require candidate-specific signed quote inputs. Declared price arithmetic is labeled separately from evidence-qualified feasibility. Two equally supported feasible replacements abstain. All external actions stay withheld, including after a signed human review intent.

Foreign-owner nodes are read-only unless a server-configured grant binds tenant, owner, delegate, frozen plan digest, exact node IDs, permission and expiry. Grants never come from the submitted plan. `OFC_CONTROL_DELEGATIONS` supplies those grants. `OFC_CONTROL_APPROVAL_KEYS` pins distinct human signer keys; delegated human intents require an Ed25519 signature over the exact plan/revision/policy/evidence/request binding. Revocation and expiry invalidate them. Delegated proof reads require a grant covering the entire plan.

## Evidence, telemetry and retention

`EvidencePlanner` reserves claim scopes durably before invoking an injected offline adapter. Reservations bind account, entitlement, locality, pool and claim fingerprint; enforce RPM/RPD/total/concurrency limits; and reject unknown outcomes instead of blindly retrying. `RecordedEvidenceAdapter` normalizes existing source records. Account, parser version, entitlement and protected raw-payload commitments are explicit. Expired cache payloads can be evicted without deleting quota reservations. There is no configured live collection path, automatic paid retry or inferred provider-origin signature.

`GET /api/control/reviews/{id}/trace` exports authenticated, redacted controller spans. `scripts/control_load.py` profiles a bounded local worker queue. These are local controller measurements; provider inference/search/rank and real cold-host performance remain unmeasured until an authorized adapter supplies them. Saved private reviews poll cursor events without resubmitting and expose explicit recovery.

`GET /api/control/reviews/{id}/public-proof` exports commitments only. It omits normalized facts, labels, actors and locality. `public_projection` optionally signs the projection; `verify_projection` requires an independent signature key or pinned projection digest. It cannot establish the undisclosed private claims. Retained private proofs require their exact pinned verifier policy; `VerifierRegistry` refuses unknown versions. Contract revisions `/1` remain the first stable isolated API revision; they are not silently renamed. `contracts` provides negotiation, an action contract, source-preserving migration ledgers and source round trips.

Run `python scripts/control_security_gate.py` and `python scripts/control_rehearsal.py` for source/license gates and an isolated encrypted SQLite backup/restore/key-rotation rehearsal. Neither changes production. PostgreSQL restore, production key rotation and multi-process qualification remain separate checks.

## Registered evaluation and research

`evaluation` publishes 410 deterministic synthetic case IDs with 260 development and 150 reserved cases, sealed-label commitments, immutable experiment digests, exact confusion/abstention denominators, Wilson and bootstrap intervals, and all five comparator slots. Labels must be sealed with an externally held key; the committed synthetic generator is not an independently blinded study. Missing comparator arms remain `NOT_MEASURED`. An acceptance index requires independently signed holdout certification and passing hard gates; no study has been certified here.

`research` defines consent-required feedback, keyed blind assignment, source/scorer IDs and explicitly stated financial assumptions. Empty studies are `NOT_RUN`; reported avoided loss is not a causal ROI claim. `/api/control/intake` accepts explicitly authenticated synthetic typed campaigns through durable admission and preserves their original objective and pools; it never calls a language model or converts cultural affinity into a business authorization.
