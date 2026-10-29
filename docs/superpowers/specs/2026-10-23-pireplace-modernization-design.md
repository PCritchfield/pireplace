# Pireplace modernization — design

Date: 2026-10-23
Status: draft, awaiting approval

## Intent

Pireplace is a Raspberry Pi that sits behind a small TV in an empty living-room
fireplace. Turn the TV on and there is a crackling Yule log on screen with
Christmas music playing. It is an appliance for holiday parties, not an app:
set it and forget it.

The current version (2018-era, untouched since ~2020) is a handful of bash
scripts around `omxplayer` (video) and `pianobar` + Patiobar (Pandora music
with a web remote). `omxplayer` no longer exists on current Raspberry Pi OS,
Patiobar is unmaintained, the Pandora password is stored in plain text, and
first-boot Wi-Fi setup lived in hand-done OS configuration rather than the
repo. This rewrite makes the box reproducible, self-contained, and testable
on a laptop, and drops the dependency on a streaming service.

## How it's used

- On first boot with no known Wi-Fi, the box offers a way to enter Wi-Fi
  credentials. After that it always boots straight to the fire with no
  interaction.
- The TV is just a window. Turning it off changes nothing; the box keeps
  running (low power, low heat).
- Two independent audio streams: the log's crackle (the video's own audio)
  and the music. Either can be muted or adjusted without affecting the
  other. Music can be paused while the log keeps crackling.
- The only thing guests change is the music *mood*: e.g. "Modern"
  (Mariah Carey, Michael Bublé, Josh Groban) vs "Classic Swing" (Sinatra,
  Crosby). Not individual song picking.
- Control happens from any phone on the home network via a simple web page.

## Goals

1. Boots to fire with zero interaction on a Raspberry Pi 4.
2. Video loops forever from a local `videos/` folder, hardware-decoded,
   full screen, no desktop, no cursor, no console text visible.
3. Music plays from a local `music/` folder whose subfolders are moods
   (`music/modern/`, `music/classic-swing/`, …). Adding a mood is adding a
   folder. Shuffle within the chosen mood, looping forever.
4. Phone web page with: mood selection, music play/pause, next track,
   music volume, log-sound mute and volume. Shows what's currently playing.
5. Each component is supervised and restarts itself on failure. A crash,
   power cut, or unplug always recovers to a playing fire on next boot.
6. First-boot Wi-Fi onboarding is part of the repo, not hand-done.
7. Everything except the Pi hardware can be run and tested on a Linux
   laptop: the same players and control service, in a window, with a sample
   video and sample audio.
8. Provisioning a fresh SD card is a documented, repeatable process from
   this repo.
9. Works fully offline once running. Network is only needed for the phone
   control page (local network) and, later, server sync.

## Non-goals (for this version)

- Any streaming-service integration (Pandora, Spotify, etc.). Music comes
  from files. The owner will rip CDs or buy downloads to fill `music/`.
- Syncing videos or music from the home media server over Tailscale. This is
  the planned next step (see "Designed for later"), not part of this build.
- Hosting music on the media server. That is a separate project.
- A CD-ripping helper.
- Multi-room audio, song search, queues, user accounts.

## Anti-goals (failure even if it technically works)

- Anything that needs babysitting during a party: expiring logins,
  a service that stops because the same account is used elsewhere,
  a dependency that breaks on an unattended update.
- Credentials for third-party services stored on the box.
- The fire depending on the internet or the home server being up.
- A login screen or password on the phone control page (it is local-network
  only; see Security).

## Constraints

- Deadline: running in the living room by Thanksgiving, 2026-11-26.
- Hardware: Raspberry Pi 4 only. The owner's Pi 3 is spare hardware, not a
  supported target; the README notes it as untested (Claude's call). TV is
  roughly 27", HDMI.
- OS: current Raspberry Pi OS Lite, 64-bit, no desktop environment.
- Owner's laptop is Linux.
- Owner works in shell and has used Ansible before (inferred from their
  other repos); this is a hobby project, so prefer few moving parts over
  cleverness.

## Decisions made

Made by the owner:

- Music from local files, two (or more) moods as folders; no streaming
  service.
- Two separate audio streams, independently controllable.
- Phone web page for control.
- Pi 4 as the target.
- Keep the box self-contained now; Tailscale-to-home-server sync comes later.

Made by Claude during design (owner may override):

- **Players:** `mpv` for both video and music, as two separate
  long-running processes. Video instance: full screen via DRM/KMS
  (no X/Wayland), hardware decode, `--loop-playlist=inf` over `videos/`.
  Music instance: audio-only, shuffled playlist of the current mood folder.
  Both expose mpv's JSON IPC socket for control.
- **Audio mixing:** the OS's default audio server (PipeWire on current
  Pi OS) mixes the two streams to HDMI. Volumes are controlled per mpv
  instance, not at the system mixer.
- **Supervision:** systemd units (video, music, control service) with
  `Restart=always`. Replaces `screen`, the `ps | grep` loop, and the
  `pkill`-by-name scripts.
- **Control service:** one small Python service that serves the phone page
  and talks to both mpv instances over IPC. Single process, minimal
  dependencies. It remembers the last mood and volumes across reboots.
- **Provisioning:** an Ansible playbook run from the laptop against a
  freshly flashed Pi OS Lite card (Raspberry Pi Imager can preset hostname
  and SSH key). The playbook installs packages, the app, systemd units,
  and the Wi-Fi onboarding tool. A fully baked SD image is not required.
- **Layout:** application code and config in the repo; media lives outside
  the repo on the Pi (e.g. `/srv/pireplace/videos` and
  `/srv/pireplace/music/<mood>/`), configured by path. Media is never
  committed.
- **Remove:** `pianobar`, Patiobar, `omxplayer`, `screen`, the Pandora
  config and credentials, `fireplace.sh`/`pbstart`/`pbstop`/`install.sh`.

## Behaviour details

- **On boot:** video starts with the log sound on at its saved volume.
  Music starts in the last-used mood at its saved volume, unless the owner
  set "start with music paused" in config (default: music on).
- **Empty folders:** no videos → black screen and the control page shows a
  clear message; nothing crash-loops. Empty mood folder → that mood is
  shown as unavailable on the page.
- **Mood switch:** replaces the music playlist with a shuffled list of the
  new mood and starts playing immediately.
- **Media formats:** whatever mpv plays. Owner-supplied log videos are
  expected to be H.264 1080p or lower for reliable hardware decode on the
  Pi 4; the docs say so.
- **Adding media:** copy files into the folders (e.g. over SSH/`rsync` or by
  mounting the card). The control page offers a "rescan" action, and a
  restart also picks them up.

## Wi-Fi onboarding

Initial provisioning has network already (Raspberry Pi Imager presets Wi-Fi
and SSH). Onboarding exists for the later cases the owner cares about:
taking the box to another house, or the home Wi-Fi password changing.

Claude's calls:

- The fire never depends on onboarding: video and music keep playing
  whatever the network state.
- The onboarding hotspot appears only after no known network has been
  reachable for a sustained period (several minutes, builder picks), so a
  router reboot mid-party does not trigger it. While the hotspot is up, the
  box keeps periodically retrying known networks and drops the hotspot as
  soon as one returns.
- Hotspot name and how to connect are documented in the README and on a
  printable card kept with the box. Nothing is shown on the TV.

## Finding the control page

- The page is reachable at `http://pireplace.local` (mDNS) on port 80,
  plus by IP.
- The repo includes a printable card with a QR code for that URL to leave
  by the fireplace. Nothing is shown on the TV.
- Phones must be on the same network as the box. The README notes that
  guest Wi-Fi networks or "client isolation" router settings will block it.

## Security

- The control page listens on the local network only. No auth, by design.
  Anyone on the home Wi-Fi can change the music, which is the point.
- No third-party credentials on the device.
- Wi-Fi onboarding hotspot only appears when no known network is reachable,
  and goes away once connected.
- Unattended OS security updates are allowed, but the build should pin or
  hold anything the fire depends on if that's needed for stability; the
  builder decides how.

## Local testing on a laptop

- A `dev` mode (e.g. `make dev`) runs the same mpv instances in windows and
  the same control service on `localhost`, pointed at a sample video and a
  sample `music/` tree generated or fetched by the dev tooling (small,
  freely licensed or synthetic test media, never committed if large).
- The control service has automated tests against a fake or real mpv IPC
  socket.
- The Ansible playbook can be dry-run or tested against a container or VM
  before touching the Pi; final check is on the real Pi 4.

## Designed for later (do not build now)

The next step is Tailscale on the box plus a sync job that pulls new
`videos/` and `music/<mood>/` from the home media server when reachable.
The box must keep working from its local copy when the server is
unreachable. This version should keep that easy: media in plain folders by
path, config in one file, nothing in the player or control service that
assumes where files came from.

## Left to the builder

- Which first-boot Wi-Fi onboarding tool to use (captive-portal hotspot
  style, compatible with NetworkManager on current Pi OS).
- Python web framework (or none), page design, and how the page discovers
  state.
- Exact mpv flags for KMS output and hardware decode on the Pi 4.
- Config file format.
- How dev mode obtains sample media.
- How to hide the console/cursor and suppress boot text on the TV.
- How to handle OS update stability.
