# OFC hosted operations

OFC Engine has one Render web service for the approved Studio, synthetic fixtures and opt-in durable six-decision tour reviews. PostgreSQL persistence and Qloo evidence publication are distinct server-side features. No public visitor can trigger the paid/quotad Qloo request.

## Deploy

1. Confirm the target is `ofc-fixture-preview` in the OFC Render workspace. Compare the latest reviewed GitHub `main` commit with Render's actual source commit. Keep automatic deployment disabled.
2. The existing dedicated OFC PostgreSQL URL stays in Render secrets as `OFC_DATABASE_URL`; `OFC_DURABLE_RUNS_MODE=ENABLED` enables browser run persistence. Use the approved SQL migrations `sql/001_private_runtime.sql` and `sql/002_browser_proposal_scope.sql`. Never change the NOW Network database.
3. When explicitly configured, `OFC_TOUR_ARTIST_ID`, `OFC_TOUR_BRAND_IDS`, and `OFC_TOUR_SELECTED_ID` enable the private-scope six-decision intake button. No Qloo credential is needed to create a run.
4. Deploy the approved main commit, verify `/readyz` and `/api/runs/status`, save a tour and restore its review after refresh and one restart. `/healthz` only proves process liveness, not database durability.

## Provider and public evidence

The operator-only `/api/qloo/live-rank` endpoint remains OFF until an explicitly authorized, bounded provider request. The server reserves the single attempt before I/O and cannot transparently retry a lost response. See [Hosted Qloo Tour](HOSTED_QLOO_TOUR.md) for the exact private variables and safe qualification.

Redacted evidence is **not** published automatically. The operator must review the source, the Qloo data-use permissions and the current plan evidence before setting `OFC_PUBLIC_QLOO_SHOWCASE=APPROVED` with an exactly matching `OFC_PUBLIC_QLOO_RUN_ID`. `GET /api/showcase` only serves a completed revision-bound, actual repaired brand review. It cannot trigger new provider requests, does not expose browser tickets and continues to verify the journal on each read. No incomplete/incorrect proof is substituted with a synthetic outcome.

## Qualification and rollback

- GitHub CI covers frozen assets, Git-history pattern audit, contracts, synthetic fixtures and Postgres integration. It is not a substitute for public hosted Qloo qualification.
- Run the hosted smoke/browser workflows at the pinned deploy SHA, including 320px/375px desktop/mobile checks. Manually inspect the read-only Qloo showcase on a second browser without credentials once publication is approved.
- Confirm approved evidence and the hosted site remain available through the November 2–16, 2026 judging period. If a Qloo scope mismatch or failed provider recheck occurs, remove the public approval flags instead of fabricating a PASS.
- For rollback, redeploy the last reviewed code revision and re-run readiness, durable restore and no-leak checks. Do not simulate rollback by editing `OFC_BUILD_SHA`.

This product does not autonomously book, pay, make contracts or guarantee a fully verified tour. Pending decisions and insufficient provider attestation must remain blocked.
