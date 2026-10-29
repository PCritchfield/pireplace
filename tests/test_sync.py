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
    (tmp_path / "modern" / track_filename(a)).write_bytes(b"already!!!")  # a.size == 10
    c = FakeClient([a])
    res = run(c, tmp_path)
    assert c.downloads == []
    assert res.error is None
    assert set(manifest(tmp_path)) == {a.id}
    assert (tmp_path / "modern" / track_filename(a)).read_bytes() == b"already!!!"


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


def _jf(tmp_path, *moods, audio_format="original", kbps=160):
    return JellyfinConfig(
        url="http://x", user_id="u" * 32, api_key_file=tmp_path / "k",
        playlists={m: "p" * 32 for m in moods},
        audio_format=audio_format, audio_bitrate_kbps=kbps,
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
    jf = JellyfinConfig("http://x", "u" * 32, tmp_path / "k", {"bad": "1" * 32, "good": "2" * 32}, "original")
    client = PerMood({"1" * 32: JellyfinError("down"), "2" * 32: [T("a")]})
    calls = []
    with caplog.at_level(logging.INFO):
        assert run_sync(cfg, jf, client, lambda: calls.append(1) or True, idle) == 1
    assert list((tmp_path / "music" / "good").glob("*.flac"))
    assert calls == [1]
    assert ("mood=bad added=0 removed=0 failed=0 delete_failed=0 skipped=0 "
            "error=cannot list playlist: down") in caplog.text
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


# --- download before delete, isolation, logging (final review) ---------------------


class Recording(FakeClient):
    """Notes which files were in the mood dir when each download started."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.present_at_download = []

    def download(self, item_id, dest):
        self.present_at_download.append(sorted(p.name for p in Path(dest).parent.iterdir()))
        return super().download(item_id, dest)


def test_downloads_happen_before_deletes(tmp_path):
    old, new = T("a"), T("b")
    run(FakeClient([old]), tmp_path)
    c = Recording([new])
    res = run(c, tmp_path)
    assert (res.added, res.removed) == (1, 1)
    assert track_filename(old) in c.present_at_download[0]
    assert files(tmp_path) == [track_filename(new)]


def test_deletes_still_happen_when_a_download_fails(tmp_path):
    old, new = T("a"), T("b")
    run(FakeClient([old]), tmp_path)
    res = run(FakeClient([new], fail_ids=[new.id]), tmp_path)
    assert (res.added, res.removed, res.failed) == (0, 1, 1)
    assert files(tmp_path) == []


def test_mkdir_error_is_a_mood_error(tmp_path):
    (tmp_path / "modern").write_text("a file where the folder should be")
    res = run(FakeClient([T("a")]), tmp_path)
    assert res.error and "filesystem error" in res.error


def test_free_space_error_is_a_mood_error(tmp_path):
    def broken(p):
        raise OSError("statvfs failed")

    res = run(FakeClient([T("a")]), tmp_path, free=broken)
    assert res.error and "statvfs failed" in res.error


def test_one_broken_mood_does_not_stop_the_others(tmp_path):
    cfg = _config(tmp_path)
    (tmp_path / "music").mkdir()
    (tmp_path / "music" / "bad").write_text("not a folder")
    jf = JellyfinConfig("http://x", "u" * 32, tmp_path / "k", {"bad": "1" * 32, "good": "2" * 32}, "original")
    client = PerMood({"1" * 32: [T("a")], "2" * 32: [T("b")]})
    assert run_sync(cfg, jf, client, lambda: True, idle) == 1
    assert list((tmp_path / "music" / "good").glob("*.flac"))


def test_each_failure_and_download_is_logged(tmp_path, caplog):
    a, b = T("a"), T("b")
    with caplog.at_level(logging.INFO):
        run(FakeClient([a, b], fail_ids=[b.id]), tmp_path)
    assert f"mood=modern item={b.id}: boom" in caplog.text
    assert f"mood=modern downloaded {track_filename(a)}" in caplog.text


def test_delete_failures_are_counted_apart_from_download_failures(tmp_path, monkeypatch, caplog):
    a, b = T("a"), T("b")
    run(FakeClient([a, b]), tmp_path)
    real_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        if self.name == track_filename(b):
            raise PermissionError("busy")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    with caplog.at_level(logging.WARNING):
        res = run(FakeClient([a]), tmp_path)
    assert (res.removed, res.failed, res.delete_failed) == (0, 0, 1)
    assert res.error == "1 deletes failed"
    assert f"mood=modern item={b.id}: cannot delete" in caplog.text
    assert b.id in manifest(tmp_path)  # still owned: retried next run


def test_skipped_playlist_items_make_the_mood_an_error(tmp_path):
    class Skipping(FakeClient):
        def playlist_tracks(self, playlist_id):
            from pireplace.jellyfin import TrackList

            tracks = TrackList(super().playlist_tracks(playlist_id))
            tracks.skipped = 2
            return tracks

    res = run(Skipping([T("a")]), tmp_path)
    assert res.added == 1 and res.skipped == 2
    assert res.error == "2 playlist items skipped (invalid Id)"


def test_existing_file_of_the_wrong_size_is_downloaded_again(tmp_path):
    a = T("a", size=13)  # FakeClient writes b"data-aaaaaaaa", 13 bytes
    (tmp_path / "modern").mkdir()
    (tmp_path / "modern" / track_filename(a)).write_bytes(b"half")
    c = FakeClient([a])
    res = run(c, tmp_path)
    assert c.downloads == [a.id] and res.added == 1
    assert (tmp_path / "modern" / track_filename(a)).read_bytes() == b"data-aaaaaaaa"


def test_stale_part_symlink_is_never_written_through(tmp_path):
    a = T("a")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (tmp_path / "modern").mkdir()
    (tmp_path / "modern" / f".{track_filename(a)}.part").symlink_to(victim)
    res = run(FakeClient([a]), tmp_path)
    assert res.added == 1 and victim.read_text() == "keep"


def test_long_extension_is_capped_and_name_fits():
    name = track_filename(Track("a" * 32, "A" * 300, "T" * 300, "x" * 500, 1))
    assert len(name.encode()) <= 200
    ext = name.rsplit(".", 1)[1]
    assert 0 < len(ext.encode()) <= 16
    assert name.startswith("A") and " - T" in name


def test_bidi_and_format_characters_are_stripped():
    name = track_filename(Track("a" * 32, "Evil\u202eArtist", "Zero\u200bWidth\ufeff", "fl\u2066ac", 1))
    assert name == "EvilArtist - ZeroWidth [aaaaaaaa].flac"


def test_corrupt_manifest_with_files_warns_ownership_reset(tmp_path, caplog):
    a = T("a")
    run(FakeClient([a]), tmp_path)
    (tmp_path / "modern" / MANIFEST).write_text("{corrupt")
    with caplog.at_level(logging.WARNING):
        run(FakeClient([a]), tmp_path)
    assert "ownership reset" in caplog.text


def test_missing_manifest_with_synced_files_warns_ownership_reset(tmp_path, caplog):
    a = T("a")
    run(FakeClient([a]), tmp_path)
    (tmp_path / "modern" / MANIFEST).unlink()
    with caplog.at_level(logging.WARNING):
        run(FakeClient([a]), tmp_path)
    assert "ownership reset" in caplog.text


def test_missing_manifest_with_only_manual_files_is_quiet(tmp_path, caplog):
    (tmp_path / "modern").mkdir()
    (tmp_path / "modern" / "my rip.flac").write_bytes(b"x")
    with caplog.at_level(logging.WARNING):
        run(FakeClient([]), tmp_path)
    assert "ownership reset" not in caplog.text


def test_unreadable_manifest_is_a_mood_error_not_a_reset(tmp_path, monkeypatch):
    a = T("a")
    run(FakeClient([a]), tmp_path)
    real = Path.read_text

    def read_text(self, *args, **kw):
        if self.name == MANIFEST:
            raise PermissionError("denied")
        return real(self, *args, **kw)

    monkeypatch.setattr(Path, "read_text", read_text)
    res = run(FakeClient([]), tmp_path)
    assert res.error and "denied" in res.error


# --- Opus transcodes, audio profile in the manifest, FLAC migration ------------------

import math

from pireplace.jellyfin import JellyfinClient
from pireplace.sync import estimate_bytes, plausible_size
from tests.fake_jellyfin import FakeJellyfin

PL = "p" * 32


@pytest.fixture
def fake():
    with FakeJellyfin() as f:
        yield f


def jclient(fake):
    return JellyfinClient(fake.url, fake.key, "u" * 32, timeout=5.0)


def jitem(id_, title, seconds, container="flac", size=30_000_000):
    return {
        "Id": id_, "AlbumArtist": "Art", "Name": title, "Container": container,
        "RunTimeTicks": int(seconds * 10_000_000),
        "MediaSources": [{"Container": container, "Size": size}],
    }


def ogg_body(seconds, kbps=160):
    """A body of exactly the expected transcode size: plausible."""
    n = int(seconds * kbps * 1000 / 8)
    return b"OggS" + b"\0" * (n - 4)


def jtrack(id_, title, seconds, container="flac", size=30_000_000):
    return Track(id_, "Art", title, container, size, float(seconds))


A, B, C = "a" * 32, "b" * 32, "c" * 32


def opus_run(client, tmp_path, free=lambda p: BIG, profile="opus-160", kbps=160):
    return sync_mood(client, "modern", PL, tmp_path / "modern", free_bytes=free,
                     profile=profile, bitrate_kbps=kbps)


def full_manifest(tmp_path):
    return json.loads((tmp_path / "modern" / MANIFEST).read_text())


def flac_mood(tmp_path, tracks):
    """A mood synced before transcoding existed: FLAC files and an old-format manifest."""
    mood = tmp_path / "modern"
    mood.mkdir()
    items = {}
    for t in tracks:
        name = track_filename(t)
        (mood / name).write_bytes(b"fLaC-" + t.id.encode())
        items[t.id] = name
    (mood / MANIFEST).write_text(json.dumps({"items": items}))
    return mood


def test_old_manifest_without_profiles_is_original(tmp_path):
    a, b = T("a"), T("b")
    flac_mood(tmp_path, [a, b])
    c = FakeClient([a, b])
    res = run(c, tmp_path)  # profile defaults to "original"
    assert (res.added, res.removed, res.failed, res.error) == (0, 0, 0, None)
    assert c.downloads == []
    assert manifest(tmp_path) == {a.id: track_filename(a), b.id: track_filename(b)}


def test_opus_profile_downloads_ogg_and_records_profile(fake, tmp_path):
    fake.playlists[PL] = [jitem(A, "One", 10), jitem(B, "Two", 12)]
    fake.transcodes = {A: ogg_body(10), B: ogg_body(12)}
    res = opus_run(jclient(fake), tmp_path)
    assert (res.added, res.failed, res.error) == (2, 0, None)
    names = files(tmp_path)
    assert names == sorted([track_filename(jtrack(A, "One", 10), "ogg"),
                            track_filename(jtrack(B, "Two", 12), "ogg")])
    assert all(n.endswith(".ogg") for n in names)
    m = full_manifest(tmp_path)
    assert m["profiles"] == {A: "opus-160", B: "opus-160"}
    assert set(m["items"].values()) == set(names)
    # the transcode endpoint, never the original download
    paths = [p for p, _ in fake.requests]
    assert not any("/Download" in p for p in paths)
    assert sum("/stream.ogg" in p for p in paths) == 2
    # a second run is a no-op
    fake.requests.clear()
    res = opus_run(jclient(fake), tmp_path)
    assert (res.added, res.removed, res.error) == (0, 0, None)
    assert not any("/stream.ogg" in p for p, _ in fake.requests)


class OrderClient:
    """Wraps a client and records downloads; Path.unlink of .flac files is recorded too."""

    def __init__(self, inner, events):
        self.inner, self.events = inner, events

    def playlist_tracks(self, pl):
        return self.inner.playlist_tracks(pl)

    def download(self, item_id, dest):
        self.events.append(("download", item_id))
        return self.inner.download(item_id, dest)

    def download_transcoded(self, item_id, dest, bitrate_kbps, **kw):
        self.events.append(("transcode", item_id))
        return self.inner.download_transcoded(item_id, dest, bitrate_kbps, **kw)


@pytest.fixture
def flac_unlinks(monkeypatch):
    events = []
    real = Path.unlink

    def unlink(self, *a, **k):
        if self.suffix == ".flac":
            events.append(("unlink", self.name))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", unlink)
    return events


def test_flac_migrates_to_ogg_download_before_delete(fake, tmp_path, flac_unlinks):
    ta, tb = jtrack(A, "One", 10), jtrack(B, "Two", 12)
    flac_mood(tmp_path, [ta, tb])
    fake.playlists[PL] = [jitem(A, "One", 10), jitem(B, "Two", 12)]
    fake.transcodes = {A: ogg_body(10), B: ogg_body(12)}
    events = flac_unlinks
    res = opus_run(OrderClient(jclient(fake), events), tmp_path)
    assert res.error is None and res.failed == 0
    assert files(tmp_path) == sorted([track_filename(ta, "ogg"), track_filename(tb, "ogg")])
    m = full_manifest(tmp_path)
    assert m["items"] == {A: track_filename(ta, "ogg"), B: track_filename(tb, "ogg")}
    assert m["profiles"] == {A: "opus-160", B: "opus-160"}
    kinds = [k for k, _ in events]
    assert kinds.count("transcode") == 2 and kinds.count("unlink") == 2
    assert "download" not in kinds
    last_transcode = max(i for i, k in enumerate(kinds) if k == "transcode")
    first_unlink = min(i for i, k in enumerate(kinds) if k == "unlink")
    assert last_transcode < first_unlink


def test_failed_transcode_keeps_flac(fake, tmp_path):
    ta, tb = jtrack(A, "One", 10), jtrack(B, "Two", 12)
    flac_mood(tmp_path, [ta, tb])
    fake.playlists[PL] = [jitem(A, "One", 10), jitem(B, "Two", 12)]
    fake.transcodes = {A: ogg_body(10), B: ogg_body(12)}
    fake.fail[f"/Audio/{B}/stream.ogg"] = 403
    res = opus_run(jclient(fake), tmp_path)
    assert res.failed == 1 and res.error
    assert files(tmp_path) == sorted([track_filename(ta, "ogg"), track_filename(tb)])
    m = full_manifest(tmp_path)
    assert m["items"] == {A: track_filename(ta, "ogg"), B: track_filename(tb)}
    assert m["profiles"] == {A: "opus-160", B: "original"}
    assert (tmp_path / "modern" / track_filename(tb)).read_bytes() == b"fLaC-" + B.encode()
    assert list((tmp_path / "modern").glob(".*.part")) == []
    assert not any("/Download" in p for p, _ in fake.requests)  # never falls back
    # the next run retries only the failed one
    del fake.fail[f"/Audio/{B}/stream.ogg"]
    fake.requests.clear()
    res = opus_run(jclient(fake), tmp_path)
    assert (res.failed, res.error) == (0, None)
    assert [p for p, _ in fake.requests if "/stream.ogg" in p][0].startswith(f"/Audio/{B}/")
    assert files(tmp_path) == sorted([track_filename(ta, "ogg"), track_filename(tb, "ogg")])


def test_truncated_transcode_is_not_accepted(fake, tmp_path):
    ta = jtrack(A, "Long", 240)
    flac_mood(tmp_path, [ta])
    fake.playlists[PL] = [jitem(A, "Long", 240)]
    fake.transcodes = {A: b"OggS" + b"\0" * 1000}  # 240 s at 160k is ~4.8 MB
    res = opus_run(jclient(fake), tmp_path)
    assert res.failed == 1 and res.error
    assert files(tmp_path) == [track_filename(ta)]
    assert full_manifest(tmp_path)["items"] == {A: track_filename(ta)}
    assert list((tmp_path / "modern").glob(".*.part")) == []


def test_oversized_transcode_is_cut_off_by_max_bytes(fake, tmp_path):
    ta = jtrack(A, "Short", 10)
    flac_mood(tmp_path, [ta])
    fake.playlists[PL] = [jitem(A, "Short", 10)]
    # 10 s at 160k is 200 kB; the cap is max(1 MiB, 3x) = 1 MiB
    fake.transcodes = {A: b"OggS" + b"\0" * (2 * 1024**2)}
    res = opus_run(jclient(fake), tmp_path)
    assert res.failed == 1
    assert files(tmp_path) == [track_filename(ta)]


def test_max_bytes_passed_from_duration(tmp_path):
    seen = {}

    class C(FakeClient):
        def download_transcoded(self, item_id, dest, bitrate_kbps, max_bytes=None):
            seen[item_id] = max_bytes
            Path(dest).write_bytes(b"OggS" + b"\0" * 100)
            return 104

    long_, unknown = jtrack(A, "Long", 600), Track(B, "Art", "U", "flac", 10, None)
    opus_run(C([long_, unknown]), tmp_path)
    assert seen[A] == math.ceil(3 * 600 * 160 * 1000 / 8)
    assert seen[B] is None  # the client default


def test_migration_keeps_flac_when_space_runs_out(fake, tmp_path, flac_unlinks):
    ts = [jtrack(A, "One", 10), jtrack(B, "Two", 10), jtrack(C, "Three", 10)]
    flac_mood(tmp_path, ts)
    fake.playlists[PL] = [jitem(A, "One", 10), jitem(B, "Two", 10), jitem(C, "Three", 10)]
    fake.transcodes = {A: ogg_body(10), B: ogg_body(10), C: ogg_body(10)}
    calls = []

    def free(p):
        calls.append(1)
        return BIG if len(calls) <= 2 else MIN_FREE_RUN + 5  # start check, first download

    res = opus_run(jclient(fake), tmp_path, free=free)
    assert (res.added, res.failed) == (1, 2) and res.error
    assert files(tmp_path) == sorted([track_filename(ts[0], "ogg"),
                                      track_filename(ts[1]), track_filename(ts[2])])
    assert [n for _, n in flac_unlinks] == [track_filename(ts[0])]
    m = full_manifest(tmp_path)
    assert m["profiles"] == {A: "opus-160", B: "original", C: "original"}


def test_space_check_uses_estimate_for_transcodes(tmp_path):
    class C(FakeClient):
        def download_transcoded(self, item_id, dest, bitrate_kbps, max_bytes=None):
            raise AssertionError("must not download")

    t = jtrack(A, "Long", 240, size=1)  # FLAC size is irrelevant for transcodes
    res = opus_run(C([t]), tmp_path, free=lambda p: MIN_FREE_START + estimate_bytes(t, 160) - 1)
    assert res.error and "not enough space" in res.error


def test_estimate_and_plausible_size():
    t = Track(A, "a", "b", "flac", 1, 240.0)
    assert estimate_bytes(t, 160) == math.ceil(240 * 20000 * 1.5)
    assert estimate_bytes(Track(A, "a", "b", "flac", 1), 160) == 10 * 1024**2
    expected = 240 * 160 * 1000 / 8
    assert plausible_size(math.ceil(0.25 * expected), 240.0, 160)
    assert not plausible_size(math.ceil(0.25 * expected) - 1, 240.0, 160)
    assert plausible_size(int(3 * expected), 240.0, 160)
    assert not plausible_size(int(3 * expected) + 1, 240.0, 160)
    assert plausible_size(1, None, 160)
    assert not plausible_size(0, None, 160)


def test_track_filename_ext_override():
    t = Track(A, "Art", "Song", "flac", 1)
    assert track_filename(t, "ogg") == "Art - Song [aaaaaaaa].ogg"
    assert track_filename(t) == "Art - Song [aaaaaaaa].flac"


def test_original_profile_unchanged(fake, tmp_path):
    fake.playlists[PL] = [jitem(A, "One", 10, size=5)]
    fake.files[A] = b"fLaC!"
    fake.transcodes[A] = ogg_body(10)
    # the space check uses Track.size: exactly enough for 5 bytes
    res = opus_run(jclient(fake), tmp_path, free=lambda p: MIN_FREE_START + 5, profile="original")
    assert (res.added, res.error) == (1, None)
    assert files(tmp_path) == [track_filename(jtrack(A, "One", 10))]
    paths = [p for p, _ in fake.requests]
    assert any(p.startswith(f"/Items/{A}/Download") for p in paths)
    assert not any("/stream.ogg" in p for p in paths)
    assert full_manifest(tmp_path)["profiles"] == {A: "original"}


def _ogg_adopt_setup(tmp_path, body):
    ta = jtrack(A, "One", 10)
    mood = flac_mood(tmp_path, [ta])
    (mood / track_filename(ta, "ogg")).write_bytes(body)  # a crash after the rename
    return ta, mood


def test_crashed_migration_ogg_is_adopted_if_plausible(tmp_path):
    ta, mood = _ogg_adopt_setup(tmp_path, ogg_body(10))

    class C(FakeClient):
        def download_transcoded(self, *a, **k):
            raise AssertionError("must adopt, not download")

    res = opus_run(C([ta]), tmp_path)
    assert res.error is None
    assert files(tmp_path) == [track_filename(ta, "ogg")]
    m = full_manifest(tmp_path)
    assert m["items"] == {A: track_filename(ta, "ogg")} and m["profiles"] == {A: "opus-160"}


@pytest.mark.parametrize("body", [b"fLaC" + b"\0" * 200_000, b"OggS" + b"\0" * 100])
def test_crashed_ogg_not_adopted_if_not_ogg_or_implausible(tmp_path, body):
    ta, mood = _ogg_adopt_setup(tmp_path, body)
    got = []

    class C(FakeClient):
        def download_transcoded(self, item_id, dest, bitrate_kbps, max_bytes=None):
            got.append(item_id)
            Path(dest).write_bytes(ogg_body(10))
            return 200_000

    res = opus_run(C([ta]), tmp_path)
    assert got == [A] and res.error is None
    assert (mood / track_filename(ta, "ogg")).read_bytes() == ogg_body(10)
    assert files(tmp_path) == [track_filename(ta, "ogg")]


def test_original_ogg_at_same_name_is_not_adopted_as_opus(tmp_path):
    """An original Ogg file has the transcode's name; it is the old file, never adopted."""
    t = jtrack(A, "One", 10, container="ogg")
    mood = flac_mood(tmp_path, [t])
    (mood / track_filename(t)).write_bytes(ogg_body(10))  # plausible-looking, but original
    got = []

    class C(FakeClient):
        def download_transcoded(self, item_id, dest, bitrate_kbps, max_bytes=None):
            got.append(item_id)
            Path(dest).write_bytes(ogg_body(10)[:-1] + b"!")
            return 200_000

    res = opus_run(C([t]), tmp_path)
    assert got == [A] and res.error is None
    assert files(tmp_path) == [track_filename(t, "ogg")]
    assert (mood / track_filename(t, "ogg")).read_bytes().endswith(b"!")
    assert full_manifest(tmp_path)["profiles"] == {A: "opus-160"}


def test_run_sync_passes_profile_and_bitrate(tmp_path):
    got = []

    class C(FakeClient):
        def download_transcoded(self, item_id, dest, bitrate_kbps, max_bytes=None):
            got.append(bitrate_kbps)
            Path(dest).write_bytes(ogg_body(10, 96))
            return 1

    cfg, jf = _config(tmp_path), _jf(tmp_path, "modern", audio_format="opus", kbps=96)
    assert run_sync(cfg, jf, C([jtrack(A, "One", 10)]), lambda: True, idle) == 0
    assert got == [96]
    m = json.loads((tmp_path / "music" / "modern" / MANIFEST).read_text())
    assert m["profiles"] == {A: "opus-96"}
