# Makerspace Printer Relay

A Python service that runs on a Raspberry Pi in the ONU Makerspace. It collects live status,
print history and camera photos from the Bambu Lab printers on the local network and writes
them to Supabase, where the [printer dashboard](https://github.com/Lexlogan312/makerspace-3d-printer-web-app)
reads them.

```
Bambu printers ──MQTT (TLS, 8883)──▶ Relay (Raspberry Pi) ──HTTPS──▶ Supabase ◀── Dashboard (Vercel)
               ──UDP announcements──▶
```

The relay is read-only with respect to printing. The
only commands it sends are a request for a full status report and, if enabled, switching a
printer's light on for camera photos.

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- A Supabase project with [`sql/schema.sql`](sql/schema.sql) applied
- Network access to the printers and to Supabase
- Printers in **LAN Only Mode**, with their access codes entered in the dashboard

## Setup

1. **Database.** Run [`sql/schema.sql`](sql/schema.sql) in the Supabase SQL Editor. It creates
   every table, access rule, analytics view, the photo bucket and the nightly cleanup.
2. **Configuration.** Copy the template and fill in the Supabase project URL and **secret** key:
   ```bash
   cp relay.example.toml relay.toml
   ```
   `relay.toml` is gitignored and must never be committed or shared.
3. **Install and test:**
   ```bash
   uv sync
   uv run pytest
   uv run python -m relay --dry-run   # prints each printer's status
   ```
4. **Deploy on the Pi.** Follow [docs/raspberry-pi-setup.md](docs/raspberry-pi-setup.md), which
   covers the printer hotspot, firewall and systemd service (files in `deploy/`).

Printers are managed from the dashboard. A printer that joins the hotspot
appears on the dashboard's Printers page and the relay connects once it's added.

## Configuration

All settings are in `relay.toml`; [`relay.example.toml`](relay.example.toml) documents each one
and its default.

| Section | Key settings |
|---|---|
| `[supabase]` | `url`, `key`. `SUPABASE_URL` / `SUPABASE_KEY` environment variables override them. |
| `[relay]` | `printer_network` (only accept printers on this subnet), push and sync intervals |
| `[camera]` | `enabled`, photo intervals, `watch_max`, and `light` (`"never"` or `"auto"`) |

## Operation

On the Pi the relay runs as the `printer-relay` systemd service.

| Task | Command |
|---|---|
| Follow the logs | `journalctl -u printer-relay -f` |
| Restart | `sudo systemctl restart printer-relay` |
| Update | `cd ~/printer-relay && git pull && uv sync && sudo systemctl restart printer-relay` |
| Test one printer's camera | `uv run python -m relay.camera --printer "<name>" --out photo.jpg` |
| Test printer lights before enabling `light = "auto"` | `uv run python -m relay.light --all` |
| Save raw MQTT messages for debugging | `uv run python -m relay --dump-raw raw/` |

Connection problems are reported per printer on the dashboard and in
`printer_connections.last_error`. See the troubleshooting table in
[docs/raspberry-pi-setup.md](docs/raspberry-pi-setup.md#troubleshooting).

## How it works

- **Discovery:** printers broadcast their IP, model and firmware every ~10 s; the relay connects
  to each printer's current address, so DHCP changes need no action.
- **Status:** each printer's MQTT reports are merged into a full state and pushed to
  `printer_status` when anything visible changes; unchanged printers are refreshed together in a
  single request to keep Supabase usage low.
- **History:** state changes become `printer_events` rows and each print becomes one `print_jobs`
  row, which feed the staff analytics. A heartbeat is recorded every minute for uptime tracking.
- **Camera:** still photos are uploaded to the public `printer-snapshots` bucket, more often while
  someone is viewing that printer.
- **Resilience:** each step of the main loop runs independently, so one failure doesn't stop the others; 
  failed writes are retried.

The database, access rules and analytics views are documented in [docs/database.md](docs/database.md).

## Project structure

```
relay/
  __main__.py     command line entry point
  app.py          main loop: printer sync, connections, status, history, heartbeat, lights
  config.py       relay.toml parsing
  database.py     Supabase REST and Storage access
  discovery.py    UDP printer announcements
  printer.py      MQTT connection per printer
  status.py       raw printer data → status record
  history.py      printer_events and print_jobs
  camera.py       camera photos and the camera test command
  presence.py     who is viewing which printer
  light.py        printer light test command
tests/            pytest suite, including property-based tests (Hypothesis)
sql/schema.sql    complete database setup
deploy/           systemd units and firewall rules for the Pi
docs/             Pi setup guide and database reference
```
