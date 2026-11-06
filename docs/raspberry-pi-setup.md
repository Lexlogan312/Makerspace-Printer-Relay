# Raspberry Pi setup

This guide takes a brand-new Raspberry Pi 5 to a running relay. No Linux experience is needed:
run the commands as written and check each result before moving on.

```
 Bambu printers ──Wi-Fi──▶ wlan0: "MakerspacePrinters" hotspot (10.42.0.0/24, no internet)
                                   │
                              Raspberry Pi 5 ── printer-relay service
                                   │
 Supabase ◀── internet ── eth0: Ethernet to the campus network
```

The Pi's built-in Wi-Fi becomes a private hotspot that only the printers join. It has no
internet access, and a firewall keeps it that way. The Pi reaches Supabase over Ethernet.

**Where:** do everything in the makerspace, with the Pi plugged into a campus Ethernet port.

**No monitor needed** after the first boot. Everything is done over SSH from a computer on the
campus network, or from a browser with Raspberry Pi Connect (step 2).

## What you need

- Raspberry Pi 5, the official 27 W USB-C power supply, and a microSD card (16 GB or more)
- An Ethernet cable and a working campus Ethernet port.
- A computer with [Raspberry Pi Imager](https://www.raspberrypi.com/software/), on the campus network
- The Supabase project URL and **secret** key (`sb_secret_…`), with the database already set up
  by `sql/schema.sql` (see the README)

---

## 1. Flash the SD card and find the Pi

1. Open Raspberry Pi Imager → **Device:** Raspberry Pi 5 → **OS:** *Raspberry Pi OS (other) →
   Raspberry Pi OS Lite (64-bit)* → **Storage:** your SD card.
2. Fill in the customization settings:
   - Hostname: `makerspace-relay`
   - Username and password: e.g. `makerspace`, with a strong password
   - **Wireless LAN: leave it off.** The Pi uses Ethernet, and its Wi-Fi becomes the printer
     hotspot. Do set the **Wireless LAN country to US**.
   - Locale: time zone `America/New_York`
   - **Services:** enable SSH with password authentication, so whoever maintains the Pi can log in
3. Write the card, put it in the Pi, plug in Ethernet, the monitor and keyboard, then power on.
   The first boot takes a few minutes and restarts once.
4. Log in on the Pi's screen and note its addresses:
   ```bash
   hostname -I                       # the Pi's IP on campus, e.g. 192.168.20.128
   ```

You can unplug the monitor and keyboard now.

> **Use the IP address, not `makerspace-relay.local`.**

## 2. Log in and update

From your computer, on the campus network:

```bash
ssh makerspace@<the Pi's IP>
```

The first time, type `yes` to trust the Pi, then enter its password. On the Pi:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git nftables dnsmasq-base
sudo raspi-config nonint do_wifi_country US
sudo reboot
```

- `git` downloads the relay code; `nftables` runs the hotspot firewall; `dnsmasq-base` gives
  printers their addresses on the hotspot.
- The Wi-Fi country tells the chip which channels are legal; the hotspot won't start without it.

Wait a minute after the reboot, then SSH back in.

### Raspberry Pi Connect (remote access from anywhere)

A backup way in that works from off campus and doesn't depend on the Pi's IP, because the Pi
connects out to Raspberry Pi's servers. It needs a free Raspberry Pi ID; use an account with a 
strong password and two-factor login, since it can open a terminal on the Pi.

```bash
sudo apt install -y rpi-connect-lite
rpi-connect on
loginctl enable-linger      # keep it running when nobody is logged in
rpi-connect signin          # open the printed link in a browser and approve the Pi
rpi-connect status          # should say "Signed in: yes"
```

To use it: go to connect.raspberrypi.com → your Pi → **Remote shell**.

## 3. Get the code and configure it

```bash
git clone https://github.com/Lexlogan312/Makerspace-Printer-Relay.git ~/printer-relay
cd ~/printer-relay
cp relay.example.toml relay.toml
chmod 600 relay.toml
nano relay.toml
```

In `nano`, fill in:
- `url`: Supabase → Project Settings → Data API → Project URL
- `key`: Supabase → Project Settings → API Keys → **Secret** key (`sb_secret_…`)
- `printer_network = "10.42.0.0/24"`, so the relay ignores Bambu printers elsewhere on campus

Save with **Ctrl+O**, **Enter**, then exit with **Ctrl+X**.

## 4. Install the relay

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env
cd ~/printer-relay
uv sync
uv run pytest
```

`uv` installs Python 3.13 and the relay's dependencies into `~/printer-relay/.venv`. All tests
should pass.

## 5. Check the network and clean up first-boot files

```bash
ping -c 3 supabase.com
timedatectl
```

`ping` should get replies, and `timedatectl` should say **System clock synchronized: yes**.

Imager's first-boot setup (cloud-init) has done its job. Turn it off so it does not re-apply old
settings, and delete its files from the SD card.

```bash
sudo touch /etc/cloud/cloud-init.disabled
sudo rm -f /boot/firmware/user-data /boot/firmware/network-config /boot/firmware/meta-data
rm -f ~/.config/com.raspberrypi.connect/auth.key
```

Then make sure no Wi-Fi network is saved, so `wlan0` is free for the hotspot:

```bash
nmcli -f NAME,TYPE con show
```

Only the Ethernet connection (e.g. `netplan-eth0`) and `lo` should be listed. If a Wi-Fi entry
(type `wifi`) is there, delete it:

```bash
nmcli -t -f UUID,TYPE con show | awk -F: '$2=="802-11-wireless"{print $1}' | xargs -r -n1 sudo nmcli con delete uuid
```

## 6. Choose a hotspot channel

The printers only use **2.4 GHz**. See which channels nearby networks use:

```bash
nmcli -f SSID,CHAN,SIGNAL dev wifi list ifname wlan0
```

Pick whichever of **1, 6 or 11** has the fewest strong networks.

## 7. Create the hotspot and its firewall

**Firewall first,** so the printers are never on the hotspot without it:

```bash
cd ~/printer-relay
sudo cp deploy/printer-isolation.nft /etc/printer-isolation.nft
sudo cp deploy/printer-isolation.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now printer-isolation.service
sudo nft list table inet printer_isolation
```

**Allow more than 8 printers.** With its standard firmware, the Pi's Wi-Fi chip accepts only 8
devices in hotspot mode. Raspberry Pi OS includes a "minimal" firmware that raises this by leaving out features a hotspot doesn't use:

```bash
sudo update-alternatives --set cyfmac43455-sdio.bin /usr/lib/firmware/cypress/cyfmac43455-sdio-minimal.bin
sudo reboot
```

After reconnecting, `update-alternatives --display cyfmac43455-sdio.bin | head -3` should point
to `…-minimal.bin`. The setting survives system updates. To undo it:
`sudo update-alternatives --auto cyfmac43455-sdio.bin` and reboot.

**Create the hotspot.** Choose a password and replace `6` with the channel from step 6:

```bash
sudo nmcli con add type wifi ifname wlan0 con-name printer-ap autoconnect yes \
  ssid MakerspacePrinters \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  ipv4.method shared ipv4.addresses 10.42.0.1/24 ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
  wifi-sec.pmf disable wifi-sec.psk "<HOTSPOT_PASSWORD>"
sudo nmcli con up printer-ap
```

**Check the isolation:** join `MakerspacePrinters` from a phone. It should get a `10.42.0.x`
address and **no internet**. Forget the network afterwards.

## 8. Join the printers to the hotspot

On **each** printer's touchscreen:

1. **Settings → WLAN:** join `MakerspacePrinters` with the hotspot password.
2. Turn on **LAN Only Mode**, so it stops trying to reach Bambu's cloud.
3. Note its **Access Code** (shown in the LAN settings).

Within about 10 seconds each printer appears on the dashboard's **Printers** page under printers
found on the network. Add it there with a name and its access code. Printers already in the
dashboard reconnect by themselves; if a printer's access code changed, update it with **Edit**.

To see which printers have joined: `sudo cat /var/lib/NetworkManager/dnsmasq-wlan0.leases`.

## 9. Test, then install the service

A dry run reads the printer list from Supabase and prints each printer's status without writing
anything:

```bash
cd ~/printer-relay
uv run python -m relay --dry-run
```

Every printer should show a status line within about 15 seconds. Press **Ctrl+C**. Then install
the relay as a service, so it starts on boot and restarts itself if it ever crashes:

```bash
sed "s/__USER__/$USER/g" deploy/printer-relay.service | sudo tee /etc/systemd/system/printer-relay.service
sudo systemctl daemon-reload
sudo systemctl enable --now printer-relay.service
journalctl -u printer-relay -f # live log; Ctrl+C stops watching
```

The dashboard should now show the printers live, and its Printers page should say the relay is
running.

## 10. Reboot test

```bash
sudo reboot
```

After a minute, SSH back in and check:

```bash
systemctl status printer-relay printer-isolation --no-pager
nmcli con show --active
```

Both services should be **active (running)**, and both `printer-ap` and the Ethernet connection
should be listed. The printers take **1–2 minutes** to rejoin the hotspot after a restart, so
they show offline briefly.

**Security updates** install automatically on Raspberry Pi OS. To confirm,
`apt-config dump APT::Periodic::Unattended-Upgrade` should print `"1"`; if it doesn't, run
`sudo apt install -y unattended-upgrades`.

---

## Day-to-day

| Task | How |
|---|---|
| Live logs | `journalctl -u printer-relay -f` |
| Restart after editing `relay.toml` | `sudo systemctl restart printer-relay` |
| Update the relay | `cd ~/printer-relay && git pull && uv sync && sudo systemctl restart printer-relay` |
| Update the service file (after `deploy/printer-relay.service` changes) | `cd ~/printer-relay && sed "s/__USER__/$USER/g" deploy/printer-relay.service \| sudo tee /etc/systemd/system/printer-relay.service && sudo systemctl daemon-reload && sudo systemctl restart printer-relay` |
| See which printers are on the hotspot | `sudo cat /var/lib/NetworkManager/dnsmasq-wlan0.leases` |
| Add a printer | Join it to the hotspot (step 8), then add it on the dashboard's Printers page. No restart needed |
| Remove a printer | Dashboard → Printers → Edit → **Retire** (keeps its history). **Delete** also erases its history |

## Troubleshooting

See [troubleshooting.md](troubleshooting.md).
