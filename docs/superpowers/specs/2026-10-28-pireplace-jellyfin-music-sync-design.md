# Pireplace — Jellyfin music sync design

Date: 2026-10-28
Status: approved 2026-10-28
Builds on: `2026-10-23-pireplace-modernization-design.md` ("Designed for later")

## Intent

The fireplace plays music from local mood folders on the Pi. The owner's
Christmas music lives on his Jellyfin server (649 FLAC tracks). Instead of
copying music by hand, the Pi should keep its mood folders in step with a
set of Jellyfin playlists, so the owner curates music in Jellyfin and the
fireplace follows. Playback stays local: the fire and its music never depend
on the server or the network being up during a party.

## Current state (facts)

- Jellyfin 12.2.0 on host `omarchy`, LAN address `192.168.20.10:8096`, on a
  deliberately separate subnet. The home router blocks `192.168.8.x` (Pi,
  laptop) from reaching it. That split is intentional and must stay.
- The server is already on the owner's Tailscale network as `omarchy`,
  `100.88.8.6`. On 2026-10-28 Tailscale was installed on the Pi by hand
  (`pireplace`, `100.83.117.48`, joined with `--accept-dns=false`).
  `http://100.88.8.6:8096/System/Info/Public` answers from the Pi. The path
  goes through a DERP relay (no direct link, since the router splits the
  subnets), so throughput is limited and the first full sync may take about an
  hour. The playbook must cope with Tailscale already being installed and
  logged in.
- Public `https://mmumbless.duckdns.org` works but goes out and back in through
  the router (hairpin), which has been flaky; not to be used.
- Four playlists exist, owned by Jellyfin user `mmumbless`
  (Id `1b7ed00cbdcc4a1eb624738191ca4efd`), shared read-only with the non-admin
  user `music-streaming` (Id `4640835a50a94bc69709d00d6f332fac`). They were
  recreated after review (see "Changes after review"); the Ids below are the new
  ones:

  | Playlist | Jellyfin Id | Tracks | Length |
  |---|---|---|---|
  | Fireplace – Modern | `dc8d8d54ecad68b2546b5c7c9b4b4766` | 86 | 4.96 h |
  | Fireplace – Classic Swing | `f4b96696936ec335bfeca0c623ad0669` | 105 | 4.97 h |
  | Fireplace – Soul & Motown | `79525d419fccb3bec7c38cef5fe07b11` | 64 | 4.18 h |
  | Fireplace – Instrumental | `30bf4d0f66600b1d1dd8cded00e25705` | 39 | 2.42 h |

  About 40 other auto-generated playlists exist on the server; select by Id.
- Jellyfin API notes (verified by the session that built the playlists):
  auth header `Authorization: MediaBrowser Token="<key>"` (the old
  `X-Emby-Token` header is rejected); list items with
  `GET /Playlists/{id}/Items?userId=<userId>` (400 without `userId`).
  `GET /Items/{itemId}/Download` should return the original file (untested).

## Goals

1. A sync job on the Pi mirrors each configured playlist into
   `/srv/pireplace/music/<mood>/`: downloads new tracks as the original files,
   deletes tracks that left the playlist.
2. Runs nightly and on demand; after a sync that changed anything, the
   controller rescans so new moods appear on the phone page.
3. Reaches Jellyfin over Tailscale only. No change to the home router.
4. Never makes things worse: if the server, Tailscale or a playlist fetch
   fails, nothing local is deleted and playback is unaffected.
5. Provisioned entirely by the playbook, including Tailscale, with secrets
   supplied by the owner through the gitignored inventory.
6. Moods for all four playlists: `modern`, `classic-swing`, `soul-motown`,
   `instrumental`. Adding a mood later is: create a playlist, add a line to the
   inventory, redeploy.

## Non-goals

- Live streaming from Jellyfin during playback.
- Song picking or browsing Jellyfin from the phone page.
- A "sync now" button on the phone page (on demand = a command over SSH).
- Transcoding; FLAC plays fine in mpv.
- Syncing fire videos from Jellyfin (could follow the same pattern later).

## Anti-goals

- Credentials in git, in logs, or on the phone page.
- Opening the server's subnet to the home network, or giving the Pi broader
  tailnet access than Jellyfin needs.
- A failed or partial sync that empties a mood folder.
- A sync that interrupts music during a party (it runs at night, and only
  rescans when something changed).

## Decisions made

By the owner:
- Download (sync), don't stream.
- Tailscale on the Pi for access to the server; keep the network split.
- Use the four playlists above.

Claude's calls (owner may override):
- **Where it runs:** on the Pi, as user `pireplace`, as
  `python -m pireplace sync --config /etc/pireplace/config.toml`, stdlib only
  (`urllib`, `json`). Supervised by a systemd service + timer
  (`pireplace-sync.service` / `.timer`), daily at 04:30 local with
  `Persistent=true`. On demand: `sudo systemctl start pireplace-sync`.
- **Address:** `pireplace_jellyfin_url` inventory var, the server's
  Tailscale address (see "Further calls": the Tailscale IP, since tailnet DNS
  is not accepted on the Pi). Plain HTTP is acceptable because Tailscale
  encrypts the link.
- **Config:** a new `[jellyfin]` section rendered from inventory vars:
  `url`, `user_id`, `api_key_file`, and a mood → playlist Id map. The API key
  lives in its own file `/etc/pireplace/jellyfin.key` (root:pireplace 0640),
  never in `config.toml`.
- **Which files the sync owns:** a hidden manifest per mood folder
  (`.pireplace-sync.json`: Jellyfin item Id → local filename). The sync only
  deletes files listed in its manifest; anything copied in by hand survives.
  Hidden files are already ignored by media discovery.
- **Filenames:** `<Artist> - <Title> [<first 8 of item Id>].<ext>`, sanitised
  for the filesystem. Order doesn't matter (moods are shuffled).
- **Downloads:** to a dot-prefixed temp file in the mood folder, renamed into
  place when complete, so a half-downloaded track is never played or scanned.
  Already-present items are skipped by Id, so reruns are cheap.
- **Safety rules:**
  - If listing a playlist fails, skip that mood entirely: no downloads, no deletes.
  - If a playlist returns zero items, treat it as an error unless the mood
    folder is also empty. Never delete a whole mood because of an empty
    response.
  - Stop downloading if free space on `/srv` would fall below 1 GB (plus the
    up-front 2 GB check in "Further calls").
  - Exit non-zero on any error, so `systemctl status` and the journal show it.
- **Rescan:** only if at least one file was added or removed, via the local
  control API (`POST /api/rescan` with `Host: pireplace.local` and JSON
  content type). Rescan reshuffles the current mood; acceptable at 04:30.
- **Tailscale:** installed from Tailscale's official apt repository with
  its signing key; machine name `pireplace`. `tailscale up` with an auth key
  from the inventory, only when the node isn't already logged in. No Tailscale
  SSH, no accept-routes, no exit node. The auth key is `no_log` and not stored
  on the Pi.
- **Access rule:** the README includes a ready-to-paste tailnet ACL that
  lets `tag:pireplace` reach only the Jellyfin host on port 8096. Applying it
  is the owner's choice. The auth key should be created with that tag.
- **Mood labels:** folder names map to labels as today
  (`soul-motown` → "Soul Motown").

## Further calls after builder review (Claude's, owner may override)

- **Step 0 of the plan is a manual check** with the real key, from the Pi
  over Tailscale:
  - `GET /Playlists/{id}/Items?userId=…` lists a private `mmumbless` playlist
  - `GET /Items/{id}/Download` returns the FLAC

  Jellyfin API keys act with admin rights, so this is expected to work.
  Fallback if it doesn't: make the four playlists public in Jellyfin. Never a
  user password on the Pi.
- **Addressing:** `tailscale up --accept-dns=false`, so Tailscale never
  touches the Pi's DNS. `pireplace.local` (mDNS via Avahi) is unaffected either
  way. Because of that, `pireplace_jellyfin_url` uses the server's Tailscale
  IP (`http://100.x.y.z:8096`), which is stable for a node. The owner reads it
  from the Tailscale admin console.
- **Tailscale repo:** pick the apt suite from `ansible_distribution_release`
  (the Pi runs Trixie). Run `tailscale up` only when `tailscale status`
  reports not logged in. Later flag changes are the owner's job in the admin
  console.
- **Moods in the playbook:** a single inventory map
  `pireplace_jellyfin_playlists` (mood → playlist Id) replaces the
  hard-coded `pireplace_moods` list as the source of mood folders. The
  playbook creates every folder in the map. With no Jellyfin config, the
  existing two default folders are kept.
- **Sync service sandbox:** same hardening style as the other system units,
  with `ReadWritePaths=/srv/pireplace/music` and `UMask=0002`, so files stay
  group-readable and writable for the `pireplace` group (mpv, and the admin's
  rsync).
- **Duplicates across moods are fine.** Each mood folder is self-contained, and
  a track in two playlists is stored twice. Expected total for the four
  playlists is about 16.5 h of FLAC, roughly 6–8 GB. The card has about 20 GB
  free after the fire video. Before downloading, the sync adds up the planned
  new bytes (from Jellyfin's item metadata where available) and refuses to
  start if that would leave less than 2 GB free. The running 1 GB floor stays
  as a backstop.
- **Errors:** each mood is processed independently.
  - A mood that listed successfully still gets its deletions and its
    successful downloads, even if some downloads failed.
  - A failed track is retried on the next run.
  - The exit code is non-zero if anything failed.
  - Updating the manifest is done atomically per mood, after its files have
    changed.
- **An empty playlist is always treated as an error for a mood that has
  synced files.** To retire a mood, remove it from the inventory map and
  delete the folder. The sync never empties a mood by itself.
- **Rescan** happens after manifests are written.
  - If the control service can't be reached, the sync logs a warning and
    still exits 0. The phone page discovers moods on every status poll anyway,
    and the controller reloads an empty player on its own.
  - The sync unit has no ordering on the control unit.
- **Secrets** can go in `inventory.ini` or the existing `secrets.yml`
  (`-e @secrets.yml`). The README shows both.
  - With no `pireplace_jellyfin_api_key`, the whole feature is skipped: no
    `[jellyfin]` section, no key file, sync timer not installed. A plain
    redeploy works exactly as today.
  - With no `pireplace_tailscale_authkey`, Tailscale is installed but not
    joined. The sync then fails cleanly until it is joined.

## Secrets (owner supplies, in `deploy/ansible/inventory.ini` or `secrets.yml`)

- `pireplace_jellyfin_api_key`: a dedicated Jellyfin API key named `pireplace`.
- `pireplace_tailscale_authkey`: a Tailscale auth key, ideally one-off and
  tagged `tag:pireplace`. Needed only for the first join.

## Testing

- Unit tests against a fake Jellyfin HTTP server (stdlib `http.server`):
  - add and remove tracks
  - a playlist fetch fails, so nothing is deleted
  - a playlist comes back empty
  - a download is interrupted, leaving no stray or partial files
  - manual files survive
  - the auth header has the exact format
  - the API key never appears in logs or exceptions
  - the disk-space guard
  - rescan is triggered only when something changed
- The playbook: syntax check plus the existing text tests. The Tailscale
  tasks are skipped when no auth key is given.
- On the Pi:
  - `tailscale status` shows the server
  - `curl` to Jellyfin's public info endpoint works over Tailscale
  - the first sync fills all four mood folders
  - the phone page shows four moods
  - a second sync changes nothing
  - with Tailscale stopped, a sync fails cleanly and the music keeps playing

## Left to the builder

- Exact filename sanitising rules and collision handling.
- Retry and backoff details for downloads.
- Whether to verify downloads by size against Jellyfin's metadata.
- Log format.

## Changes after review (2026-10-28)

A whole-branch review and three guardian reviews (security, architecture,
infrastructure) changed these decisions. Where they conflict with the sections
above, this section wins.

- **Credential: a limited user's access token, not an API key.** Dashboard API
  keys act as an administrator and cannot call the per-user playlist endpoints.
  The sync authenticates as the dedicated non-admin Jellyfin user
  `music-streaming` (Music library only, downloads allowed). Its access token
  comes from `POST /Users/AuthenticateByName` via `scripts/jellyfin_token.sh`
  (prompts, password never on argv). The inventory variable keeps the name
  `pireplace_jellyfin_api_key`; `pireplace_jellyfin_user_id` is that user's Id
  (default `4640835a50a94bc69709d00d6f332fac`). This replaces the "Step 0" plan
  of relying on an API key's admin rights; "never a user password on the Pi"
  still holds (only the token is stored).
- **Playlists recreated.** Jellyfin 12.2 crashes when a share is added to an
  existing private playlist, so the four playlists were recreated with the
  read-only share in the create call (`POST /Playlists` with
  `"Users":[{"UserId":"<sync user id>","CanEdit":false}]`). New Ids are in the
  table under "Current state"; the old ones are gone.
- **Token storage.** `/etc/pireplace/jellyfin.key` is root:root 0600 (was
  root:pireplace 0640). `pireplace-sync.service` receives it with
  `LoadCredential=jellyfin.key:...`; the CLI prefers
  `$CREDENTIALS_DIRECTORY/jellyfin.key` and falls back to `api_key_file`. The
  control page, Wi-Fi watcher and players cannot read it; on demand means
  `systemctl start pireplace-sync`, not a hand run as `pireplace`.
- **Network confinement.** The sync unit has `IPAddressDeny=any`, allows
  loopback plus the Jellyfin host (an Ansible drop-in built from the host of
  `pireplace_jellyfin_url`, which must therefore be an IPv4 address), and
  `RestrictNetworkInterfaces=lo tailscale0`; also no capabilities,
  `PrivateIPC`, `SystemCallFilter=@system-service`. The control page refuses
  Tailscale's ranges (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`). The Jellyfin
  client ignores environment proxies.
- **Tailscale gated on the feature.** Every Tailscale task (repo, key, pin,
  package, service, login) is one block that runs only when the Jellyfin token
  is set; a plain redeploy installs no Tailscale. `tailscale up` runs only in
  state `NeedsLogin` with an auth key, adds `--shields-up`,
  `--advertise-tags=tag:pireplace` and `--timeout=60s`, and reads the key from a
  root-only file (`--auth-key=file:`), removed afterwards; a failure stops the
  deploy with the manual command. The note to the owner follows the actual
  state. unattended-upgrades follows the Tailscale origin, and an apt pin limits
  that repository to `tailscale*` packages.
- **Tailnet policy.** The README now gives a complete minimal policy that
  replaces the default allow-all rule (tags `tag:pireplace`, `tag:jellyfin`; the
  Pi reaches only `tcp:8096` on Jellyfin; only the owner's devices reach the Pi,
  on `tcp:22`), how to tag joined nodes, and checks to run from the Pi.
- **Rescan deferred while playing.** Before rescanning, the sync reads
  `GET /api/status`. If music is playing, the status is unknown, or the rescan
  fails, it writes `<music dir>/.pireplace-rescan-pending` and a later run
  rescans when the music is not playing, even if that run changed nothing. The
  sync sends no `Host` header of its own; the control page accepts its own
  address.
- **Retries.** The sync unit restarts on failure every 10 minutes (at most 4
  starts in 6 hours), never on exit status 2, with `TimeoutStartSec=6h`. Each
  download has a 15-minute deadline.
- **Download before delete.** Within a mood, downloads run first, then
  deletions (deletions still happen if some downloads failed). The up-front space
  check is unchanged and skipped when nothing is to be fetched.
- **Sync correctness.** Each mood is isolated (any filesystem error becomes that
  mood's error); download failures, delete failures and playlist items with an
  invalid Id (not 32 hex) are counted and logged separately; the list request
  asks for `fields=MediaSources`; downloads are fsynced, written to a freshly
  created `.part` (never through a link), and a same-name file whose size
  differs from Jellyfin's is downloaded again rather than adopted; extensions
  are capped at 16 bytes and Unicode format characters are stripped from names;
  a corrupt or missing manifest with synced files logs that ownership was reset.
