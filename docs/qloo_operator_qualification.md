# Operator Qloo qualification

The existing private Qloo capture and `python -m ofc_engine.qloo_run` remain the preferred zero-network replay path.

`python -m ofc_engine.qloo_operator` provides a separate operator-only boundary for future live qualification. It does not run from the public Render preview, GitHub Actions, or the web UI.

Run preflight without provider traffic:

```bash
python -m ofc_engine.qloo_operator \
  --artist-id "4BBEF799-A0C4-4110-AB01-39216993C312" \
  --brands "C70CE2B8-0AD1-4150-BF18-5A6347F2E860,43A36E73-9BC7-4B09-A116-78E51554C3E3,E26BC58D-18D2-405B-BC7B-7C556ADE3829" \
  --selected "C70CE2B8-0AD1-4150-BF18-5A6347F2E860"
```

If a matching private capture already exists, the CLI re-evaluates it offline. If the capture is stale or fails normalization, it **does not automatically refresh**. A cache miss without explicit live permission is a dry run.

The live path requires both `--live` and the exact acknowledgement `--authorize EXACTLY_ONE_QLOO_REQUEST`. **Only request explicit live permission if a new provider response is truly necessary and quota remains.** Before sending, the CLI reserves one attempt by query fingerprint in a mode-0700 directory. A timeout or unknown outcome leaves the reservation in place; no automatic retry is made. A new attempt with the same fingerprint is blocked unless a separate manual incident review intentionally changes the local attempt state. Private responses are mode-0600, outside Git.

The reservation records the *attempt*, not a provider-side billing or quota receipt; the operator must verify Qloo usage independently. Tests use an injected mock transport and never call Qloo.

The returned comparison is `RECORDED_REDACTED`, including after a successfully captured request. It is a bounded rank observation, not fresh independent recheck evidence, provider cryptographic attestation, comprehensive suitability approval, or an authorization to execute actions. Public preview is still synthetic and read-only.
