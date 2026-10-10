# OFC Engine

A pre-execution cultural decision layer for autonomous agents. **This repository skeleton is a key-independent foundation, not the product or a live Qloo integration.**

## Current implementation

- Strict, versioned contract candidates for plan/decision/evidence/review/repair.
- Candidate pool and dependency DAG validation.
- Pure selective patching with revision/digest preconditions; transitive dependency invalidation.
- In-memory single-process atomic patch + idempotency reference for tests (**not durable**).
- Evidence review-integrity checks: exact plan revision/digest, scoped query and
  response references, freshness, source labels and unresolved verdict detection.
- Pure bounded repair previews that require a REPAIR verdict and bound replacement
  evidence, preserve untouched decisions, and require new dependent rechecks.
- Unit/negative tests and fast CI.

No Qloo credential, API requests, trusted agent orchestration, persisted evidence, external execution, or live causal results are present. `PASS/REPAIR/UNCERTAIN` in model definitions are contract enums, not currently running policy.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
```

Python >= 3.11. Provider-specific semantics require live API observations before adapter and policy calibration.

## Architecture boundary

`src/ofc_engine/contracts.py`: schema ownership; `revisions.py`: pure revision operations; `store.py`: non-durable reference store; `proof.py`: provider-neutral evidence/review integrity checks; `repair.py`: simulation-only bounded repair previews and recheck tracking. Future work replaces the store with a transactional backend and adds provider/evaluation/runtime/HTTP/frontend behind explicit contracts.

## Limitations

A valid synthetic graph does not imply cultural fit. The preliminary schema and Python digest format have not been approved for cross-language parity. Releasing a real action is out of scope. The in-memory reference store does not guarantee durability across process restarts and must never be exposed as a production decision authority. Review integrity is **not** a cultural fitness verdict, provider authenticity attestation or release authorization: a future trusted Qloo adapter must prove evidence origin and a qualified evaluator must produce the verdicts. A changed plan requires a new exact-revision review; safe carry-forward of unchanged decisions is not implemented. Repair attempt ledgers are also **non-durable** and cannot enforce production budgets. The repair preview requires caller-submitted verdicts; it does not choose a candidate or establish Qloo authenticity. No external action is executed.
