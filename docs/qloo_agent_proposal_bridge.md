# Recorded Qloo review for a frozen agent proposal

`ofc_engine.qloo_agent_bridge` reads an existing **local** typed `AgentProposal`, freezes it using the same `freeze_proposal` contract as the agent intake, and compares Evidence OFF/ON using one private Qloo capture. It performs **zero network requests**, does not alter the saved SQLite plan, and never authorizes action execution.

The candidate IDs in the targeted `brand` or `partner` decision must exactly match the already queried Qloo brand pool. The operator must provide the artist Qloo ID and original brand ID order (the request fingerprint includes the order).

```bash
python -m ofc_engine.qloo_agent_bridge \
  --proposal "$HOME/.local/share/ofc-engine/agent-proposal.json" \
  --capture "$HOME/.local/share/ofc-engine/private-qloo-probes/6481dd2708544107ad48cff73db3c073.json" \
  --decision-id brand \
  --artist-id "4BBEF799-A0C4-4110-AB01-39216993C312" \
  --brands "C70CE2B8-0AD1-4150-BF18-5A6347F2E860,43A36E73-9BC7-4B09-A116-78E51554C3E3,E26BC58D-18D2-405B-BC7B-7C556ADE3829"
```

The example proposal in `examples/agent_proposal.json` contains **fictional** brand IDs and localities, so it intentionally cannot reuse the above Qloo response as trustworthy evidence. Provide a real agent proposal whose fixed candidate pool contains the three actual Qloo IDs; for this specific replay the decision must not have a locality. Other decisions lacking relevant captured evidence remain `UNCERTAIN`, so a multi-decision plan cannot falsely complete a repair.

This route does not independently establish the origin of the agent, artist identity, provider authentication, business suitability, cultural correctness, a fresh post-repair provider check or action release. `PASS` means top-ranked in this *recorded* fixed pool only. The full raw provider response stays outside the repo and public Render preview. No raw affinity scores are printed.

For a single-decision proof without constructing an agent proposal, use the already-qualified `qloo_run` or `qloo_proof_export` commands.


## Existing SQLite intake verification

After submitting the **same exact proposal** through the local `ofc_engine.proposal_cli`, add `--database` to the recorded review command:

```bash
python -m ofc_engine.proposal_cli --database "$HOME/.local/share/ofc-engine/ofc-agent-smoke.sqlite3" --input "$HOME/.local/share/ofc-engine/agent-proposal.json"

python -m ofc_engine.qloo_agent_bridge \
  --database "$HOME/.local/share/ofc-engine/ofc-agent-smoke.sqlite3" \
  --proposal "$HOME/.local/share/ofc-engine/agent-proposal.json" \
  --capture "$HOME/.local/share/ofc-engine/private-qloo-probes/6481dd2708544107ad48cff73db3c073.json" \
  --decision-id brand \
  --artist-id "4BBEF799-A0C4-4110-AB01-39216993C312" \
  --brands "C70CE2B8-0AD1-4150-BF18-5A6347F2E860,43A36E73-9BC7-4B09-A116-78E51554C3E3,E26BC58D-18D2-405B-BC7B-7C556ADE3829"
```

This option rejects missing, revised, mismatched or inconsistent stored baselines and audits the local event journal before and after the replay. `stored_baseline_matched=true` establishes local plan-state consistency, **not** agent identity or Qloo authenticity. The rank repair is a preview and does not commit a changed candidate to SQLite. Concurrent distributed trust, authenticated provider evaluation, persistent policy decisions and action release remain out of scope.

The CLI does not make network requests. The SQLite store may maintain local WAL/schema metadata when opened; it does not write a repair revision during recorded review.
