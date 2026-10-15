# Try OFC Engine

## Browser walkthrough

Open the externally hosted OFC demo URL provided with the submission. The public preview has a single bright, white product workspace. Choose **Try the demo**, then:

1. Run the **Repair** example. Compare the frozen baseline with and without affinity evidence; inspect the single changed brand candidate and the still-pending recheck.
2. Switch to **Provider outage**. OFC cannot invent evidence, so the proposed action remains blocked.
3. Switch to **Missing dependent evidence**. An affected downstream choice still requires review.
4. Switch to **Repair budget exhausted**. The agent cannot loop endlessly to force a pass.

These four public scenarios are explicitly **synthetic**. They show the contract, repair logic and interface; they are not live Qloo calls.

## Run locally

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m ofc_engine.hosted_preview --port 18080
```

Open `http://127.0.0.1:18080/`. This is the **same frozen product UI** as the public preview. Stop with Ctrl+C.

The repository includes focused end-to-end tests of the Qloo adapter and the authenticated local operator workflow. Run them without a Qloo credential or an external network call:

```bash
python -m pytest tests/test_qloo_live_recheck.py tests/test_qloo_decision_gate.py tests/test_agent_workspace.py
```

For complete qualification: `python -m pytest`. Live provider calls are never a prerequisite for the test suite.

## Reproducing the Qloo-backed rank repair privately

A participant with an authorized Qloo hackathon key can supply an artist entity, a fixed brand shortlist, and a local agent proposal. The provider adapter obtains an `/v2/insights` rank response only with explicit per-request operator consent. OFC retains a private capture, scopes the ranking to the frozen plan and requires a later distinct observation to review the recorded repair. Existing captures can be reused **without another API call**.

See [recorded decision gate](qloo_decision_gate.md), [live recheck](qloo_live_recheck.md) and [connected local agent review](agent_evidence_review.md). These operations are private by design. Do not paste a Qloo key into a public application or upload the operator's raw captures.

## Trust boundary

A recorded top affinity rank can justify a **rank-only** candidate change. It does not authenticate the provider's response cryptographically, clear pending checks, establish a broader cultural policy, or authorize an external action. The public UI must not be described as a live Qloo-backed agent service until that capability has actually been deployed and qualified.

## Private historical tour acceptance (saved real Qloo response)

The local operator can turn the **existing single-decision brand proposal** plus its saved original and later private Qloo responses into a six-decision tour *replay*. No Qloo API call occurs. The original brand choice and immutable candidate pool are preserved. The opener, Chicago/Austin/Seattle venues and dependent afterparty are explicitly **operator-constructed, unverified hypotheses**—not Qloo recommendations or an independently generated full-tour plan.

```bash
python -m ofc_engine.tour_replay \\
  --original-proposal "$ORIGINAL_PROPOSAL" \\
  --original-capture "$ORIGINAL_CAPTURE" \\
  --later-capture "$LATER_CAPTURE" \\
  --artist-id "$ARTIST_ID" \\
  --brands "$BRAND_IDS" --port 18080
```

Open only `http://127.0.0.1:18080/#provider` in the approved white studio. The run and proposal remain in a mode-0700 private directory outside the repository. Close the server with Ctrl+C. If another process occupies 18080, stop it before starting this command. The projection redacts real Qloo entity IDs and private captures. It never alters the already-qualified original SQLite journal.

**Historical replay is not a fresh live session.** If the two saved captures fall outside the same evidence-age window, the engine may show the original reference as stale and refuse to assert a recorded rank recheck PASS. That does not erase the real observation; it prevents misleading freshness or release claims. No provider-authenticated verification, venue qualification, dependent clearance, or external action is authorized.

### Optional hosted historical excerpt (explicit approval only)

When private replay produces `public-excerpt-draft.json`, the file contains **only the constrained, redacted decision projection** and a corruption-detection SHA-256 checksum. The checksum is **not a Qloo signature or provider attestation**. Do not put this file or the original captures in the repository. Review the draft and the provider's redistribution rules before deciding whether to publish it.

The hosted preview remains synthetic-only by default. Its optional recorded excerpt mode requires both `OFC_PUBLISH_RECORDED_PROOF=APPROVED` and an operator-supplied, base64-encoded validated draft under `OFC_PUBLIC_RECORDED_PACK_B64` at deployment. No provider request is issued by either the hosted preview or this feature. When approved and present, the existing white Studio displays this historical recorded evidence in its Provider section, while fixture scenarios remain labeled synthetic. There is no upload endpoint.

**Publication is not yet authorized or deployed.** A locally generated draft is not consent to expose Qloo-derived data. This historical excerpt does not qualify live Qloo integration, independent verification, dependent decisions, or release authority.


## Hosted Qloo rank inspection — disabled until separately authorized

The public preview can run a **single fixed-scope live Qloo rank inspection** through the server, without sending the API key or raw Qloo entity IDs to browser JavaScript. It does not replace the recorded tour workflow or certify a verified plan.

This feature is **OFF by default**. Merely deploying OFC or configuring `QLOO_API_KEY` does not enable requests. An operator must separately authorize a provider call and explicitly configure:

- `OFC_HOSTED_QLOO_MODE=ONE_REQUEST_EXPLICITLY_APPROVED`
- `OFC_HOSTED_QLOO_ARTIST_ID` — resolved artist entity ID
- `OFC_HOSTED_QLOO_BRAND_IDS` — the exact 2–5 ID shortlist (comma-separated)
- `OFC_HOSTED_QLOO_SELECTED_ID` — initial choice within that shortlist
- `QLOO_API_KEY` — server secret, **never** in browser JavaScript or repository

No Qloo request occurs at startup, on health checks, page loads, refreshes, or synthetic fixtures. Only an explicitly confirmed POST from the same-origin browser can reserve a request. The response contains the initial choice's **rank position and rank-only verdict**. It never applies a financial action, issues a verified plan, or claims independent provider attestation. The fixed pool is configured by the operator and cannot be replaced by browser input.

The process reserves one request attempt before contacting Qloo, without retries or refunds on failure. Subsequent attempts reuse the in-memory observed result, or remain blocked after error. **This is only a per-process limit, not a provider-side spending cap**: process restarts, multiple replicas, different services or external users can cause more calls once enabled. For a real public service, qualify an external shared quota and provider billing limits first. Do not enable the mode while the operator's current no-new-Qloo-calls restriction applies.

The existing public fixture mode and separately approved historical recorded-excerpt mode remain unchanged. Live inspection and the historical excerpt cannot both be enabled in the same server instance. No hosting deployment or publication is performed by this implementation.

**Remaining product work:** a Qloo rank observation still needs provider-origin qualification, downstream dependent evidence, broader artist/tour suitability policy, durable run lifecycle, independently validated rechecks and a safe verification/handoff policy. Neither this one-call inspection nor the historical replay satisfies those full-plan requirements.
