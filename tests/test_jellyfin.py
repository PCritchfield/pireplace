import socket
import time

import pytest

from pireplace.jellyfin import JellyfinClient, JellyfinError, Track
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


def test_download_http_error_removes_preexisting_dest(fake, tmp_path):
    dest = tmp_path / ".part"
    dest.write_bytes(b"stale")
    with pytest.raises(JellyfinError) as e:
        client(fake).download("z" * 32, dest)  # 404
    assert "404" in str(e.value) and fake.key not in str(e.value)
    assert not dest.exists()


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
