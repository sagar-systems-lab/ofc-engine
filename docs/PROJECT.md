# OFC Engine

**Pre-execution decision control for AI agents.**

Agents can assemble plans and propose actions, but selecting a culturally appropriate partner or experience requires evidence that ordinary tool calls do not supply. OFC is a decision layer between an agent's proposal and an irreversible action: inspect the proposed choice, review scoped taste evidence, repair only the affected field when justified, then hold execution until verification is satisfied.

## What works

- **Typed agent proposals:** a frozen objective, constraints, audience context, candidate pools and dependency graph.
- **Qloo affinity evidence:** a real `/v2/insights` adapter for an artist-signal / fixed-brand-pool query, request fingerprinting, normalized ranking and conservative source checks.
- **Selective repair:** rank-backed replacement of one mutable candidate, committed with SQLite compare-and-swap and a hash-linked audit journal.
- **Separate recheck:** a later Qloo response can support the changed rank; it is not silently replayed as the original observation or treated as general cultural certification.
- **Fail-closed controls:** a pending recheck cannot authorize an external action. Wrong candidate pools, incompatible evidence, uncertain outcomes and tampered revision history block progress.
- **Product surfaces:** the single white decision workspace and a synthetic end-to-end lab; an authenticated **local-only** operator flow can review previously saved private Qloo evidence on the same interface.

## Why Qloo matters

Qloo supplies a ranked cultural-affinity signal that the agent's initial candidate selection lacked. In the bounded brand-partner case, evidence can change the chosen candidate. The comparison reuses the same frozen candidate pool and preserves unaffected decisions; without Qloo evidence the result is `UNCERTAIN`, not an invented approval.

Qloo rank is one input to a broader suitability decision. OFC does **not** claim that rank alone proves cultural fit, provider-authenticated attestation, or permission to take an external action.

## Architecture

```text
Agent proposal
    -> frozen plan + constraints
    -> scoped Qloo evidence review (or UNCERTAIN)
    -> selective candidate repair + durable journal
    -> later rank observation + pending trust review
    -> no external action without qualified authority
```

The public hosted preview is currently a **synthetic, clearly labeled product demonstration**. It never receives Qloo credentials or private captured responses, and it is not a substitute for the real Qloo workflow. The Qloo integration, authenticated local operator API and tests are in this repository; see [Try it](TRY_IT.md) for reproducible steps. All public publishing of provider-derived evidence requires separate authorization and rights review.

MIT licensed. Implementation is Python, with a static web interface and a read-only Docker preview.
