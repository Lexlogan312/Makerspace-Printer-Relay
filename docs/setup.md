# Setup guide

How the makerspace printer system fits together, how to set it up from nothing, and how to do
the routine jobs that come up afterwards. For problems, see
[troubleshooting.md](troubleshooting.md). Logins and keys are listed in the separate handoff
accounts document.

## How it fits together

```
Bambu printers ──Wi-Fi hotspot──▶ Raspberry Pi (relay) ──internet──▶ Supabase ◀── Dashboard (Vercel) ◀── students, staff
```

| Part | What it does | Where it's managed |
|---|---|---|
| **Printers** | Report their status over the Pi's private Wi-Fi hotspot, `MakerspacePrinters` | Each printer's touchscreen |
| **Raspberry Pi** | Runs the relay: reads every printer and writes to Supabase | SSH or [Raspberry Pi Connect](https://connect.raspberrypi.com) |
| **Supabase** | The database, staff sign-in, camera photos and live updates | [supabase.com/dashboard](https://supabase.com/dashboard) |
| **Dashboard** | The website students and staff use: https://onu-3dprint.vercel.app | [vercel.com](https://vercel.com) |
| **Code** | This repository (relay) and the dashboard repository | GitHub |

If the Pi stops, the website still loads but shows a warning that the printers haven't
reported. If Supabase is down, the website can't show anything.

## Setting up from nothing

Do these in order. If you're only replacing one part (for example a new Pi), skip to that step.

### 1. Supabase

1. Create a project at supabase.com. Any region in the US East is fine.
2. **SQL Editor:** paste all of [`sql/schema.sql`](../sql/schema.sql) and run it. It creates every
   table, access rule and analytics view, the camera photo bucket, the error-code list and the
   nightly cleanup.
3. **Authentication → URL Configuration:** set **Site URL** to the website's address
   (`https://onu-3dprint.vercel.app`) and add `https://onu-3dprint.vercel.app/staff/reset-password`
   to **Redirect URLs**. Invitation and password-reset emails depend on this.
4. Note two values from **Project Settings**: the **Project URL** (Data API) and, under API Keys,
   the **publishable** key (`sb_publishable_…`, safe to put in the website) and the **secret**
   key (`sb_secret_…`, only ever on the Pi).

### 2. Dashboard (Vercel)

1. Import the dashboard repository into Vercel.
2. Under **Settings → Environment Variables**, add:

   | Variable | Value |
   |---|---|
   | `VITE_SUPABASE_URL` | the Project URL |
   | `VITE_SUPABASE_PUBLISHABLE_KEY` | the **publishable** key, never the secret one |
   | `VITE_SITE_URL` | `https://onu-3dprint.vercel.app` |

3. Deploy. From then on, every push to `main` deploys automatically. Changing a variable only
   takes effect after a redeploy (**Deployments → ⋯ → Redeploy**).
4. **Reservation emails:** create the makerspace Gmail account, add its secrets in Supabase and
   deploy the `request-reservation` function, as described under "Reservation emails" in the
   dashboard repository's README.

### 3. Raspberry Pi and printers

Follow [raspberry-pi-setup.md](raspberry-pi-setup.md). It takes a new Pi to a running relay,
creates the printer hotspot, and joins the printers to it. The relay needs the Project URL and
the **secret** key.

### 4. Staff accounts

For each staff member:

1. Supabase → **Authentication → Users → Invite user**, with their email. The email link takes
   them straight to choosing a password.
2. Once they've accepted, give them staff access in the SQL Editor:
   ```sql
   insert into public.admins (user_id) select id from auth.users where email = '<their email>';
   ```

Signed-in staff see the staff tabs. Someone who can sign in but isn't in `admins` sees "This
account isn't a staff account".

### 5. Check it works

- The website shows every printer, and printing ones count down.
- Signed in, **Printers** shows "Relay running" and each printer's connection.
- After a print finishes, it appears under **History**. **Analytics** fills in over the following days.

## Routine jobs

| Job | How |
|---|---|
| **Add a printer** | Join it to `MakerspacePrinters` and turn on LAN Only Mode on its touchscreen. It appears on **Printers** as found on the network; click **Add it**, enter a name and the access code from the printer's LAN settings |
| **Take a printer out of use** | **Printers** → its status → **Out of service** (with a note students see), or **Reserved** for a class. **In service** puts it back |
| **Remove a printer** | **Printers → Edit → Retire** keeps its history. **Delete** erases it |
| **A printer's access code changed** | **Printers → Edit**, enter the new code |
| **Add a staff member** | Step 4 above |
| **Remove a staff member** | Supabase → **Authentication → Users** → delete the user (this also removes them from `admins`) |
| **Change where reservation requests go** | Supabase → **Edge Functions → Secrets** → `RESERVATION_EMAIL`. No redeploy needed |
| **See past reservation requests** | Supabase → **Table Editor** → `reservation_requests` (kept 92 days) |
| **Update the relay** after a code change | On the Pi: `cd ~/printer-relay && git pull && uv sync && sudo systemctl restart printer-relay` |
| **Database change** in `sql/schema.sql` | Run the whole file again in the SQL Editor. It's safe to re-run |
| **Replace the Supabase secret key** | Supabase → API Keys → create a new secret key. On the Pi, put it in `~/printer-relay/relay.toml` and run `sudo systemctl restart printer-relay`. Check the website updates, then delete the old key |
| **Change the hotspot password** | On the Pi: `sudo nmcli con modify printer-ap wifi-sec.psk "<new password>" && sudo nmcli con up printer-ap`, then re-join every printer with the new password |

## Things that run by themselves

- **Security updates** on the Pi install automatically.
- **The relay restarts itself** if it crashes, and starts when the Pi boots.
- **Old history** (over 92 days) is deleted every night; the dashboard shows at most 90 days.
- **Automatic checks** run on GitHub after every push. A red X in a repository's **Actions** tab
  means a change broke something; don't pull it onto the Pi until it's fixed.