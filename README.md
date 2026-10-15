# OFC Engine

A pre-execution cultural decision layer for autonomous agents. The hosted Studio supports durable six-decision tour reviews, a narrowly scoped operator-approved Qloo brand-ranking integration, and an explicitly approved, read-only Qloo evidence showcase. Without approved evidence, the public site remains a synthetic or evidence-unavailable preview. **A brand ranking is not full-tour cultural approval or execution authorization.**

**Live Studio:** https://ofc-fixture-preview.onrender.com — **[Hosted Qloo tour setup and limitations](docs/HOSTED_QLOO_TOUR.md)**. The public Qloo showcase is opt-in and must not be claimed live until a genuine, revision-bound observation is qualified.

## Product at a glance

An agent proposes a choice. OFC checks its scoped Qloo affinity evidence, makes at most a justified candidate-only repair, records the change, and keeps execution locked until verification is complete. It can report **PASS / REPAIR / UNCERTAIN** for a narrow evidence-backed ranking; it does not equate a rank with cultural suitability or release authority.

- [Product and Qloo integration](docs/PROJECT.md)
- [Try the interface and reproduce the tests](docs/TRY_IT.md)
- [Private operator evidence review](docs/agent_evidence_review.md)

The default public preview uses **synthetic** fixtures and evidence-unavailable reviews. The optional public Qloo showcase uses only previously acquired, operator-approved **redacted** decision evidence: it makes no Qloo network requests and never authorizes a plan. Raw Qloo responses remain private.

## Current implementation

- Strict, versioned contract candidates for plan/decision/evidence/review/repair.
- Candidate pool and dependency DAG validation.
- Pure selective patching with revision/digest preconditions; transitive dependency invalidation.
- In-memory single-process patch store for lightweight tests.
- SQLite-backed atomic plan revisions, cross-connection compare-and-swap,
  restart-safe idempotent replay, pending recheck IDs and inspectable journal.
- Evidence review-integrity checks: exact plan revision/digest, scoped query and
  response references, freshness, source labels and unresolved verdict detection.
- Pure bounded repair previews that require a REPAIR verdict and bound replacement
  evidence, preserve untouched decisions, and require new dependent rechecks.
- Revision-bound before/after repair traces for a proof inspector, with explicitly
  PENDING / BLOCKED / STRUCTURAL_PASS recheck outcomes (never a release permit).
- Deterministic fixture-backed evaluation, bounded orchestration, dependent rechecks
  and same-baseline evidence OFF/ON comparison (not live Qloo causality).
- Unit/negative tests and fast CI.

No Qloo credentials or raw provider captures are committed. A local operator can explicitly request bounded Qloo data, inspect private captures, and run recorded rank-only decisions. Provider authentication, trusted real-world suitability policy, real causal proof, and action execution remain unqualified. `PASS/REPAIR/UNCERTAIN` in recorded previews are narrow affinity-rank verdicts, not general cultural certification.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
```

To inspect a deterministic, **entirely synthetic** three-city tour walkthrough:

```bash
python examples/synthetic_three_city_trace.py
python examples/synthetic_engine_run.py
```

The JSON includes baseline and revised plan digests, exact candidate changes,
which dependent decisions need rechecking, evidence IDs/reason codes and the
integrity-only post-repair state. `STRUCTURAL_PASS` is **not** a Qloo-authenticated
cultural approval, provider certificate, booking, or external action.

The optional filesystem-backed store can be initialized with:

```python
from ofc_engine.durable import SQLiteRevisionStore
store = SQLiteRevisionStore("./ofc.sqlite3")
```

Store writes require an already validated plan and patch from the calling
application. A successful storage transaction is **not** approval of the
repair or its supporting evidence. Use a persistent local volume for the
SQLite database and WAL; distributed deployments need a separate shared
transactional database and operations qualification.

Python >= 3.11. Provider-specific semantics require live API observations before adapter and policy calibration.

## Architecture boundary

`src/ofc_engine/contracts.py`: schema ownership; `revisions.py`: pure revision operations; `store.py`: non-durable reference store; `durable.py`: SQLite revision persistence, deduplication and hash-linked audit; `proof.py`: provider-neutral evidence/review integrity checks; `repair.py`: simulation-only bounded repair previews and recheck tracking; `trace.py`: revision-bound proof-inspector trace construction; `workflow.py`: synthetic-only evaluator, orchestration and fixed-baseline comparison. Future work adds trusted provider/evaluation and guarded runtime/HTTP/frontend contracts, with a shared database selected and qualified for deployment.

## Limitations

A valid synthetic graph does not imply cultural fit. The preliminary schema and Python digest format have not been approved for cross-language parity. Releasing a real action is out of scope. The in-memory reference store is not restart durable. SQLite state survives a process restart on persistent disk, but is not a cross-host distributed state store and has no authorization layer. Its hash-linked journal detects ordinary inconsistencies, not deliberate tampering by a privileged database writer. Review integrity is **not** a cultural fitness verdict, provider authenticity attestation or release authorization: a future trusted Qloo adapter must prove evidence origin and a qualified evaluator must produce the verdicts. A changed plan requires a new exact-revision review; safe carry-forward of unchanged decisions is not implemented. Repair attempt ledgers are also **non-durable** and cannot enforce production budgets. The SQLite store persists unresolved recheck IDs but deliberately does not clear them or authorize an action; a trusted evidence-review transition remains to be implemented. The repair preview requires caller-submitted verdicts; it does not choose a candidate or establish Qloo authenticity. `workflow.py` chooses candidates only in strictly labeled synthetic fixtures; real-world evaluation requires qualified Qloo evidence and a separately approved policy. No external action is executed.


## Qloo capability probes

The diagnostic client is deliberately separate from the decision engine. It
does not convert Qloo responses into policy verdicts or certify cultural fit.
Without `--live`, these commands make **zero network requests**:

```bash
python -m ofc_engine.qloo_probe --search "Example artist" --type artist
python -m ofc_engine.qloo_probe --artist-id ARTIST_QLOO_ID --brands BRAND_ID_1,BRAND_ID_2
python -m ofc_engine.qloo_probe --artist-id ARTIST_QLOO_ID --brands BRAND_ID_1,BRAND_ID_2 --locality "Chicago" --locality-mode signal
```

With an event-provided credential configured **only on the server** as the
`QLOO_API_KEY` environment variable, add `--live` to perform one bounded GET
request. Optional `--capture` writes raw response material under
`~/.local/share/ofc-engine/private-qloo-probes/` with restricted filesystem
permissions. Never commit local captures or disclose the key. Respect the
provider's event credential and data-use terms.

The client pins `https://hackathon.api.qloo.com`, refuses redirects, limits
response size/timeouts, and never retries automatically. Search and fixed
shortlist requests reflect Qloo's published API shape; actual entity identity,
explainability, response structure, locality parameter support, and ranking
semantics remain unqualified until tested with the real API.

The two locality variants are distinct experiments: result restriction
(`filter`) versus locality-influenced ranking (`signal`). Do not assume either
variant is supported for brand-type queries without live validation.


## Local proof console

Run a read-only, synthetic web demonstration:

```bash
python -m ofc_engine.webdemo --port 8765
```

Open `http://127.0.0.1:8765/` for the browser-local plan authoring
workspace. **New plan** creates a blank typed proposal. **Load canonical
example** fetches the six-decision frozen tour fixture. After client-side
contract validation, **Run preflight** shows `USER_REVIEW` with no evidence
for arbitrary plans. Only the unchanged canonical fixture unlocks the
synthetic proof lab at `/proof`. Saved drafts/search remain on the device.
The hosted service never accepts plan submissions or private agent runs;
exports target the separately authenticated local intake, not live Qloo.

The fixture proof viewer uses the existing
synthetic evaluation and repair workflow to display a three-city tour baseline,
decision verdicts, selective changes, dependent rechecks, evidence inspection
and a same-baseline evidence OFF/ON contrast. The scenario selector includes
a provider outage, missing dependent evidence and repair-budget exhaustion.

This local viewer binds to loopback only. It provides no authentication,
Qloo-backed decision authority, external action execution or deployable
production API. Results remain explicitly marked synthetic and release-blocked.


### Saved synthetic runs

For a local demonstration where the browser can reload a previously completed
synthetic run without recalculating it, start the SQLite-backed viewer:

```bash
python -m ofc_engine.runtime_web --database ./ofc-runs.sqlite3 --port 8765
```

The viewer stores completed immutable snapshots and their ordered event
timelines in a local SQLite file. A successful request returns a unique run
URL; reloading that URL retrieves the saved snapshot and verifies the event
cursor. The API supports idempotent run creation and
`GET /api/runs/{run_id}/events?after_sequence=N` for safe replay. A missing or
corrupt saved run is shown as unavailable rather than silently recomputed.

The original `ofc_engine.webdemo` command remains available without
persistence. Both modes use synthetic evidence only; they provide no
authenticated multi-user hosting, Qloo verification, real action release,
or production run state. The SQLite file requires a persistent local disk,
is intentionally excluded from Git, and must be protected by the operator.


## Public synthetic preview container

The fixture-only proof console can be deployed as a read-only demonstration
without Qloo credentials or a writable database:

```bash
docker build -t ofc-fixture-preview .
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
  -p 127.0.0.1:8000:8000 ofc-fixture-preview
```

Open `http://127.0.0.1:8000`. For a managed HTTP deployment, publish port
`8000` through a platform-provided **HTTPS reverse proxy** and set `PORT` to
the assigned container port when necessary. The process binds to all container
interfaces only in this dedicated, stateless preview mode. Verify the host's
network controls, request limits and public availability before sharing a URL.
Set `OFC_BUILD_SHA` to a 7–40 character lowercase hexadecimal commit SHA to
expose a non-sensitive version marker in `GET /healthz` and `GET /readyz`.

This endpoint serves only packaged web assets and four precomputed **synthetic**
`GET /api/demo?scenario=...` responses. Writes, saved-run URLs and any action
release routes are unavailable. The server bounds request targets, concurrent
connections and idle read time. For local operator run recovery, continue using
`ofc_engine.runtime_web` on loopback with a protected SQLite file.

A container build is not equivalent to a deployed, monitored service. The
public preview contains no Qloo verification, authenticated live data, user
sessions, real-world actions or qualified cross-host persistence. Do not mount
secrets or attach production data to this container.


## Deploying the fixture preview

The repository includes a Render Blueprint (`render.yaml`) for one Docker
web service on the **Free** compute plan. It uses `/readyz` for health checks
and disables automatic deployments. Connect Render to this private GitHub
repository, review the Blueprint and its free-plan selection, and explicitly
deploy the service. The repository stays private; only the synthetic,
read-only web preview is intended to be publicly accessible. No databases,
Qloo keys or environment secrets are required.

After receiving the actual HTTPS `*.onrender.com` service URL, run
the **hosted qualification** GitHub Actions workflow manually. Provide its
`base_url` input. The two independent jobs verify HTTP security, all four
fixture scenarios, blocked private routes, and a Chromium journey at desktop,
375px and 320px widths. An optional `expected_sha` can be supplied only
when `OFC_BUILD_SHA` is configured on the service to that exact revision.

The same read-only HTTP check can be run without a browser:

```bash
python scripts/smoke_hosted.py --base-url https://YOUR-SERVICE.onrender.com --wait-seconds 150
```

A successful local Docker test or a checked-in Blueprint **does not establish**
that the hosted site exists or works. Confirm both manual workflow jobs and
inspect the real hosted UI before any public demonstration. Free instances can
cold-start or have capacity limits; do not use this preview to carry private
evidence or authorize real agent actions.


### Decision workspace

The preview uses a single proof-first workspace rather than a free-form
chat view. Verdicts are inspectable per decision, with source references,
selective changes, dependent recheck state and revision events. Status and
short motion transitions are driven by received snapshots; reduced-motion
users receive the same information without animation. Scenario outcomes
remain synthetic and external execution is always disabled.


## Agent proposal intake

Freeze a typed agent proposal into a revision-zero plan and SQLite audit record
without requesting Qloo evidence or allowing external actions:

```bash
python -m ofc_engine.proposal_cli --database ./ofc.sqlite3 --input examples/agent_proposal.json
```

The JSON input contains an `intent` (run ID, request ID, objective, immutable
constraints and audience context) and a `decisions` array (selected candidate,
fixed candidate pool, allowed mutation fields, and dependency IDs). The
intake validates the typed graph and fingerprints the complete initial
proposal, constraints and candidate pools. Identical retries return the same
baseline; reusing a run ID with different content or after a revision is
rejected. The durable record is local SQLite, not cross-host persistence.

The response state is always `BASELINE_FROZEN / EVIDENCE_PENDING` with
`release_authorized=false`. This command does **not** evaluate real cultural
fit, authenticate provider evidence or execute an agent action. Use a
separate run ID for a changed proposal; do not treat an old review or
fixture result as release permission.


## End-to-end offline proposal check

An agent proposal can be exercised through the existing synthetic evaluator
without altering its saved revision-zero plan:

```bash
python examples/agent_proposal_fixture_check.py \
  --database ./ofc.sqlite3 \
  --proposal examples/agent_proposal.json \
  --scenario repair
```

The check stores or replays the typed baseline, then compares fixture evidence
OFF and ON using that exact frozen plan. It reports candidate changes,
dependent rechecks, structural outcomes and the unchanged stored revision.
Use `outage`, `dependent_missing`, or `budget_exhausted` to inspect failure paths.

No Qloo requests or real external actions occur. A fixture repair is a
simulation, not a committed plan revision, verified live decision or release
authorization. The hosted read-only preview is unchanged.


## Local preview measurement

For an observed baseline of the **synthetic**, stateless HTTP preview,
start the local server and run:

```bash
python -m ofc_engine.hosted_preview --port 18080
python scripts/bench_preview.py --base-url http://127.0.0.1:18080 \
  --samples 64 --workers 4 --warmup 8
```

The benchmark uses bounded loopback-only requests, validates all four
synthetic outcomes, detects changing response bodies and records observed
p50/p95/p99 and throughput. Results depend on the host, concurrency,
runtime and sample window. They are **not** Qloo latency measurements,
production SLOs or public Render capacity qualifications.


## Local agent API

To submit agent-proposed plans over HTTP on the local machine, start the
authenticated, loopback-only API. Set a high-entropy operator token in
`OFC_AGENT_TOKEN` before starting the server:

```bash
python -m ofc_engine.agent_api --database ./ofc-agent.sqlite3 --port 8770
```

Requests to `POST /api/proposals` must include
`Authorization: Bearer <operator token>` and a strict JSON body matching
`examples/agent_proposal.json`. A successful submission freezes and journals the
initial plan, returning revision-zero digests and `EVIDENCE_PENDING`. Reusing
the same proposal is idempotent. A conflicting proposal returns HTTP 409.

`GET /api/proposals/{run_id}` reports a minimal, integrity-checked
revision and pending-recheck status without returning the original audience
or constraints. Other operations, including action execution and release,
are unavailable. Host/origin validation, a required bearer token, a strict
request-size cap, and loopback binding protect this **single-operator local
interface**. It is not suitable for public hosting or untrusted multi-tenant
use; do not expose it through Render or copy operator tokens into Git.


## Recorded Qloo qualification

Private captured affinity results can drive a fixed-pool rank-only review and bounded recorded replay. The operator CLI defaults to cache reuse and blocks unapproved fresh requests. For a private redacted proof export with a local digest check, see [recorded proof export](docs/qloo_recorded_proof_export.md). If the response is old, use explicit historical replay; never relabel it as fresh. The public site remains synthetic-only.
