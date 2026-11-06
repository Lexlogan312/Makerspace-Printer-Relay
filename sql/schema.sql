-- The whole database for the printer relay and the dashboard: tables, access rules, analytics
-- views, the camera photo bucket and live updates.
--
-- Run it in the Supabase SQL Editor (paste the whole file, then Run). It works on a new, empty
-- project, and it's safe to run again on an existing one: it only adds what's missing and brings
-- older setups up to date (see "Upgrading an older database" at the end).
--
-- Who can do what:
--   public (the dashboard's publishable key, no login): read printers and printer_status
--   staff (signed in and listed in public.admins): manage printers and access codes, read the rest
--   relay (the secret key on the Pi): reads and writes everything; the secret key skips these rules

-- ───────────────────────────── Staff (admins) ─────────────────────────────
-- To make someone staff: invite them under Authentication → Users, then run
--   insert into public.admins (user_id) select id from auth.users where email = '<their email>';

create table if not exists public.admins (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  created_at timestamptz not null default now()
);
alter table public.admins enable row level security;  -- no policies: only is_admin() reads it
revoke all on table public.admins from anon, authenticated;

create or replace function public.is_admin()
returns boolean
language sql stable security definer set search_path = ''
as $$ select exists (select 1 from public.admins where user_id = auth.uid()) $$;

-- ───────────────────────────── printers (public) ─────────────────────────────
-- One row per printer. Publicly readable, so nothing secret goes here (access codes are in
-- printer_connections).
create table if not exists public.printers (
  id                  uuid primary key default gen_random_uuid(),
  serial              text not null constraint printers_serial_key unique,
  label               text not null,
  model               text not null default 'Unknown',   -- the relay fills in known models
  firmware_version    text,                                -- filled in by the relay
  -- 'maintenance' shows as Out of service, 'offline_permanent' as Retired (hidden from students)
  maintenance_status  text not null default 'operational' constraint printers_maintenance_status_check
                        check (maintenance_status in ('operational', 'reserved', 'maintenance', 'offline_permanent')),
  -- shown to students with Reserved or Out of service
  status_note         text constraint printers_status_note_length check (char_length(status_note) <= 120),
  filament_color      text,                                -- the loaded filament, kept current by the relay: "#RRGGBB"
  filament_color_name text,                                -- e.g. 'Sky Blue' (Elegoo PLA names)
  filament_type       text,                                -- e.g. 'PLA', 'PETG'
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

-- Keep updated_at current on every edit, whether from the dashboard or the relay.
create or replace function public.set_updated_at()
returns trigger language plpgsql set search_path = ''
as $$ begin new.updated_at = now(); return new; end $$;
drop trigger if exists set_updated_at on public.printers;
create trigger set_updated_at before update on public.printers
  for each row execute function public.set_updated_at();

-- ───────────────────────────── printer_status (public) ─────────────────────────────
-- Live state, one row per printer, written by the relay.
create table if not exists public.printer_status (
  printer_id        uuid primary key references public.printers (id) on delete cascade,
  gcode_state       text,                                  -- the printer's own state: IDLE, PREPARE, RUNNING, PAUSE, FINISH, FAILED…
  mc_percent        smallint,                              -- progress
  mc_remaining_time integer,                               -- minutes left, as of last_seen
  layer_num         integer,
  total_layer_num   integer,
  nozzle_temper     numeric,
  bed_temper        numeric,
  wifi_signal       text,
  is_online         boolean not null default false,
  last_seen         timestamptz not null default now(),
  print_error       integer not null default 0,
  job_name          text,                                  -- file name of the print in progress (public, at the technician's request)
  snapshot_at       timestamptz                            -- when the latest camera photo was taken
);

-- ───────────────────────────── printer_connections (staff only) ─────────────────────────────
create table if not exists public.printer_connections (
  printer_id  uuid primary key references public.printers (id) on delete cascade,
  host        text,                          -- current IP; the relay updates it from network announcements
  access_code text not null,
  last_error  text                           -- written by the relay; null = connected
);

-- ───────────────────────────── discovered_printers (staff only) ─────────────────────────────
-- Printers announcing themselves on the hotspot that aren't in `printers` yet.
create table if not exists public.discovered_printers (
  serial           text primary key,
  ip               text,
  model            text,
  dev_name         text,                     -- the printer's own name, e.g. 3DP-039-916
  firmware_version text,
  last_seen        timestamptz not null default now()
);

-- ───────────────────────────── History (written by the relay, staff only) ─────────────────────────────
-- Each online/offline change, print state change and new error.
create table if not exists public.printer_events (
  id         uuid primary key,               -- made by the relay, so a retry can't add a row twice
  printer_id uuid not null references public.printers (id) on delete cascade,
  ts         timestamptz not null,
  kind       text not null check (kind in ('connection', 'state', 'error')),
  from_value text,
  to_value   text
);
create index if not exists printer_events_printer_ts on public.printer_events (printer_id, ts);

-- One row per print. Kept when its printer is deleted (printer_id becomes null).
create table if not exists public.print_jobs (
  id                  uuid primary key,      -- made by the relay
  printer_id          uuid references public.printers (id) on delete set null,
  printer_label       text not null,         -- the printer's name when the print started
  job_name            text,
  started_at          timestamptz not null,
  ended_at            timestamptz,           -- null while printing
  outcome             text constraint print_jobs_outcome_check
                        check (outcome in ('finished', 'failed', 'canceled', 'unknown')),
  filament_type       text,
  filament_color      text,                  -- "#RRGGBB"
  filament_color_name text,                  -- closest named color, for grouping
  print_error         integer not null default 0
);
create index if not exists print_jobs_printer_started on public.print_jobs (printer_id, started_at);
create index if not exists print_jobs_started on public.print_jobs (started_at);
create index if not exists print_jobs_open on public.print_jobs (printer_id) where ended_at is null;

-- One row per minute the relay is running and can reach Supabase.
create table if not exists public.relay_heartbeats (
  id                  uuid primary key default gen_random_uuid(),
  ts                  timestamptz not null default now(),   -- Supabase's clock, not the Pi's
  printers_online     smallint,
  max_push_latency_ms integer      -- slowest printer report -> Supabase write in that minute
);
create index if not exists relay_heartbeats_ts on public.relay_heartbeats (ts);

-- Deleting a printer leaves its unfinished print with no printer, so close it.
create or replace function public.close_jobs_of_deleted_printer()
returns trigger
language plpgsql security definer set search_path = ''
as $$
begin
  update public.print_jobs set ended_at = now(), outcome = 'unknown'
  where printer_id = old.id and ended_at is null;
  return old;
end $$;
drop trigger if exists close_open_jobs on public.printers;
create trigger close_open_jobs before delete on public.printers
  for each row execute function public.close_jobs_of_deleted_printer();

-- ───────────────────────────── Access rules ─────────────────────────────
alter table public.printers enable row level security;
alter table public.printer_status enable row level security;
drop policy if exists "public read" on public.printers;
drop policy if exists "public read" on public.printer_status;
create policy "public read" on public.printers for select using (true);
create policy "public read" on public.printer_status for select using (true);

drop policy if exists "admins insert" on public.printers;
drop policy if exists "admins update" on public.printers;
drop policy if exists "admins delete" on public.printers;
create policy "admins insert" on public.printers for insert to authenticated with check (public.is_admin());
create policy "admins update" on public.printers for update to authenticated using (public.is_admin()) with check (public.is_admin());
create policy "admins delete" on public.printers for delete to authenticated using (public.is_admin());

alter table public.printer_connections enable row level security;
revoke all on table public.printer_connections from anon;
grant select, insert, update, delete on table public.printer_connections to authenticated;
drop policy if exists "admins manage" on public.printer_connections;
create policy "admins manage" on public.printer_connections for all to authenticated
  using (public.is_admin()) with check (public.is_admin());

alter table public.discovered_printers enable row level security;
revoke all on table public.discovered_printers from anon;
grant select, delete on table public.discovered_printers to authenticated;
drop policy if exists "admins read" on public.discovered_printers;
drop policy if exists "admins delete" on public.discovered_printers;
create policy "admins read" on public.discovered_printers for select to authenticated using (public.is_admin());
create policy "admins delete" on public.discovered_printers for delete to authenticated using (public.is_admin());

do $$
declare t text;
begin
  foreach t in array array['printer_events', 'print_jobs', 'relay_heartbeats'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on table public.%I from anon', t);
    execute format('grant select on table public.%I to authenticated', t);
    execute format('drop policy if exists "admins read" on public.%I', t);
    execute format('create policy "admins read" on public.%I for select to authenticated using (public.is_admin())', t);
  end loop;
end $$;

-- ───────────────────────────── Adding a printer ─────────────────────────────
-- The dashboard's Add printer: adds the printer and its access code together (both or neither).
-- SECURITY INVOKER, so the staff-only rules above still apply; the is_admin() check just gives a
-- clear message.
create or replace function public.add_printer(p_serial text, p_label text, p_access_code text)
returns uuid
language plpgsql security invoker set search_path = ''
as $$
declare
  new_id uuid;
begin
  if not public.is_admin() then
    raise exception 'Only makerspace staff can add printers' using errcode = '42501';
  end if;
  if coalesce(trim(p_serial), '') = '' or coalesce(trim(p_label), '') = '' or coalesce(trim(p_access_code), '') = '' then
    raise exception 'Serial number, name and access code are all required' using errcode = '22023';
  end if;
  insert into public.printers (serial, label) values (upper(trim(p_serial)), trim(p_label)) returning id into new_id;
  insert into public.printer_connections (printer_id, access_code) values (new_id, trim(p_access_code));
  return new_id;
end $$;

revoke all on function public.add_printer(text, text, text) from public, anon;
grant execute on function public.add_printer(text, text, text) to authenticated;

-- ───────────────────────────── Analytics views (staff only) ─────────────────────────────
-- Every stat uses full 24-hour days in the makerspace's time zone. Days with no activity have no
-- row, so treat a missing day as 0. The views use security_invoker, so they follow the staff-only
-- rules of the tables underneath. They hold no data, so they're simply rebuilt each run.

drop view if exists public.print_job_hours, public.printer_utilization_daily, public.fleet_utilization_daily,
  public.busy_hours_daily, public.print_jobs_daily, public.filament_usage_daily,
  public.printer_reliability_daily, public.relay_uptime_daily cascade;

create or replace function public.analytics_time_zone()
returns text language sql immutable as $$ select 'America/New_York' $$;

-- Every print split into the clock hours it covers. A 9:30–11:45 print gives
-- 9:00 → 0.5 h, 10:00 → 1 h, 11:00 → 0.75 h. Running prints count up to now.
create view public.print_job_hours with (security_invoker = true) as
select j.id as job_id,
       j.printer_id,
       coalesce(p.label, j.printer_label) as printer_label,
       (h.slot at time zone public.analytics_time_zone())::date as day,
       extract(isodow from h.slot at time zone public.analytics_time_zone())::int as weekday,  -- 1 = Monday
       extract(hour from h.slot at time zone public.analytics_time_zone())::int as hour,
       extract(epoch from least(coalesce(j.ended_at, now()), h.slot + interval '1 hour')
                        - greatest(j.started_at, h.slot)) / 3600.0 as hours
from public.print_jobs j
left join public.printers p on p.id = j.printer_id
cross join lateral generate_series(date_trunc('hour', j.started_at), coalesce(j.ended_at, now()), interval '1 hour') as h(slot)
where h.slot < coalesce(j.ended_at, now());  -- skips the empty hour when a print ends exactly on the hour

-- Utilization: hours spent printing ÷ 24.
create view public.printer_utilization_daily with (security_invoker = true) as
select day, printer_id, printer_label,
       round(sum(hours)::numeric, 2) as printing_hours,
       round((sum(hours) / 24 * 100)::numeric, 1) as utilization_percent
from public.print_job_hours
group by day, printer_id, printer_label;

-- Utilization of the whole fleet: printing hours ÷ (24 × printers not retired).
-- Uses today's printer count for every day.
create view public.fleet_utilization_daily with (security_invoker = true) as
with fleet as (select count(*) as printers from public.printers where maintenance_status <> 'offline_permanent')
select h.day,
       round(sum(h.hours)::numeric, 2) as printing_hours,
       fleet.printers as printer_count,
       round((sum(h.hours) / nullif(24 * fleet.printers, 0) * 100)::numeric, 1) as utilization_percent
from public.print_job_hours h cross join fleet
group by h.day, fleet.printers;

-- Busy times: busy_printers = average number of printers printing in that hour of that day.
-- Average it over several weeks of the same weekday and hour for the heatmap.
create view public.busy_hours_daily with (security_invoker = true) as
select day, weekday, hour, round(sum(hours)::numeric, 2) as busy_printers
from public.print_job_hours
group by day, weekday, hour;

-- Prints per printer per day (by the day the print started).
create view public.print_jobs_daily with (security_invoker = true) as
select (j.started_at at time zone public.analytics_time_zone())::date as day,
       j.printer_id,
       coalesce(p.label, j.printer_label) as printer_label,
       count(*) as jobs_started,
       count(*) filter (where j.outcome = 'finished') as finished,
       count(*) filter (where j.outcome = 'failed') as failed,
       count(*) filter (where j.outcome = 'unknown') as unknown,
       count(*) filter (where j.ended_at is null) as in_progress,
       round((avg(extract(epoch from j.ended_at - j.started_at) / 60)
              filter (where j.outcome = 'finished'))::numeric, 1) as avg_finished_minutes,
       count(*) filter (where j.outcome = 'canceled') as canceled
from public.print_jobs j
left join public.printers p on p.id = j.printer_id
group by 1, 2, 3;

-- Filament: prints and print hours by type and color (by the day the print started).
create view public.filament_usage_daily with (security_invoker = true) as
select (started_at at time zone public.analytics_time_zone())::date as day,
       coalesce(filament_type, 'Unknown') as filament_type,
       coalesce(filament_color_name, 'unknown') as color_name,
       count(*) as jobs,
       round(sum(extract(epoch from coalesce(ended_at, now()) - started_at) / 3600)::numeric, 2) as printing_hours
from public.print_jobs
group by 1, 2, 3;

-- Reliability: hours offline and new errors per printer per day.
create view public.printer_reliability_daily with (security_invoker = true) as
with connection_changes as (
  select printer_id, ts, to_value,
         lead(ts) over (partition by printer_id order by ts) as next_ts
  from public.printer_events
  where kind = 'connection'
),
offline_periods as (
  select printer_id, ts as started_at, coalesce(next_ts, now()) as ended_at
  from connection_changes
  where to_value = 'offline'
),
offline as (
  select o.printer_id,
         (h.slot at time zone public.analytics_time_zone())::date as day,
         sum(extract(epoch from least(o.ended_at, h.slot + interval '1 hour')
                              - greatest(o.started_at, h.slot)) / 3600.0) as offline_hours
  from offline_periods o
  cross join lateral generate_series(date_trunc('hour', o.started_at), o.ended_at, interval '1 hour') as h(slot)
  where h.slot < o.ended_at
  group by 1, 2
),
errors as (
  select printer_id, (ts at time zone public.analytics_time_zone())::date as day, count(*) as errors
  from public.printer_events
  where kind = 'error' and to_value <> '0300-400C'  -- a canceled print isn't a printer problem
  group by 1, 2
)
select coalesce(o.day, e.day) as day,
       p.id as printer_id,
       p.label as printer_label,
       round(coalesce(o.offline_hours, 0)::numeric, 2) as offline_hours,
       coalesce(e.errors, 0) as errors
from offline o
full join errors e on e.printer_id = o.printer_id and e.day = o.day
join public.printers p on p.id = coalesce(o.printer_id, e.printer_id);

-- Uptime of the relay (running and able to write to Supabase): minutes with a heartbeat ÷
-- minutes in the day. The first day counts from the first heartbeat, and today counts up to
-- now. Every day from the first heartbeat has a row, including days with 0% uptime. For a longer
-- period, add up minutes_up and minutes_total.
create view public.relay_uptime_daily with (security_invoker = true) as
with first_beat as (select min(ts) as ts from public.relay_heartbeats),
days as (
  select d::date as day,
         greatest(d::timestamp at time zone public.analytics_time_zone(), first_beat.ts) as day_start,
         least((d + interval '1 day')::timestamp at time zone public.analytics_time_zone(), now()) as day_end
  from first_beat,
       generate_series((first_beat.ts at time zone public.analytics_time_zone())::date::timestamp,
                       (now() at time zone public.analytics_time_zone())::date::timestamp, interval '1 day') as d
),
beats as (
  select (ts at time zone public.analytics_time_zone())::date as day,
         count(distinct date_trunc('minute', ts)) as minutes_up,
         avg(printers_online) as avg_printers_online,
         max(max_push_latency_ms) as max_latency_ms,
         percentile_cont(0.95) within group (order by max_push_latency_ms) as p95_latency_ms
  from public.relay_heartbeats
  group by 1
)
select days.day,
       coalesce(beats.minutes_up, 0) as minutes_up,
       greatest(ceil(extract(epoch from days.day_end - days.day_start) / 60), 1)::int as minutes_total,
       round(least(100.0, 100.0 * coalesce(beats.minutes_up, 0)
                    / greatest(ceil(extract(epoch from days.day_end - days.day_start) / 60), 1))::numeric, 2) as uptime_percent,
       beats.max_latency_ms,
       round(beats.p95_latency_ms::numeric) as p95_latency_ms,
       round(beats.avg_printers_online, 1) as avg_printers_online  -- were the printers reachable, too?
from days left join beats on beats.day = days.day;

do $$
declare v text;
begin
  foreach v in array array['print_job_hours', 'printer_utilization_daily', 'fleet_utilization_daily',
                           'busy_hours_daily', 'print_jobs_daily', 'filament_usage_daily',
                           'printer_reliability_daily', 'relay_uptime_daily'] loop
    execute format('revoke all on table public.%I from anon', v);
    execute format('grant select on table public.%I to authenticated', v);
  end loop;
end $$;

-- ───────────────────────────── Camera photos ─────────────────────────────
-- The relay uploads printer-snapshots/<printer id>.jpg, replacing the previous photo, and sets
-- printer_status.snapshot_at. Public, so anyone can view a photo by its address, but nobody can
-- list, upload, change or delete files: there are no write rules, so only the relay's secret key
-- can write. JPEGs only, up to 1 MB.
-- To make photos private later: update storage.buckets set public = false where id = 'printer-snapshots';
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('printer-snapshots', 'printer-snapshots', true, 1048576, array['image/jpeg'])
on conflict (id) do update
  set public = true, file_size_limit = excluded.file_size_limit, allowed_mime_types = excluded.allowed_mime_types;

-- ───────────────────────────── Live updates (Realtime) ─────────────────────────────
-- The board gets printers and printer_status changes the moment the relay writes them; the staff
-- pages get the rest. Realtime only delivers a change to someone allowed to read the row, so
-- students never receive the staff-only tables.
do $$
declare t text;
begin
  foreach t in array array['printers', 'printer_status', 'printer_connections', 'discovered_printers',
                           'relay_heartbeats', 'print_jobs'] loop
    if not exists (select 1 from pg_publication_tables
                   where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = t) then
      execute format('alter publication supabase_realtime add table public.%I', t);
    end if;
  end loop;
end $$;

-- ───────────────────────────── Deleting old history ─────────────────────────────
-- The dashboard shows at most 90 days, so every night at about 4 AM, history older than 92 days is
-- deleted (two spare days keep the oldest day of the 90-day charts complete). Only the three
-- tables that grow forever; a print that's still running is never deleted. Printers, their
-- status, access codes and staff accounts are never touched.
-- To run it by hand: select public.delete_old_history();
-- To see when it last ran: select * from cron.job_run_details order by start_time desc limit 5;
create or replace function public.delete_old_history(keep_days integer default 92)
returns void
language sql set search_path = ''
as $$
  delete from public.relay_heartbeats where ts < now() - make_interval(days => keep_days);
  delete from public.printer_events where ts < now() - make_interval(days => keep_days);
  delete from public.print_jobs where ended_at < now() - make_interval(days => keep_days);
$$;
-- Only the scheduler (and you, in the SQL Editor) may run it, never the website.
revoke all on function public.delete_old_history(integer) from public, anon, authenticated;

create extension if not exists pg_cron;
-- Times are UTC: 8:30 UTC is 3:30 or 4:30 AM in Ohio. Scheduling under the same name again
-- replaces the old schedule, so re-running this file doesn't add a second job.
select cron.schedule('delete-old-history', '30 8 * * *', 'select public.delete_old_history()');

-- ───────────────────────────── Upgrading an older database ─────────────────────────────
-- Nothing below changes a new database. On one set up with the older numbered SQL files, it adds
-- what those files added later and removes what they removed. If the relay is running, stop it
-- first (sudo systemctl stop printer-relay), then update it and start it again after this runs:
-- an older relay could write values the new rules turn away.

-- Columns added over time.
alter table public.printers add column if not exists firmware_version text;
alter table public.printers add column if not exists filament_color_name text;
alter table public.printers add column if not exists filament_type text;
alter table public.printers add column if not exists status_note text;
alter table public.printer_status add column if not exists print_error integer not null default 0;
alter table public.printer_status add column if not exists snapshot_at timestamptz;
alter table public.printer_status add column if not exists job_name text;

-- The Reserved status, and the length limit on its note.
alter table public.printers drop constraint if exists printers_maintenance_status_check;
alter table public.printers add constraint printers_maintenance_status_check
  check (maintenance_status in ('operational', 'reserved', 'maintenance', 'offline_permanent'));
alter table public.printers drop constraint if exists printers_status_note_length;
alter table public.printers add constraint printers_status_note_length check (char_length(status_note) <= 120);

-- Removed features and columns nothing uses.
drop function if exists public.watch_printer(uuid);   -- camera "live view" table, replaced by Realtime Presence
drop table if exists public.camera_watch;
alter table public.printers drop column if exists connection_info;          -- moved to printer_connections
alter table public.printer_connections drop column if exists port;          -- Bambu printers always use 8883
alter table public.print_jobs drop column if exists total_layers;
alter table public.relay_heartbeats drop column if exists printers_total;
alter table public.printer_status drop column if exists subtask_name;       -- now job_name

-- Print outcomes: canceled prints are their own outcome, spelled the American way.
alter table public.print_jobs drop constraint if exists print_jobs_outcome_check;
update public.print_jobs set outcome = 'canceled' where outcome = 'cancelled';
update public.print_jobs set outcome = 'canceled'
where outcome = 'failed' and print_error = 50348044;   -- 0x0300400C = error 0300-400C, a canceled print
alter table public.print_jobs add constraint print_jobs_outcome_check
  check (outcome in ('finished', 'failed', 'canceled', 'unknown'));

-- Filament color names, spelled the American way (analytics groups prints by these names).
update public.printers set filament_color_name = 'Gray' where filament_color_name = 'Grey';
update public.printers set filament_color_name = 'Space Gray' where filament_color_name = 'Space Grey';
update public.print_jobs set filament_color_name = 'Gray' where filament_color_name = 'Grey';
update public.print_jobs set filament_color_name = 'Space Gray' where filament_color_name = 'Space Grey';

-- Make new tables, columns and functions usable through the API straight away.
notify pgrst, 'reload schema';
