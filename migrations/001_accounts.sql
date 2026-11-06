-- Vyral accounts. Safe to run repeatedly.
--
-- Tables live in a private "vyral" schema that Supabase's public Data API doesn't
-- expose, so browsers can't reach them directly and all access goes through the server.
create schema if not exists vyral;
grant usage on schema vyral to authenticated;

create table if not exists vyral.projects (
    id text primary key check (id ~ '^[a-f0-9]{12}$'),
    owner_id uuid not null references auth.users (id) on delete cascade,
    name text not null,
    data jsonb not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists projects_owner_updated on vyral.projects (owner_id, updated_at desc);

create table if not exists vyral.usage (
    owner_id uuid not null references auth.users (id) on delete cascade,
    month text not null check (month ~ '^[0-9]{4}-[0-9]{2}$'),
    videos integer not null default 0 check (videos >= 0),
    ai_requests integer not null default 0 check (ai_requests >= 0),
    primary key (owner_id, month)
);

-- Row-level security is a second lock behind the server's own owner checks. The server
-- runs user requests as the "authenticated" role, so these rules apply to its queries.
alter table vyral.projects enable row level security;
alter table vyral.usage enable row level security;

drop policy if exists projects_select_own on vyral.projects;
drop policy if exists projects_insert_own on vyral.projects;
drop policy if exists projects_update_own on vyral.projects;
drop policy if exists projects_delete_own on vyral.projects;
create policy projects_select_own on vyral.projects for select to authenticated
    using (owner_id = (select auth.uid()));
create policy projects_insert_own on vyral.projects for insert to authenticated
    with check (owner_id = (select auth.uid()));
create policy projects_update_own on vyral.projects for update to authenticated
    using (owner_id = (select auth.uid())) with check (owner_id = (select auth.uid()));
create policy projects_delete_own on vyral.projects for delete to authenticated
    using (owner_id = (select auth.uid()));

-- Users can read their usage, but only the server can change it.
drop policy if exists usage_select_own on vyral.usage;
create policy usage_select_own on vyral.usage for select to authenticated
    using (owner_id = (select auth.uid()));

grant select, insert, update, delete on vyral.projects to authenticated;
grant select on vyral.usage to authenticated;
