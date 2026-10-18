# Control implementation coverage

This is an implementation index for the source audit, not a certificate that every acceptance condition has passed. The feature remains disabled by default. No live provider transport, production migration, deployment or paid resource was enabled.

| Audit item | Implemented and exercised | Remaining boundary |
|---|---|---|
| M01 Cascading repair | Bounded rounds; root and dependent repair; final evaluation; unavailable evidence stays stale/uncertain | Evidence is supplied or replayed; no automatic live refresh |
| M02 Domain decisions | Candidate-specific venue/capacity, rights, schedule and quoted budget eligibility; ambiguity abstention | No inferred travel times or general-purpose utility calibration |
| M03 Edge semantics | All seven edge types produce explicit results; budget and approval edge input checks | Approval edges withhold pending human authority; they never authorize execution |
| M04 Ownership | Immutable owner masks; externally supplied tenant/plan/node/permission grants; foreign mutation negatives | Owner strings identify decisions inside a frozen plan, not ownership of external assets |
| M05 Acquisition | Offline adapter interface, durable reservations, source/account/entitlement binding, quota and scoped replay cache | Live graph collection, external entitlement discovery and automatic refresh/reconciliation remain unimplemented |
| M06 Durable rounds | Per-round digest/policy/evidence CAS, persisted pending obligations, crash replay and no duplicate revisions | Requires isolated PostgreSQL qualification before rollout |
| M07 Domain evidence | Brand rank, city-scoped place relevance, signed venue/cost/rights/time policies, recorded adapter normalization | No independently integrated venue inventory, availability or rights service feed |
| M08 Compatibility | Stable contract negotiation, typed withheld action, exact source preservation, field migration ledger, retained verifier dispatch | Old policies need their pinned verifier implementation and independent trust anchors |
| M09 Human intents | Distinct human identity; plan-scoped delegation; Ed25519 intent binding; expiry/revocation; no action release | Signer grants and keys must be provisioned outside plan payloads |
| M10 Evaluation | Registered 410 synthetic cases, 260/150 split, sealed labels, five comparator slots, confusion/abstention denominators, intervals, index gate | No independent blinded labels or measured model comparator arms have been supplied |
| M11 Progress/traces | Async admission, cursor events, saved-review progress/reconnect/resume, allowlisted spans and percentile classes | Full integrated live search/rank/inference trace attribution remains unimplemented |
| M12 Queue/reliability | Weighted tenant selection, bounded estimates, reservation quotas, scoped replay, local concurrent load and crash tests | Live-provider load/chaos and calibrated cold-host QoS remain unqualified |
| M13 Provenance/export | Account/parser/entitlement/raw commitments, authority revocation, commitments-only optional signed projection | Public projection verifies commitments, not hidden private facts or upstream provider signatures |
| M14 Preview licensing | Per-field policy eligibility and explicit unqualified preview reasons | Preview never commits |
| M15 Cost assurance | Declared arithmetic labeled separately; alternative-specific qualified quotes required for budgeted repairs | No currency conversion or speculative prices |
| M16 Agent identities | Three distinct agent identities, scoped proof delegation/revocation, transport parity and foreign-owner patch tests | Named agent framework integrations beyond the transport clients are not supplied |
| M17 Lifecycle | Expiry/legal hold, tenant-scoped pruning, audit tombstones, idempotency retention, terminal incident finalizer | Cleanup and encryption rotation are explicit operator operations |
| M18 Security/release | Role negatives, signature and revocation tests, offline source/license gate, retained verifier selection, encrypted restore/key rehearsal | PostgreSQL restore and production canary automation remain unqualified; no deployment was attempted |
| M19 Research instruments | Consent-required schema, keyed assignment, source/scorer IDs, assumptions and outcome report | External feedback and causal ROI remain NOT_RUN |
| M20 Typed intake | Authenticated typed synthetic campaign admission preserves scope and traverses durable graph review | Natural-language intake still needs the separately enabled existing model profile and cannot manufacture qualified business evidence |

The new tests and scripts cover local contracts and failure handling. They do not turn absent external measurements into success. Production rollout remains a separate decision after the outstanding code integrations and target-environment checks.
