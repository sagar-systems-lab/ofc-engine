# One-command local recorded proof session

Use an existing operator-generated `proof.json` manifest to open the recorded evidence journey without file-picker steps. The local server reads and validates this one redacted manifest, then serves the existing `/recorded` browser view with auto-load enabled. No raw Qloo response, agent proposal, database, secret, or authorization token is read or exposed by this server.

```bash
cd ~/projects/ofc-engine
source .venv/bin/activate
python -m ofc_engine.local_proof_server \
  --manifest /home/sagar/.local/share/ofc-engine/private-qloo-proof-workspaces/workspace-duly425y/proof.json
```

The command prints `OPEN_LOCAL_PROOF: http://127.0.0.1:<port>/`. Open that URL in a browser. The default port is selected by the operating system to avoid conflicts. The process remains in the foreground; Ctrl+C closes the local session.

This session binds **only** to `127.0.0.1`; its HTTP routes serve static HTML/JS/CSS and `GET /api/local-proof` with the **redacted, already generated** recorded-only manifest. Host and cross-origin request checks reject DNS-rebinding-like requests. POST/PUT/PATCH/DELETE are disabled; there are no proposal, patch, release, Qloo or live-request endpoints. It does not contact Qloo and does not mutate SQLite.

The manifest must have an intact local SHA-256, supported recorded-only schema, one already-persisted revision-1 repair, two journal events, a still-pending recheck and every provider-authentication/release flag set to false. The browser independently repeats these structural and checksum checks before rendering. A checksum is **not** a provider signature; this interface must not be described as a fully verified live Qloo decision or action authorization.

The public `/recorded` viewer continues to use explicit local file import, making no fetches when the user selects a file. Only this locally served page includes a one-time auto-load flag, which permits one same-origin GET to `/api/local-proof`. The public fixture container has no such API endpoint. Do not expose this loopback process through a tunnel, reverse proxy or a public deployment. Only use proof manifests that you intentionally created and reviewed.

This is a usability improvement for recorded proof inspection. Authenticating new live provider evidence, safely completing the pending recheck, and a multi-decision real-Qloo product workflow are still separate work.
