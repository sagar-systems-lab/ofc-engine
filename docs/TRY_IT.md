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
