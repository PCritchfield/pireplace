import json
from pathlib import Path

import pytest

from pireplace.jellyfin import JellyfinError, Track
from pireplace.sync import (
    MANIFEST,
    MIN_FREE_RUN,
    MIN_FREE_START,
    sync_mood,
    track_filename,
)

BIG = 10 * 1024**4


def T(i, artist="Art", title=None, container="flac", size=10):
    return Track(
        id=i * 8 if len(i) == 1 else i,
        artist=artist,
        title=title or f"Song {i}",
        container=container,
        size=size,
    )


class FakeClient:
    def __init__(self, tracks=None, list_error=None, fail_ids=()):
        self.tracks = tracks or []
        self.list_error = list_error
        self.fail_ids = set(fail_ids)
        self.downloads = []

    def playlist_tracks(self, playlist_id):
        if self.list_error:
            raise JellyfinError(self.list_error)
        return list(self.tracks)

    def download(self, item_id, dest):
        self.downloads.append(item_id)
        dest = Path(dest)
        if item_id in self.fail_ids:
            raise JellyfinError("boom")
        dest.write_bytes(b"data-" + item_id.encode())
        return 10


def run(client, tmp_path, free=lambda p: BIG):
    return sync_mood(client, "modern", "pl", tmp_path / "modern", free_bytes=free)


def manifest(tmp_path):
    return json.loads((tmp_path / "modern" / MANIFEST).read_text())["items"]


def files(tmp_path):
    return sorted(p.name for p in (tmp_path / "modern").iterdir() if not p.name.startswith("."))


def test_first_sync_downloads_all_and_writes_manifest(tmp_path):
    tracks = [T("a"), T("b"), T("c")]
    res = run(FakeClient(tracks), tmp_path)
    assert (res.added, res.removed, res.failed, res.error) == (3, 0, 0, None)
    assert files(tmp_path) == sorted(track_filename(t) for t in tracks)
    assert set(manifest(tmp_path)) == {t.id for t in tracks}
    assert list((tmp_path / "modern").glob(".*.part")) == []


def test_second_sync_is_noop(tmp_path):
    tracks = [T("a"), T("b")]
    run(FakeClient(tracks), tmp_path)
    c = FakeClient(tracks)
    res = run(c, tmp_path)
    assert (res.added, res.removed, res.failed, res.error) == (0, 0, 0, None)
    assert c.downloads == []


def test_removed_from_playlist_is_deleted(tmp_path):
    a, b = T("a"), T("b")
    run(FakeClient([a, b]), tmp_path)
    res = run(FakeClient([a]), tmp_path)
    assert (res.added, res.removed, res.error) == (0, 1, None)
    assert files(tmp_path) == [track_filename(a)]
    assert set(manifest(tmp_path)) == {a.id}


def test_manual_files_survive(tmp_path):
    (tmp_path / "modern").mkdir()
    (tmp_path / "modern" / "my rip.flac").write_bytes(b"x")
    res = run(FakeClient(tracks=[]), tmp_path)
    assert res.error is None
    assert (tmp_path / "modern" / "my rip.flac").exists()
    run(FakeClient([T("a")]), tmp_path)
    run(FakeClient([T("b")]), tmp_path)
    assert (tmp_path / "modern" / "my rip.flac").exists()


def test_list_error_touches_nothing(tmp_path):
    a = T("a")
    run(FakeClient([a]), tmp_path)
    before = (tmp_path / "modern" / MANIFEST).read_bytes()
    res = run(FakeClient(list_error="down"), tmp_path)
    assert res.error and "down" in res.error
    assert (res.added, res.removed) == (0, 0)
    assert (tmp_path / "modern" / MANIFEST).read_bytes() == before
    assert files(tmp_path) == [track_filename(a)]


def test_empty_playlist_with_synced_files_is_error(tmp_path):
    a = T("a")
    run(FakeClient([a]), tmp_path)
    res = run(FakeClient([]), tmp_path)
    assert res.error == "playlist is empty; refusing to delete synced tracks"
    assert res.removed == 0
    assert files(tmp_path) == [track_filename(a)]
    assert set(manifest(tmp_path)) == {a.id}


def test_failed_download_counts_and_retries_next_run(tmp_path):
    a, b = T("a"), T("b")
    res = run(FakeClient([a, b], fail_ids=[b.id]), tmp_path)
    assert (res.added, res.failed) == (1, 1)
    assert res.error == "1 downloads failed"
    assert list((tmp_path / "modern").glob(".*.part")) == []
    assert set(manifest(tmp_path)) == {a.id}
    c = FakeClient([a, b])
    res = run(c, tmp_path)
    assert (res.added, res.failed, res.error) == (1, 0, None)
    assert c.downloads == [b.id]


def test_leftover_part_file_from_crash_is_overwritten(tmp_path):
    a = T("a")
    (tmp_path / "modern").mkdir()
    (tmp_path / "modern" / f".{track_filename(a)}.part").write_bytes(b"stale")
    res = run(FakeClient([a]), tmp_path)
    assert res.added == 1
    assert (tmp_path / "modern" / track_filename(a)).read_bytes().startswith(b"data-")


def test_duplicate_ids_download_once(tmp_path):
    a = T("a")
    c = FakeClient([a, a])
    res = run(c, tmp_path)
    assert res.added == 1
    assert c.downloads == [a.id]


def test_existing_file_at_target_name_is_adopted(tmp_path):
    a = T("a")
    (tmp_path / "modern").mkdir()
    (tmp_path / "modern" / MANIFEST).write_text("{corrupt")
    (tmp_path / "modern" / track_filename(a)).write_bytes(b"already")
    c = FakeClient([a])
    res = run(c, tmp_path)
    assert c.downloads == []
    assert res.error is None
    assert set(manifest(tmp_path)) == {a.id}
    assert (tmp_path / "modern" / track_filename(a)).read_bytes() == b"already"


def test_space_check_refuses_before_any_change(tmp_path):
    old, new = T("a"), T("b", size=1000)
    run(FakeClient([old]), tmp_path)
    c = FakeClient([new])
    res = run(c, tmp_path, free=lambda p: MIN_FREE_START + 999)
    assert res.error and "not enough space" in res.error
    assert (res.added, res.removed) == (0, 0)
    assert c.downloads == []
    assert files(tmp_path) == [track_filename(old)]
    assert set(manifest(tmp_path)) == {old.id}


def test_space_check_treats_none_size_as_zero(tmp_path):
    a = T("a", size=None)
    res = run(FakeClient([a]), tmp_path, free=lambda p: MIN_FREE_START)
    assert res.error is None and res.added == 1


def test_running_low_on_space_stops_midway(tmp_path):
    tracks = [T("a"), T("b"), T("c")]
    # start check and first pre-download check are fine; then just under the run floor
    levels = iter([BIG, BIG, MIN_FREE_RUN + 5, MIN_FREE_RUN + 5])
    c = FakeClient(tracks)
    res = run(c, tmp_path, free=lambda p: next(levels))
    assert c.downloads == ["a" * 8]
    assert (res.added, res.failed) == (1, 2)
    assert res.error == "2 downloads failed"


@pytest.mark.parametrize("artist,title,expect", [
    ("AC/DC", 'What "Child"?', 'AC_DC - What _Child__ [aaaaaaaa].flac'),
    (None, None, "Unknown Artist - Untitled [aaaaaaaa].flac"),
    ("Bublé", "It's Beginning…", "Bublé - It's Beginning… [aaaaaaaa].flac"),
    ("..x", "y", "x - y [aaaaaaaa].flac"),
])
def test_track_filename(artist, title, expect):
    assert track_filename(Track("a" * 32, artist, title, "flac", 1)) == expect


def test_track_filename_missing_container_and_control_chars():
    name = track_filename(Track("a" * 32, "A\x00B\n", "C\x7fD", None, 1))
    assert name == "A_B_ - C_D [aaaaaaaa].audio"


def test_track_filename_long_names_fit_200_bytes():
    for ch in ("a", "é", "日"):
        name = track_filename(Track("a" * 32, ch * 500, ch * 500, "flac", 1))
        assert len(name.encode()) <= 200
        assert name.endswith(" [aaaaaaaa].flac")
        assert name.startswith(ch)


def test_track_filename_hostile_id_cannot_escape():
    for hostile in ("../../etc", "..\\..\\x", "/abs/path/x", "....////", ""):
        name = track_filename(Track(hostile, "A", "B", "flac", 1))
        assert "/" not in name and "\\" not in name
        assert not name.startswith(".")
        assert Path(name).name == name


def test_hostile_id_download_stays_inside_mood_dir(tmp_path):
    t = Track("../../etc", "A", "B", "flac", 1)
    res = run(FakeClient([t]), tmp_path)
    assert res.added == 1
    mood = tmp_path / "modern"
    assert {p.parent for p in tmp_path.rglob("*") if p.is_file()} == {mood}
    assert not (tmp_path.parent / "etc").exists()


def test_tampered_manifest_cannot_delete_outside_mood_dir(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    mood = tmp_path / "modern"
    mood.mkdir()
    (mood / MANIFEST).write_text(json.dumps({"items": {
        "x1": "../victim.txt", "x2": str(victim), "x3": "sub/../../victim.txt", "x4": 5,
    }}))
    a = T("a")
    res = run(FakeClient([a]), tmp_path)
    assert victim.read_text() == "keep"
    assert res.error is None
    assert set(manifest(tmp_path)) == {a.id}


def test_shared_filename_not_deleted_while_still_listed(tmp_path):
    a = Track("abcdefgh1", "A", "S", "flac", 1)
    b = Track("abcdefgh2", "A", "S", "flac", 1)  # same name: same first 8 chars
    assert track_filename(a) == track_filename(b)
    run(FakeClient([a, b]), tmp_path)
    run(FakeClient([b]), tmp_path)
    assert files(tmp_path) == [track_filename(b)]


def test_two_tracks_same_name_different_ids_both_kept(tmp_path):
    a = Track("aaaaaaaa", "A", "Same", "flac", 1)
    b = Track("bbbbbbbb", "A", "Same", "flac", 1)
    res = run(FakeClient([a, b]), tmp_path)
    assert res.added == 2
    assert len(files(tmp_path)) == 2


# --- run_sync / trigger_rescan -------------------------------------------------

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from pireplace.config import JellyfinConfig, load_config
from pireplace.sync import RESCAN_PENDING, music_playing, run_sync, trigger_rescan


def _config(tmp_path, port=80):
    p = tmp_path / "etc" / "config.toml"
    p.parent.mkdir(exist_ok=True)
    p.write_text(f'[media]\nmusic_dir = "{tmp_path / "music"}"\n[web]\nport = {port}\n')
    return load_config(p)


def _jf(tmp_path, *moods):
    return JellyfinConfig(
        url="http://x", user_id="u" * 32, api_key_file=tmp_path / "k",
        playlists={m: "p" * 32 for m in moods},
    )


class PerMood:
    """Client whose playlist_tracks answers by playlist id."""

    def __init__(self, by_id):
        self.by_id = by_id

    def playlist_tracks(self, playlist_id):
        r = self.by_id[playlist_id]
        if isinstance(r, Exception):
            raise r
        return list(r)

    def download(self, item_id, dest):
        Path(dest).write_bytes(b"x")
        return 1


def idle():
    return False


@pytest.fixture(autouse=True)
def _plenty_of_space(monkeypatch):
    monkeypatch.setattr("pireplace.sync.default_free_bytes", lambda p: BIG)


def test_run_sync_rescans_only_when_changed(tmp_path):
    cfg, jf = _config(tmp_path), _jf(tmp_path, "modern")
    client = FakeClient([T("a")])
    calls = []
    assert run_sync(cfg, jf, client, lambda: calls.append(1) or True, idle) == 0
    assert calls == [1]
    assert run_sync(cfg, jf, client, lambda: calls.append(1) or True, idle) == 0
    assert calls == [1]


def test_run_sync_exit_1_when_any_mood_errors(tmp_path, caplog):
    cfg = _config(tmp_path)
    jf = JellyfinConfig("http://x", "u" * 32, tmp_path / "k", {"bad": "1" * 32, "good": "2" * 32})
    client = PerMood({"1" * 32: JellyfinError("down"), "2" * 32: [T("a")]})
    calls = []
    with caplog.at_level(logging.INFO):
        assert run_sync(cfg, jf, client, lambda: calls.append(1) or True, idle) == 1
    assert list((tmp_path / "music" / "good").glob("*.flac"))
    assert calls == [1]
    assert "mood=bad added=0 removed=0 failed=0 error=cannot list playlist: down" in caplog.text
    assert "mood=good added=1 removed=0 failed=0" in caplog.text
    assert caplog.text.index("mood=bad") < caplog.text.index("mood=good")


def test_run_sync_rescan_failure_still_exit_0(tmp_path):
    cfg, jf = _config(tmp_path), _jf(tmp_path, "modern")
    assert run_sync(cfg, jf, FakeClient([T("a")]), lambda: False, idle) == 0


class _Rescan(BaseHTTPRequestHandler):
    def do_GET(self):
        type(self).seen.append((self.path, dict(self.headers.items()), b""))
        body = json.dumps({"music": {"available": True, "playing": False}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        type(self).seen.append((self.path, dict(self.headers.items()), self.rfile.read(n)))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


@pytest.fixture
def rescan_server():
    handler = type("H", (_Rescan,), {"seen": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    t.start()
    yield server.server_address[1], handler.seen
    server.shutdown()
    server.server_close()
    t.join()


def test_trigger_rescan_sends_required_headers(rescan_server):
    port, seen = rescan_server
    assert trigger_rescan(port) is True
    path, headers, body = seen[0]
    assert path == "/api/rescan"
    assert headers["Host"] == f"127.0.0.1:{port}"  # urllib's own; the page accepts its address
    assert headers["Content-Type"] == "application/json"
    assert body == b"{}"


def test_trigger_rescan_unreachable_returns_false(caplog):
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with caplog.at_level(logging.WARNING):
        assert trigger_rescan(port, timeout=1.0) is False
    assert "rescan" in caplog.text


# --- the real control API (pireplace.web) --------------------------------------

from pireplace.web import make_server
from tests.test_web import STUB_STATUS, StubFireplace, _start, _stop


@pytest.fixture
def control():
    """A real control server that knows no host names: only its own address passes."""
    stub = StubFireplace()
    srv, t = _start(stub, allowed_hosts=("not-this-host.example",))
    yield srv
    _stop(srv, t)


def test_trigger_rescan_is_accepted_by_the_real_control_api(control):
    assert make_server  # the helper builds it with pireplace.web.make_server
    assert trigger_rescan(control.port) is True
    assert control.stub.calls == [("rescan",)]


def test_music_playing_reads_the_real_status(control, monkeypatch):
    assert music_playing(control.port) is True
    paused = {**STUB_STATUS, "music": {**STUB_STATUS["music"], "playing": False}}
    monkeypatch.setattr(control.stub, "status", lambda: paused)
    assert music_playing(control.port) is False
    monkeypatch.setattr(control.stub, "status", lambda: {**STUB_STATUS, "music": {"available": False}})
    assert music_playing(control.port) is False


def test_music_playing_is_none_when_status_cannot_be_read(control, monkeypatch, caplog):
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with caplog.at_level(logging.WARNING):
        assert music_playing(port, timeout=1.0) is None
    assert "status" in caplog.text
    monkeypatch.setattr(control.stub, "status", lambda: {"moods": []})  # no "music"
    assert music_playing(control.port) is None


# --- rescan deferred while music plays -----------------------------------------


class Calls:
    def __init__(self, playing=False, rescan_ok=True):
        self.playing_answer = playing
        self.rescan_ok = rescan_ok
        self.rescans = 0
        self.asked = 0

    def rescan(self):
        self.rescans += 1
        return self.rescan_ok

    def playing(self):
        self.asked += 1
        return self.playing_answer


def _marker(tmp_path):
    return tmp_path / "music" / RESCAN_PENDING


def test_changed_and_idle_rescans_now(tmp_path):
    cfg, jf = _config(tmp_path), _jf(tmp_path, "modern")
    c = Calls(playing=False)
    assert run_sync(cfg, jf, FakeClient([T("a")]), c.rescan, c.playing) == 0
    assert c.rescans == 1 and not _marker(tmp_path).exists()


@pytest.mark.parametrize("state", [True, None])
def test_changed_while_playing_or_unknown_defers_with_a_marker(tmp_path, state, caplog):
    cfg, jf = _config(tmp_path), _jf(tmp_path, "modern")
    c = Calls(playing=state)
    with caplog.at_level(logging.INFO):
        assert run_sync(cfg, jf, FakeClient([T("a")]), c.rescan, c.playing) == 0
    assert c.rescans == 0 and _marker(tmp_path).exists()
    assert "rescan deferred" in caplog.text


def test_pending_marker_rescans_on_a_later_run_without_changes(tmp_path):
    cfg, jf = _config(tmp_path), _jf(tmp_path, "modern")
    client = FakeClient([T("a")])
    run_sync(cfg, jf, client, Calls().rescan, Calls(playing=True).playing)
    assert _marker(tmp_path).exists()
    c = Calls(playing=False)
    assert run_sync(cfg, jf, client, c.rescan, c.playing) == 0  # nothing changed this run
    assert c.rescans == 1 and not _marker(tmp_path).exists()


def test_pending_marker_kept_while_still_playing(tmp_path):
    cfg, jf = _config(tmp_path), _jf(tmp_path, "modern")
    (tmp_path / "music").mkdir()
    _marker(tmp_path).write_text("")
    c = Calls(playing=True)
    assert run_sync(cfg, jf, FakeClient([]), c.rescan, c.playing) == 0
    assert c.rescans == 0 and _marker(tmp_path).exists()


def test_nothing_changed_and_no_marker_asks_nothing(tmp_path):
    cfg, jf = _config(tmp_path), _jf(tmp_path, "modern")
    c = Calls()
    assert run_sync(cfg, jf, FakeClient([]), c.rescan, c.playing) == 0
    assert (c.asked, c.rescans) == (0, 0) and not _marker(tmp_path).exists()


def test_failed_rescan_leaves_a_marker_for_the_next_run(tmp_path):
    cfg, jf = _config(tmp_path), _jf(tmp_path, "modern")
    c = Calls(playing=False, rescan_ok=False)
    assert run_sync(cfg, jf, FakeClient([T("a")]), c.rescan, c.playing) == 0
    assert c.rescans == 1 and _marker(tmp_path).exists()


def test_unwritable_marker_is_only_a_warning(tmp_path, monkeypatch, caplog):
    cfg, jf = _config(tmp_path), _jf(tmp_path, "modern")
    c = Calls(playing=True)

    def boom(self, *a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "touch", boom)
    with caplog.at_level(logging.WARNING):
        assert run_sync(cfg, jf, FakeClient([T("a")]), c.rescan, c.playing) == 0
    assert "pending" in caplog.text
