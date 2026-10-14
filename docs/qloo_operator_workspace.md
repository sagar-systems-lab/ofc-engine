# Operator proof workspace (private and recorded-only)

Generate a standalone, read-only HTML report for **an existing agent proposal with a recorded Qloo repair stored at SQLite revision 1**. It reuses the actual recorded comparison, journal audit and post-commit diagnostic. It does not call Qloo, modify the database or clear pending rechecks. The public Render preview stays synthetic.

The report shows the same frozen baseline in Evidence OFF/ON, ordinal ranking, one selective candidate change, persisted journal/revision and the still-blocked execution boundary. Raw Qloo scores, artist/brand IDs, provider payloads, secrets, agent objective and constraints are excluded; candidates are displayed as aliases. The accompanying JSON manifest is self-checksummed for local change detection, **not** authenticated by Qloo.

## Run on a disposable stored-repair database

Use your existing private proposal and Qloo capture from the previous operator qualification, with the SQLite database that already contains revision 1.

\`\`\`bash
python -m ofc_engine.qloo_operator_workspace \
  --database "$RUN_DIR/test.sqlite3" \
  --proposal "$PROPOSAL" \
  --capture "$CAPTURE" \
  --decision-id brand \
  --artist-id "4BBEF799-A0C4-4110-AB01-39216993C312" \
  --brands "C70CE2B8-0AD1-4150-BF18-5A6347F2E860,43A36E73-9BC7-4B09-A116-78E51554C3E3,E26BC58D-18D2-405B-BC7B-7C556ADE3829"
\`\`\`

The command returns \`PRIVATE_RECORDED_PROOF_CREATED\`, \`html_path\` and \`manifest_path\`. They are written in a mode-0700 private directory as mode-0600 files under \`~/.local/share/ofc-engine/private-qloo-proof-workspaces/\`. Open the HTML locally in a browser. No local web server or external CDN is needed. If the original capture is older than 24 hours, add \`--historical\` and clearly disclose that this is a point-in-time replay, not a current result.

If the operator has an **actual later privately captured** Qloo response, \`--recheck-capture /path/to/new-capture.json\` can include a second recorded-rank observation. Do not fabricate a newer timestamp or reuse the original capture as proof of an independent request. Neither an original replay nor a later recorded response establishes provider cryptographic authenticity or release permission. The pending recheck remains unresolved.

The output is intended for **private operator demonstration and filming**. Do not upload it to public Render, GitHub or a submission portal without reviewing Qloo's data terms and the exact sanitized report. For public submissions, the existing synthetic UI remains distinctly labeled, and real-provider claims must be grounded in independently inspectable authorized evidence.

## Scope and failure policy

Only a one-decision recorded brand/partner repair matching the stored revision-1 plan is supported. Different agent proposals, extra unreviewed decisions, missing or corrupt journals, inconsistent candidate replacements, bad Qloo fingerprints and unsafe local files fail closed. A later independent provider-verification and policy transition is still required to clear pending rechecks and release real actions.
