# Makerspace Printer Relay

Runs on a Raspberry Pi in the ONU Makerspace. It reads live status from the Bambu Lab printers
over their local MQTT connection (`mqtts://<printer-ip>:8883`, user `bblp`, password = LAN
access code, topic `device/<serial>/report`) and sends it to Supabase for the
[printer dashboard](https://github.com/Lexlogan312/makerspace-3d-printer-web-app). It also
records history (print jobs, state changes, uptime) for the staff analytics, and saves camera
photos.

```
printers ──MQTT──▶ relay (Pi) ──REST──▶ Supabase ◀── dashboard (React on Vercel)
    └── IP announcements (UDP 2021) ──▶ relay
```

- **Printers are managed in the dashboard**, not in a config file. Staff add, rename, reserve,
  retire and fix access codes there, and the relay picks up changes on its own.
- **IPs are found automatically.** Printers announce themselves every ~10 s, and the relay
  connects to (and saves) each printer's current IP.
- **It never controls a print.** Its messages to a printer are `pushall` (asking for a full
  status report, since A1s otherwise only send changed fields) and, if turned on, switching the
  printer's light for clearer photos.

## Setup

1. **Database.** In the Supabase SQL Editor, paste and run [`sql/schema.sql`](sql/schema.sql).
   It creates every table, view, access rule and the photo bucket. It's also safe to run again,
   and running it on an older database brings it up to date (stop the relay first, then update
   and restart it afterwards).
2. **Staff.** Invite each person under *Authentication → Users*. Once they've accepted, make them
   staff in the SQL Editor:
   ```sql
   insert into public.admins (user_id) select id from auth.users where email = '<their email>';
   ```
3. **Config.** Copy the template and fill in the Supabase URL and **secret** key:
   ```bash
   cp relay.example.toml relay.toml
   ```
   `relay.toml` is gitignored. Never commit it or put the secret key anywhere else.
4. **Run it** on a computer on the same network as the printers:
   ```bash
   uv sync
   uv run python -m relay --dry-run         # read-only: prints each printer's status
   uv run python -m relay                   # for real
   uv run python -m relay --dump-raw raw/   # also save raw MQTT messages (handy for tests)
   uv run pytest                            # tests
   ```
5. **Add printers** from the dashboard: once a printer joins the hotspot, it shows up on the
   Printers page as found on the network.

To deploy on the Pi (hotspot, firewall, service), follow
[docs/raspberry-pi-setup.md](docs/raspberry-pi-setup.md). The service and firewall files are in
`deploy/`.

## Camera photos

The relay saves still photos from each printer's built-in camera (on by default; `[camera]` in
`relay.toml` turns it off or changes the timing):

- **printing**, while someone has the board's Photos switch on: every minute
- **everything else** (open printers, and printing ones while nobody has Photos on): every
  10 minutes, plus one right after each print ends so people can see whether the plate is clear
- **while someone has a printer's details open**: every 4 seconds, for up to 6 printers at once
  (`watch_max`, up to 20). The dashboard stops this after 3 minutes unless they resume it.

Who's watching comes from Supabase Realtime Presence (channel `camera-watch`): the dashboard joins
it while a printer is open or while Photos is on, and the relay listens, so nothing polls the
database. Each photo replaces the printer's last one in the public Storage bucket
`printer-snapshots`, and `printer_status.snapshot_at` says when it was taken. Only the relay can
upload, and nobody can list the bucket.

A photo is one ~65 KB frame, so even the fast refresh is light on the hotspot. Test one printer:

```bash
uv run python -m relay.camera --printer "Fred Printstone" --out fred.jpg   # uses relay.toml
uv run python -m relay.camera --host 10.42.0.112 --code 12345678         # or by IP + access code
```

This has to run on the printer network (on the Pi, or a laptop joined to `MakerspacePrinters`).
If it fails, the message says why. A flat tan or gray blur means the protective film is still on
the camera lens.

**Printer lights.** With `light = "auto"` under `[camera]` (off by default), the relay turns a
printer's light on while it prints (and for 30 s after, for the photo of the result) or while
someone watches it, and off again afterwards. It only turns off lights it turned on. Check that
every printer accepts it first: `uv run python -m relay.light --all`.

## Supabase usage

Every request to Supabase is logged, and the Free plan includes 1 GB of logs a month, so the relay
keeps requests down: status is pushed when something a student would see changes (temperature and
layer number alone don't count; they ride along), unchanged printers are refreshed together in a
single request, and camera viewers arrive over Realtime instead of being polled.

## What's in the database

See [docs/database.md](docs/database.md) for every table and analytics view, who writes what,
and how print jobs are tracked.

## Code layout

| File | What it does |
|---|---|
| `relay/__main__.py` | Command line: loads `relay.toml`, starts the relay, stops cleanly on Ctrl+C / SIGTERM |
| `relay/app.py` | The main loop: sync printers, fix connections, push status, record history, heartbeat, lights |
| `relay/config.py` | Reads `relay.toml`. Defines `Printer`, a printer as stored in Supabase |
| `relay/database.py` | Every Supabase read and write (REST API and Storage), plus the `--dry-run` version |
| `relay/discovery.py` | Listens for printers' UDP announcements to learn their IP, model and firmware |
| `relay/printer.py` | One MQTT connection per printer. Merges the partial reports into a full state |
| `relay/status.py` | Raw printer data → the status record (state, progress, time left, filament, …) |
| `relay/history.py` | Status changes → `printer_events` rows and one `print_jobs` row per print |
| `relay/camera.py` | Camera photos: the printer's port-6000 frame protocol, the background worker, and a test command |
| `relay/presence.py` | Who's watching which printer on the dashboard (Supabase Realtime Presence) |
| `relay/light.py` | Test command for printer lights (the relay's own light control is in `app.py`) |
| `tests/` | Tests (`uv run pytest`); shared fakes are in `tests/fixtures.py` |
| `sql/schema.sql` | The whole database, run in the Supabase SQL Editor |
| `deploy/` | The systemd service and the hotspot firewall for the Pi |
