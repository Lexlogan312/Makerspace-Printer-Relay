  # Database

All tables live in Supabase's `public` schema. [`sql/schema.sql`](../sql/schema.sql) creates everything below,
and running it again brings an older database up to date.

**Who can access what:**
- **Public dashboard**: can read `printers` and `printer_status`.
- **Admins**: can manage printers, and read everything else.
- **Relay**: can read and write everything. The secret key bypasses row-level security.

## Tables

| Table | Written by | Read by | What it holds |
|---|---|---|---|
| `printers` | admins, plus relay for `filament_color`, `filament_color_name`, `filament_type`, `model`, `firmware_version` | everyone | One row per printer: serial, label, model, `maintenance_status` (`operational`, `reserved`, `maintenance` = out of service, `offline_permanent` = retired), `status_note` (shown to students with Reserved or Out of service), and the loaded filament (hex color, Elegoo color name, type) |
| `printer_connections` | admins (`access_code`), relay (`host`, `last_error`) | admins | How to connect. Separate from `printers` because access codes must not be public |
| `printer_status` | relay | everyone | Live state, one row per printer (upserted). `job_name` is the file name of the print in progress, shown on the public board at the technician's request; the history in `print_jobs` stays staff-only. `snapshot_at` is when the latest camera photo was taken |
| `discovered_printers` | relay | admins | Printers announcing themselves on the hotspot that aren't in `printers` yet |
| `print_jobs` | relay | admins | One row per print: start/end, outcome, filament. Admin-only, so past prints can't be traced to students |
| `printer_events` | relay | admins | Each online/offline change, `gcode_state` change, and new error |
| `relay_heartbeats` | relay | admins | One row per minute the relay is running and can reach Supabase |
| `admins` | you, in the SQL editor | nobody (`is_admin()` checks it) | Which signed-in users are admins |
| `printer_error_codes` | `sql/schema.sql` | everyone | What each printer error code means: a group (`printer`, `filament`, `loading`, `file`, `power`, `canceled`), a short label for the board, and Bambu's full text. Unlisted codes count as `printer` |
| Storage bucket `printer-snapshots` | relay | everyone (public bucket, no listing) | `<printer id>.jpg`: the latest camera photo of each printing printer, replaced each minute while someone has the board's Photos switch on, otherwise every 10 minutes. The dashboard adds `?v=<snapshot_at>` so a new photo never shows a cached old one |

### Old history is deleted after 92 days

The dashboard shows at most 90 days, so a nightly job deletes `relay_heartbeats`, `printer_events` and finished `print_jobs` rows older
than 92 days. Nothing else is deleted. To keep more, change `keep_days` in
`delete_old_history()` (and the dashboard's longest range, in `AnalyticsPage.tsx`, to match).

### Adding a printer

1. On the printer's touchscreen, join it to the hotspot and note its
   **access code** (LAN settings).
2. Within ~10 s it appears on the dashboard's Printers page under printers found on the network.
3. Add it there (Printers > Add printer), with a name and the access code. That calls the
   `add_printer(serial, label, access_code)` database function, which adds both rows or neither
   and only works for admins.
4. Within ~30 s the relay connects: `printer_connections.host` fills in and `last_error` stays empty.

The relay fills in `host`, `model` and `firmware_version`, and removes the printer from
`discovered_printers`.

Without the dashboard, the same in the Supabase SQL Editor (one line per printer):

```sql
with new_printers(serial, label, access_code) as (values
  ('03900D5C0000001', 'PrinterNameOne', '12345678'),
  ('03900D5C0000002', 'PrinterNameTwo', '87654321')
),
added as (
  insert into printers (serial, label)
  select serial, label from new_printers
  returning id, serial
)
insert into printer_connections (printer_id, access_code)
select added.id, new_printers.access_code
from added join new_printers using (serial);
```

### Removing a printer

Retire it from the dashboard (Edit > Retire), which sets `maintenance_status = 'offline_permanent'`.
The relay disconnects and closes any open print job, and the history is kept. Deleting the row instead removes its status and events.
Its print jobs are kept so fleet totals stay correct.

### `last_error` messages

| Message | Meaning |
|---|---|
| *(empty)* | Connected |
| Not seen on the network… | No announcement from this printer in the last minute: it's off or not on the hotspot |
| Access code rejected… | Wrong `access_code` |
| Can't reach the printer at … | It announced itself but MQTT didn't answer, usually right after it boots |
| No IP address yet… | No announcement yet and no saved `host` |

## How print jobs are tracked

Bambu's LAN reports have no job id or start time, so jobs come from `gcode_state`:
- A job **starts** when the printer enters `PREPARE`, `SLICING`, `RUNNING` or `PAUSE`.
- It **ends** when the printer leaves those states: `FINISH` → `finished`, `FAILED` → `failed`,
  `FAILED` with error `0300-400C` → `canceled` (the print was canceled, not a printer problem),
  and anything else → `unknown`.
- **Times** are when the relay saw the change, accurate to a few seconds.
- **Gaps:**
  - If a printer drops off Wi-Fi briefly mid-print, the job continues, and ends when the printer
    is next seen in `FINISH`/`FAILED`.
  - If a printer stays offline for over an hour mid-print (e.g. power loss), the job is closed
    as `unknown` at the moment it went offline, so it doesn't count as printing forever.
  - If a print ends while the relay is down, the end time is the printer's last report before
    the relay stopped.
- **Restarts:** after a relay restart, an unfinished job continues in the same row.

## Analytics views (admin-only)

Every stat uses full 24-hour days in the `America/New_York` time zone (set in
`analytics_time_zone()`). A day with no activity has no row, so treat a missing day as 0.

| View | Columns | Use |
|---|---|---|
| `printer_utilization_daily` | day, printer_id, printer_label, printing_hours, utilization_percent | Hours printing ÷ 24, per printer |
| `fleet_utilization_daily` | day, printing_hours, printer_count, utilization_percent | Same for all printers (÷ 24 × printers not retired) |
| `busy_hours_daily` | day, weekday (1 = Mon), hour, busy_printers | Average printers printing in that hour. Average over weeks for the heatmap |
| `print_jobs_daily` | day, printer_id, printer_label, jobs_started, finished, failed, unknown, in_progress, avg_finished_minutes, canceled | Job counts by outcome |
| `filament_usage_daily` | day, filament_type, color_name, jobs, printing_hours | Which filament gets used |
| `printer_reliability_daily` | day, printer_id, printer_label, offline_hours, errors, filament_problems | Printers that may need maintenance. `errors` counts only printer problems (see `printer_error_codes`); running out of or jamming filament is counted separately |
| `relay_uptime_daily` | day, minutes_up, minutes_total, uptime_percent, max_latency_ms, p95_latency_ms, avg_printers_online | The >95% uptime and <10 s latency goals |
| `print_job_hours` | job_id, printer_id, printer_label, day, weekday, hour, hours | Building block for the views above |

For a longer period, add up the daily rows, for example
`sum(minutes_up) / sum(minutes_total)` for uptime over a semester, or
`sum(printing_hours) / (24 × days)` for a printer's utilization.

**Latency** is measured from the first printer report after the previous push to Supabase
confirming the write, and it doesn't include the dashboard's own Realtime delay.

## Printer error codes

Printers report errors as codes like `07FF-8011`. `printer_error_codes` gives each one a group
and a short label, and `error_category(code)` returns a code's group (`printer` when it isn't
listed). To add a code that shows up as "Printer error …" on the dashboard, look it up in Bambu's
A1 error list (Bambu Studio's `resources/hms/hms_en_26A.json`, under `device_error`) and add a row:

```sql
insert into public.printer_error_codes (code, category, label, details)
values ('XXXX-XXXX', 'printer', 'Short label', 'Bambu''s text for this code');
```

Add it to `sql/schema.sql` as well, so a fresh setup has it too.
