# Pireplace Modernization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the omxplayer/pianobar/Patiobar scripts with a self-contained Raspberry Pi 4 appliance: two supervised mpv players (log video, music by mood folder), a stdlib-only Python control service with a phone web page, Wi-Fi onboarding, Ansible provisioning, and a laptop dev mode.

**Architecture:** Two long-running `mpv --idle` processes (video, music) expose JSON IPC sockets. A single Python process (`python -m pireplace serve`) discovers media, loads playlists into the players, persists mood/volume state, and serves a JSON API plus one static HTML page. systemd supervises everything; Ansible provisions a freshly flashed card. The same code runs on a Linux laptop in windows via `make dev`.

**Tech Stack:** Python ≥ 3.11 standard library only at runtime (`tomllib`, `http.server`, `socket`, `json`); pytest for tests; mpv; PipeWire; NetworkManager (`nmcli`); systemd; Ansible; ffmpeg and qrencode for dev/print tooling only.

**Spec:** `docs/superpowers/specs/2026-10-23-pireplace-modernization-design.md`

## Global Constraints

- Target hardware: Raspberry Pi 4 only. Pi 3 is documented as untested.
- OS: current Raspberry Pi OS Lite, 64-bit, no desktop environment.
- Runtime Python dependencies: none beyond the standard library (Python ≥ 3.11). Dev/test dependencies (pytest) live in `pyproject.toml` optional `dev` extra only.
- No streaming-service code, no third-party credentials anywhere in the repo or on the device.
- Media is never committed. On the Pi: videos in `/srv/pireplace/videos`, music in `/srv/pireplace/music/<mood>/`.
- Moods are the immediate subfolders of the music dir; a mood's tracks are all audio files anywhere beneath it (CD rips are nested `Artist/Album/track`). Mood label = folder name with `-`/`_` → space, title-cased (`classic-swing` → `Classic Swing`).
- Default moods created at provision: `modern`, `classic-swing`.
- Video extensions: `.mp4 .mkv .webm .mov .m4v`. Audio extensions: `.mp3 .flac .m4a .aac .ogg .opus .wav`. Case-insensitive. Hidden files and folders (leading `.`) ignored.
- Volumes are integers 0–100.
- Control page: `http://pireplace.local` on port 80, no auth. Nothing is ever drawn on the TV except the fire.
- Fire keeps playing regardless of network state or controller crashes.
- Wi-Fi hotspot SSID `pireplace-setup`; appears only after 300 s with no connection; while up, retries known networks every 300 s.
- Supervision: systemd with `Restart=always`. No `screen`, no `ps | grep`, no `pkill`.
- Deadline: running on the Pi by 2026-11-26.

## Review Focus

1. **Nested and messy music folders** — CD rips in `Artist/Album/` subfolders, plus `cover.jpg`, `.DS_Store`, `Thumbs.db`, `._` files: tracks found recursively, junk ignored. Test in Task 2.
2. **Odd filenames** — spaces, apostrophes, quotes, unicode (`Bublé – It's Beginning….flac`): pass through to mpv intact. Test in Task 3 (IPC round-trip) and Task 4.
3. **Player not up yet or restarting** — controller/web called while a socket is missing: status reports the player unavailable, API returns 503, nothing crashes; startup retries. Test in Tasks 4 and 5.
4. **Bad API input** — unknown mood, volume `150`/`-3`/`"loud"`, malformed JSON, missing fields: 400 with `{"error": ...}`, state unchanged. Test in Task 5.
5. **Missing or corrupt state file / saved mood that no longer exists** — fall back to config defaults, then first available mood. Test in Tasks 1 and 4.

---

## File Structure

```
pyproject.toml                     package metadata, pytest config, dev extra
Makefile                           test, dev, card, lint, deploy targets
pireplace/__init__.py
pireplace/__main__.py              CLI: serve | wifi-watch
pireplace/config.py                Config dataclass, load_config()
pireplace/state.py                 State dataclass, StateStore (JSON, atomic)
pireplace/library.py               find_videos(), find_moods(), mood_label()
pireplace/mpv.py                   MpvClient over JSON IPC
pireplace/controller.py            Fireplace: orchestration and state
pireplace/web.py                   HTTP server, JSON API, setup routes
pireplace/wifi.py                  Nmcli wrapper, watchdog decision logic
pireplace/static/index.html        phone control page (vanilla JS)
pireplace/static/setup.html        Wi-Fi setup page
tests/...                          one test module per pireplace module
tests/fake_mpv.py                  fake mpv IPC server for tests
dev/config.toml                    laptop config (.dev/ paths, port 8080)
scripts/dev.sh                     run two windowed mpvs + controller locally
scripts/make_sample_media.sh       ffmpeg-generated sample video and tracks
scripts/make_card.sh               printable card with QR codes
deploy/systemd/*.service           unit files
deploy/tmpfiles/pireplace.conf     /run/pireplace socket dir
deploy/polkit/50-pireplace.rules   lets pireplace user drive NetworkManager
deploy/ansible/site.yml            provisioning playbook
deploy/ansible/inventory.example.ini
deploy/ansible/templates/config.toml.j2
README.md                          rewritten
```

Removed: `fireplace.sh`, `install.sh`, `pbstart`, `pbstop`, `config`, `video/` (and its `.gitignore`); `.gitignore` rewritten.

---

### Task 1: Project scaffold, config, and persisted state

**Files:**
- Delete: `fireplace.sh`, `install.sh`, `pbstart`, `pbstop`, `config`, `video/.gitignore`
- Modify: `.gitignore` (replace contents: `.dev/`, `build/`, `__pycache__/`, `*.egg-info/`, `.pytest_cache/`, and the old video extension list)
- Create: `pyproject.toml`, `Makefile`, `pireplace/__init__.py`, `pireplace/config.py`, `pireplace/state.py`
- Test: `tests/test_config.py`, `tests/test_state.py`

**Interfaces:**
- Produces:
  - `pireplace.config.ConfigError(Exception)`
  - `@dataclass(frozen=True) Config` with fields: `video_dir: Path`, `music_dir: Path`, `video_socket: Path`, `music_socket: Path`, `state_file: Path`, `start_music: bool`, `default_mood: str | None`, `music_volume: int`, `log_volume: int`, `host: str`, `port: int`, `hotspot_ssid: str`, `hotspot_password: str`, `hotspot_after_s: int`, `hotspot_retry_s: int`
  - `load_config(path: Path) -> Config`
  - `pireplace.state.State` dataclass: `mood: str | None`, `music_volume: int`, `log_volume: int`, `log_muted: bool`, `music_playing: bool`
  - `StateStore(path: Path, defaults: State)` with `load() -> State`, `save(state: State) -> None`

TOML layout and defaults (used when a key is absent):

```toml
[media]
video_dir = "/srv/pireplace/videos"
music_dir = "/srv/pireplace/music"

[players]
video_socket = "/run/pireplace/video.sock"
music_socket = "/run/pireplace/music.sock"

[playback]
start_music = true
default_mood = "classic-swing"
music_volume = 60
log_volume = 40

[web]
host = "0.0.0.0"
port = 80

[state]
file = "/var/lib/pireplace/state.json"

[wifi]
hotspot_ssid = "pireplace-setup"
hotspot_password = ""        # required non-empty only when wifi-watch runs
hotspot_after_s = 300
hotspot_retry_s = 300
```

- [ ] **Step 1: Remove legacy files and write `pyproject.toml` / `Makefile`**

`pyproject.toml`: project `pireplace`, `requires-python = ">=3.11"`, no runtime deps, `[project.optional-dependencies] dev = ["pytest>=8"]`, package data includes `pireplace/static/*`. `Makefile` target `test` runs `python -m pytest -q`.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_config.py
def test_empty_file_gives_defaults(tmp_path):
    p = tmp_path / "c.toml"; p.write_text("")
    c = load_config(p)
    assert c.video_dir == Path("/srv/pireplace/videos")
    assert c.music_volume == 60 and c.log_volume == 40 and c.port == 80
    assert c.default_mood == "classic-swing" and c.start_music is True
    assert c.hotspot_after_s == 300 and c.hotspot_retry_s == 300

def test_overrides_are_applied(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[web]\nport = 8080\n[media]\nmusic_dir = "/tmp/m"\n')
    c = load_config(p)
    assert c.port == 8080 and c.music_dir == Path("/tmp/m")

@pytest.mark.parametrize("body", ['[playback]\nmusic_volume = 150\n',
                                  '[playback]\nlog_volume = "loud"\n',
                                  '[web]\nport = "eighty"\n',
                                  'not = [valid toml'])
def test_invalid_values_raise(tmp_path, body):
    p = tmp_path / "c.toml"; p.write_text(body)
    with pytest.raises(ConfigError):
        load_config(p)
```

```python
# tests/test_state.py
DEFAULTS = State(mood="classic-swing", music_volume=60, log_volume=40,
                 log_muted=False, music_playing=True)

def test_missing_file_returns_defaults(tmp_path):
    assert StateStore(tmp_path / "s.json", DEFAULTS).load() == DEFAULTS

def test_round_trip(tmp_path):
    store = StateStore(tmp_path / "sub" / "s.json", DEFAULTS)
    s = replace(DEFAULTS, mood="modern", music_volume=10, log_muted=True)
    store.save(s)
    assert store.load() == s

@pytest.mark.parametrize("raw", ["", "{not json", '{"music_volume": "x"}',
                                 '{"music_volume": 999}', "[]"])
def test_corrupt_file_returns_defaults(tmp_path, raw):
    p = tmp_path / "s.json"; p.write_text(raw)
    assert StateStore(p, DEFAULTS).load() == DEFAULTS

def test_partial_file_fills_from_defaults(tmp_path):
    p = tmp_path / "s.json"; p.write_text('{"mood": "modern"}')
    assert StateStore(p, DEFAULTS).load() == replace(DEFAULTS, mood="modern")

def test_save_is_atomic_no_temp_left(tmp_path):
    store = StateStore(tmp_path / "s.json", DEFAULTS); store.save(DEFAULTS)
    assert [f.name for f in tmp_path.iterdir()] == ["s.json"]
```

- [ ] **Step 3: Run tests, verify they fail**

Run: `python -m pytest tests/test_config.py tests/test_state.py -q`
Expected: FAIL (import errors)

- [ ] **Step 4: Implement `load_config` and `StateStore`**

`load_config` uses `tomllib`; wraps `TOMLDecodeError` and any type/range violation in `ConfigError` naming the key. `StateStore.save` creates parent dirs, writes to a temp file in the same dir, `os.replace`s it. `load` validates each field's type and range; an invalid field falls back to its default, a non-object file falls back entirely.

- [ ] **Step 5: Run tests, verify they pass**

Run: `make test`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: scaffold python package with config and persisted state; remove legacy scripts"
```

---

### Task 2: Media discovery

**Files:**
- Create: `pireplace/library.py`
- Test: `tests/test_library.py`

**Interfaces:**
- Produces:
  - `VIDEO_EXTS: frozenset[str]`, `AUDIO_EXTS: frozenset[str]` (values in Global Constraints)
  - `find_videos(video_dir: Path) -> list[Path]` — recursive, sorted, missing dir → `[]`
  - `find_moods(music_dir: Path) -> dict[str, list[Path]]` — key per immediate non-hidden subfolder that contains ≥ 1 audio file anywhere beneath it; values sorted; missing dir → `{}`
  - `mood_label(name: str) -> str`

- [ ] **Step 1: Write failing tests**

```python
def touch(p): p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b"")

def test_nested_album_folders_and_junk(tmp_path):
    m = tmp_path / "music"
    touch(m / "classic-swing/Frank Sinatra/Christmas Songs/01 Jingle Bells.flac")
    touch(m / "classic-swing/Frank Sinatra/Christmas Songs/cover.jpg")
    touch(m / "classic-swing/.DS_Store")
    touch(m / "classic-swing/._01 Jingle Bells.flac")
    touch(m / "classic-swing/Thumbs.db")
    touch(m / "modern/Bublé – It's Beginning to Look.MP3")
    touch(m / "empty-mood/notes.txt")
    touch(m / ".hidden-mood/a.mp3")
    moods = find_moods(m)
    assert list(moods) == ["classic-swing", "modern"]
    assert [p.name for p in moods["classic-swing"]] == ["01 Jingle Bells.flac"]
    assert moods["modern"][0].name == "Bublé – It's Beginning to Look.MP3"

def test_loose_files_in_music_root_are_not_a_mood(tmp_path):
    touch(tmp_path / "stray.mp3")
    assert find_moods(tmp_path) == {}

def test_missing_dirs(tmp_path):
    assert find_moods(tmp_path / "nope") == {}
    assert find_videos(tmp_path / "nope") == []

def test_videos_sorted_and_filtered(tmp_path):
    for n in ["b.mp4", "a.MKV", "readme.txt", ".c.mp4", "sub/d.webm"]:
        touch(tmp_path / n)
    assert [p.name for p in find_videos(tmp_path)] == ["a.MKV", "b.mp4", "d.webm"]

@pytest.mark.parametrize("name,label", [("classic-swing", "Classic Swing"),
                                        ("modern", "Modern"),
                                        ("kids_songs", "Kids Songs")])
def test_mood_label(name, label):
    assert mood_label(name) == label
```

- [ ] **Step 2: Run, verify fail** — `python -m pytest tests/test_library.py -q` → FAIL (import error)

- [ ] **Step 3: Implement** with `Path.rglob("*")`, skipping any path where some part relative to the root starts with `.`; extension check via `suffix.lower()`. `videos` sort key is the path relative to `video_dir`.

- [ ] **Step 4: Run, verify pass** — `make test` → all pass

- [ ] **Step 5: Commit** — `git commit -am "feat: discover videos and mood folders"` (add new files first)

---

### Task 3: mpv IPC client

**Files:**
- Create: `pireplace/mpv.py`, `tests/fake_mpv.py`
- Test: `tests/test_mpv.py`

**Interfaces:**
- Produces:
  - `MpvUnavailable(Exception)` — socket missing, refused, closed, or timed out
  - `MpvError(Exception)` — mpv replied with `"error" != "success"`
  - `MpvClient(socket_path: Path, timeout: float = 1.0)` with `command(*args) -> Any` (returns reply `data`), `get(prop: str) -> Any`, `set(prop: str, value: Any) -> None`
  - `tests/fake_mpv.FakeMpv(socket_path)`: context manager running a thread-backed unix-socket server; attributes `props: dict`, `commands: list[list]`; replies `get_property`/`set_property` from/to `props` (missing prop → `"property not found"` error); records every command; can emit an unsolicited event line before each reply (`emit_events=True`).

Protocol: one connection per call; send `json.dumps({"command": [...], "request_id": n}) + "\n"`; read newline-delimited JSON, skip lines without a matching `request_id` (events), return on match.

- [ ] **Step 1: Write failing tests**

```python
def test_get_and_set(tmp_path):
    with FakeMpv(tmp_path / "s") as fake:
        fake.props["volume"] = 40
        c = MpvClient(tmp_path / "s")
        assert c.get("volume") == 40
        c.set("volume", 55)
        assert fake.props["volume"] == 55

def test_events_are_skipped(tmp_path):
    with FakeMpv(tmp_path / "s", emit_events=True) as fake:
        fake.props["pause"] = False
        assert MpvClient(tmp_path / "s").get("pause") is False

def test_unicode_and_quotes_round_trip(tmp_path):
    name = "/m/Bublé – It's \"Beginning\".flac"
    with FakeMpv(tmp_path / "s") as fake:
        MpvClient(tmp_path / "s").command("loadfile", name, "append")
        assert fake.commands[-1] == ["loadfile", name, "append"]

def test_missing_socket_raises_unavailable(tmp_path):
    with pytest.raises(MpvUnavailable):
        MpvClient(tmp_path / "nope", timeout=0.2).get("volume")

def test_error_reply_raises_mpverror(tmp_path):
    with FakeMpv(tmp_path / "s"):
        with pytest.raises(MpvError):
            MpvClient(tmp_path / "s").get("no-such-prop")

@pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv not installed")
def test_real_mpv(tmp_path):
    sock = tmp_path / "real.sock"
    proc = subprocess.Popen(["mpv", "--idle=yes", "--vo=null", "--ao=null",
                             f"--input-ipc-server={sock}"])
    try:
        wait_for(sock.exists, 5)
        c = MpvClient(sock)
        c.set("volume", 33)
        assert c.get("volume") == 33
    finally:
        proc.terminate(); proc.wait(5)
```

- [ ] **Step 2: Run, verify fail** — `python -m pytest tests/test_mpv.py -q` → FAIL

- [ ] **Step 3: Implement `MpvClient` and `FakeMpv`**

- [ ] **Step 4: Run, verify pass** — `make test` → all pass (real-mpv test passes or skips)

- [ ] **Step 5: Commit** — `git add pireplace/mpv.py tests/ && git commit -m "feat: mpv JSON IPC client"`

---

### Task 4: Fireplace controller

**Files:**
- Create: `pireplace/controller.py`
- Test: `tests/test_controller.py`

**Interfaces:**
- Consumes: `Config`, `State`, `StateStore` (Task 1); `find_videos`, `find_moods`, `mood_label` (Task 2); `MpvClient`, `MpvUnavailable` (Task 3)
- Produces:
  - `UnknownMood(Exception)`
  - `Fireplace(config: Config, store: StateStore, video: MpvClient, music: MpvClient, shuffle: Callable[[list], None] = random.shuffle)`
  - `startup() -> None` — load videos into video player only if its `playlist-count` is 0; apply log volume/mute; resolve mood (saved → `config.default_mood` → first available → `None`); load music only if its `playlist-count` is 0; apply music volume; set `pause` to `not state.music_playing` (first boot: `music_playing = config.start_music`). Raises `MpvUnavailable` if a player isn't up (caller retries).
  - `status() -> dict` (shape below; never raises — an unreachable player reports `"available": False`)
  - `set_mood(name: str) -> None` — `UnknownMood` if not in `find_moods`; shuffles tracks, `loadfile <first> replace`, then `loadfile <each> append`; unpauses; persists
  - `play() -> None`, `pause() -> None`, `next_track() -> None` (`playlist-next`)
  - `set_music_volume(level: int) -> None`, `set_log_volume(level: int) -> None` — `ValueError` unless `0 <= level <= 100` and `type(level) is int`
  - `set_log_muted(muted: bool) -> None`
  - `rescan() -> None` — reload video playlist and the current mood (if the current mood vanished, fall back as in `startup`)
  - All mutators persist via `store.save` only after the player call succeeds; `MpvUnavailable` propagates.

Video load: `loadfile <first> replace`, then `append` the rest; `loop-playlist` is set by the unit flags.

`status()` shape:

```python
{"moods": [{"name": "classic-swing", "label": "Classic Swing", "tracks": 42}, ...],
 "mood": "classic-swing",
 "music": {"available": True, "playing": True, "volume": 60, "now_playing": "White Christmas"},
 "log":   {"available": True, "volume": 40, "muted": False, "videos": 3}}
```

`now_playing` = mpv `media-title`, or `None`.

- [ ] **Step 1: Write failing tests** (use two `FakeMpv` instances with `playlist-count` 0 and a tmp media tree; `shuffle=lambda xs: xs.reverse()` for determinism)

```python
def test_startup_loads_saved_mood_and_volumes(env):
    env.store.save(replace(env.defaults, mood="modern", music_volume=20, log_volume=70))
    env.fp.startup()
    assert env.music.commands[0][:3] == ["loadfile", str(env.modern_tracks[-1]), "replace"]
    assert env.music.props["volume"] == 20 and env.video.props["volume"] == 70

def test_startup_saved_mood_gone_falls_back_to_default_then_first(env):
    env.store.save(replace(env.defaults, mood="deleted-mood"))
    env.fp.startup()
    assert env.fp.status()["mood"] == "classic-swing"

def test_startup_skips_reload_if_players_already_have_playlists(env):
    env.video.props["playlist-count"] = 3; env.music.props["playlist-count"] = 9
    env.fp.startup()
    assert not any(c[0] == "loadfile" for c in env.video.commands + env.music.commands)

def test_startup_first_boot_respects_start_music_false(env_factory):
    env = env_factory(start_music=False); env.fp.startup()
    assert env.music.props["pause"] is True

def test_no_moods_leaves_music_idle(env_factory):
    env = env_factory(music_tree={}); env.fp.startup()
    s = env.fp.status()
    assert s["mood"] is None and s["moods"] == []

def test_set_mood_unknown_raises_and_keeps_state(env):
    env.fp.startup()
    with pytest.raises(UnknownMood):
        env.fp.set_mood("polka")
    assert env.store.load().mood == "classic-swing"

def test_set_mood_loads_odd_filenames_verbatim(env):
    env.fp.startup(); env.fp.set_mood("modern")
    loaded = [c[1] for c in env.music.commands if c[0] == "loadfile"]
    assert "Bublé – It's \"Beginning\".flac" in [Path(p).name for p in loaded]

@pytest.mark.parametrize("bad", [-1, 101, 50.5, "50", True])
def test_volume_validation(env, bad):
    with pytest.raises(ValueError):
        env.fp.set_music_volume(bad)

def test_status_when_music_player_down(env):
    env.music.stop()
    s = env.fp.status()
    assert s["music"]["available"] is False and s["log"]["available"] is True

def test_mutator_with_player_down_raises_and_does_not_persist(env):
    env.fp.startup(); env.music.stop()
    with pytest.raises(MpvUnavailable):
        env.fp.set_music_volume(10)
    assert env.store.load().music_volume == 60

def test_pause_play_and_mute_persist(env):
    env.fp.startup(); env.fp.pause(); env.fp.set_log_muted(True)
    st = env.store.load()
    assert st.music_playing is False and st.log_muted is True
```

- [ ] **Step 2: Run, verify fail** — `python -m pytest tests/test_controller.py -q` → FAIL

- [ ] **Step 3: Implement `Fireplace`**

- [ ] **Step 4: Run, verify pass** — `make test` → all pass

- [ ] **Step 5: Commit** — `git add pireplace/controller.py tests/test_controller.py && git commit -m "feat: fireplace controller"`

---

### Task 5: Web API and phone control page

**Files:**
- Create: `pireplace/web.py`, `pireplace/static/index.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `Fireplace`, `UnknownMood` (Task 4); `MpvUnavailable` (Task 3)
- Produces: `make_server(fireplace: Fireplace, host: str, port: int) -> ThreadingHTTPServer` (port 0 allowed for tests); `extra_routes` hook: `make_server(..., extra: dict[tuple[str, str], Handler] | None = None)` where `Handler = Callable[[dict | None], tuple[int, dict]]`, keyed by `(method, path)` — used by Task 7.

Routes (JSON bodies, `Content-Type: application/json`):

| Method | Path | Body | Success |
|---|---|---|---|
| GET | `/` | — | `index.html` |
| GET | `/api/status` | — | 200 `status()` |
| POST | `/api/mood` | `{"mood": str}` | 200 status |
| POST | `/api/music` | `{"action": "play" \| "pause" \| "next"}` | 200 status |
| POST | `/api/volume` | `{"target": "music" \| "log", "level": int}` | 200 status |
| POST | `/api/log` | `{"muted": bool}` | 200 status |
| POST | `/api/rescan` | — | 200 status |

Errors: malformed JSON, missing/wrong-typed field, unknown action/target, `ValueError`, `UnknownMood` → 400 `{"error": "<message>"}`. `MpvUnavailable` → 503 `{"error": "player not running"}`. Unknown path → 404. Unexpected exception → 500 `{"error": "internal error"}` and logged; server keeps serving. Request bodies over 4 KiB → 413.

Page: one self-contained HTML file, no external assets (works offline). Large touch targets: a button per mood (unavailable moods disabled), play/pause, next, music volume slider, log mute toggle and volume slider, now-playing line. Polls `/api/status` every 5 s and after each action. Shows a plain message when a player is unavailable or no videos/moods exist.

- [ ] **Step 1: Write failing tests** (start `make_server` on port 0 in a thread with a stub `Fireplace` that records calls and can be told to raise)

```python
def test_status_ok(srv): assert get(srv, "/api/status") == (200, STUB_STATUS)
def test_index_served(srv):
    code, body, ctype = get_raw(srv, "/")
    assert code == 200 and "text/html" in ctype and b"<html" in body

@pytest.mark.parametrize("path,body", [
    ("/api/mood", b"{not json"), ("/api/mood", b"{}"), ("/api/mood", b'{"mood": 3}'),
    ("/api/music", b'{"action": "dance"}'),
    ("/api/volume", b'{"target": "music", "level": "loud"}'),
    ("/api/volume", b'{"target": "tv", "level": 5}'),
    ("/api/log", b'{"muted": "yes"}')])
def test_bad_input_is_400(srv, path, body):
    code, resp = post_raw(srv, path, body)
    assert code == 400 and "error" in resp and srv.stub.calls == []

def test_value_error_from_controller_is_400(srv):
    srv.stub.raise_on("set_music_volume", ValueError("out of range"))
    assert post(srv, "/api/volume", {"target": "music", "level": 150})[0] == 400

def test_unknown_mood_is_400(srv):
    srv.stub.raise_on("set_mood", UnknownMood("polka"))
    assert post(srv, "/api/mood", {"mood": "polka"})[0] == 400

def test_player_down_is_503(srv):
    srv.stub.raise_on("pause", MpvUnavailable())
    assert post(srv, "/api/music", {"action": "pause"})[0] == 503

def test_unexpected_error_is_500_and_server_survives(srv):
    srv.stub.raise_on("rescan", RuntimeError("boom"))
    assert post(srv, "/api/rescan", None)[0] == 500
    assert get(srv, "/api/status")[0] == 200

def test_oversize_body_is_413(srv):
    assert post_raw(srv, "/api/mood", b"x" * 5000)[0] == 413

def test_happy_paths_dispatch(srv):
    post(srv, "/api/mood", {"mood": "modern"})
    post(srv, "/api/volume", {"target": "log", "level": 0})
    post(srv, "/api/log", {"muted": True})
    assert srv.stub.calls == [("set_mood", "modern"), ("set_log_volume", 0), ("set_log_muted", True)]
```

- [ ] **Step 2: Run, verify fail** — `python -m pytest tests/test_web.py -q` → FAIL

- [ ] **Step 3: Implement `make_server` and `index.html`**

- [ ] **Step 4: Run, verify pass** — `make test` → all pass

- [ ] **Step 5: Commit** — `git add pireplace/web.py pireplace/static tests/test_web.py && git commit -m "feat: web API and phone control page"`

---

### Task 6: CLI entry point and laptop dev mode

**Files:**
- Create: `pireplace/__main__.py`, `dev/config.toml`, `scripts/dev.sh`, `scripts/make_sample_media.sh`
- Modify: `Makefile` (add `dev`, `sample-media`)
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: everything from Tasks 1–5
- Produces: `python -m pireplace serve --config PATH` — loads config, builds `Fireplace`, calls `startup()` retrying every 2 s until both players answer (logging once per minute, serving the page meanwhile so status shows players unavailable), then serves forever. Exit code 2 with a one-line message on `ConfigError`. `main(argv: list[str]) -> int`. Task 7 adds the `wifi-watch` subcommand.

`dev/config.toml`: media under `.dev/media/videos` and `.dev/media/music`, sockets `.dev/run/video.sock` / `.dev/run/music.sock`, state `.dev/state.json`, `host = "127.0.0.1"`, `port = 8080`. Relative paths in a config file resolve against the config file's directory's parent (repo root for `dev/config.toml`) — implement in `load_config` and add a test to `tests/test_config.py`.

`scripts/make_sample_media.sh` (idempotent, skips existing): ffmpeg generates `.dev/media/videos/fire-test.mp4` (20 s `testsrc2` 1280×720 with a quiet brown-noise track) and 3 short sine-tone tracks in each of `.dev/media/music/modern/` and `.dev/media/music/classic-swing/Some Artist/Some Album/`, one with a unicode/apostrophe filename.

`scripts/dev.sh`: checks `mpv`, `ffmpeg` present (clear error if not); runs sample-media; starts video mpv windowed (`--idle=yes --loop-playlist=inf --input-ipc-server=.dev/run/video.sock --force-window=yes`) and music mpv (`--idle=yes --no-video --loop-playlist=inf --input-ipc-server=.dev/run/music.sock`); runs `python -m pireplace serve --config dev/config.toml`; traps EXIT to kill both mpvs. Prints `Open http://127.0.0.1:8080`.

- [ ] **Step 1: Write failing tests**

```python
def test_bad_config_exits_2(tmp_path, capsys):
    p = tmp_path / "c.toml"; p.write_text("[web]\nport = 'x'\n")
    assert main(["serve", "--config", str(p)]) == 2
    assert "port" in capsys.readouterr().err

def test_relative_paths_resolve_against_repo_root(tmp_path):  # in test_config.py
    (tmp_path / "dev").mkdir(); p = tmp_path / "dev" / "c.toml"
    p.write_text('[media]\nvideo_dir = ".dev/media/videos"\n')
    assert load_config(p).video_dir == tmp_path / ".dev/media/videos"
```

- [ ] **Step 2: Run, verify fail** — `make test` → FAIL

- [ ] **Step 3: Implement `__main__.py`, config path resolution, scripts, Makefile targets**

- [ ] **Step 4: Run, verify pass** — `make test` → all pass

- [ ] **Step 5: Manual dev-mode check**

Run: `make dev`, open `http://127.0.0.1:8080`.
Expected: test video loops in a window with its own noise; music plays; switching mood changes the tone track; muting the log silences only the video noise; pausing music leaves the noise playing; killing the music mpv makes the page show music unavailable without the page or video breaking.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat: serve CLI and laptop dev mode"`

---

### Task 7: Wi-Fi onboarding

**Files:**
- Create: `pireplace/wifi.py`, `pireplace/static/setup.html`
- Modify: `pireplace/__main__.py` (add `wifi-watch`), `pireplace/web.py` caller in `__main__` (register setup routes via `extra`)
- Test: `tests/test_wifi.py`

**Interfaces:**
- Consumes: `Config` (Task 1); `make_server(..., extra=...)` (Task 5)
- Produces:
  - `Runner = Callable[[list[str]], str]` (runs a command, returns stdout, raises `subprocess.CalledProcessError`)
  - `Nmcli(run: Runner)`: `connected() -> bool` (a non-hotspot wifi or ethernet connection is activated), `hotspot_active() -> bool`, `start_hotspot(ssid: str, password: str) -> None`, `stop_hotspot() -> None`, `try_known_networks() -> bool`, `scan() -> list[str]` (unique SSIDs, strongest first, empty SSIDs dropped), `connect(ssid: str, password: str) -> None`
  - `decide(now: float, last_connected: float, hotspot_since: float | None, connected: bool, after_s: int, retry_s: int) -> Literal["none", "start_hotspot", "retry_known", "stop_hotspot"]`
  - `python -m pireplace wifi-watch --config PATH` — loop every 10 s applying `decide`; caches the last `scan()` before starting the hotspot (single radio can't scan in AP mode)
  - Setup routes for `make_server(extra=...)`: `GET /setup` → `setup.html`; `GET /api/wifi/networks` → `{"networks": [...]}` (cached scan); `POST /api/wifi/connect` `{"ssid": str, "password": str}` → 202 then stops hotspot and connects; 400 on missing/blank ssid or non-string fields.

`decide` rules:
- connected and hotspot up → `stop_hotspot`
- connected → `none`
- not connected, no hotspot, `now - last_connected >= after_s` → `start_hotspot`
- hotspot up for `>= retry_s` since `hotspot_since` → `retry_known` (watcher stops hotspot, tries known networks for up to 30 s, restarts hotspot on failure, resets `hotspot_since`)
- otherwise → `none`

`wifi-watch` exits 2 if `hotspot_password` is shorter than 8 characters (WPA2 minimum).

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.parametrize("args,expected", [
    (dict(now=100, last_connected=0, hotspot_since=None, connected=True), "none"),
    (dict(now=299, last_connected=0, hotspot_since=None, connected=False), "none"),
    (dict(now=300, last_connected=0, hotspot_since=None, connected=False), "start_hotspot"),
    (dict(now=500, last_connected=0, hotspot_since=400, connected=False), "none"),
    (dict(now=700, last_connected=0, hotspot_since=400, connected=False), "retry_known"),
    (dict(now=700, last_connected=0, hotspot_since=400, connected=True), "stop_hotspot"),
])
def test_decide(args, expected):
    assert decide(**args, after_s=300, retry_s=300) == expected

def test_router_reboot_under_grace_never_starts_hotspot():
    assert decide(now=1000+120, last_connected=1000, hotspot_since=None,
                  connected=False, after_s=300, retry_s=300) == "none"

def test_scan_parses_and_dedupes():
    out = "HomeNet:80\n:60\nHomeNet:40\nCafé \\: Guest:70\n"
    n = Nmcli(lambda cmd: out)
    assert n.scan() == ["HomeNet", "Café : Guest"]

def test_connect_passes_args_as_list_not_shell():
    calls = []
    Nmcli(lambda c: calls.append(c) or "").connect("My Net; rm -rf /", "pa ss")
    assert calls[-1][:4] == ["nmcli", "device", "wifi", "connect"]
    assert "My Net; rm -rf /" in calls[-1] and "pa ss" in calls[-1]

@pytest.mark.parametrize("body", [b"{}", b'{"ssid": "", "password": "x"}',
                                  b'{"ssid": 5, "password": "x"}'])
def test_connect_route_rejects_bad_input(setup_srv, body):
    assert post_raw(setup_srv, "/api/wifi/connect", body)[0] == 400

def test_short_hotspot_password_exits_2(tmp_path):
    p = tmp_path / "c.toml"; p.write_text('[wifi]\nhotspot_password = "short"\n')
    assert main(["wifi-watch", "--config", str(p)]) == 2
```

- [ ] **Step 2: Run, verify fail** — `python -m pytest tests/test_wifi.py -q` → FAIL

- [ ] **Step 3: Implement** — `nmcli -t` terse output; split on unescaped `:` and unescape `\:`; hotspot via `nmcli device wifi hotspot ifname wlan0 con-name pireplace-hotspot ssid <ssid> password <pw>`; `stop_hotspot` downs `pireplace-hotspot`. Never use `shell=True`. Never log the Wi-Fi password.

- [ ] **Step 4: Run, verify pass** — `make test` → all pass

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: wifi onboarding watchdog and setup page"`

---

### Task 8: Provisioning (systemd, polkit, Ansible)

**Files:**
- Create: `deploy/systemd/pireplace-video.service`, `deploy/systemd/pireplace-music.service` (user units), `deploy/systemd/pireplace-control.service`, `deploy/systemd/pireplace-wifi.service` (system units), `deploy/tmpfiles/pireplace.conf`, `deploy/polkit/50-pireplace.rules`, `deploy/ansible/site.yml`, `deploy/ansible/inventory.example.ini`, `deploy/ansible/templates/config.toml.j2`
- Modify: `Makefile` (add `lint`, `deploy`)

**Interfaces:**
- Consumes: `python -m pireplace serve|wifi-watch --config /etc/pireplace/config.toml` (Tasks 6–7)
- Produces: `make deploy HOST=pireplace.local` runs the playbook; `make lint` runs `ansible-playbook --syntax-check`, `ansible-lint`, and `systemd-analyze verify` on the unit files.

Decisions:
- Service user `pireplace` (groups `video`, `render`, `audio`), lingering enabled so its user PipeWire session and user units start at boot without login.
- mpv units run as **user** units of `pireplace` (need the user's PipeWire). Video unit baseline flags: `--vo=gpu --gpu-context=drm --hwdec=auto-safe --fullscreen --idle=yes --loop-playlist=inf --no-osc --osd-level=0 --no-input-default-bindings --input-ipc-server=/run/pireplace/video.sock --really-quiet`. Music: `--no-video --idle=yes --loop-playlist=inf --input-ipc-server=/run/pireplace/music.sock --really-quiet`. Both `Restart=always`, `RestartSec=2`. Exact KMS/hwdec flags may be adjusted on hardware (spec leaves to builder).
- Controller and wifi watcher are **system** units. Control: `User=pireplace`, `AmbientCapabilities=CAP_NET_BIND_SERVICE`, `StateDirectory=pireplace`, `Restart=always`. Wifi: `User=pireplace`, `Restart=always`; NetworkManager access via the polkit rule, which grants `org.freedesktop.NetworkManager.*` to user `pireplace` only.
- `tmpfiles.d`: `d /run/pireplace 0750 pireplace pireplace -`.
- Packages: `mpv`, `pipewire`, `pipewire-pulse`, `wireplumber`, `network-manager`, `avahi-daemon`, `python3` (≥ 3.11), `unattended-upgrades`.
- App installed to `/opt/pireplace` (copy of `pireplace/` package), config rendered to `/etc/pireplace/config.toml` from vars (`hotspot_password` is an Ansible var the owner sets in their inventory, required, never committed).
- Media dirs `/srv/pireplace/videos`, `/srv/pireplace/music/modern`, `/srv/pireplace/music/classic-swing`, owned by `pireplace`, group-writable by the SSH admin user so `rsync` works.
- Quiet TV: append `quiet loglevel=3 logo.nologo vt.global_cursor_default=0 consoleblank=0` to `/boot/firmware/cmdline.txt` (idempotently); set `disable_splash=1` in `/boot/firmware/config.txt`; disable `getty@tty1`. Hostname `pireplace`.
- Updates: unattended-upgrades limited to the security origin; `apt-mark hold mpv` is **not** applied by default (document how in README if an update ever breaks playback).
- Playbook is idempotent: a second run reports zero changes.

- [ ] **Step 1: Write unit files, tmpfiles, polkit rule, playbook, template, inventory example**

- [ ] **Step 2: Lint**

Run: `make lint`
Expected: syntax check OK, ansible-lint passes (production profile or documented skips), `systemd-analyze verify` reports no errors for all four units.

- [ ] **Step 3: Apply to the Pi 4** (freshly flashed Pi OS Lite 64-bit, Imager-preset hostname, Wi-Fi, SSH key)

Run: `make deploy HOST=pireplace.local` then run it again.
Expected: first run succeeds; second run `changed=0`.

- [ ] **Step 4: Commit** — `git add deploy Makefile && git commit -m "feat: systemd units, polkit rule, and ansible provisioning"`

---

### Task 9: README, printable card, and hardware acceptance

**Files:**
- Modify: `README.md` (rewrite)
- Create: `scripts/make_card.sh`; Makefile target `card` → `build/card.html`

**Interfaces:**
- Consumes: everything above.

README sections: what it is; hardware (Pi 4; Pi 3 untested); flashing with Raspberry Pi Imager (hostname `pireplace`, Wi-Fi, SSH key); `make deploy`; adding media (`rsync` examples into `/srv/pireplace/videos` and `/srv/pireplace/music/<mood>/`, nested album folders fine, H.264 ≤ 1080p recommended for log videos); adding a mood (make a folder, press Rescan); control page `http://pireplace.local`; guest networks / client isolation block the page (expected); Wi-Fi onboarding (`pireplace-setup` appears after ~5 min offline, open `http://10.42.0.1/setup`); laptop dev (`make dev`, needs mpv + ffmpeg); troubleshooting (`journalctl -u pireplace-control`, `journalctl --user -M pireplace@ -u pireplace-video`, how to hold mpv if an update breaks it).

`make_card.sh`: uses `qrencode -t SVG` to produce `build/card.html` with two panels: "Fireplace remote" QR → `http://pireplace.local`, and "Wi-Fi setup" with SSID `pireplace-setup`, the password passed as `HOTSPOT_PASSWORD` env var, and QR → `http://10.42.0.1/setup`. `build/` is gitignored, so the password never lands in git.

- [ ] **Step 1: Write README and card script; run `make card HOTSPOT_PASSWORD=...`** — Expected: `build/card.html` opens and both QR codes scan to the right URLs.

- [ ] **Step 2: Hardware acceptance on the Pi 4** — tick each:
  - Power on with media present → fire and music within 60 s, no console text, no cursor.
  - TV off/on → still playing.
  - Phone at `http://pireplace.local`: mood switch, pause/play, next, both volumes, log mute — each affects only its own stream.
  - `sudo systemctl kill pireplace-control` and `systemctl --user -M pireplace@ kill pireplace-music` → both return on their own; video never stops.
  - Pull power, restore → same mood and volumes as before.
  - Router off for 2 min then on → no hotspot appears, box rejoins.
  - Forget the home network, reboot → `pireplace-setup` appears after ~5 min; setup page joins a network; hotspot disappears.
  - Empty `/srv/pireplace/videos`, rescan → black screen, page shows the message, nothing crash-loops (`systemctl --user -M pireplace@ status pireplace-video` stable).

- [ ] **Step 3: Commit** — `git add README.md scripts/make_card.sh Makefile && git commit -m "docs: rewrite README; add printable card"`
