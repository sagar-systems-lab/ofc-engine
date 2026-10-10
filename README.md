# OFC Engine

A pre-execution cultural decision layer for autonomous agents. **This repository skeleton is a key-independent foundation, not the product or a live Qloo integration.**

## Current implementation

- Strict, versioned contract candidates for plan/decision/evidence/review/repair.
- Candidate pool and dependency DAG validation.
- Pure selective patching with revision/digest preconditions; transitive dependency invalidation.
- In-memory single-process atomic patch + idempotency reference for tests (**not durable**).
- Unit/negative tests and fast CI.

No Qloo credential, API requests, agent orchestration, persisted evidence, external execution, or live causal results are present. `PASS/REPAIR/UNCERTAIN` in model definitions are contract enums, not currently running policy.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
```

Python >= 3.11. Provider-specific semantics require live API observations before adapter and policy calibration.

## Architecture boundary

`src/ofc_engine/contracts.py`: schema ownership; `revisions.py`: pure revision operations; `store.py`: non-durable reference store. Future work replaces the store with a transactional backend and adds provider/evaluation/runtime/HTTP/frontend behind explicit contracts.

## Limitations

A valid synthetic graph does not imply cultural fit. The preliminary schema and Python digest format have not been approved for cross-language parity. Releasing a real action is out of scope. The in-memory reference store does not guarantee durability across process restarts and must never be exposed as a production decision authority.
