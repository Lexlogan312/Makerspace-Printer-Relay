# Troubleshooting

Find what you're seeing, then work down its checks in order. Commands run on the Raspberry Pi,
over SSH (`ssh makerspace@<the Pi's IP>`) or Raspberry Pi Connect's **Remote shell**. How the
parts fit together is in [setup.md](setup.md).

**First, two quick checks that narrow down most problems:**
- Signed in to the website, open **Printers**. "Relay running" means the Pi and Supabase are fine,
  so the problem is a printer or the website. A red "The relay hasn't reported" banner means the
  problem is the Pi (see [The relay](#the-relay)).
- On the Pi, `journalctl -u printer-relay -n 50 --no-pager` shows the relay's recent log. Lines
  with `WARNING` or `ERROR` usually say what's wrong.

## The board

| What you see | Check |
|---|---|
| **"The printers haven't reported since …, so this may be out of date"** | The relay stopped writing. See [The relay](#the-relay) |
| **"Not connected yet"** instead of the board | The website was built without its Supabase settings. In Vercel, check `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY`, then redeploy |
| **The site doesn't load at all** | Check the latest deployment on Vercel (a failed build keeps the previous version live, so this is rare), and whether Supabase is up: [status.supabase.com](https://status.supabase.com). If the Supabase project shows as **paused**, restore it from its dashboard |
| **One printer shows Offline** | See [A printer](#a-printer) |
| **Every printer shows Offline** | The relay or the hotspot is down. See [The relay](#the-relay), then [The hotspot](#the-hotspot) |
| **"Needs attention: …"** | The printer stopped with an error, and the reason is shown (e.g. "Filament ran out"). Fix it at the printer. Staff see Bambu's full explanation in the printer's details |
| **"Printer error XXXX-XXXX"** | A code the system doesn't have a label for yet. Look it up, and add a label as described in [database.md](database.md#printer-error-codes) |
| **A finished print still shows as printing** | The printer hasn't reported since. It corrects itself within about a minute; if not, see [A printer](#a-printer) |
| **No camera photos** | Photos appear only for printers in use, and every few minutes unless someone has **Photos** on. If none ever appear: `journalctl -u printer-relay \| grep -i camera` on the Pi. A flat tan or gray image means the protective film is still on the camera lens |
| **No "Request to reserve" button** | `VITE_RESERVATION_EMAIL` isn't set in Vercel (or wasn't when it last deployed), or you're signed in as staff, who don't see it |

## A printer

The **Printers** page shows each printer's connection problem in red. It says one of these:

| Message | Fix |
|---|---|
| **Not seen on the network** | The printer is off, or not on `MakerspacePrinters`. Turn it on; check **Settings → WLAN** on its touchscreen |
| **Access code rejected** | **Printers → Edit** and re-enter the code shown in the printer's LAN settings. If it's right and still rejected, turn on **Developer Mode** on the printer |
| **Can't reach the printer at …** | Usually right after the printer boots; wait a minute. If it lasts, restart the printer |
| **No IP address yet** | The relay hasn't heard the printer announce itself. Check it's on the hotspot |

Other printer problems:

| What you see | Check |
|---|---|
| **A new printer doesn't appear on Printers** | It must be on `MakerspacePrinters`, with LAN Only Mode on. It appears within about 10 seconds of joining |
| **A new printer can't join the hotspot** | The hotspot is full. See [The hotspot](#the-hotspot) |
| **Filament color or type looks wrong** | It comes from the printer's own spool settings. Fix the filament settings on the printer |
| **Log says `unknown model code`** | A printer model the relay doesn't know. Add its code to `MODEL_NAMES` in `relay/discovery.py` |

## The relay

The relay is the program on the Pi. Work down this list:

1. **Is the Pi on?** Its green light flickers when it's running. If it's unresponsive, unplug its
   power for 10 seconds and plug it back in. Everything starts by itself; printers take 1–2 minutes
   to rejoin the hotspot.
2. **Can you reach it?** If SSH to its IP fails, use Raspberry Pi Connect. If neither works, the
   Pi has no campus connection.
3. **Is the service running?** `systemctl status printer-relay --no-pager`. If it isn't
   **active (running)**: `sudo systemctl restart printer-relay`, then read the log.
4. **What does the log say?** `journalctl -u printer-relay -n 50 --no-pager`

| Log message | Meaning |
|---|---|
| `Supabase … failed (401)` | Wrong key in `relay.toml`. It must be the **secret** key (`sb_secret_…`); see "Replace the Supabase secret key" in [setup.md](setup.md#routine-jobs) |
| `Supabase … failed` with a connection error | No internet. Try `ping -c 3 supabase.com` and `nmcli device` |
| TLS or certificate errors, or wrong times | The Pi's clock is off. `timedatectl` should say **synchronized: yes** |
| `has no access code in printer_connections, skipping` | The printer was added without an access code. Add it with **Printers → Edit** |
| `… need the latest database setup: run sql/schema.sql` | The database is older than the relay. Run `sql/schema.sql` in the Supabase SQL Editor |
| `Config file not found` / `Supabase url/key not set` | `~/printer-relay/relay.toml` is missing or incomplete. Recreate it from `relay.example.toml` |

## The hotspot

| What you see | Check |
|---|---|
| **Printers can't see `MakerspacePrinters`** | On the Pi: `nmcli con show --active` should list `printer-ap`. If not: `sudo nmcli con up printer-ap`. The hotspot must be 2.4 GHz; if it's up but unreliable, try another channel (step 6 of [raspberry-pi-setup.md](raspberry-pi-setup.md)) |
| **Some printers can't join** | The hotspot is full: about 20 devices with the minimal Wi-Fi firmware, 8 without. Check the firmware with `update-alternatives --display cyfmac43455-sdio.bin \| head -3` (it should point to `…-minimal.bin`). |
| **Printers keep dropping off** | Range or interference. Keep the Pi central and unobstructed |
| **Which printers are connected?** | `sudo cat /var/lib/NetworkManager/dnsmasq-wlan0.leases` |

## Staff sign-in

| What you see | Check |
|---|---|
| **"This account isn't a staff account"** | They can sign in but aren't in `admins`. Run the `insert into public.admins …` line from [setup.md](setup.md#4-staff-accounts) |
| **An invitation or reset email never arrives** | Check spam (ONU's filter may catch it). Supabase's built-in email sends only a few per hour and may only deliver to members of the Supabase organization; wait an hour, or invite them to the organization. |
| **"This reset link has expired, or you're signed out"** | Links work once and expire. Send a new one with **Forgot password** on the sign-in page |
| **The email link opens the board, not the password page** | Supabase's **Redirect URLs** is missing `https://onu-3dprint.vercel.app/staff/reset-password` (see [setup.md](setup.md#1-supabase)) |
| **Someone forgot their password** | **Forgot password** on the sign-in page. Or, in Supabase → **Authentication → Users**, send them a recovery email |

## Staff pages

| What you see | Check |
|---|---|
| **"Couldn't load analytics"** (or printers, history) | Usually the database is older than the website: run `sql/schema.sql` in the SQL Editor. Otherwise, check Supabase is up |
| **Analytics look empty** | They build up from prints the relay has seen. A new setup has little to show for a few days |
| **History doesn't go back far** | History older than 92 days is deleted every night, by design |
| **A red X on GitHub (Actions tab)** | A change broke a test. Open the failed run to see which. Don't pull that version onto the Pi until it's fixed |
