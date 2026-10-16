-- Private dynamic Qloo attempts. Apply only to the OFC database.
create table if not exists ofc_private.dynamic_qloo_budget (
    id smallint primary key check (id = 1),
    utc_day date not null,
    reserved integer not null check (reserved >= 0)
);
insert into ofc_private.dynamic_qloo_budget(id,utc_day,reserved)
values (1, (now() at time zone 'UTC')::date, 0)
on conflict (id) do nothing;

create table if not exists ofc_private.dynamic_qloo_attempts (
    plan_id text not null references ofc_private.plans(plan_id),
    decision_id text not null,
    pass_no smallint not null check (pass_no in (1,2)),
    baseline_digest text not null,
    query_fingerprint text not null,
    state text not null check (state in ('RESERVED','COMPLETE')),
    result text,
    result_digest text,
    primary key (plan_id,decision_id,pass_no)
);
alter table ofc_private.dynamic_qloo_budget enable row level security;
alter table ofc_private.dynamic_qloo_attempts enable row level security;
revoke all on ofc_private.dynamic_qloo_budget, ofc_private.dynamic_qloo_attempts
    from public, anon, authenticated;
