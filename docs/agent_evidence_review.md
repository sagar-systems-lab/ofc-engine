# Agent API: recorded evidence review

The loopback-only authenticated agent API can now serve a read-only evaluation of a frozen and persisted plan using private Qloo captures. The operator supplies the original proposal and private evidence indexes at server startup. HTTP clients cannot choose local file paths, request Qloo data or commit a patch through this endpoint.

The reviewed brand rank may originate from an earlier real HTTPS request, but the submitted file is not a provider attestation. The endpoint does not certify cultural suitability, clear pending checks or authorize external action. It does not expose raw scores, source files or the Qloo key.

## Start a local evidence-backed agent session

First, create private original/recheck evidence index files following `docs/qloo_decision_gate.md`, referencing already saved captures. The new Qloo response should be placed in the recheck index with the exact same artist and fixed brand shortlist as the original.

Set `OFC_AGENT_TOKEN` to a private random 32–256 character ASCII string, and start:

```bash
python -m ofc_engine.agent_api \
  --database "$DB" --port 8770 \
  --operator-proposal "$PROPOSAL" \
  --evidence-index "$ORIGINAL_INDEX" \
  --recheck-index "$RECHECK_INDEX"
```

The server loads the private files into its own operator-controlled session, validates the existing journal and returns only a limited decision report to authorized local clients:

```http
GET /api/proposals/{run_id}/review
Authorization: Bearer <local operator token>
```

A successful response contains per-decision `PASS / REPAIR / UNCERTAIN`, recorded rank recheck status, plan revision/digest, pending rechecks and explicit `release_authorized=false`. Without operator evidence configured, this route returns 404. Any other run ID also returns 404. A malformed, stale or forged capture fails closed. There is no `POST /review` or provider-triggering route.

A token is required even on loopback. Do not put it in browser JavaScript or on a public Render service. This API is local only, not designed for external multi-user hosting.

This is the agent-facing **recorded evidence review** milestone, not a verified live release gate. A fresh response obtained via `qloo_live_recheck` can be inspected here without another provider request; pending verification and action release remain blocked until separate trusted evidence policy and authority are qualified.


## One-command local product — final frozen UI

Start the already-repaired, private single-decision agent workflow from one terminal:

```bash
python -m ofc_engine.agent_workspace \
  --database "$DB" \
  --proposal "$PROPOSAL" \
  --capture "$ORIGINAL_CAPTURE" \
  --recheck-capture "$FRESH_CAPTURE" \
  --artist-id "$ARTIST_ID" \
  --brands "$BRAND_IDS" \
  --decision-id brand --port 18080
```

Open **only** `http://127.0.0.1:18080/`. The root serves the exact same frozen `studio.html` asset used by the public synthetic preview. Its existing **Recorded proof** navigation opens `/recorded` on that **same origin**; this local-only route loads the redacted private proof already checked against the persisted revision. No extra browser server or replacement UI is launched. All existing styling and static UI assets remain unchanged.

Agent clients on the same origin can call authenticated `POST /api/proposals`, `GET /api/proposals/{run_id}`, or `GET /api/proposals/{run_id}/review` with the private bearer token. Unless `OFC_AGENT_TOKEN` was provided, startup creates a random token in a file under `~/.local/share/ofc-engine/private-agent-sessions/` with mode 0600. Never pass it to page JavaScript or upload it. The redacted `/api/local-proof` is read-only, origin/host restricted and **only available on the private loopback server**, not Render.

Both the recorded proof and review report are derived from saved local original and later captures. There are **zero provider requests**; no new repair commit, no pending-recheck clearance, no authority to release an action. An explicit `--historical` flag allows labeled point-in-time review of old captures; it is not a freshness bypass or a verification certificate.

Stop the server with Ctrl+C. If port 18080 is already in use by a synthetic preview, stop that previous local process first. Do not run the private workspace on a publicly accessible host.

The public `ofc_engine.hosted_preview` and Render deployments continue to serve **only synthetic fixtures**. This milestone unifies the **local presentation** but does not by itself qualify a real-provider public deployment or a trusted action-release policy.
