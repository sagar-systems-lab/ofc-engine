# Inspect a committed recorded repair

This operator-only diagnostic reopens the SQLite plan produced by `qloo_recorded_commit`. It checks revision 1, immutable baseline metadata, the two-event hash-linked journal and the still-pending target recheck. It then evaluates the already-stored selected brand against operator-supplied recorded Qloo affinity captures. No API calls or journal mutations occur.

The original capture is **operator-supplied**, not cryptographically linked to the historical commit. A different or newer file is not evidence of an independent provider request. Rank `PASS` in this diagnostic is **recorded rank only** and never clears the pending recheck or authorizes an action.

## Local replay

Use the **disposable test SQLite database** with a committed revision-1 repair, its exact proposal, and its private original capture. Point both capture flags to the original for a useful zero-network replay test:

```bash
python -m ofc_engine.qloo_recorded_recheck \
  --database "$RUN_DIR/test.sqlite3" \
  --proposal "$PROPOSAL" \
  --original-capture "$CAPTURE" \
  --recheck-capture "$CAPTURE" \
  --decision-id brand \
  --artist-id "4BBEF799-A0C4-4110-AB01-39216993C312" \
  --brands "C70CE2B8-0AD1-4150-BF18-5A6347F2E860,43A36E73-9BC7-4B09-A116-78E51554C3E3,E26BC58D-18D2-405B-BC7B-7C556ADE3829"
```

This must report `ORIGINAL_CAPTURE_REPLAY_ONLY`. A different later recorded capture can report `RECORDED_LATER_CAPTURE_RANK_PASS_ONLY` if current-rank checks pass. Neither status establishes a fresh independent provider recheck; in both cases `provider_authenticated=false`, `independent_recheck_completed=false`, `pending_recheck_cleared=false` and `release_authorized=false`.

If the supplied later capture is stale, wrong-query, invalid, or inconsistent with the saved plan, it cannot qualify the recheck. No private payload or affinity scores are printed. Live Qloo qualification, authenticated evidence lineage, persisted review decisions and recheck discharge remain future tasks. Do not create a fake second capture merely to make this diagnostic report a later timestamp.
