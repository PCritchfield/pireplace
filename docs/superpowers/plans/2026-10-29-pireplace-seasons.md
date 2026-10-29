# Seasons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let anyone on the control page switch the fireplace between configured seasons (Fall, Halloween, Winter), each with its own collections, palettes and icons, all synced nightly as smaller Opus files so every season works offline.

**Architecture:** Seasons are a new optional `[seasons]` section in `config.toml`, parsed into `Config.seasons`. The controller derives the current season from the effective mood and adds a `set_season`; the web API exposes `season`/`seasons` and `POST /api/season`. The sync asks Jellyfin for Opus transcodes, records a per-file audio profile in the manifest, and migrates old FLAC files download-before-delete. The static page turns its title into a season menu and gains eight palettes.

**Tech Stack:** Python 3.12/3.13 stdlib, pytest, vanilla JS + inline CSS/SVG, Ansible + Jinja2, systemd.

**Spec:** `docs/superpowers/specs/2026-10-29-pireplace-seasons-design.md`

## Global Constraints

- Python stdlib only; Python 3.12 (laptop) and 3.13 (Pi). Single static page, vanilla JS, no external loads.
- Mood/season names match `[a-z0-9][a-z0-9-]*` (existing `_MOOD_NAME`).
- Seasons are config, never date-driven. Picking a season starts its first mood that has tracks.
- Without `[seasons]` in the config everything behaves exactly as today (no picker, plain title).
- Never fall back to downloading the original file when a transcode is requested.
- Download-before-delete per mood; a mood never ends a run with fewer playable tracks than it started.
- Jellyfin token never in logs, errors, git or chat. Existing request/redirect/proxy protections stay.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (or the implementer harness's own model line).
- Tests: `.venv/bin/python -m pytest -q`; current baseline 462 passed, 1 skipped on master d84e048.

## Review Focus

1. A season whose first mood is empty but a later one has tracks: the switch must start the later one, not go silent. (Task 3 test `test_set_season_skips_empty_first_mood`.)
2. A transcode response that is an HTML proxy/login page or JSON error with HTTP 200: must be rejected, never renamed into place. (Task 2 test `test_transcoded_download_rejects_non_ogg_body`.)
3. A connection dropped mid-transcode (no Content-Length): truncated file must be rejected by the duration plausibility check. (Task 4 test `test_truncated_transcode_is_not_accepted`.)
4. Migration on a nearly full card: if a mood's Opus copies cannot all fit, the FLAC files they would replace stay. (Task 4 test `test_migration_keeps_flac_when_space_runs_out`.)
5. Saved mood that has since been removed from every season (or a folder no season lists): status must still report a season ("Other") and the page must still show and highlight it. (Task 3 test `test_status_puts_unlisted_mood_in_other_season`.)

---

### Task 1: Config — seasons and audio profile

**Files:**
- Modify: `pireplace/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Season: name: str; label: str; moods: tuple[str, ...]`
  - `Config.seasons: tuple[Season, ...] = ()` (new field with default, after `allowed_hosts`)
  - `JellyfinConfig.audio_format: str = "opus"` (`"opus"` or `"original"`), `JellyfinConfig.audio_bitrate_kbps: int = 160` (range 64–320)
  - `def audio_profile(jf: JellyfinConfig) -> str` → `"opus-160"` or `"original"`

- [ ] **Step 1: Write the failing tests** in `tests/test_config.py`:
  - `test_seasons_parsed_in_file_order`: config with `[seasons.fall] label="Fall" moods=["harvest","thanksgiving"]` then `[seasons.winter] label="Winter" moods=["modern"]` → `cfg.seasons == (Season("fall","Fall",("harvest","thanksgiving")), Season("winter","Winter",("modern",)))`.
  - `test_no_seasons_section_gives_empty_tuple`: existing minimal config → `cfg.seasons == ()`.
  - `test_season_rejects_mood_in_two_seasons`: same mood listed twice across seasons → `ConfigError` mentioning the mood name.
  - `test_season_rejects_empty_moods_and_label`: `moods=[]` → `ConfigError`; `label=""` → `ConfigError`.
  - `test_season_rejects_bad_names`: season id `"Fall!"` and mood `"Bad Name"` → `ConfigError`.
  - `test_jellyfin_audio_defaults_to_opus_160`: `[jellyfin]` without audio keys → `audio_format == "opus"`, `audio_bitrate_kbps == 160`, `audio_profile(jf) == "opus-160"`.
  - `test_jellyfin_audio_original_and_range`: `audio_format = "original"` → profile `"original"`; `audio_format="mp3"` → `ConfigError`; `audio_bitrate_kbps = 32` → `ConfigError`.
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_config.py -q` — new tests FAIL.
- [ ] **Step 3:** Implement in `pireplace/config.py`: parse `data.get("seasons", {})` in `load_config` (table of tables, TOML order kept); reuse `_MOOD_NAME` for season ids and moods; parse `audio_format`/`audio_bitrate_kbps` in `load_jellyfin_config` with `_coerce`.
- [ ] **Step 4:** Run the full suite — PASS (462 + new).
- [ ] **Step 5: Commit** `feat(config): seasons and Jellyfin audio profile`.

### Task 2: Jellyfin client — durations and transcoded downloads

**Files:**
- Modify: `pireplace/jellyfin.py`, `tests/fake_jellyfin.py`
- Test: `tests/test_jellyfin.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `Track.duration_s: float | None = None` (new last field; from item `RunTimeTicks` / 10_000_000; non-int or ≤0 → None)
  - `JellyfinClient.download_transcoded(self, item_id: str, dest: Path, bitrate_kbps: int) -> int`
  - fake: `FakeJellyfin.transcodes: dict[str, bytes]`, `FakeJellyfin.transcode_type: dict[str, str]` (Content-Type override), route `GET /Audio/{id}/stream.ogg`.

Request: `GET /Audio/{id}/stream.ogg?audioCodec=opus&audioBitRate={kbps*1000}&audioChannels=2&static=false&userId={user_id}`, same Authorization header, opener, deadline and `read1` loop as `download`. Accept only if: HTTP 200; `Content-Type` starts with `audio/` (e.g. `audio/ogg`); first 4 bytes are `b"OggS"`; if `Content-Length` is present it matches. On any failure `dest` is removed and `JellyfinError` raised with a message naming the path, never the query string or headers. Data fsynced before return.

- [ ] **Step 1: Write the failing tests:**
  - `test_track_duration_from_runtime_ticks`: item `RunTimeTicks: 2_400_000_000` → `duration_s == 240.0`; missing → `None`; `True` → `None`.
  - `test_transcoded_download_writes_ogg`: fake `transcodes[id] = b"OggS" + b"x"*1000` → file written, returns 1004; the request path is `/Audio/<id>/stream.ogg` and its query has `audioCodec=opus`, `audioBitRate=160000`, `static=false`.
  - `test_transcoded_download_rejects_non_ogg_body`: HTTP 200, `text/html` body → `JellyfinError`, `dest` absent. Also `audio/ogg` type but body `b"<html>"` → `JellyfinError`, `dest` absent.
  - `test_transcoded_download_http_error_has_no_secrets`: fake `fail["/Audio/<id>/stream.ogg"] = 403` → `JellyfinError` whose `str()` contains `403` and not the key and not `audioBitRate`.
  - `test_transcoded_download_never_follows_redirect`: mirrors the existing redirect test for `/Download`.
- [ ] **Step 2:** Run `tests/test_jellyfin.py` — FAIL.
- [ ] **Step 3:** Implement; extend the fake's `do_GET` with the `/Audio/<id>/stream.ogg` route (honours `fail`, `html`, `truncate`, `trickle` like `/Download`; sends no Content-Length for transcodes, as Jellyfin streams).
- [ ] **Step 4:** Full suite PASS.
- [ ] **Step 5: Commit** `feat(jellyfin): track durations and Opus transcoded downloads`.

### Task 3: Controller and API — seasons

**Files:**
- Modify: `pireplace/controller.py`, `pireplace/web.py`
- Test: `tests/test_controller.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `Config.seasons`, `Season` (Task 1).
- Produces:
  - `class UnknownSeason(Exception)`, `class EmptySeason(Exception)` in `controller.py`
  - `OTHER_SEASON = "other"`, label `"Other"`
  - `Fireplace.seasons(self, moods: dict[str, list[Path]]) -> list[dict]` → `[{"name","label","moods":[...]}]`: configured seasons in order (moods listed even if their folder is missing), plus `{"name":"other","label":"Other","moods":[unlisted folders with tracks]}` appended only when non-empty. Empty list when `config.seasons == ()`.
  - `Fireplace.status()` gains `"season": str | None` (season of the effective mood: current mood, else `config.default_mood`, else first season; `None` when there are no seasons) and `"seasons": list[dict]`.
  - `Fireplace.set_season(self, name: str) -> None`: first mood of the season with tracks → `set_mood(that)`. Unknown name → `UnknownSeason`; no mood with tracks → `EmptySeason(label)`.
  - `POST /api/season` body `{"season": str}`; `UnknownSeason` → 400 `{"error": "unknown season: <name>"}`; `EmptySeason` → 409 `{"error": "No music in <Label> yet; it arrives with the next sync."}`.

- [ ] **Step 1: Write the failing tests** (use `env_factory` with a music tree and a config carrying seasons):
  - `test_status_without_seasons_is_unchanged`: no seasons → `status()["seasons"] == []`, `status()["season"] is None`, other keys as before.
  - `test_status_reports_season_of_current_mood`: seasons fall=[harvest], winter=[modern], mood `modern` → `season == "winter"`; `seasons` lists both in config order.
  - `test_status_season_follows_default_mood_when_none_saved`: no saved mood, `default_mood="modern"` → `season == "winter"`.
  - `test_status_puts_unlisted_mood_in_other_season`: folder `extra` with tracks, in no season, saved as mood → last entry `{"name":"other","label":"Other","moods":["extra"]}`, `season == "other"`.
  - `test_set_season_starts_first_mood`: fall=[harvest, thanksgiving] both with tracks → `set_season("fall")` sets mood `harvest`, music unpaused, state saved.
  - `test_set_season_skips_empty_first_mood`: `harvest` folder missing/empty, `thanksgiving` has tracks → mood `thanksgiving`.
  - `test_set_season_unknown_and_empty`: `"spring"` → `UnknownSeason`; season whose moods have no tracks → `EmptySeason`.
  - web: `test_post_season_switches`, `test_post_season_unknown_is_400`, `test_post_season_empty_is_409_with_message`, `test_post_season_requires_json` (415 like other POSTs).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement; reuse `find_moods` once per call; keep `status()` exception handling as is.
- [ ] **Step 4:** Full suite PASS.
- [ ] **Step 5: Commit** `feat(controller): seasons in status and POST /api/season`.

### Task 4: Sync — transcode, audio profile in the manifest, FLAC migration

**Files:**
- Modify: `pireplace/sync.py`, `pireplace/__main__.py` (pass the profile/bitrate through)
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: `audio_profile`, `JellyfinConfig.audio_format/audio_bitrate_kbps` (Task 1); `Track.duration_s`, `download_transcoded` (Task 2).
- Produces:
  - Manifest format: `{"items": {id: name}, "profiles": {id: profile}}`; a missing `"profiles"` key or entry means `"original"` (old manifests load unchanged).
  - `track_filename(track: Track, ext: str | None = None) -> str` (`ext` overrides the container; transcodes use `"ogg"`).
  - `estimate_bytes(track: Track, bitrate_kbps: int) -> int` = `ceil(duration_s * bitrate_kbps * 1000 / 8 * 1.5)`; unknown duration → `10 * 1024**2`.
  - `plausible_size(size: int, duration_s: float | None, bitrate_kbps: int) -> bool`: True when duration unknown and size > 0; else `0.25 * expected <= size <= 3 * expected` where `expected = duration_s * bitrate_kbps * 1000 / 8`.
  - `sync_mood(client, mood, playlist_id, mood_dir, free_bytes=..., profile="original", bitrate_kbps=160) -> MoodResult`; `run_sync` passes `audio_profile(jf)` and `jf.audio_bitrate_kbps`.

Rules:
- Profile `"original"`: behaviour exactly as today (`client.download`, size from `Track.size`).
- Profile `"opus-N"`: a track is "current" when its manifest profile equals the wanted profile. Tracks not in the manifest, or with a different profile, go to `to_fetch` with name `track_filename(t, "ogg")`, fetched with `download_transcoded`, accepted only if `plausible_size`; otherwise the `.part` is removed and counted as failed.
- A re-fetched (migrated) track's old file is deleted only after its new file is in place and recorded; if the new one fails, the old name and profile stay in the manifest.
- Space: the start-of-run check and the per-file check use `estimate_bytes` instead of `Track.size` for transcoded fetches. Migration downloads run before any deletion of the old files.
- Adoption after a crash for transcoded names: an existing `<name>.ogg` is adopted only if it starts with `OggS` and `plausible_size`; record it with the wanted profile.

- [ ] **Step 1: Write the failing tests** (FakeJellyfin with `transcodes`):
  - `test_old_manifest_without_profiles_is_original`: load an old-format manifest → items intact; with profile `"original"` nothing is re-fetched.
  - `test_opus_profile_downloads_ogg_and_records_profile`: empty mood, 2 tracks with `RunTimeTicks` → two `*.ogg` files, manifest `profiles` both `"opus-160"`.
  - `test_flac_migrates_to_ogg_download_before_delete`: mood with 2 FLAC files in an old manifest → after run: two `.ogg`, no `.flac`, manifest names `.ogg`; assert (via a client wrapper that records order) every `download_transcoded` happened before the first FLAC unlink.
  - `test_failed_transcode_keeps_flac`: one track's transcode returns 403 → its FLAC file and manifest entry (profile original) stay; the other migrates; `result.failed == 1`, `result.error` set.
  - `test_truncated_transcode_is_not_accepted`: transcode body `OggS` + far too few bytes for a 240 s track → no `.ogg` in place, FLAC kept, failed counted.
  - `test_migration_keeps_flac_when_space_runs_out`: `free_bytes` stub that drops below `MIN_FREE_RUN` after one download → first track migrated, the rest keep FLAC, no FLAC deleted for un-migrated tracks.
  - `test_estimate_and_plausible_size`: `estimate_bytes(Track(..., duration_s=240), 160) == ceil(240*20000*1.5)`; unknown → 10 MiB; `plausible_size` boundaries at 0.25× and 3×.
  - `test_original_profile_unchanged`: profile `"original"` path still uses `/Items/{id}/Download` and `Track.size` (existing tests keep passing unchanged).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full suite PASS.
- [ ] **Step 5: Commit** `feat(sync): Opus transcodes with per-file profile and FLAC migration`.

### Task 5: Page — season picker and Fall/Halloween palettes

**Files:**
- Modify: `pireplace/static/index.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `status.season`, `status.seasons` (Task 3), `POST /api/season`.
- Produces: none for later tasks.

Requirements (spec "Page" section, palette table verbatim): title button `Fireplace ▾` with `aria-haspopup="menu"` and `aria-expanded`; menu items in `seasons` order, current marked `aria-checked`/pressed; a season none of whose moods has tracks is disabled with "(no music yet)"; Escape and outside tap close; selection posts `{season}` and renders the returned status. Mood grid shows only the current season's moods (all moods when `seasons` is empty). Subtitle `"<Season label> · <Mood label>"` (mood label only when no seasons). Picker hidden when `seasons.length <= 1`. Add the eight palettes and icons from the spec table to the existing `THEMES` mechanism (CSS variable blocks + JS entries); keep Christmas palettes unchanged. Text contrast ≥ 4.5:1 per palette (nudge only if needed and list changes in the report). Works at 320 px.

- [ ] **Step 1: Write the failing tests** (INDEX_HTML text checks, like the existing palette tests):
  - `test_index_has_all_twelve_palette_keys_in_themes`: the eight new mood names plus the four Christmas ones appear as keys inside the `THEMES` table.
  - `test_index_has_season_picker_markup`: contains `aria-haspopup`, `aria-expanded`, and `"/api/season"`.
  - `test_index_still_has_no_external_loads` (existing) keeps passing.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. Verify visually with a stubbed `/api/status` (the controller session can screenshot); report contrast ratios for each new palette.
- [ ] **Step 4:** Full suite PASS.
- [ ] **Step 5: Commit** `feat(web): season picker and Fall/Halloween palettes`.

### Task 6: Deploy and docs

**Files:**
- Modify: `deploy/ansible/site.yml`, `deploy/ansible/templates/config.toml.j2`, `deploy/ansible/inventory.example.ini` (comment only), `README.md`
- Test: `tests/test_deploy.py`

**Interfaces:**
- Consumes: config shape from Task 1.

- `pireplace_jf_default_playlists` gains the eight moods with the spec's Ids (order: the spec table).
- New play var `pireplace_seasons_default` (list of `{name, label, moods}` in order fall, halloween, winter with the spec's moods) and `pireplace_seasons: "{{ pireplace_seasons_custom | default(pireplace_seasons_default) }}"` (inventory override through the indirection, like `pireplace_jf_*`).
- Template renders `[seasons.<name>]` blocks with `label` and `moods` via `to_json`, after `[playback]`, only when the list is non-empty.
- Template renders `audio_format = "opus"` and `audio_bitrate_kbps = 160` in `[jellyfin]` (overridable vars `pireplace_jellyfin_audio_format`, `pireplace_jellyfin_audio_bitrate_kbps`).
- README: "Seasons" section (how to define seasons, that picking one starts its first collection, that the Jellyfin user needs audio transcoding allowed, one-time migration from FLAC to Opus and expected size ~3 GB).

- [ ] **Step 1: Write the failing tests:** `test_template_renders_seasons_in_order`, `test_template_omits_seasons_when_empty`, `test_default_playlists_include_fall_and_halloween` (the eight Ids), `test_template_renders_audio_profile` (render the template with Jinja2 as the existing deploy tests do and parse with `tomllib`, then `load_config`/`load_jellyfin_config` on the result).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement; `ansible-playbook --syntax-check deploy/ansible/site.yml` passes.
- [ ] **Step 4:** Full suite PASS.
- [ ] **Step 5: Commit** `feat(deploy): seasons, Fall and Halloween playlists, Opus sync`.

### After the tasks (controller, not a subagent)

- Before deploy, verify the transcode request against the real server from the Pi (read-only): `curl` the `/Audio/<one Id>/stream.ogg?...` URL for one short track as the music-streaming user, check HTTP 200, `Content-Type: audio/ogg`, `OggS` header, plausible size. If refused (permission), stop and tell the owner which Jellyfin setting to enable.
- Owner deploys; owner starts one sync run; watch sizes per mood and confirm FLAC → Opus migration and final disk use.
