# pireplace

A Raspberry Pi 4 that plays looping fire videos on a TV, with background music
you pick from your phone. Plug it in and the fire is on. The only thing ever drawn
on the TV is the fire.

- The fire video plays in a loop. The fire's own sound ("log" sound) and the music
  have separate volumes.
- Music moods are folders of music. Switch mood, pause, skip, change volumes and
  mute the log sound from a web page at `http://pireplace.local`.
- No streaming services and no accounts. You supply the video and music files.
- The fire keeps playing if the network drops or the control page crashes.

## Hardware

- Raspberry Pi 4, microSD card, power supply, HDMI cable and TV.
- Raspberry Pi OS Lite, 64-bit (Bookworm or newer, so Python is 3.11+). No desktop.
- Raspberry Pi 3 is untested.
- Your laptop with Ansible and `rsync` for setup.

## Install

1. Flash the card with Raspberry Pi Imager: choose Raspberry Pi OS Lite (64-bit).
   In the OS customisation settings set hostname `pireplace`, your home Wi-Fi,
   a username, and your SSH public key. Enable SSH.
2. Boot the Pi and check `ssh <user>@pireplace.local` works.
3. Put the hotspot password in a file that stays out of git. It is the WPA2
   password of the `pireplace-setup` Wi-Fi the Pi opens when it has no network.
   It is **required**, must be 8 to 63 characters, and the placeholder
   `CHANGE_ME...` is refused.

   ```sh
   echo 'hotspot_password: "choose-your-own-password"' > secrets.yml
   ```

   `secrets.yml` is gitignored. Alternatives: put `hotspot_password=...` in
   `deploy/ansible/inventory.ini` (also gitignored; copy from
   `deploy/ansible/inventory.example.ini`), or use `ansible-vault`.
4. Deploy. Set `ansible_user` to your Imager username if it differs from your laptop
   username (inventory file, or `-e ansible_user=<user>` in `ANSIBLE_ARGS`).

   ```sh
   make deploy HOST=pireplace.local ANSIBLE_ARGS='-e @secrets.yml -e ansible_user=<user>'
   # or, with an inventory file:
   make deploy INVENTORY=deploy/ansible/inventory.ini
   ```

   Running it again is safe; it should report `changed=0`.
5. **Reboot the Pi once** after the first deploy (`ssh <user>@pireplace.local sudo reboot`).
   The settings that hide console text and the cursor on the TV only take effect
   after a reboot. Log out and in again too, so your SSH user picks up the
   `pireplace` group (needed to write media). The deploy also replaces `console=tty1`
   with `console=tty3` and disables cloud-init, so boot messages go to the invisible
   tty3 (view them with Ctrl+Alt+F3 on a keyboard attached to the Pi, or `journalctl -b`)
   and the console, including the rescue shell, is on tty3. Never remove the last console
   from `cmdline.txt`: on the Pi 4 `serial0` is not usable, so boot would stop.

`make deploy` needs `ansible-playbook` on your laptop. If you installed Ansible in
`.venv`, the Makefile uses it.

## Adding media

Media is never in the repo. On the Pi, videos go in `/srv/pireplace/videos` and
music in `/srv/pireplace/music/<mood>/`.

```sh
rsync -av --progress fire-logs/*.mp4 <user>@pireplace.local:/srv/pireplace/videos/
rsync -av --progress "Some Album/" <user>@pireplace.local:/srv/pireplace/music/classic-swing/"Some Album"/
rsync -av ~/Music/Christmas/ <user>@pireplace.local:/srv/pireplace/music/christmas/
```

- Videos: `.mp4 .mkv .webm .mov .m4v`. H.264 up to 1080p is recommended; the Pi 4
  decodes it in hardware.
- Music: `.mp3 .flac .m4a .aac .ogg .opus .wav`.
- Extensions are case-insensitive. Hidden files and folders (leading `.`) are ignored.
- A mood is an immediate subfolder of `/srv/pireplace/music`. Its tracks are every
  audio file anywhere beneath it, so ripped CDs in `Artist/Album/track` folders work.
- The deploy creates the moods `modern` and `classic-swing`.

**Add a mood:** make a folder, put music in it, press **Rescan media** on the
control page. `christmas` shows as "Christmas", `classic-swing` as "Classic Swing".

```sh
ssh <user>@pireplace.local mkdir /srv/pireplace/music/christmas
```

## Using it

Open `http://pireplace.local` on a phone or laptop on the same Wi-Fi. No login.
Music: play/pause, next, volume. Mood: pick one. Fire sound: on/off and volume.
Rescan media: after adding files.

`make card HOTSPOT_PASSWORD=...` writes `build/card.html`, a printable card with
a QR code for the remote page and one for Wi-Fi setup (with the hotspot SSID and
password). It needs `qrencode`. `build/` is gitignored, so the password stays out
of git. Don't leave the printed card where guests can read it if the password matters
to you.

The page answers to `pireplace.local`, `pireplace`, and the Pi's own IP address. If you
reach it by another name (say a DNS entry on your router), add that name to the
playbook's `pireplace_allowed_hosts` list and re-deploy (it becomes `allowed_hosts`
under `[web]` in `/etc/pireplace/config.toml`, which each deploy rewrites); other names get
"forbidden host". This stops other websites from driving the box through a guest's browser.

**Guest Wi-Fi will not work.** Guest networks usually enable client isolation, so
phones cannot reach the Pi and `http://pireplace.local` does not load. This is
expected; join the main network.

## Wi-Fi onboarding

If the Pi has no connection for about 5 minutes it opens a Wi-Fi network called
`pireplace-setup` (password: your `hotspot_password`). While it is up it retries
known networks every 5 minutes. Join `pireplace-setup` on your phone, open
`http://10.42.0.1/setup`, pick the network and enter its password. The hotspot
goes away once the Pi has joined. A brief router outage does not start the hotspot.

The setup page only exists while you are connected to the `pireplace-setup` hotspot:
from the home network (or in laptop dev mode) `/setup` is "not found", so nobody on
your Wi-Fi can change the Pi's network.

## Laptop development

The app runs on a laptop with mpv windows instead of the TV.

```sh
sudo apt install mpv ffmpeg      # ffmpeg makes the sample media; qrencode only for the card
python3 -m venv .venv            # needs python3-venv: sudo apt install python3-venv
.venv/bin/pip install -e '.[dev]'
make dev                         # sample media in .dev/media, UI at http://127.0.0.1:8080
make test                        # pytest
make lint                        # playbook and unit checks; reports what it skips
```

Python 3.11+ and the standard library are all the runtime needs; pytest is the
only dev dependency. Keep the repo path short: dev sockets live in `.dev/run`, and Unix socket paths
are limited to about 107 bytes.

## Troubleshooting

What runs on the Pi:

| Unit | Scope | Does |
| --- | --- | --- |
| `pireplace-control` | system | control page on port 80 |
| `pireplace-wifi` | system | hotspot watcher |
| `pireplace-video` | user `pireplace` | mpv, fire video |
| `pireplace-music` | user `pireplace` | mpv, music |

```sh
sudo systemctl status pireplace-control pireplace-wifi
journalctl -u pireplace-control -e
journalctl -u pireplace-wifi -e
sudo journalctl _SYSTEMD_USER_UNIT=pireplace-video.service -e
sudo journalctl _SYSTEMD_USER_UNIT=pireplace-music.service -e
sudo systemctl --user --machine=pireplace@.host status pireplace-video pireplace-music
sudo systemctl --user --machine=pireplace@.host restart pireplace-music
```

All four restart on their own after a crash (`Restart=always`, and systemd never gives up).
The video player starts the fire on its own, even if the control page is down. While
the control page runs it checks both players every 5 seconds: a player that restarted
is reloaded within about 5 seconds and gets your saved settings back (mood, volumes,
log mute, music pause); only a fresh boot starts from the defaults (fire sound on, music
playing unless `start_music = false`). A restart of the control page itself changes nothing that is playing. The exception:
`pireplace-control` and `pireplace-wifi` stay down on exit status 2 (invalid config or
hotspot_password); fix `/etc/pireplace/config.toml` (or re-run the deploy) and restart them.

- **Page says no fire videos or no moods:** add files, press Rescan media.
- **`pireplace.local` does not resolve:** use the Pi's IP address from your router.
  Check you are not on a guest network.
- **Black screen, no console text:** expected when no video is present.
- **An mpv update breaks playback:** hold the package and roll back:

  ```sh
  sudo apt-mark hold mpv
  # to go back to a known-good version:
  sudo apt install mpv=<good-version>
  ```

  `apt list -a mpv` shows available versions. `sudo apt-mark unhold mpv` when fixed.
  Only security updates and the Raspberry Pi archive install automatically
  (unattended-upgrades).
- **Deploy fails on `hotspot_password`:** it is missing, not 8 to 63 characters,
  or still the `CHANGE_ME` placeholder.
- **Boot messages are gone from the TV:** that is intended. They go to tty3: press
  Ctrl+Alt+F3 on a keyboard attached to the Pi, or run `journalctl -b`.
- **No sound on the TV:** the deploy adds an HDMI sink on ALSA card `vc4hdmi0` (the port
  nearest the power socket) and turns the headphone jack off (needs a reboot). Check
  that it exists and is the default (marked `*`):

  ```sh
  sudo -u pireplace XDG_RUNTIME_DIR=/run/user/$(id -u pireplace) wpctl status
  ```

  There should be a "Fireplace HDMI" sink. If the TV is on the other port, deploy with
  `-e pireplace_hdmi_card=vc4hdmi1`. `aplay -l` lists the cards.
- **`Cannot load libcuda.so.1` in the mpv log:** harmless. mpv probes hardware decoders
  and CUDA does not exist on a Pi.
- **Deploy ends with "REBOOT REQUIRED":** the boot settings that keep console text off
  the TV changed. Run `sudo reboot` on the Pi.

## Before the party

Run through this on the real Pi 4 and tick each item.

- [ ] Wi-Fi detection matches the code: on the Pi, connected to home Wi-Fi, run
      `nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device`. The Wi-Fi line must read like
      `wlan0:wifi:connected:<your network>` (TYPE `wifi` or `ethernet`, STATE exactly
      `connected`); that is what `pireplace/wifi.py` looks for. If the format differs,
      fix `connected()` before relying on the hotspot.
- [ ] Power on with media present: fire and music within 60 s, no console text, no cursor.
- [ ] Sound comes out of the TV (HDMI), not the Pi's 3.5 mm jack.
- [ ] Boot with the TV in standby, then turn the TV on: picture and sound both work.
- [ ] TV off then on: still playing.
- [ ] Phone at `http://pireplace.local`: mood switch, pause/play, next, both volumes,
      log mute. Each affects only its own stream.
- [ ] `sudo systemctl kill pireplace-control` and
      `sudo systemctl --user --machine=pireplace@.host kill pireplace-music`: both return on their own;
      the video never stops.
- [ ] `sudo systemctl --user --machine=pireplace@.host restart pireplace-video` while playing: the fire
      is back within about 5 s with its sound at the same volume and mute setting.
- [ ] Pull power, restore: same mood and volumes as before.
- [ ] Router off for 2 minutes, then on: no hotspot appears, the box rejoins.
- [ ] Forget the home network on the Pi, reboot: `pireplace-setup` appears after about
      5 minutes; the setup page joins a network; the hotspot disappears.
- [ ] While on the hotspot, the Pi's address really is 10.42.0.1 (`http://10.42.0.1/setup`
      loads; on the Pi `ip -4 addr show wlan0`). If not, re-deploy with
      `-e pireplace_hotspot_address=<that address>` (in `ANSIBLE_ARGS`).
- [ ] From the home network, `http://pireplace.local/setup` says not found.
- [ ] Empty `/srv/pireplace/videos`, press Rescan: black screen, the page shows the
      message, nothing crash-loops (`sudo systemctl --user --machine=pireplace@.host status pireplace-video`
      stays stable).
