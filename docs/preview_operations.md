# Preview operations

Applies only to the stateless, synthetic Render preview. The local agent API
and SQLite run stores are separate and must never be exposed by this service.

## Before deployment

1. Confirm that main passed CI and the history audit has no blocker.
2. Confirm the Render service is Free, uses Docker, and has auto-deploy off.
3. Record the current deploy ID, actual source commit and /readyz identity.
4. Never attach Qloo keys, local agent tokens, databases or private evidence.

## Deploy and qualify

Manually deploy the approved main revision. Compare the actual source commit
in the Render deployment record against the intended commit.

The OFC_BUILD_SHA environment variable does **not** select a Git revision.
Changing that variable alone is not an upgrade or rollback.

Run the bounded hosted HTTP and browser scripts against
https://ofc-fixture-preview.onrender.com with an expected SHA matching the
actual deployment:

- scripts/smoke_hosted.py (with --expected-sha and --wait-seconds 150)
- scripts/browser_smoke.py
- scripts/accessibility_smoke.py

Verify the same commit is reported in Render's deploy record and /readyz,
the four synthetic cases behave correctly, and every action release stays
blocked. Local Chromium PASS does not qualify the public HTTPS deployment.

## Failure and rollback

1. Stop promotion and retain failing deployment logs without copying secrets.
2. Identify the last previously qualified **source commit**, not merely the
   build SHA environment variable.
3. Redeploy that exact prior artifact if Render supports it. Otherwise,
   prepare a normal reviewed Git revert pull request. Never force-push.
4. Repeat actual deploy-commit comparison, /readyz check, all four scenario
   probes, and desktop/mobile browser checks.

Do not change OFC_BUILD_SHA to an older value without rolling back the code.
That would create a misleading version identity.

The public preview has no durable state to migrate. Separate SQLite crash
tests prove transaction rollback before commit, not disk-loss recovery or
cross-host restoration. This procedure is documented; a live Render rollback
rehearsal is **not yet qualified**.

## Offline release rehearsal

Run `python -m scripts.rehearse_release --output release-rehearsal.json` to
replay all four fixture scenarios, verify restart-safe proposal idempotency,
and test SQLite backup/restoration. CI publishes the sanitized JSON report as
`offline-release-rehearsal`. It never contacts Qloo or Render, authorizes
real actions, proves disk-loss recovery, or substitutes for hosted deployment
and rollback verification.

## Release boundary

These checks do not establish WCAG certification, production SLOs, real
Qloo evidence, verified cultural recommendations or action authorization.
Public source release still requires a manual confidentiality and dependency
audit and user approval. GitHub must remain PRIVATE until approved.
