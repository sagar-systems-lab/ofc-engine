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


## Hosted Qloo rank inspection — operator controlled and OFF by default

The public Studio supports one fixed-pool server-side Qloo rank observation, but only the operator can authorize spending the request. Other visitors can view the **redacted rank-only result** after it has been stored. This is not a full six-decision tour qualification, independent provider attestation, or permission to execute.

The feature stays **OFF by default**, even if a Qloo key is present. Operator configuration requires all of:

- `OFC_HOSTED_QLOO_MODE=ONE_REQUEST_EXPLICITLY_APPROVED`
- `OFC_HOSTED_QLOO_ARTIST_ID`, `OFC_HOSTED_QLOO_BRAND_IDS` (fixed 2–5 IDs), `OFC_HOSTED_QLOO_SELECTED_ID` (one of those brands)
- `QLOO_API_KEY` (server-side provider secret)
- `OFC_HOSTED_OPERATOR_TOKEN` (distinct, random, 32–256-character access secret for the person authorized to spend provider quota)
- `OFC_HOSTED_QLOO_LEDGER_PATH` (absolute SQLite file path in an already-existing, private mode-0700 directory on a genuinely **persistent shared volume**)

Provider key and operator token must never be uploaded, checked into GitHub, copied into public frontend code, or included in a demonstration recording. The operator token can be entered into the local prompt on the existing Studio page for an explicitly confirmed spend. It remains transient browser input and is only transmitted to the same-origin backend. Ordinary visitors never need this token to inspect a stored, redacted observation.

One SQLite row records an irreversible reservation **before** Qloo network traffic begins. Across processes that use the same SQLite database on one compatible filesystem, the reservation is atomic. Success stores only the sanitized rank result. Timeout, crash or unknown network outcomes leave the attempt consumed: process restarts never automatically retry or refund it. The SQLite file and its parent must be owned by the service user; the ledger cannot be relocated to a new path as a mechanism to claim a spending cap.

**Operational limitation:** SQLite locks on one persistent shared volume are not distributed consensus or a Qloo billing cap. Separate deployments, ephemeral container volumes, database resets, concurrent replicas without a common filesystem, or new approval configurations can bypass the single-ledger scope. A hosting plan without durable storage **must not enable live mode**. Qualify the provider's actual event quota/billing limits and shared production state before authorizing any new real request.

A public `GET /api/qloo/result` returns only rank position and explicitly blocked decision status after the operator has produced a result. It cannot spend provider quota. Refresh, health checks, ordinary browsing, synthetic scenarios and unauthenticated `POST /api/qloo/live-rank` make zero provider calls. The default hosted app remains synthetic-only; the recorded historical excerpt remains a separate mutually exclusive mode.

**Current authorization:** No additional real Qloo requests are approved. Leave the live environment variables unset; do not deploy the new mode or supply a real token/key without a separate operator decision.

**Remaining product scope:** supported non-brand Qloo decisions, real multi-decision tour evidence, independent verification, downstream rechecks, policy-qualified handoff, hosted end-to-end qualification and submission are still open. A rank-only observation is not a verified tour.
