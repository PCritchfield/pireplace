# Jellyfin Music Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A nightly job on the Pi mirrors four Jellyfin playlists into the local mood folders over Tailscale, safely and without touching playback.

**Architecture:** A new stdlib-only `pireplace sync` CLI. It loads an optional `[jellyfin]` config section, lists each playlist through a small Jellyfin client, and reconciles each mood folder against a hidden per-folder manifest (download new, delete removed, never touch unmanaged files). If anything changed, it pokes the local control API to rescan. Ansible installs Tailscale, renders the config and key file, and installs a systemd service and timer. All of it is skipped when no Jellyfin key is configured.

**Tech Stack:** Python ≥ 3.11 stdlib (`urllib.request`, `json`, `tomllib`); pytest; Ansible; systemd timers; Tailscale apt repo.

**Spec:** `docs/superpowers/specs/2026-10-28-pireplace-jellyfin-music-sync-design.md` (builds on `docs/superpowers/specs/2026-10-23-pireplace-modernization-design.md`)

## Global Constraints

- Runtime dependencies: Python standard library only (Python ≥ 3.11).
- Auth header, exact: `Authorization: MediaBrowser Token="<key>"`. Never `X-Emby-Token`.
- List endpoint: `GET {url}/Playlists/{playlist_id}/Items?userId={user_id}`. Download endpoint: `GET {url}/Items/{item_id}/Download`.
- The API key never appears in logs, exception messages, the manifest, or `config.toml`. It is read from `api_key_file` (default `/etc/pireplace/jellyfin.key`, root:pireplace 0640).
- Manifest file: `<mood dir>/.pireplace-sync.json`. In-progress downloads are dot-prefixed temp files in the mood dir. Media discovery already ignores dotfiles.
- Filenames: `<Artist> - <Title> [<first 8 chars of item Id>].<ext>`.
- Space: refuse to start a mood's downloads if the planned new bytes would leave < 2 GB free (2 * 1024**3); stop mid-run if free space would drop below 1 GB (1024**3).
- Safety: a failed playlist listing means no downloads and no deletes for that mood. An empty playlist is an error for a mood with synced files. The sync never deletes files missing from its manifest.
- Rescan only when something was added or removed, via `POST http://127.0.0.1:<web port>/api/rescan` with headers `Host: pireplace.local` and `Content-Type: application/json`, body `{}`. A failed rescan is a warning, not a failure.
- Exit codes: 0 if every mood synced (rescan failure allowed), 1 if any mood had an error, 2 for config errors or missing/unreadable key file.
- Schedule: `pireplace-sync.timer`, `OnCalendar=*-*-* 04:30:00`, `Persistent=true`. The service runs as user `pireplace`, `UMask=0002`, `ReadWritePaths=/srv/pireplace/music`.
- Tailscale: official apt repo, suite from `ansible_distribution_release`, `tailscale up --hostname=pireplace --accept-dns=false --authkey=…` only when not logged in. The auth key is `no_log`.
- With no `pireplace_jellyfin_api_key`: no `[jellyfin]` section, no key file, timer neither installed nor enabled. A plain redeploy is unchanged.
- Known values: Jellyfin URL `http://100.88.8.6:8096`. User Id `1b7ed00cbdcc4a1eb624738191ca4efd`. Playlists:
  - modern → `385d9e4b0602d3d75a00bc6d31dc5aa6`
  - classic-swing → `dfbf231b1aaa92df17ff5248fbde6fbf`
  - soul-motown → `352617a64fc727e324fd1bfa625cb0f6`
  - instrumental → `a371cfe3e0f01be7542358ad2f50f7b5`

## Review Focus

1. **Awkward track metadata.** A missing artist or title, `/`, `:`, `?`, or quotes in titles, unicode, or very long names should still give a valid, unique filename (Task 3).
2. **Bad key or server errors (401/403/500, HTML error pages).** These should give a clear error with no key in it, no deletes, and exit 1 (Tasks 2, 4).
3. **Truncated download** (the connection drops mid-file, or fewer bytes than `Content-Length`). There should be no file left behind, the manifest should be unchanged, and the track should be retried next run (Tasks 2, 3).
4. **Manifest missing or corrupt while music files exist** (for example, a hand-copied mood). Unmanaged files are never deleted. A file already at the exact target name is adopted instead of downloaded again (Task 3).
5. **The same item listed twice in one playlist.** It should download once and appear in the manifest once (Task 3).

---

### Task 0 (owner, manual): prove the API key works over Tailscale

Not a builder task. The owner creates a Jellyfin API key named `pireplace` (Dashboard → API Keys) and runs this on the Pi, keeping the key out of chat:

- [ ] `read -rs K; curl -s -H "Authorization: MediaBrowser Token=\"$K\"" "http://100.88.8.6:8096/Playlists/385d9e4b0602d3d75a00bc6d31dc5aa6/Items?userId=1b7ed00cbdcc4a1eb624738191ca4efd" | head -c 300`. Expected: JSON with `"Items"`.
- [ ] Take one `Id` from that output and run `curl -s -o /tmp/t.flac -w '%{http_code} %{size_download}\n' -H "Authorization: MediaBrowser Token=\"$K\"" http://100.88.8.6:8096/Items/<Id>/Download`. Expected: `200` and several MB. Delete `/tmp/t.flac`.
- If either fails with 401 or 403: make the four playlists public in Jellyfin and retry. The code doesn't change.

---

### Task 1: Jellyfin config section

**Files:**
- Modify: `pireplace/config.py` (add a dataclass and loader; leave `Config` and `_SPEC` untouched)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) JellyfinConfig`: `url: str` (no trailing slash), `user_id: str`, `api_key_file: Path`, `playlists: dict[str, str]` (mood → playlist Id, in file order).
  - `load_jellyfin_config(path: Path) -> JellyfinConfig | None`: returns None when the file has no `[jellyfin]` table, and raises `ConfigError` on invalid values.
  - `read_api_key(cfg: JellyfinConfig) -> str`: stripped file contents. Raises `ConfigError("jellyfin: cannot read api_key_file <path>")` if the file is missing or empty. The message names the path, never the contents.

TOML shape:

```toml
[jellyfin]
url = "http://100.88.8.6:8096"
user_id = "1b7ed00cbdcc4a1eb624738191ca4efd"
api_key_file = "/etc/pireplace/jellyfin.key"   # optional, this is the default

[jellyfin.playlists]
modern = "385d9e4b0602d3d75a00bc6d31dc5aa6"
classic-swing = "dfbf231b1aaa92df17ff5248fbde6fbf"
```

Validation: `url` must start with `http://` or `https://`. `user_id` and every playlist Id must be 32 hex characters. Mood names must match `^[a-z0-9][a-z0-9-]*$`. There must be at least one playlist. Relative `api_key_file` paths resolve the same way as other config paths.

- [ ] **Step 1: Write failing tests**

```python
def test_no_jellyfin_section_returns_none(tmp_path):
    p = tmp_path / "c.toml"; p.write_text("[web]\nport = 80\n")
    assert load_jellyfin_config(p) is None

def test_full_section(tmp_path):
    p = tmp_path / "c.toml"; p.write_text(JF_TOML)  # the TOML shape above
    j = load_jellyfin_config(p)
    assert j.url == "http://100.88.8.6:8096"
    assert list(j.playlists) == ["modern", "classic-swing"]
    assert j.api_key_file == Path("/etc/pireplace/jellyfin.key")

def test_trailing_slash_stripped(tmp_path): ...  # url "http://h:8096/" -> "http://h:8096"

@pytest.mark.parametrize("mutation", [
    ('url = "http://100.88.8.6:8096"', 'url = "ftp://x"'),
    ('user_id = "1b7ed00cbdcc4a1eb624738191ca4efd"', 'user_id = "nothex"'),
    ('modern = "385d9e4b0602d3d75a00bc6d31dc5aa6"', 'modern = "short"'),
    ('modern =', '"Bad Mood!" ='),
])
def test_invalid_values_raise(tmp_path, mutation): ...  # ConfigError

def test_empty_playlists_raises(tmp_path): ...

def test_read_api_key_strips_and_never_leaks(tmp_path):
    k = tmp_path / "k"; k.write_text("s3cret\n")
    assert read_api_key(replace(cfg, api_key_file=k)) == "s3cret"
    k.write_text("")
    with pytest.raises(ConfigError) as e: read_api_key(replace(cfg, api_key_file=k))
    assert "s3cret" not in str(e.value)
```

- [ ] **Step 2: Run, verify fail.** Run `.venv/bin/python -m pytest tests/test_config.py -q`. Expected: FAIL (import error).
- [ ] **Step 3: Implement** `JellyfinConfig`, `load_jellyfin_config`, `read_api_key` in `pireplace/config.py`.
- [ ] **Step 4: Run, verify pass.** Run `make test`. Expected: all pass.
- [ ] **Step 5: Commit.** `git commit -m "feat(config): optional [jellyfin] section and api key file"`

---

### Task 2: Jellyfin client

**Files:**
- Create: `pireplace/jellyfin.py`, `tests/fake_jellyfin.py`
- Test: `tests/test_jellyfin.py`

**Interfaces:**
- Produces:
  - `class JellyfinError(Exception)`: messages carry the HTTP status and endpoint path, never the key or headers.
  - `@dataclass(frozen=True) Track`: `id: str`, `artist: str | None`, `title: str | None`, `container: str | None` (file extension without the dot, lower-case, e.g. `"flac"`), `size: int | None`.
  - `class JellyfinClient(url: str, api_key: str, user_id: str, timeout: float = 30.0)`, with:
    - `playlist_tracks(playlist_id: str) -> list[Track]`: in playlist order, with repeated Ids collapsed to their first occurrence.
    - `download(item_id: str, dest: Path) -> int`: streams to `dest` in 1 MiB chunks and returns the bytes written. Raises `JellyfinError` if the status isn't 200 or the byte count differs from `Content-Length` (when present). On any error, deletes `dest` before raising.
  - `tests/fake_jellyfin.FakeJellyfin`: a context manager running a thread-backed `http.server` on 127.0.0.1:0 with attributes `url`, `key`, `playlists: dict[str, list[dict]]` (raw item JSON), `files: dict[str, bytes]`, `fail: dict[str, int]` (path → status to return), `truncate: set[str]` (item Ids whose download sends half the bytes with the full `Content-Length`), and `requests: list[tuple[str, dict]]` (path and headers seen). It returns 401 unless the Authorization header is exactly `MediaBrowser Token="<key>"`, and 400 when `userId` is missing on the list endpoint.

Track field mapping from Jellyfin item JSON: `Id`. `artist` comes from `AlbumArtist`, falling back to the first entry of `Artists`, then None. `title` comes from `Name`. `container` comes from `Container`, or the first `MediaSources[0].Container`. `size` comes from `MediaSources[0].Size`.

- [ ] **Step 1: Write failing tests**

```python
def test_lists_tracks_with_exact_auth_header(fake):
    fake.playlists["p" * 32] = [item("a" * 32, "Frank Sinatra", "Jingle Bells", "flac", 1234)]
    tracks = client(fake).playlist_tracks("p" * 32)
    assert tracks == [Track("a" * 32, "Frank Sinatra", "Jingle Bells", "flac", 1234)]
    path, headers = fake.requests[-1]
    assert path.startswith(f"/Playlists/{'p'*32}/Items?") and "userId=" in path
    assert headers["Authorization"] == f'MediaBrowser Token="{fake.key}"'
    assert "X-Emby-Token" not in headers

def test_duplicate_ids_collapsed(fake): ...
def test_artist_fallbacks_and_missing_fields(fake): ...  # Artists[0]; then None; missing Container -> MediaSources

@pytest.mark.parametrize("status", [401, 403, 500])
def test_list_errors_raise_without_key(fake, status):
    fake.fail[f"/Playlists/{'p'*32}/Items"] = status
    with pytest.raises(JellyfinError) as e: client(fake).playlist_tracks("p" * 32)
    assert str(status) in str(e.value) and fake.key not in str(e.value)

def test_html_error_page_is_jellyfin_error(fake): ...  # 200 with non-JSON body -> JellyfinError

def test_download_writes_file(fake, tmp_path):
    fake.files["a" * 32] = b"x" * 3_000_000
    dest = tmp_path / ".part"
    assert client(fake).download("a" * 32, dest) == 3_000_000 and dest.read_bytes() == b"x" * 3_000_000

def test_truncated_download_removes_dest(fake, tmp_path):
    fake.files["a" * 32] = b"x" * 1000; fake.truncate.add("a" * 32)
    dest = tmp_path / ".part"
    with pytest.raises(JellyfinError): client(fake).download("a" * 32, dest)
    assert not dest.exists()

def test_unreachable_server_is_jellyfin_error(tmp_path): ...  # url to a closed port, timeout small
```

- [ ] **Step 2: Run, verify fail.** Run `.venv/bin/python -m pytest tests/test_jellyfin.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `pireplace/jellyfin.py` with `urllib.request`. Catch `URLError`, `HTTPError`, `TimeoutError`, `OSError` and `ValueError` (JSON) into `JellyfinError` using `from None`. Implement `tests/fake_jellyfin.py`.
- [ ] **Step 4: Run, verify pass.** Run `make test`. Expected: all pass, no ResourceWarnings.
- [ ] **Step 5: Commit.** `git commit -m "feat: minimal Jellyfin client for playlists and downloads"`

---

### Task 3: Mood folder sync engine

**Files:**
- Create: `pireplace/sync.py`
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: `Track`, `JellyfinError` (Task 2); a client object with `playlist_tracks` and `download` (duck-typed, so tests can pass an in-memory fake).
- Produces:
  - `MANIFEST = ".pireplace-sync.json"`, `MIN_FREE_START = 2 * 1024**3`, `MIN_FREE_RUN = 1024**3`.
  - `track_filename(track: Track) -> str`: `<Artist> - <Title> [<id[:8]>].<ext>`.
    - Replace `/ \ : * ? " < > |` and control characters with `_`, and strip leading dots and spaces.
    - A missing artist becomes `Unknown Artist`, a missing title `Untitled`, and a missing container `audio`.
    - Truncate artist and title so the UTF-8 byte length of the whole name is at most 200.
  - `@dataclass MoodResult`: `mood: str`, `added: int`, `removed: int`, `failed: int`, `error: str | None`.
  - `sync_mood(client, mood: str, playlist_id: str, mood_dir: Path, free_bytes: Callable[[Path], int] = default_free_bytes) -> MoodResult`.
  - `default_free_bytes(path: Path) -> int`: `shutil.disk_usage(path).free`.

`sync_mood` algorithm, in order:
1. `mkdir(parents=True, exist_ok=True)` the mood dir.
2. Load the manifest: `{"items": {item_id: filename}}`. If it's missing or corrupt, use an empty dict.
3. List the tracks. If that raises `JellyfinError`, return `MoodResult(error=…)` having touched nothing.
4. If the list is empty and the manifest isn't, return an error saying `playlist is empty; refusing to delete synced tracks` having touched nothing.
5. Work out what to delete (manifest Ids not in the list) and what to fetch (listed Ids not in the manifest). For each to-fetch track, if a file already exists at `track_filename(track)`, adopt it into the manifest without downloading it.
6. Check space: the sum of `size` (treat None as 0) of the tracks still to fetch, plus `MIN_FREE_START`, must be ≤ `free_bytes(mood_dir)`. Otherwise return an error saying `not enough space` with no downloads. Deletions in this mood are also skipped, so the error leaves the folder unchanged.
7. Delete the to-delete files, ignoring files already gone, and drop them from the manifest.
8. Download each to-fetch track to `mood_dir / f".{filename}.part"`, then `os.replace` it into the final name. Before each download, stop if `free_bytes() - (size or 0) < MIN_FREE_RUN`, counting the remaining tracks as failed. A `JellyfinError` on one track counts it as failed and moves on.
9. Write the manifest atomically (temp file plus `os.replace`) whenever anything changed.
10. Set `error` if `failed > 0` (`"N downloads failed"`).

- [ ] **Step 1: Write failing tests** (with an in-memory `FakeClient` defined in the test file):

```python
def test_first_sync_downloads_all_and_writes_manifest(tmp_path): ...  # added == 3, files exist, manifest has 3 ids
def test_second_sync_is_noop(tmp_path): ...                           # added == removed == 0, no download calls
def test_removed_from_playlist_is_deleted(tmp_path): ...
def test_manual_files_survive(tmp_path):
    (tmp_path / "modern").mkdir(); (tmp_path / "modern" / "my rip.flac").write_bytes(b"x")
    sync_mood(FakeClient(tracks=[]), ...)  # also exercises the empty-list rule on an empty manifest: no error
    assert (tmp_path / "modern" / "my rip.flac").exists()
def test_list_error_touches_nothing(tmp_path): ...       # pre-existing manifest + files unchanged, error set
def test_empty_playlist_with_synced_files_is_error(tmp_path): ...
def test_failed_download_counts_and_retries_next_run(tmp_path): ...  # no .part left; second run downloads it
def test_duplicate_ids_download_once(tmp_path): ...
def test_existing_file_at_target_name_is_adopted(tmp_path): ...       # corrupt manifest + file present -> no download
def test_space_check_refuses_before_any_change(tmp_path): ...         # free_bytes small -> error, nothing deleted/added
def test_running_low_on_space_stops_midway(tmp_path): ...
@pytest.mark.parametrize("artist,title,expect", [
    ("AC/DC", 'What "Child"?', 'AC_DC - What _Child__ [aaaaaaaa].flac'),
    (None, None, "Unknown Artist - Untitled [aaaaaaaa].flac"),
    ("Bublé", "It's Beginning…", "Bublé - It's Beginning… [aaaaaaaa].flac"),
    ("..x", "y", "x - y [aaaaaaaa].flac"),
])
def test_track_filename(artist, title, expect): ...
def test_track_filename_long_names_fit_200_bytes(): ...
def test_two_tracks_same_name_different_ids_both_kept(tmp_path): ...  # id suffix makes them distinct
```

- [ ] **Step 2: Run, verify fail.** Run `.venv/bin/python -m pytest tests/test_sync.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `pireplace/sync.py`.
- [ ] **Step 4: Run, verify pass.** Run `make test`. Expected: all pass.
- [ ] **Step 5: Commit.** `git commit -m "feat: manifest-based mood folder sync"`

---

### Task 4: `pireplace sync` command and rescan

**Files:**
- Modify: `pireplace/__main__.py` (add the `sync` subcommand)
- Modify: `pireplace/sync.py` (add `run_sync` and `trigger_rescan`)
- Test: `tests/test_sync.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: `load_config` / `Config` (existing); `load_jellyfin_config`, `read_api_key` (Task 1); `JellyfinClient` (Task 2); `sync_mood` (Task 3).
- Produces:
  - `trigger_rescan(port: int, timeout: float = 10.0) -> bool`: POST as in Global Constraints. Returns False on any error and logs a warning.
  - `run_sync(config: Config, jf: JellyfinConfig, client, rescan: Callable[[], bool]) -> int`:
    - Runs `sync_mood` for each mood in `jf.playlists` order, into `config.music_dir / mood`.
    - Logs one INFO line per mood: `mood=<m> added=<n> removed=<n> failed=<n>`, plus ` error=<msg>` when set.
    - Calls `rescan()` once if any mood added or removed files.
    - Returns 0 if no mood has an error, else 1.
  - CLI: `python -m pireplace sync --config PATH`.
    - Exit 2 with a one-line stderr message on `ConfigError`, a missing `[jellyfin]` section (`no [jellyfin] section in <path>`), or an unreadable key.
    - Otherwise it builds the client and returns `run_sync(...)`.

- [ ] **Step 1: Write failing tests**

```python
def test_run_sync_rescans_only_when_changed(tmp_path): ...       # second call: rescan not called
def test_run_sync_exit_1_when_any_mood_errors(tmp_path): ...     # other moods still synced
def test_run_sync_rescan_failure_still_exit_0(tmp_path): ...     # rescan returns False -> 0
def test_trigger_rescan_sends_required_headers():                 # against a tiny local HTTPServer
    ...  # asserts path /api/rescan, Host: pireplace.local, Content-Type: application/json, body {}
def test_trigger_rescan_unreachable_returns_false(): ...
def test_sync_cli_no_jellyfin_section_exits_2(tmp_path, capsys): ...
def test_sync_cli_missing_key_exits_2_without_leaking(tmp_path, capsys): ...
def test_sync_cli_end_to_end_with_fake_jellyfin(tmp_path, fake):  # config.toml pointing at fake.url, key file
    assert main(["sync", "--config", str(cfg)]) == 0
    assert any((tmp_path / "music" / "modern").glob("*.flac"))
def test_api_key_never_logged(tmp_path, fake, caplog): ...        # wrong key -> exit 1, key absent from caplog
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, verify pass.** Run `make test`.
- [ ] **Step 5: Commit.** `git commit -m "feat: pireplace sync command with rescan after changes"`

---

### Task 5: Provisioning, Tailscale and docs

**Files:**
- Create: `deploy/systemd/pireplace-sync.service`, `deploy/systemd/pireplace-sync.timer`
- Modify: `deploy/ansible/site.yml`, `deploy/ansible/templates/config.toml.j2`, `deploy/ansible/inventory.example.ini`, `Makefile` (lint covers the new units), `README.md`
- Test: `tests/test_deploy.py`

**Interfaces:**
- Consumes: `python3 -m pireplace sync --config /etc/pireplace/config.toml` (Task 4).
- New inventory vars, all optional:
  - `pireplace_jellyfin_api_key`
  - `pireplace_jellyfin_url` (default `http://100.88.8.6:8096`)
  - `pireplace_jellyfin_user_id` (default `1b7ed00cbdcc4a1eb624738191ca4efd`)
  - `pireplace_jellyfin_playlists` (default: the four-mood map in Global Constraints)
  - `pireplace_tailscale_authkey`

Playbook changes:
- **Mood folders:** created from `pireplace_jellyfin_playlists.keys()` when a Jellyfin key is set, otherwise from the existing `pireplace_moods`.
- **`[jellyfin]` config section:** the template renders it only when `pireplace_jellyfin_api_key` is defined and non-empty.
- **Key file:** `/etc/pireplace/jellyfin.key`, mode 0640, owner root, group pireplace, `no_log: true`. Created only when the key is set; removed (`state: absent`) when it isn't.
- **Sync units:** installed and the timer enabled and started only when the key is set. Otherwise the timer is disabled, stopped and removed if present.
- **`pireplace-sync.service`:**
  - `Type=oneshot`, `User=pireplace`, `Group=pireplace`, `UMask=0002`
  - `ExecStart=/usr/bin/python3 -m pireplace sync --config /etc/pireplace/config.toml`
  - `ProtectSystem=strict`, `ReadWritePaths=/srv/pireplace/music`, `ProtectHome=yes`
  - `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, plus the same hardening set as pireplace-control except `UMask`
  - `Nice=10`, `IOSchedulingClass=idle`
  - **No** `RestartPreventExitStatus` and no `Restart`.
- **`pireplace-sync.timer`:** `OnCalendar=*-*-* 04:30:00`, `Persistent=true`, `RandomizedDelaySec=5min`, `WantedBy=timers.target`.
- **Tailscale:**
  - Install the signing key at `/usr/share/keyrings/tailscale-archive-keyring.gpg` from `https://pkgs.tailscale.com/stable/debian/{{ ansible_distribution_release }}.noarmor.gpg`.
  - Add the apt source `deb [signed-by=…] https://pkgs.tailscale.com/stable/debian {{ ansible_distribution_release }} main`.
  - Install the `tailscale` package and enable and start `tailscaled`.
  - Run `tailscale status --json`, parse `BackendState`. Only if it isn't `Running` and `pireplace_tailscale_authkey` is set, run `tailscale up --hostname=pireplace --accept-dns=false --authkey=<key>` with `no_log: true`.
  - If it isn't running and no auth key is set, print a debug note telling the owner to run `sudo tailscale up --hostname=pireplace --accept-dns=false`.
  - These steps must be no-ops on the current Pi, where Tailscale was installed by hand with the same repo file and is already logged in.
- **README section "Music from Jellyfin":**
  - Create the API key; where to put the secrets (inventory or `secrets.yml`).
  - The server's Tailscale IP and how to find it.
  - `sudo systemctl start pireplace-sync` and `sudo journalctl -u pireplace-sync`.
  - That the first sync may take about an hour over a relay.
  - How to add or retire a mood.
  - Disabling Tailscale key expiry for appliances.
  - A ready-to-paste tailnet ACL fragment restricting `tag:pireplace` to the Jellyfin host on port 8096:
    ```json
    {"tagOwners": {"tag:pireplace": ["autogroup:admin"]},
     "grants": [{"src": ["tag:pireplace"], "dst": ["100.88.8.6"], "ip": ["tcp:8096"]}]}
    ```
    Note that it is fragment-only and must be merged by the owner.
  - The "Before the party" checklist gets: Task 0's checks, then the first sync, the four moods visible on the phone page, a second sync reporting no changes, and a sync with Tailscale stopped failing cleanly while music keeps playing.

- [ ] **Step 1: Write failing tests** in `tests/test_deploy.py`:
  - the sync service has `Type=oneshot`, `User=pireplace`, `UMask=0002`, `ReadWritePaths=/srv/pireplace/music` and the exact `ExecStart`
  - the timer has `OnCalendar=*-*-* 04:30:00` and `Persistent=true`
  - the key-file task is `no_log` with mode 0640
  - the `tailscale up` task is `no_log`, contains `--accept-dns=false`, and is guarded by `BackendState`
  - the sync-unit, key and timer tasks are all conditional on `pireplace_jellyfin_api_key`
  - rendering the template with and without the key (via `ansible localhost -m template`, or jinja2 from the venv): with the key, `tomllib` parses a `[jellyfin]` section that `load_jellyfin_config` accepts; without it, no `[jellyfin]` table.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the units, playbook, template, inventory example and README.
- [ ] **Step 4: Verify.** Run `make test` and `make lint` (`systemd-analyze verify` on both new units). Also run a real local `ansible-playbook --syntax-check`. Expected: all pass.
- [ ] **Step 5: Commit.** `git commit -m "feat(deploy): Tailscale, Jellyfin sync service and timer"`

---

### Task 6 (owner + controller): on the Pi

- [ ] The owner puts `pireplace_jellyfin_api_key` in `deploy/ansible/inventory.ini` (or `secrets.yml`) and runs `make deploy INVENTORY=deploy/ansible/inventory.ini ANSIBLE_ARGS=-K`. Expected: failed=0, and the timer is listed by `systemctl list-timers pireplace-sync.timer`.
- [ ] Run `sudo systemctl start pireplace-sync` and follow it with `sudo journalctl -u pireplace-sync -f`. Expected: four `mood=… added=…` lines, exit 0.
- [ ] The phone page lists four moods; switching to Soul & Motown plays it.
- [ ] Rerun the sync. Expected: added=0 removed=0, and no rescan.
- [ ] Run `sudo systemctl stop tailscaled; sudo systemctl start pireplace-sync`. Expected: exit 1 with clean errors, music unchanged. Then start `tailscaled` again.
