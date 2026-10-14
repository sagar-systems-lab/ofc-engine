# Persist a recorded rank repair (operator-only)

The existing `qloo_agent_bridge` compares recorded Qloo ranks without mutating the frozen plan. This optional local CLI commits **one** candidate replacement to the SQLite revision journal, only after explicit operator acknowledgement.

It is a recorded-rank state mutation, **not** provider-authenticated cultural approval, a fresh evidence recheck, or permission for an agent to execute an action. Public Render and GitHub CI must not receive private captures or secrets. No Qloo request is sent.

## Preconditions

- An actual agent proposal was accepted into the existing local SQLite database at revision 0.
- The exact proposal, immutable candidate pool, constraints and captured query fingerprint still match.
- The capture is within the configured evidence window (default 24 hours), with no missing or uncertain decisions.
- Recorded OFF/ON comparison yields precisely one selective repair, with a structurally consistent trace and no dependent decisions requiring independent review.
- The operator explicitly authorizes storing the **recorded** change. No operator approval is inferred from a recorded `PASS`.

## One-time local command

```bash
python -m ofc_engine.qloo_recorded_commit \
  --database "$HOME/.local/share/ofc-engine/ofc-agent-smoke.sqlite3" \
  --proposal "$HOME/.local/share/ofc-engine/agent-proposal.json" \
  --capture "$HOME/.local/share/ofc-engine/private-qloo-probes/6481dd2708544107ad48cff73db3c073.json" \
  --decision-id brand \
  --artist-id "4BBEF799-A0C4-4110-AB01-39216993C312" \
  --brands "C70CE2B8-0AD1-4150-BF18-5A6347F2E860,43A36E73-9BC7-4B09-A116-78E51554C3E3,E26BC58D-18D2-405B-BC7B-7C556ADE3829" \
  --request-id recorded-rank-commit-001 \
  --acknowledge COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE
```

The proposed original candidate is replaced using the existing SQLite compare-and-swap and idempotency journal. `pending_recheck_ids` contains `brand` after persistence. Retries with the same request ID and identical proposal/capture return `idempotent_replay=true`, without another journal mutation. Changed requests or already-revised plans are rejected.

The CLI reports `RECORDED_REPAIR_STORED_RECHECK_PENDING`, `source_mode=RECORDED_REDACTED`, `provider_authenticated=false`, `fresh_provider_recheck=false`, `overall_plan_verified=false`, and `release_authorized=false`. It does **not** clear pending rechecks. A real independent provider-bound verification and separate authorized release policy remain unfinished. Do not treat this operation as the final plan approval.

Operator note: do not run the command if you want to preserve the stored revision-zero baseline for subsequent experiments. Use a separate new run ID/database instead of resetting production state.
