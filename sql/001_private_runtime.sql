-- Private OFC runtime storage. Nothing is exposed through PostgREST.
create schema if not exists ofc_private;
revoke all on schema ofc_private from public, anon, authenticated;

create table if not exists ofc_private.plans (
    plan_id text primary key,
    revision integer not null check (revision >= 0),
    digest text not null,
    snapshot text not null,
    journal_head text not null,
    pending_rechecks text not null
);
create table if not exists ofc_private.events (
    plan_id text not null references ofc_private.plans(plan_id),
    sequence integer not null,
    event_payload text not null,
    previous_hash text not null,
    event_hash text not null,
    primary key (plan_id, sequence)
);
create table if not exists ofc_private.requests (
    plan_id text not null references ofc_private.plans(plan_id),
    request_id text not null,
    patch_digest text not null,
    response_snapshot text not null,
    invalidated text not null,
    primary key (plan_id, request_id)
);
create table if not exists ofc_private.provider_scope (
    id smallint primary key check (id = 1),
    scope text not null
);
create table if not exists ofc_private.provider_attempt (
    id smallint primary key check (id = 1),
    scope text not null,
    state text not null check (state in ('RESERVED', 'COMPLETE')),
    result text
);

alter table ofc_private.plans enable row level security;
alter table ofc_private.events enable row level security;
alter table ofc_private.requests enable row level security;
alter table ofc_private.provider_scope enable row level security;
alter table ofc_private.provider_attempt enable row level security;

create table if not exists ofc_private.browser_budget (
    id smallint primary key check (id = 1),
    day_utc date not null,
    writes integer not null check (writes >= 0)
);
insert into ofc_private.browser_budget(id,day_utc,writes)
values (1, current_date, 0) on conflict (id) do nothing;
create table if not exists ofc_private.browser_runs (
    plan_id text primary key references ofc_private.plans(plan_id),
    ticket_digest text not null,
    expires_at timestamptz not null
);
alter table ofc_private.browser_budget enable row level security;
alter table ofc_private.browser_runs enable row level security;
revoke all on all tables in schema ofc_private from public, anon, authenticated;
