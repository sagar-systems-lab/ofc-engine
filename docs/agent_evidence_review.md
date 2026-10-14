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
