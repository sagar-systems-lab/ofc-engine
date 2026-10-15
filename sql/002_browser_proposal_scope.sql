-- Retain the exact submitted scope privately so an operator-approved Qloo observation
-- can be bound to the same frozen browser run without trusting browser-restored drafts.
alter table ofc_private.browser_runs
    add column if not exists proposal_snapshot text;
