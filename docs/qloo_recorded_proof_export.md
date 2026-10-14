# Recorded Qloo proof export

Use the private capture to build a compact proof of the **recorded** rank-repair workflow without further Qloo requests. It is not a provider certificate or a release permit.

```bash
python -m ofc_engine.qloo_proof_export \
  --capture "$HOME/.local/share/ofc-engine/private-qloo-probes/6481dd2708544107ad48cff73db3c073.json" \
  --artist-id "4BBEF799-A0C4-4110-AB01-39216993C312" \
  --brands "C70CE2B8-0AD1-4150-BF18-5A6347F2E860,43A36E73-9BC7-4B09-A116-78E51554C3E3,E26BC58D-18D2-405B-BC7B-7C556ADE3829" \
  --selected "C70CE2B8-0AD1-4150-BF18-5A6347F2E860"
```

The command writes a mode-0600 JSON document under `~/.local/share/ofc-engine/private-qloo-proofs/` and prints the exact path and local SHA-256. To verify that the exported document has not changed since its digest was generated:

```bash
python -m ofc_engine.qloo_proof_export --verify /path/printed/by/export.json
```

A self-generated SHA-256 check detects accidental modification; **it does not authenticate Qloo or prevent deliberate modification and checksum replacement**.

If the capture is older than 24 hours, run the same export command with `--historical`. Historical mode replays the original capture as of shortly after its collection time and explicitly labels `historical_replay=true`. It does **not** assert current relevance.

The artifact includes candidate IDs, observed order, fixed-baseline digest, evidence OFF/ON verdicts, selective repair count, revision change, structural recheck states and negative trust markers. Raw Qloo scores, query payload, measurements, credentials, and raw response are excluded. The export itself makes zero provider requests.

Treat exported artifacts as private until you have reviewed provider data-sharing terms and their contents. They are not automatically published or attached to the public, fixture-only Render preview. Do not upload raw private captures or API credentials to public services.
