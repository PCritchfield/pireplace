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
   `pireplace` group (needed to write media).

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

**Guest Wi-Fi will not work.** Guest networks usually enable client isolation, so
phones cannot reach the Pi and `http://pireplace.local` does not load. This is
expected; join the main network.

## Wi-Fi onboarding

If the Pi has no connection for about 5 minutes it opens a Wi-Fi network called
`pireplace-setup` (password: your `hotspot_password`). While it is up it retries
known networks every 5 minutes. Join `pireplace-setup` on your phone, open
`http://10.42.0.1/setup`, pick the network and enter its password. The hotspot
goes away once the Pi has joined. A brief router outage does not start the hotspot.

## Laptop development

The app runs on a laptop with mpv windows instead of the TV.

```sh
sudo apt install mpv ffmpeg      # ffmpeg makes the sample media; qrencode only for the card
make dev                         # sample media in .dev/media, UI at http://127.0.0.1:8080
make test                        # pytest
make lint                        # playbook and unit checks; reports what it skips
```

Python 3.11+ and the standard library are all the runtime needs; pytest is a dev
extra (`pip install -e '.[dev]'`).
Keep the repo path short: dev sockets live in `.dev/run`, and Unix socket paths
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
sudo journalctl --user -M pireplace@ -u pireplace-video -e
systemctl --user -M pireplace@ status pireplace-video pireplace-music
systemctl --user -M pireplace@ restart pireplace-music
```

All four restart on their own after a crash (`Restart=always`).

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
  Only security updates install automatically (unattended-upgrades).
- **Deploy fails on `hotspot_password`:** it is missing, not 8 to 63 characters,
  or still the `CHANGE_ME` placeholder.

## Before the party

Run through this on the real Pi 4 and tick each item.

- [ ] Power on with media present: fire and music within 60 s, no console text, no cursor.
- [ ] TV off then on: still playing.
- [ ] Phone at `http://pireplace.local`: mood switch, pause/play, next, both volumes,
      log mute. Each affects only its own stream.
- [ ] `sudo systemctl kill pireplace-control` and
      `systemctl --user -M pireplace@ kill pireplace-music`: both return on their own;
      the video never stops.
- [ ] Pull power, restore: same mood and volumes as before.
- [ ] Router off for 2 minutes, then on: no hotspot appears, the box rejoins.
- [ ] Forget the home network on the Pi, reboot: `pireplace-setup` appears after about
      5 minutes; the setup page joins a network; the hotspot disappears.
- [ ] Empty `/srv/pireplace/videos`, press Rescan: black screen, the page shows the
      message, nothing crash-loops (`systemctl --user -M pireplace@ status pireplace-video`
      stays stable).
