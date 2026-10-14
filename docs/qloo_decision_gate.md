# Integrated recorded Qloo decision gate

This operator-only workflow reuses the frozen agent proposal, existing rank evaluator, verified SQLite journal, and bounded repair preview. It can inspect more than one **independent** fixed-pool Qloo brand-affinity decision in one saved plan, commit at most one evidence-supported candidate-only repair with explicit acknowledgement, and compare a separate later recorded capture against the stored revision.

A rank-only PASS is not a general cultural-suitability decision. Recorded captures, even ones originating from prior real HTTPS requests, are operator-supplied and cannot establish provider authentication, independent recheck completion, or action release. Locality-scoped and dependent decisions are not qualified by this API shape and are blocked or returned UNCERTAIN. The workflow does not make any Qloo API requests or execute external actions.

## Private evidence index

Create a JSON file in your protected local operator directory. Each entry references the existing private Qloo capture from a previous real request, not a committed artifact. Only absolute paths are accepted:

```json
{
  "decisions": {
    "brand": {
      "artist_id": "ARTIST_QLOO_ID",
      "brand_ids": ["BRAND_1_QLOO_ID", "BRAND_2_QLOO_ID", "BRAND_3_QLOO_ID"],
      "capture_file": "/home/operator/.local/share/ofc-engine/private-qloo-probes/PRIVATE_CAPTURE.json"
    }
  }
}
```

For multi-decision plans add another independent brand decision with its own distinct query and capture. An omitted or incomplete decision is UNCERTAIN, never automatically PASS. The store must already contain exactly the submitted immutable baseline from `ofc_engine.proposal_cli` or `agent_api`.

## Inspect without network or storage changes

```bash
python -m ofc_engine.qloo_decision_gate \
  --database "$DB" --proposal "$PROPOSAL" \
  --evidence-index "$PRIVATE_EVIDENCE_INDEX"
```

With recent, scope-matched responses, the command shows one verdict per decision and can report `RECORDED_REPAIR_PROPOSED`. This is a dry-run: it does not change SQLite and it requires no API key.

Use `--historical` to inspect old captures as a point-in-time replay. Historical mode **cannot commit** a repair. Do not edit the timestamp to defeat the freshness window.

## Explicit durable commit

Only with current (non-historical) qualified recorded evidence, a single REPAIR decision, all other decisions PASS and no unsupported dependent invalidation, add:

```bash
--commit --request-id YOUR_UNIQUE_OPERATOR_REQUEST_ID \
  --acknowledge COMMIT_RECORDED_REPAIR_WITH_RECHECK_NO_RELEASE
```

SQLite CAS persists revision 1 and retains pending rechecks; concurrent or stale snapshots, reused conflicting request IDs, missing evidence and ambiguous rankings fail closed. A committed recorded-rank repair is **not** permission to release.

## Read-only post-commit comparison

With a real, distinct **later** private Qloo capture, create a second private index containing just the changed decision and run:

```bash
python -m ofc_engine.qloo_decision_gate \
  --database "$DB" --proposal "$PROPOSAL" \
  --evidence-index "$PRIVATE_EVIDENCE_INDEX" \
  --recheck-index "$PRIVATE_LATER_EVIDENCE_INDEX"
```

The gate checks the original frozen scope, exact persisted revision and journal, later capture timestamp and bound rank outcome. A result of `RECORDED_RANK_RECHECK_PASS_NO_RELEASE` means only that a later supplied recorded rank favors the stored choice. It **does not discharge SQLite pending rechecks** because neither the raw capture file nor its timestamp proves provider independence. A different file or later timestamp must never be manufactured as a substitute for a second actual request.

For a new real Qloo request use the existing `qloo_live_recheck` operator flow only after explicit per-request approval and checking the shared API quota. It is currently limited to one-decision plans. Provider-side independent provenance, trusted cultural-fit policy, multi-decision fresh live rechecks, and final verified-plan handoff require additional qualification before any release-ready state can be claimed. This milestone changes no UI, Render service, Qloo tokens or public proof artifact.
