# OFC Engine

A pre-execution cultural decision layer for autonomous agents. **This repository skeleton is a key-independent foundation, not the product or a live Qloo integration.**

## Current implementation

- Strict, versioned contract candidates for plan/decision/evidence/review/repair.
- Candidate pool and dependency DAG validation.
- Pure selective patching with revision/digest preconditions; transitive dependency invalidation.
- In-memory single-process patch store for lightweight tests.
- SQLite-backed atomic plan revisions, cross-connection compare-and-swap,
  restart-safe idempotent replay, pending recheck IDs and inspectable journal.
- Evidence review-integrity checks: exact plan revision/digest, scoped query and
  response references, freshness, source labels and unresolved verdict detection.
- Pure bounded repair previews that require a REPAIR verdict and bound replacement
  evidence, preserve untouched decisions, and require new dependent rechecks.
- Revision-bound before/after repair traces for a proof inspector, with explicitly
  PENDING / BLOCKED / STRUCTURAL_PASS recheck outcomes (never a release permit).
- Deterministic fixture-backed evaluation, bounded orchestration, dependent rechecks
  and same-baseline evidence OFF/ON comparison (not live Qloo causality).
- Unit/negative tests and fast CI.

No Qloo credential, API requests, trusted agent orchestration, persisted provider evidence, external execution, or live causal results are present. `PASS/REPAIR/UNCERTAIN` in model definitions are contract enums, not currently running policy.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
```

To inspect a deterministic, **entirely synthetic** three-city tour walkthrough:

```bash
python examples/synthetic_three_city_trace.py
python examples/synthetic_engine_run.py
```

The JSON includes baseline and revised plan digests, exact candidate changes,
which dependent decisions need rechecking, evidence IDs/reason codes and the
integrity-only post-repair state. `STRUCTURAL_PASS` is **not** a Qloo-authenticated
cultural approval, provider certificate, booking, or external action.

The optional filesystem-backed store can be initialized with:

```python
from ofc_engine.durable import SQLiteRevisionStore
store = SQLiteRevisionStore("./ofc.sqlite3")
```

Store writes require an already validated plan and patch from the calling
application. A successful storage transaction is **not** approval of the
repair or its supporting evidence. Use a persistent local volume for the
SQLite database and WAL; distributed deployments need a separate shared
transactional database and operations qualification.

Python >= 3.11. Provider-specific semantics require live API observations before adapter and policy calibration.

## Architecture boundary

`src/ofc_engine/contracts.py`: schema ownership; `revisions.py`: pure revision operations; `store.py`: non-durable reference store; `durable.py`: SQLite revision persistence, deduplication and hash-linked audit; `proof.py`: provider-neutral evidence/review integrity checks; `repair.py`: simulation-only bounded repair previews and recheck tracking; `trace.py`: revision-bound proof-inspector trace construction; `workflow.py`: synthetic-only evaluator, orchestration and fixed-baseline comparison. Future work adds trusted provider/evaluation and guarded runtime/HTTP/frontend contracts, with a shared database selected and qualified for deployment.

## Limitations

A valid synthetic graph does not imply cultural fit. The preliminary schema and Python digest format have not been approved for cross-language parity. Releasing a real action is out of scope. The in-memory reference store is not restart durable. SQLite state survives a process restart on persistent disk, but is not a cross-host distributed state store and has no authorization layer. Its hash-linked journal detects ordinary inconsistencies, not deliberate tampering by a privileged database writer. Review integrity is **not** a cultural fitness verdict, provider authenticity attestation or release authorization: a future trusted Qloo adapter must prove evidence origin and a qualified evaluator must produce the verdicts. A changed plan requires a new exact-revision review; safe carry-forward of unchanged decisions is not implemented. Repair attempt ledgers are also **non-durable** and cannot enforce production budgets. The SQLite store persists unresolved recheck IDs but deliberately does not clear them or authorize an action; a trusted evidence-review transition remains to be implemented. The repair preview requires caller-submitted verdicts; it does not choose a candidate or establish Qloo authenticity. `workflow.py` chooses candidates only in strictly labeled synthetic fixtures; real-world evaluation requires qualified Qloo evidence and a separately approved policy. No external action is executed.
