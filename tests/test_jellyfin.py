import logging
import socket
from urllib.parse import parse_qs, urlsplit
import time

import pytest

from pireplace.jellyfin import JellyfinClient, JellyfinError, Track, TrackList
from tests.fake_jellyfin import FakeJellyfin

USER = "u" * 32
PL = "p" * 32


@pytest.fixture
def fake():
    with FakeJellyfin() as f:
        yield f


def client(fake, timeout=5.0):
    return JellyfinClient(fake.url, fake.key, USER, timeout=timeout)


def item(id_, artist, title, container, size):
    return {
        "Id": id_,
        "AlbumArtist": artist,
        "Name": title,
        "Container": container,
        "MediaSources": [{"Container": container, "Size": size}],
    }


def test_lists_tracks_with_exact_auth_header(fake):
    fake.playlists[PL] = [item("a" * 32, "Frank Sinatra", "Jingle Bells", "flac", 1234)]
    tracks = client(fake).playlist_tracks(PL)
    assert tracks == [Track("a" * 32, "Frank Sinatra", "Jingle Bells", "flac", 1234)]
    path, headers = fake.requests[-1]
    assert path.startswith(f"/Playlists/{PL}/Items?") and "userId=" in path
    assert headers["Authorization"] == f'MediaBrowser Token="{fake.key}"'
    assert "X-Emby-Token" not in headers


def test_duplicate_ids_collapsed(fake):
    a, b = "a" * 32, "b" * 32
    fake.playlists[PL] = [
        item(a, "A", "One", "mp3", 1),
        item(b, "B", "Two", "mp3", 2),
        item(a, "A", "One again", "mp3", 1),
    ]
    tracks = client(fake).playlist_tracks(PL)
    assert [t.id for t in tracks] == [a, b]
    assert tracks[0].title == "One"


def test_artist_fallbacks_and_missing_fields(fake):
    fake.playlists[PL] = [
        {"Id": "1" * 32, "Artists": ["First", "Second"], "Name": "T1", "Container": "mp3"},
        {"Id": "2" * 32, "Name": "T2", "MediaSources": [{"Container": "FLAC", "Size": 99}]},
        {"Id": "3" * 32},
    ]
    t1, t2, t3 = client(fake).playlist_tracks(PL)
    assert t1 == Track("1" * 32, "First", "T1", "mp3", None)
    assert t2 == Track("2" * 32, None, "T2", "flac", 99)
    assert t3 == Track("3" * 32, None, None, None, None)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_list_errors_raise_without_key(fake, status):
    fake.fail[f"/Playlists/{PL}/Items"] = status
    with pytest.raises(JellyfinError) as e:
        client(fake).playlist_tracks(PL)
    assert str(status) in str(e.value) and fake.key not in str(e.value)
    assert f"/Playlists/{PL}/Items" in str(e.value)
    assert e.value.__cause__ is None and e.value.__context__ is None


def test_wrong_key_is_401_without_leaking_key(fake):
    bad = JellyfinClient(fake.url, "wrong-key-xyz", USER, timeout=5.0)
    with pytest.raises(JellyfinError) as e:
        bad.playlist_tracks(PL)
    assert "401" in str(e.value) and "wrong-key-xyz" not in str(e.value)


def test_html_error_page_is_jellyfin_error(fake):
    fake.html.add(f"/Playlists/{PL}/Items")
    with pytest.raises(JellyfinError) as e:
        client(fake).playlist_tracks(PL)
    assert e.value.__cause__ is None and e.value.__context__ is None


def test_download_writes_file(fake, tmp_path):
    fake.files["a" * 32] = b"x" * 3_000_000
    dest = tmp_path / ".part"
    assert client(fake).download("a" * 32, dest) == 3_000_000 and dest.read_bytes() == b"x" * 3_000_000
    assert fake.requests[-1][0] == f"/Items/{'a' * 32}/Download"


def test_truncated_download_removes_dest(fake, tmp_path):
    fake.files["a" * 32] = b"x" * 1000
    fake.truncate.add("a" * 32)
    dest = tmp_path / ".part"
    with pytest.raises(JellyfinError):
        client(fake).download("a" * 32, dest)
    assert not dest.exists()


def test_download_http_error_leaves_preexisting_dest(fake, tmp_path):
    dest = tmp_path / ".part"
    dest.write_bytes(b"stale")
    with pytest.raises(JellyfinError) as e:
        client(fake).download("f" * 32, dest)  # 404
    assert "404" in str(e.value) and fake.key not in str(e.value)
    assert dest.read_bytes() == b"stale"


def test_download_existing_dest_is_not_deleted(fake, tmp_path):
    fake.files["a" * 32] = b"x" * 10
    dest = tmp_path / ".part"
    dest.write_bytes(b"mine")
    with pytest.raises(JellyfinError):
        client(fake).download("a" * 32, dest)
    assert dest.read_bytes() == b"mine"


def test_unreachable_server_is_jellyfin_error(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    c = JellyfinClient(f"http://127.0.0.1:{port}", "secret-key-abc", USER, timeout=0.5)
    with pytest.raises(JellyfinError) as e:
        c.playlist_tracks(PL)
    assert "secret-key-abc" not in str(e.value)
    assert e.value.__cause__ is None
    dest = tmp_path / ".part"
    with pytest.raises(JellyfinError):
        c.download("a" * 32, dest)
    assert not dest.exists()


def test_environment_proxy_is_never_used(fake, monkeypatch):
    # A proxy from the environment would see the token; the client must ignore it.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(var, f"http://127.0.0.1:{dead}")
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    fake.playlists[PL] = [item("a" * 32, "A", "T", "flac", 1)]
    assert [t.id for t in client(fake).playlist_tracks(PL)] == ["a" * 32]


def test_trickling_download_hits_the_per_file_deadline(fake, tmp_path):
    fake.files["a" * 32] = b"x" * 400
    fake.trickle["a" * 32] = 0.02  # 4 bytes every 20 ms: about 2 s in all
    c = JellyfinClient(fake.url, fake.key, USER, timeout=5.0, download_deadline=0.15)
    dest = tmp_path / ".part"
    started = time.monotonic()
    with pytest.raises(JellyfinError) as e:
        c.download("a" * 32, dest)
    assert time.monotonic() - started < 1.5  # gave up mid-stream, not at the end
    assert "took longer than" in str(e.value) and fake.key not in str(e.value)
    assert not dest.exists()


def test_default_download_deadline_is_fifteen_minutes(fake):
    assert client(fake)._download_deadline == 15 * 60


def test_list_asks_for_media_sources(fake):
    fake.playlists[PL] = [item("a" * 32, "A", "T", "flac", 1)]
    client(fake).playlist_tracks(PL)
    query = parse_qs(urlsplit(fake.requests[-1][0]).query)
    assert query["fields"] == ["MediaSources"] and query["userId"] == [USER]


def test_items_without_a_valid_id_are_skipped_and_counted(fake, caplog):
    fake.playlists[PL] = [
        item("a" * 32, "A", "Good", "flac", 1),
        item("../../etc/passwd", "B", "Bad", "flac", 1),
        item("b" * 31, "C", "Short", "flac", 1),
        {"Name": "No Id"},
        {"Id": 12345, "Name": "Numeric"},
    ]
    with caplog.at_level(logging.WARNING):
        tracks = client(fake).playlist_tracks(PL)
    assert [t.id for t in tracks] == ["a" * 32]
    assert isinstance(tracks, TrackList) and tracks.skipped == 4
    assert caplog.text.count("invalid Id") == 4


def test_download_is_fsynced_before_returning(fake, tmp_path, monkeypatch):
    import pireplace.jellyfin as jf

    synced = []
    real = jf.os.fsync
    monkeypatch.setattr(jf.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    fake.files["a" * 32] = b"x" * 10
    client(fake).download("a" * 32, tmp_path / ".part")
    assert len(synced) == 1


def test_download_never_writes_through_an_existing_symlink(fake, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    dest = tmp_path / ".part"
    dest.symlink_to(victim)
    fake.files["a" * 32] = b"x" * 10
    with pytest.raises(JellyfinError):
        client(fake).download("a" * 32, dest)
    assert victim.read_text() == "keep"


def test_track_duration_from_runtime_ticks(fake):
    a, b, c = "a" * 32, "b" * 32, "c" * 32
    fake.playlists[PL] = [
        {**item(a, "x", "t", "flac", 1), "RunTimeTicks": 2_400_000_000},
        item(b, "x", "t", "flac", 1),
        {**item(c, "x", "t", "flac", 1), "RunTimeTicks": True},
    ]
    tracks = client(fake).playlist_tracks(PL)
    assert [t.duration_s for t in tracks] == [240.0, None, None]


def test_track_duration_rejects_non_positive_and_non_int():
    from pireplace.jellyfin import _track

    base = item("a" * 32, "x", "t", "flac", 1)
    for bad in (0, -5, "100", 1.5, None):
        assert _track({**base, "RunTimeTicks": bad}).duration_s is None


def test_transcoded_download_writes_ogg(fake, tmp_path):
    fake.transcodes["a" * 32] = b"OggS" + b"x" * 1000
    dest = tmp_path / ".part"
    assert client(fake).download_transcoded("a" * 32, dest, 160) == 1004
    assert dest.read_bytes() == b"OggS" + b"x" * 1000
    parts = urlsplit(fake.requests[-1][0])
    assert parts.path == f"/Audio/{'a' * 32}/stream.ogg"
    q = parse_qs(parts.query)
    assert q["audioCodec"] == ["opus"] and q["audioBitRate"] == ["160000"]
    assert q["static"] == ["false"] and q["audioChannels"] == ["2"] and q["userId"] == [USER]
    assert fake.requests[-1][1]["Authorization"] == f'MediaBrowser Token="{fake.key}"'


def test_transcoded_download_accepts_content_type_parameters(fake, tmp_path):
    fake.transcodes["a" * 32] = b"OggS" + b"x" * 10
    fake.transcode_type["a" * 32] = "audio/ogg; codecs=opus"
    assert client(fake).download_transcoded("a" * 32, tmp_path / ".part", 128) == 14


def test_transcoded_download_rejects_non_ogg_body(fake, tmp_path):
    dest = tmp_path / ".part"
    fake.html.add(f"/Audio/{'a' * 32}/stream.ogg")
    with pytest.raises(JellyfinError):
        client(fake).download_transcoded("a" * 32, dest, 160)
    assert not dest.exists()
    fake.transcodes["b" * 32] = b"<html>"
    fake.transcode_type["b" * 32] = "audio/ogg"
    with pytest.raises(JellyfinError):
        client(fake).download_transcoded("b" * 32, dest, 160)
    assert not dest.exists()
    fake.transcodes["c" * 32] = b"OggS" + b"x" * 10
    fake.transcode_type["c" * 32] = "text/html"
    with pytest.raises(JellyfinError):
        client(fake).download_transcoded("c" * 32, dest, 160)
    assert not dest.exists()


def test_transcoded_download_rejects_short_body(fake, tmp_path):
    dest = tmp_path / ".part"
    fake.transcodes["a" * 32] = b"Og"
    with pytest.raises(JellyfinError):
        client(fake).download_transcoded("a" * 32, dest, 160)
    assert not dest.exists()


def test_transcoded_download_truncated_removes_dest(fake, tmp_path):
    dest = tmp_path / ".part"
    fake.transcodes["a" * 32] = b"OggS" + b"x" * 1000
    fake.truncate.add("a" * 32)
    with pytest.raises(JellyfinError):
        client(fake).download_transcoded("a" * 32, dest, 160)
    assert not dest.exists()


def test_transcoded_download_http_error_has_no_secrets(fake, tmp_path):
    path = f"/Audio/{'a' * 32}/stream.ogg"
    fake.fail[path] = 403
    dest = tmp_path / ".part"
    dest.write_bytes(b"stale")
    with pytest.raises(JellyfinError) as e:
        client(fake).download_transcoded("a" * 32, dest, 160)
    msg = str(e.value)
    assert "403" in msg and fake.key not in msg and "audioBitRate" not in msg
    assert e.value.__cause__ is None and dest.read_bytes() == b"stale"


def test_transcoded_download_never_follows_redirect(fake, tmp_path):
    with FakeJellyfin() as other:
        other.key = fake.key
        other.transcodes["a" * 32] = b"OggS" + b"x" * 10
        fake.redirects[f"/Audio/{'a' * 32}/stream.ogg"] = other.url + f"/Audio/{'a' * 32}/stream.ogg"
        dest = tmp_path / ".part"
        with pytest.raises(JellyfinError):
            client(fake).download_transcoded("a" * 32, dest, 160)
        assert not dest.exists()
        assert other.requests == []


def test_transcoded_download_hits_the_deadline(fake, tmp_path):
    fake.transcodes["a" * 32] = b"OggS" + b"x" * 400
    fake.trickle["a" * 32] = 0.05
    c = JellyfinClient(fake.url, fake.key, USER, timeout=5.0, download_deadline=0.2)
    dest = tmp_path / ".part"
    with pytest.raises(JellyfinError):
        c.download_transcoded("a" * 32, dest, 160)
    assert not dest.exists()


def test_transcoded_download_is_fsynced_and_never_follows_symlink(fake, tmp_path, monkeypatch):
    import pireplace.jellyfin as jf

    synced = []
    real = jf.os.fsync
    monkeypatch.setattr(jf.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    fake.transcodes["a" * 32] = b"OggS" + b"x" * 10
    client(fake).download_transcoded("a" * 32, tmp_path / ".part", 160)
    assert len(synced) == 1
    victim = tmp_path / "victim"
    victim.write_text("keep")
    dest = tmp_path / ".link"
    dest.symlink_to(victim)
    with pytest.raises(JellyfinError):
        client(fake).download_transcoded("a" * 32, dest, 160)
    assert victim.read_text() == "keep"


def test_transcoded_download_over_max_bytes_aborts(fake, tmp_path):
    fake.transcodes["a" * 32] = b"OggS" + b"x" * 5000
    dest = tmp_path / ".part"
    with pytest.raises(JellyfinError) as e:
        client(fake).download_transcoded("a" * 32, dest, 160, max_bytes=1000)
    assert "audioBitRate" not in str(e.value)
    assert not dest.exists()


def test_transcoded_content_type_reject_leaves_preexisting_dest(fake, tmp_path):
    fake.transcodes["a" * 32] = b"OggS" + b"x" * 10
    fake.transcode_type["a" * 32] = "text/html"
    dest = tmp_path / ".part"
    dest.write_bytes(b"mine")
    with pytest.raises(JellyfinError):
        client(fake).download_transcoded("a" * 32, dest, 160)
    assert dest.read_bytes() == b"mine"


def test_transcoded_existing_dest_is_not_deleted(fake, tmp_path):
    fake.transcodes["a" * 32] = b"OggS" + b"x" * 10
    dest = tmp_path / ".part"
    dest.write_bytes(b"mine")
    with pytest.raises(JellyfinError):
        client(fake).download_transcoded("a" * 32, dest, 160)
    assert dest.read_bytes() == b"mine"


@pytest.mark.parametrize("bad", ["..", "a/b", "", "z" * 32, "a" * 31, "a" * 33])
def test_downloads_reject_bad_item_ids_without_a_request(fake, tmp_path, bad):
    with pytest.raises(JellyfinError):
        client(fake).download(bad, tmp_path / "d1")
    with pytest.raises(JellyfinError):
        client(fake).download_transcoded(bad, tmp_path / "d2", 160)
    assert fake.requests == []


@pytest.mark.parametrize("bad", [True, 31, 513, 0, -1, 160.0, "160", None])
def test_transcoded_download_rejects_bad_bitrate_without_a_request(fake, tmp_path, bad):
    with pytest.raises(JellyfinError):
        client(fake).download_transcoded("a" * 32, tmp_path / ".part", bad)
    assert fake.requests == []
