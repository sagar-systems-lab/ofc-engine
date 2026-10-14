# Recorded Qloo affinity replay

This command evaluates an existing private Qloo capture against a fixed brand pool. It makes **no Qloo API requests** and does not require an API key.

After merging the implementation, run it from the repository's virtual environment:

```bash
python -m ofc_engine.qloo_run \
  --capture "$HOME/.local/share/ofc-engine/private-qloo-probes/6481dd2708544107ad48cff73db3c073.json" \
  --artist-id "4BBEF799-A0C4-4110-AB01-39216993C312" \
  --brands "C70CE2B8-0AD1-4150-BF18-5A6347F2E860,43A36E73-9BC7-4B09-A116-78E51554C3E3,E26BC58D-18D2-405B-BC7B-7C556ADE3829" \
  --selected "C70CE2B8-0AD1-4150-BF18-5A6347F2E860"
```

The report compares two arms from the same frozen baseline: evidence disabled and recorded Qloo affinity evidence enabled. With usable evidence, only fixed-pool ranking is reviewed. An initially non-leading candidate can receive a bounded revision preview and a recorded-evidence re-evaluation; this is **not a fresh provider recheck**. Dependencies without a qualified context-bound query remain unresolved rather than being treated as PASS.

The output includes verdicts, selected IDs, revision progression, affected decisions, reason codes and repair traces. It never outputs raw affinity values, credentials or the full provider response. `real_qloo_requests` remains `0`.

Recorded response integrity is not provider authentication. `RECORDED_RANK_REVIEW_ONLY` is not a cultural-fit certification, action approval, payment instruction or deploy qualification. Results older than the default 24-hour evidence window are blocked; `--age-hours` accepts 1–72 for offline historical review, without making stale data live or authoritative.

Private captures must stay outside the repository. Do not commit, upload or forward the raw capture or `QLOO_API_KEY`.
