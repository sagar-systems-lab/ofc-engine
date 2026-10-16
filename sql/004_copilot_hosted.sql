-- Hosted Copilot session and credential storage. Private OFC Postgres only.
create table if not exists ofc_private.copilot_browser_budget (
    id smallint primary key check(id=1),
    utc_day date not null,
    sessions integer not null check(sessions >= 0)
);
insert into ofc_private.copilot_browser_budget(id,utc_day,sessions)
values (1, (now() at time zone 'UTC')::date, 0)
on conflict(id) do nothing;

create table if not exists ofc_private.copilot_sessions (
    session_id text primary key,
    ticket_digest text not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    prompt_count smallint not null default 0 check(prompt_count between 0 and 4)
);

create table if not exists ofc_private.copilot_messages (
    run_id text primary key,
    session_id text not null references ofc_private.copilot_sessions(session_id),
    request_id text not null,
    prompt_digest text not null,
    model_id text not null,
    status text not null check(status in ('RESERVED','COMPLETE','BLOCKED')),
    result text,
    result_digest text,
    created_at timestamptz not null default now(),
    unique(session_id,request_id)
);

create table if not exists ofc_private.copilot_search (
    run_id text not null references ofc_private.copilot_messages(run_id),
    fingerprint text not null,
    status text not null check(status in ('RESERVED','COMPLETE')),
    result text,
    result_digest text,
    primary key(run_id,fingerprint)
);
create table if not exists ofc_private.copilot_llm_budget (
    id smallint primary key check(id=1),
    utc_day date not null,
    reserved integer not null check(reserved >= 0)
);
insert into ofc_private.copilot_llm_budget(id,utc_day,reserved)
values (1, (now() at time zone 'UTC')::date, 0)
on conflict(id) do nothing;

create table if not exists ofc_private.copilot_credentials (
    provider text primary key check(provider in ('qloo','gemini','groq')),
    ciphertext bytea not null,
    masked_suffix text not null,
    updated_at timestamptz not null default now(),
    checked_at timestamptz,
    check_status text not null default 'NOT_TESTED'
);
create table if not exists ofc_private.copilot_owner_sessions (
    token_digest text primary key,
    expires_at timestamptz not null
);
create table if not exists ofc_private.copilot_connection_attempts (
    id smallint primary key check(id=1),
    utc_day date not null,
    reserved integer not null check(reserved >= 0)
);
insert into ofc_private.copilot_connection_attempts(id,utc_day,reserved)
values (1, (now() at time zone 'UTC')::date, 0)
on conflict(id) do nothing;

alter table ofc_private.copilot_browser_budget enable row level security;
alter table ofc_private.copilot_sessions enable row level security;
alter table ofc_private.copilot_messages enable row level security;
alter table ofc_private.copilot_search enable row level security;
alter table ofc_private.copilot_llm_budget enable row level security;
alter table ofc_private.copilot_credentials enable row level security;
alter table ofc_private.copilot_owner_sessions enable row level security;
alter table ofc_private.copilot_connection_attempts enable row level security;
revoke all on
    ofc_private.copilot_browser_budget,
    ofc_private.copilot_sessions,
    ofc_private.copilot_messages,
    ofc_private.copilot_search,
    ofc_private.copilot_llm_budget,
    ofc_private.copilot_credentials,
    ofc_private.copilot_owner_sessions,
    ofc_private.copilot_connection_attempts
from public, anon, authenticated;
