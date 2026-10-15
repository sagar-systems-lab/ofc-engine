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


## Hosted OFC agent decision run

The hosted provider mode now evaluates a **frozen six-decision agent proposal** rather than showing a standalone Qloo rank. The server matches the proposal's artist ID, selected brand and ordered 2–5 candidate pool against the Qloo configuration before allowing any provider request.

On the one explicitly operator-authorized Qloo observation, the existing decision evaluator reviews the response, commits at most one allowlisted brand change into the same durable SQLite database, invalidates affected dependents and produces a revision-bound `REVIEW_REQUIRED` result. The approved Studio displays the redacted six-decision before/after result, its same-baseline Qloo OFF/ON comparison and blockers. Five unsupported tour decisions remain unverified; no release or external action is authorized.

No public browser token prompt is used. Browser visitors can **only read** the stored redacted run. The provider spend endpoint remains restricted to a separate server-side operator credential and explicit consent. Qloo credentials, candidate entity IDs and raw response content are never included in the public result.

In addition to the existing gated Qloo configuration, an enabled deployment must set:

- `OFC_HOSTED_AGENT_PROPOSAL_PATH` — an existing absolute path to a private, service-owned mode-0600 six-decision `AgentProposal` JSON file. It must be readable on startup and must match the configured Qloo artist, selected candidate, fixed pool and `external_action=false` constraint.

The file belongs on a private filesystem outside the public repository and Docker build context. The persistent SQLite ledger contains the frozen plan and durable repair journal as well as the already existing provider-attempt reservation. Restarting with another frozen baseline cannot silently reuse the prior scope. A successful provider observation persists only a sanitized rank summary and decision projection, not the raw Qloo payload.

**Limits:** A single live rank supports only the brand-partner decision. The remaining five decisions cannot be granted PASS from it. Provider-origin assurance and a distinct qualified current-revision recheck are still absent; the safe result is `REVIEW_REQUIRED`. A persisted redacted observation is a bounded inspectable product run, not an interactive unlimited-access provider service or a provider signature. No extra Qloo request, public deployment, or provider-derived-content publication is approved by shipping this code.



## Hosted proposal preflight (no provider request)

The existing **Create a plan → Review plan** screen checks the submitted decisions against the same strict server contracts used by agent intake. The server freezes the submitted plan for an integrity digest and checks dependency structure without storing it, requesting Qloo or granting action authority. Any decision without independently scoped evidence remains `UNCERTAIN`; the result is `REVIEW_REQUIRED`.

If the submitted choices match the canonical tour **template**, the separate synthetic proof can be opened. Template compatibility is **not** proof for the newly submitted run. This distinction remains visible even when a browser gives the proposal a new run identifier.

`POST /api/preflight` is bounded to 96 KiB, validates same-origin browser requests, rejects malformed or cyclic dependencies, and never changes the provider attempt ledger. No new browser design was introduced; the existing frozen Studio is extended by an optional, separate script at serving time. The hosted Free instance needs no database for this deliberately read-only function.

The private historical tour, separately authorized live Qloo run, and final full-plan verification each retain their own proof and trust requirements.


## Durable hosted agent runs (separate OFC database)

The hosted plan editor supports an optional **Save this review for 24 hours** action after server preflight. When storage is explicitly enabled, a browser plan is assigned a new server-generated run ID, frozen in PostgreSQL, journaled at revision 0, and made readable only with its one-time bearer ticket. The ticket stays in that browser tab's session storage. A reload can retrieve the same journal-bound, still-uncertain review. Submitted proposals never inherit the synthetic example's evidence, and this endpoint does **not** call Qloo.

This mode is OFF until all required OFC-only database migration and secret checks pass. Set the server environment variables (never commit their values):

- `OFC_DURABLE_RUNS_MODE=ENABLED`
- `OFC_DATABASE_URL` — OFC Engine **dedicated** PostgreSQL connection, TLS required. The connection user needs access to `ofc_private`. Do **not** point this at NOW Network or another project.

Apply `sql/001_private_runtime.sql` **only to the OFC Engine project** before enabling it. The schema is private: `anon` and `authenticated` have no table or schema privileges. There is no public REST/RPC write entry point. The backend has a database-enforced global maximum of 80 browser-created runs per UTC day, with tickets expiring after 24 hours; expiry restricts reading, not guaranteed immediate physical erasure. Dedicated maintenance can purge expired rows later.

For the separately consented one-attempt **live** Qloo path, an OFC-only `OFC_DATABASE_URL` replaces the old local `OFC_HOSTED_QLOO_LEDGER_PATH`. A private six-decision frozen proposal can be supplied using either a mode-0600 server-owned file at `OFC_HOSTED_AGENT_PROPOSAL_PATH` or the bounded server-secret `OFC_HOSTED_AGENT_PROPOSAL_B64` (not both). The provider request remains OFF unless `OFC_HOSTED_QLOO_MODE=ONE_REQUEST_EXPLICITLY_APPROVED` plus the separate server-side Qloo key and operator token are also configured and the operator explicitly initiates the call. A restart or another process using the same OFC database cannot refill the provider request reservation.

None of these controls establishes provider authentication, independent rank recheck, required venue/opener evidence, or full-tour verification. Every browser-created plan starts `REVIEW_REQUIRED` with `UNCERTAIN` decisions. Every isolated single-rank provider run still withholds action until all independent release conditions have genuine evidence.

**Operational release status:** OFC Engine's separate Free Supabase project exists, but production schema application, private connection configuration, deployment activation and live hosted E2E are separate gated steps. The default Render fixture preview stays synthetic-only, and NOW Network is not used.
