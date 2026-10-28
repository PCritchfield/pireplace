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
