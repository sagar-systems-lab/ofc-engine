# Revision-bound live Qloo recheck (operator-only)

After the recorded repair is stored at revision 1, use `qloo_live_recheck` to preflight its saved journal and optionally conduct **one** new direct Qloo HTTPS request. This path reuses the local operator-only Qloo transport and quota reservation; it never runs from public Render, GitHub CI, or the web browser. A failed request still consumes the local one-attempt reservation because its upstream outcome may be unknown.

The recorded repair's selected candidate, frozen baseline, query pool, and pending recheck are validated **before** network access. If the original capture is older than the current freshness window, it is validated only at its original collection timestamp as a **historical reference**, not treated as fresh evidence. Future-dated original captures are rejected; the new response still must be fresh, distinct, and later than the original. The preflight output explicitly reports `historical_original_reference=true` when applicable. The reservation is scoped to the exact stored revision and query fingerprint, not to an arbitrary user-controlled retry ID. The raw new response is saved in the protected private Qloo capture directory, outside Git. No automatic retry, no alternate endpoint fallback, no release or action execution.

## Safe offline preflight (zero requests)

Use a local SQLite database where `qloo_recorded_commit` stored the revision-1 repair, the exact original agent proposal, and the original capture. For example, on the disposable qualification database:

```bash
python -m ofc_engine.qloo_live_recheck \
  --database "$RUN_DIR/test.sqlite3" \
  --proposal "$PROPOSAL" \
  --original-capture "$CAPTURE" \
  --decision-id brand \
  --artist-id "4BBEF799-A0C4-4110-AB01-39216993C312" \
  --brands "C70CE2B8-0AD1-4150-BF18-5A6347F2E860,43A36E73-9BC7-4B09-A116-78E51554C3E3,E26BC58D-18D2-405B-BC7B-7C556ADE3829"
```

Expected status is `LIVE_RECHECK_READY_NO_NETWORK`, with `real_qloo_requests=0` and the revision-bound reservation fingerprint. This does not use or require the API key.

## Explicit one-attempt live request

Only when the operator has confirmed event-key access, an unused quota allocation and necessity for a fresh independent HTTP observation, run the **same** command with:

```bash
--live --authorize EXACTLY_ONE_QLOO_RECHECK_REQUEST
```

The operator must privately configure `QLOO_API_KEY` in their shell. Never enter the key into chat, GitHub, frontend, or Render. This triggers one new `/v2/insights` request. Success produces `FRESH_QLOO_RANK_PASS_REVIEW_PENDING` when the newly observed recorded rank still favors the stored replacement, or `FRESH_QLOO_RANK_UNRESOLVED` otherwise. Both are **non-release** statuses. The report includes the new private capture path but no scores or raw provider body.

One new API call observed by the client is stronger than replaying the same recorded file, but is not cryptographic provider attestation, an approved cultural-suitability policy, or a qualified independent release decision. Both paths retain the SQLite pending-recheck ID. `fresh_provider_request_observed=true` documents the new transport observation; `provider_authenticated=false`, `independent_recheck_completed=false`, `pending_recheck_cleared=false`, and `release_authorized=false` remain non-negotiable.

### Failure semantics

Preflight, missing token/acknowledgement, missing/corrupt SQLite journal, wrong original Qloo fingerprint or already-cleared pending state block **before** a provider request. After the local attempt reservation, a timeout, network failure, malformed response, private-capture write error or storage conflict blocks the result, with no automatic retry. An explicit manual incident review is required to consider another request; modifying the request ID alone cannot bypass the reservation.

The existing `qloo_recorded_recheck` offline diagnostic and original recorded proof export remain available. This new module does not replace them and does not add execution authorization. The repo stays private until the user approves its public release.
