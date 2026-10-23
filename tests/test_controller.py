from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from pireplace.config import Config
from pireplace.controller import Fireplace, UnknownMood
from pireplace.library import find_moods
from pireplace.mpv import MpvClient, MpvUnavailable
from pireplace.state import State, StateStore
from tests.fake_mpv import FakeMpv

ODD = "Bublé – It's \"Beginning\".flac"

DEFAULT_TREE = {
    "classic-swing/Artist/Album/01 a.mp3": b"",
    "classic-swing/Artist/Album/02 b.mp3": b"",
    "modern/" + ODD: b"",
    "modern/two.ogg": b"",
    "modern/three.mp3": b"",
}


@dataclass
class Env:
    fp: Fireplace
    store: StateStore
    defaults: State
    video: FakeMpv
    music: FakeMpv
    modern_tracks: list[Path]


def _touch(root: Path, tree: dict) -> None:
    for rel, data in tree.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


@pytest.fixture
def env_factory(tmp_path):
    stack = []

    def make(start_music=True, music_tree=None, video_tree=None) -> Env:
        music_dir, video_dir = tmp_path / "music", tmp_path / "videos"
        music_dir.mkdir()
        video_dir.mkdir()
        _touch(music_dir, DEFAULT_TREE if music_tree is None else music_tree)
        _touch(video_dir, video_tree or {"fire.mp4": b"", "log.mkv": b""})
        cfg = Config(
            video_dir=video_dir, music_dir=music_dir,
            video_socket=tmp_path / "v.sock", music_socket=tmp_path / "m.sock",
            state_file=tmp_path / "state.json", start_music=start_music,
            default_mood="classic-swing", music_volume=60, log_volume=40,
            host="127.0.0.1", port=8080, hotspot_ssid="x", hotspot_password="",
            hotspot_after_s=300, hotspot_retry_s=300,
        )
        defaults = State(mood=cfg.default_mood, music_volume=60, log_volume=40,
                         log_muted=False, music_playing=start_music)
        store = StateStore(cfg.state_file, defaults)
        video = FakeMpv(cfg.video_socket).__enter__()
        music = FakeMpv(cfg.music_socket).__enter__()
        stack.extend([video, music])
        for fake in (video, music):
            fake.props.update({"playlist-count": 0, "volume": 0, "pause": False,
                               "mute": False, "media-title": "Song"})
        fp = Fireplace(cfg, store, MpvClient(cfg.video_socket), MpvClient(cfg.music_socket),
                       shuffle=lambda xs: xs.reverse())
        modern = sorted(find_moods(music_dir).get("modern", []))
        return Env(fp, store, defaults, video, music, modern)

    yield make
    for fake in stack:
        fake.stop()


@pytest.fixture
def env(env_factory):
    return env_factory()


def test_startup_loads_saved_mood_and_volumes(env):
    env.store.save(replace(env.defaults, mood="modern", music_volume=20, log_volume=70))
    env.fp.startup()
    assert env.music.commands[0][:3] == ["loadfile", str(env.modern_tracks[-1]), "replace"]
    assert env.music.props["volume"] == 20 and env.video.props["volume"] == 70


def test_startup_loads_videos(env):
    env.fp.startup()
    loads = [c for c in env.video.commands if c[0] == "loadfile"]
    assert [c[2] for c in loads] == ["replace", "append"]


def test_startup_saved_mood_gone_falls_back_to_default_then_first(env):
    env.store.save(replace(env.defaults, mood="deleted-mood"))
    env.fp.startup()
    assert env.fp.status()["mood"] == "classic-swing"


def test_startup_default_gone_falls_back_to_first(env_factory):
    env = env_factory(music_tree={"modern/a.mp3": b"", "zen/b.mp3": b""})
    env.fp.startup()
    assert env.fp.status()["mood"] == "modern"


def test_startup_skips_reload_if_players_already_have_playlists(env):
    env.video.props["playlist-count"] = 3
    env.music.props["playlist-count"] = 9
    env.fp.startup()
    assert not any(c[0] == "loadfile" for c in env.video.commands + env.music.commands)


def test_startup_first_boot_respects_start_music_false(env_factory):
    env = env_factory(start_music=False)
    env.fp.startup()
    assert env.music.props["pause"] is True


def test_startup_player_down_raises(env):
    env.music.stop()
    with pytest.raises(MpvUnavailable):
        env.fp.startup()


def test_no_moods_leaves_music_idle(env_factory):
    env = env_factory(music_tree={})
    env.fp.startup()
    s = env.fp.status()
    assert s["mood"] is None and s["moods"] == []
    assert not any(c[0] == "loadfile" for c in env.music.commands)


def test_set_mood_unknown_raises_and_keeps_state(env):
    env.fp.startup()
    with pytest.raises(UnknownMood):
        env.fp.set_mood("polka")
    assert env.store.load().mood == "classic-swing"


def test_set_mood_loads_odd_filenames_verbatim(env):
    env.fp.startup()
    env.fp.set_mood("modern")
    loaded = [c[1] for c in env.music.commands if c[0] == "loadfile"]
    assert ODD in [Path(p).name for p in loaded]


def test_set_mood_replaces_then_appends_unpauses_and_persists(env):
    env.fp.startup()
    env.music.commands.clear()
    env.music.props["pause"] = True
    env.fp.set_mood("modern")
    assert [c[2] for c in env.music.commands] == ["replace", "append", "append"]
    assert env.music.props["pause"] is False
    st = env.store.load()
    assert st.mood == "modern" and st.music_playing is True


@pytest.mark.parametrize("bad", [-1, 101, 50.5, "50", True])
def test_volume_validation(env, bad):
    with pytest.raises(ValueError):
        env.fp.set_music_volume(bad)
    with pytest.raises(ValueError):
        env.fp.set_log_volume(bad)


def test_volumes_set_and_persist(env):
    env.fp.startup()
    env.fp.set_music_volume(11)
    env.fp.set_log_volume(22)
    st = env.store.load()
    assert (st.music_volume, st.log_volume) == (11, 22)
    assert env.music.props["volume"] == 11 and env.video.props["volume"] == 22


def test_next_track(env):
    env.fp.next_track()
    assert ["playlist-next"] in env.music.commands


def test_status_shape(env):
    env.fp.startup()
    s = env.fp.status()
    assert s == {
        "moods": [{"name": "classic-swing", "label": "Classic Swing", "tracks": 2},
                  {"name": "modern", "label": "Modern", "tracks": 3}],
        "mood": "classic-swing",
        "music": {"available": True, "playing": True, "volume": 60, "now_playing": "Song"},
        "log": {"available": True, "volume": 40, "muted": False, "videos": 2},
    }


def test_status_when_music_player_down(env):
    env.music.stop()
    s = env.fp.status()
    assert s["music"]["available"] is False and s["log"]["available"] is True


def test_status_survives_missing_props_and_media_errors(env, monkeypatch):
    del env.video.props["mute"]
    assert env.fp.status()["log"]["available"] is False

    def boom(_):
        raise PermissionError

    import pireplace.controller as c

    monkeypatch.setattr(c, "find_moods", boom)
    monkeypatch.setattr(c, "find_videos", boom)
    s = env.fp.status()
    assert s["moods"] == [] and s["mood"] is None and s["music"]["available"] is True


def test_mutator_with_player_down_raises_and_does_not_persist(env):
    env.fp.startup()
    env.music.stop()
    with pytest.raises(MpvUnavailable):
        env.fp.set_music_volume(10)
    assert env.store.load().music_volume == 60


def test_pause_play_and_mute_persist(env):
    env.fp.startup()
    env.fp.pause()
    env.fp.set_log_muted(True)
    st = env.store.load()
    assert st.music_playing is False and st.log_muted is True
    assert env.music.props["pause"] is True and env.video.props["mute"] is True
    env.fp.play()
    assert env.store.load().music_playing is True and env.music.props["pause"] is False


def test_rescan_reloads_videos_and_current_mood(env):
    env.fp.startup()
    env.video.commands.clear()
    env.music.commands.clear()
    env.fp.rescan()
    assert [c[2] for c in env.video.commands] == ["replace", "append"]
    assert [c[2] for c in env.music.commands] == ["replace", "append"]


def test_rescan_current_mood_vanished_falls_back(env):
    env.fp.startup()
    env.fp.set_mood("modern")
    for p in env.modern_tracks:
        p.unlink()
    env.music.commands.clear()
    env.fp.rescan()
    assert env.store.load().mood == "classic-swing"
    assert env.music.commands and env.music.commands[0][0] == "loadfile"
