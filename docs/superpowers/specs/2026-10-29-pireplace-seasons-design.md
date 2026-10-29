# pireplace: seasons (design)

Date: 2026-10-29. Status: approved in conversation; written for planning.

## Intent

The fireplace plays one of several music "moods" (collections), each synced nightly from a
Jellyfin playlist. Today there is one set of four Christmas moods, and the phone control page
re-tints itself per mood. The owner now has Fall and Halloween music and wants to switch the
whole fireplace between **seasons**: pick a season, and the mood buttons show that season's
collections, each with its own colours and icon.

Why it matters to the owner:
- **His choice, never the calendar's.** He plays fall music in summer and winter music in spring
  ("Christmas in July"). Nothing may switch seasons automatically.
- **Set and forget, and works offline.** Every season must be playable at any time without the
  media server reachable (the server is on a separate network, reached over Tailscale).
- **Reusable.** Other people may build their own pireplace; seasons and collections must be
  configuration, not code.

## What the owner decided

- Three seasons for him: **Fall**, **Halloween**, **Winter** (in that order in the picker).
  Halloween is its own season, not part of Fall.
- **Season picker lives in the page title.** Tapping "Fireplace ▾" opens a menu of seasons.
  Picking one switches the mood buttons to that season's collections.
- **Picking a season starts that season's first collection** (approved with the mockup).
- **Open to anyone on the page.** No owner-only gate; the page is only on the local network.
- **One fire video for all seasons for now.** Per-season fire videos are a later project.
- **Music shrinks on the way down:** the nightly sync asks Jellyfin for a smaller audio format
  instead of the original FLAC files (owner chose this over a bigger SD card or syncing only the
  current season). Reason: 12 sets as FLAC would need ~21 GB; the card has ~12 GB free.
- **Looks approved from the mockup** (local, untracked copy:
  `.superpowers/sdd/2026-10-28-pireplace-jellyfin-music-sync/seasons-mockup.html`; every value
  that matters is repeated below).

## Collections and Jellyfin playlists (owner's set)

All playlists are shared read-only with the Jellyfin user `music-streaming`
(Id `4640835a50a94bc69709d00d6f332fac`), which the Pi syncs as.

| Season | Mood folder (name) | Label | Playlist Id | Tracks |
|---|---|---|---|---|
| Fall | harvest | Harvest | 2468e8f94e04ea7cd30bc7e07e2cf723 | 41 |
| Fall | thanksgiving | Thanksgiving | e8fccbd51d0366b8e01c7c559280a23e | 40 |
| Fall | autumn-standards | Autumn Standards | 8168bbe27de41b39641c7decf353feac | 41 |
| Fall | four-seasons | Four Seasons | bff6a0a9d940b5422c8c632c4bea3946 | 20 |
| Halloween | spooky-classics | Spooky Classics | c2a72fb4b5e405f93e0f49073ff5465d | 101 |
| Halloween | monster-party | Monster Party | 99994b20b9e956085b7e570729cb9cf1 | 72 |
| Halloween | graveyard-punk | Graveyard Punk | dd675304017333b0dd2e8cdd8122719f | 45 |
| Halloween | goth-night | Goth Night | ce2cc8851c29a131ba05149e04fceb02 | 28 |
| Winter | classic-swing | Classic Swing | f4b96696936ec335bfeca0c623ad0669 | 105 |
| Winter | modern | Modern | dc8d8d54ecad68b2546b5c7c9b4b4766 | 86 |
| Winter | soul-motown | Soul Motown | 79525d419fccb3bec7c38cef5fe07b11 | 64 |
| Winter | instrumental | Instrumental | 30bf4d0f66600b1d1dd8cded00e25705 | 39 |

Order within a season is the order above; the first one is what a season switch starts.

## Goals

1. Seasons defined in the Pi's config file (rendered by Ansible from a variable with the owner's
   set as the default). A season = id, label, ordered list of mood names.
2. The control page title becomes the season picker; mood buttons show only the current season's
   moods; header shows icon, "Fireplace ▾", and "Season · Mood" subtitle.
3. Season switch = start the first mood of that season that has tracks, exactly as tapping that
   mood does today (loads the folder, unpauses, persists the mood).
4. The current season is the season of the effective mood (no separate stored season): the
   current mood, else `playback.default_mood`, else the first season. On the owner's Pi a fresh
   install therefore starts Classic Swing and shows Winter. (Claude's call.)
5. All twelve moods synced nightly, transcoded, so every season works offline.
6. Fall and Halloween palettes and icons as approved (table below); Winter keeps the existing
   Christmas palettes; unknown moods fall back to the default fire palette.
7. Without any seasons configured, everything behaves exactly as today (no picker, plain title).

## Non-goals

- Automatic, date-based or scheduled season changes.
- Per-season fire videos (later).
- Editing seasons, playlists, palettes or icons from the web page.
- Authentication on the page.
- Changing the Christmas palettes.

## Anti-goals (failure even if it "works")

- A season switch that leaves silence because the chosen season's first mood is empty while
  others in it have music.
- A sync run that deletes the existing FLAC library before the transcoded replacement is fully
  on disk (the fireplace must never end a night with less music than it started with).
- The SD card filled to the point where the journal, state file or Wi-Fi profiles cannot be
  written.
- Guests seeing a season with no music in it as if it were usable.

## Behaviour details

### Config

`config.toml` gains an optional `[seasons]` section. Proposed shape (builder may adjust names,
keeping TOML table order as display order):

```toml
[seasons.fall]
label = "Fall"
moods = ["harvest", "thanksgiving", "autumn-standards", "four-seasons"]

[seasons.halloween]
label = "Halloween"
moods = ["spooky-classics", "monster-party", "graveyard-punk", "goth-night"]

[seasons.winter]
label = "Winter"
moods = ["classic-swing", "modern", "soul-motown", "instrumental"]
```

Validation: season ids and mood names use the same rules as today's mood names; a mood may
belong to at most one season; a season needs at least one mood; label non-empty.
Mood folders on disk that no season lists appear in an automatic last season labelled "Other"
(only when there are any), so a folder added by hand is never invisible. (Builder's call
recorded: chosen over hiding them.)
`[jellyfin.playlists]` stays a flat mood → playlist map, extended with the eight new moods.
`playback.default_mood` stays; the owner's default remains `classic-swing`.

Mood labels: today labels are derived from folder names (`soul-motown` → "Soul Motown"). Keep
that. An optional per-mood label override is allowed but not required.

### API

- `GET /api/status` additionally returns `season` (id of the current season, or the first season
  when no mood is set) and `seasons`: `[{name, label, moods: [mood names]}]`. Each entry in the
  existing `moods` list keeps `{name, label, tracks}`; the page uses `tracks` to grey out empty
  moods as today.
- `POST /api/season` with `{"season": "<id>"}`: start the first mood in that season with tracks.
  Unknown season → 404-style error as unknown moods get today; a season with no tracks at all →
  409 with a readable message ("No music in Halloween yet; it arrives with the next sync").
- Existing endpoints unchanged. The same Host/JSON/CSRF guards apply.

### Page

- Title "Fireplace ▾" is a button (keyboard and screen-reader accessible: `aria-haspopup`,
  `aria-expanded`), opening a small menu listing seasons in config order, current one marked.
  Selecting closes the menu and calls `POST /api/season`. Escape and tapping outside close it.
- A season whose moods have no tracks at all is shown disabled in the menu with "(no music yet)".
- Hidden entirely when only one season exists (including the no-seasons case).
- Subtitle: "Season · Mood label".
- Palettes: same mechanism as the Christmas set (CSS variables per mood, `THEMES` table in JS,
  inline SVG icons, WCAG AA text contrast, 320 px works, no external loads). Snow effect only
  where already used (Winter: modern, instrumental).

New palettes (approved in the mockup; builder may nudge values only to meet contrast):

| mood | bg | header | card | line | ink | accent ("on") | text on accent | accent2 | icon |
|---|---|---|---|---|---|---|---|---|---|
| spooky-classics | #17122a | #221a3a | #1e1833 | #3a2f5c | #efeaff | #f2c14e | #17122a | #c9b8ff | crescent moon and bat |
| monster-party | #140d08 | #24150a | #1f130a | #4a2a12 | #fff1e0 | #ff7a1a | #140d08 | #9be15d | jack-o'-lantern |
| graveyard-punk | #0e0e0e | #1b1b1b | #161616 | #363636 | #f1ede4 | #d7263d | #ffffff | #e8e1cf | skull |
| goth-night | #1a0a14 | #2a1020 | #240e1b | #4a2038 | #f3e8ee | #c9c9d6 | #1a0a14 | #e07aa0 | bat |
| harvest | #2a1a0e | #3a2414 | #33200f | #5a3a1e | #fbefdc | #e0a83a | #2a1a0e | #f0b27a | wheat sheaf |
| thanksgiving | #2b1210 | #3d1a16 | #361613 | #5e2c24 | #fbeee6 | #e8833a | #2b1210 | #f2c879 | slice of pie |
| autumn-standards | #1f1a14 | #2c241b | #282018 | #4a3c2c | #f6ecdd | #d9902f | #1f1a14 | #e8c690 | maple leaf |
| four-seasons | #1c2116 | #283020 | #232b1c | #42503a | #f1efe2 | #c87533 | #1c2116 | #d9c98a | violin |

The mockup's SVG icons are drafts the owner approved; reuse or refine them.

### Sync: transcoding

- Download each track transcoded by Jellyfin to Opus at ~160 kbps in an Ogg (or WebM) container
  instead of the original file. mpv on the Pi plays Opus. Exact endpoint/parameters are the
  builder's call (Jellyfin's universal audio endpoint or `/Audio/{id}/stream` with codec and
  bitrate parameters); verify against Jellyfin 12.2.
- The `music-streaming` user may need Jellyfin's audio transcoding permission
  (`EnableAudioPlaybackTranscoding`). If the server refuses, the sync reports a clear error; the
  owner grants the permission himself (Claude must not change Jellyfin users).
- Failure and integrity rules (Claude's call):
  - Never fall back to downloading the original file. If Jellyfin refuses or ignores the
    transcode (error status, or a response that is not the requested audio format), that track
    fails, the mood reports a clear error, the mood's existing files (FLAC included) stay, and
    the next nightly run retries. Other moods carry on independently, as today.
  - A transcoded download is accepted only if the transfer completed cleanly and its size is
    plausible for the track's duration at the requested bitrate (for example between 25% and
    300% of duration × bitrate); otherwise it is discarded, never renamed into place.
  - Adoption after a crash can no longer compare against Jellyfin's file size for transcoded
    tracks; use the same duration-based plausibility check plus the recorded format.
- Space check: transcoded sizes are not known in advance; estimate from track duration ×
  bitrate (plus margin) instead of the FLAC size.
- **Migration of the existing FLAC library:** the manifest records the audio format/profile a
  file was fetched with. When the configured profile differs from a file's, the track is
  re-fetched in the new format, and the old file is deleted only after the new one is complete
  (download-before-delete, per mood, as today). Expected end state: ~3 GB for all twelve moods,
  down from 9.15 GB for four.
- Keep everything else about the sync: per-mood isolation, manifest, refusal to empty a mood,
  nightly timer, network sandbox.
- Rough sizes for planning: 682 tracks; FLAC averages ~31 MB/track; Opus 160k ≈ 4–5 MB/track.

### Deploy

- Ansible variable for seasons with the owner's set as default; template renders `[seasons]`.
- `pireplace_jf_default_playlists` gains the eight new moods (Ids above); the music folders are
  created for all of them (existing task already loops over the playlist map).
- Nothing in the deploy restarts NetworkManager (existing rule).

## Constraints

- Python stdlib only; Python 3.12 on the laptop, 3.13 on the Pi. Vanilla JS, single static page.
- Pi 4, Raspberry Pi OS Trixie, ~12 GB free on a 29 GB card today; persistent journal capped at
  64 MB with 1 GB kept free.
- Secrets (Jellyfin token) never in logs, git or chat.
- The repo is public; master is protected (PR required).
- Tests must cover: config validation, status/season API, season switch skipping empty moods,
  "Other" season, page palette keys, sync format migration (download-before-delete), space
  estimate, transcoding request parameters (with the fake Jellyfin in tests/fake_jellyfin.py).

## Left to the builder

- Exact config key names (within the shape above), API field names beyond those listed.
- Opus container and exact Jellyfin endpoint, as long as the result plays in mpv and is ~160 kbps.
- How the menu is drawn, as long as it matches the mockup's look and is accessible.
- Whether the "Other" season is labelled "Other" or "More".
