from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from pireplace.config import Config, Season
from pireplace.controller import EmptySeason, Fireplace, UnknownMood, UnknownSeason
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
    marker: Path


def _touch(root: Path, tree: dict) -> None:
    for rel, data in tree.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


@pytest.fixture
def env_factory(tmp_path):
    stack = []

    def make(start_music=True, music_tree=None, video_tree=None, seasons=(),
             default_mood="classic-swing") -> Env:
        music_dir, video_dir = tmp_path / "music", tmp_path / "videos"
        music_dir.mkdir()
        video_dir.mkdir()
        _touch(music_dir, DEFAULT_TREE if music_tree is None else music_tree)
        _touch(video_dir, video_tree or {"fire.mp4": b"", "log.mkv": b""})
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        cfg = Config(
            video_dir=video_dir, music_dir=music_dir,
            video_socket=run_dir / "v.sock", music_socket=run_dir / "m.sock",
            state_file=tmp_path / "state.json", start_music=start_music,
            default_mood=default_mood, seasons=tuple(seasons), music_volume=60, log_volume=40,
            host="127.0.0.1", port=8080, hotspot_ssid="x", hotspot_password="",
            hotspot_after_s=300, hotspot_retry_s=300,
        )
        defaults = State(mood=cfg.default_mood, music_volume=60, log_volume=40,
                         log_muted=False, music_playing=start_music)
        store = StateStore(cfg.state_file, defaults)
        video = FakeMpv(cfg.video_socket).__enter__()
        music = FakeMpv(cfg.music_socket).__enter__()
        stack.extend([video, music])
        for pid, fake in ((101, video), (202, music)):
            fake.props.update({"playlist-count": 0, "volume": 0, "pause": False,
                               "mute": False, "media-title": "Song", "pid": pid})
        fp = Fireplace(cfg, store, MpvClient(cfg.video_socket), MpvClient(cfg.music_socket),
                       shuffle=lambda xs: xs.reverse())
        modern = sorted(find_moods(music_dir).get("modern", []))
        return Env(fp, store, defaults, video, music, modern, run_dir / "boot-done")

    yield make
    for fake in stack:
        fake.stop()


@pytest.fixture
def env(env_factory):
    return env_factory()


def test_reconcile_loads_saved_mood_and_volumes(env):
    env.store.save(replace(env.defaults, mood="modern", music_volume=20, log_volume=70))
    env.fp.reconcile()
    assert env.music.commands[0][:3] == ["loadfile", str(env.modern_tracks[-1]), "replace"]
    assert env.music.props["volume"] == 20 and env.video.props["volume"] == 70


def test_reconcile_loads_videos(env):
    env.fp.reconcile()
    loads = [c for c in env.video.commands if c[0] == "loadfile"]
    assert [c[2] for c in loads] == ["replace", "append"]


def test_reconcile_saved_mood_gone_falls_back_to_default_then_first(env):
    env.store.save(replace(env.defaults, mood="deleted-mood"))
    env.fp.reconcile()
    assert env.fp.status()["mood"] == "classic-swing"


def test_reconcile_default_gone_falls_back_to_first(env_factory):
    env = env_factory(music_tree={"modern/a.mp3": b"", "zen/b.mp3": b""})
    env.fp.reconcile()
    assert env.fp.status()["mood"] == "modern"


def test_reconcile_skips_reload_if_players_already_have_playlists(env):
    env.video.props["playlist-count"] = 3
    env.music.props["playlist-count"] = 9
    env.fp.reconcile()
    assert not any(c[0] == "loadfile" for c in env.video.commands + env.music.commands)


def test_reconcile_first_boot_respects_start_music_false(env_factory):
    env = env_factory(start_music=False)
    env.fp.reconcile()
    assert env.music.props["pause"] is True


def test_reconcile_one_player_down_still_reconciles_the_other(env):
    env.music.stop()
    env.fp.reconcile()  # does not raise
    assert [c[2] for c in env.video.commands if c[0] == "loadfile"] == ["replace", "append"]
    assert env.video.props["volume"] == 40
    assert not env.marker.exists()  # boot not finished until both players are set up


def test_reconcile_survives_unreadable_music_dir(env, monkeypatch):
    import pireplace.controller as c

    def boom(_):
        raise PermissionError("music dir")

    monkeypatch.setattr(c, "find_moods", boom)
    env.fp.reconcile()
    assert env.video.props["volume"] == 40


def test_boot_without_marker_applies_boot_defaults_and_creates_marker(env):
    env.store.save(replace(env.defaults, log_muted=True, music_playing=False, log_volume=33))
    env.fp.reconcile()
    assert env.marker.exists()
    assert env.video.props["mute"] is False and env.video.props["volume"] == 33
    assert env.music.props["pause"] is False  # start_music=True
    st = env.store.load()
    assert st.log_muted is False and st.music_playing is True


def test_boot_with_empty_players_unmutes_and_respects_start_music_false(env_factory):
    env = env_factory(start_music=False)
    env.store.save(replace(env.defaults, log_muted=True, music_playing=True))
    env.fp.reconcile()
    assert env.video.props["mute"] is False
    assert env.music.props["pause"] is True
    assert env.store.load().music_playing is False


def test_boot_sets_pause_and_mute_before_loading(env_factory):
    env = env_factory(start_music=False)
    env.fp.reconcile()
    music = env.music.requests
    first_load = next(i for i, r in enumerate(music) if r[0] == "loadfile")
    assert ["set_property", "pause", True] in music[:first_load]


def test_same_pid_next_round_sends_nothing(env):
    env.fp.reconcile()
    env.video.requests.clear()
    env.music.requests.clear()
    env.fp.reconcile()
    assert [r for r in env.video.requests + env.music.requests if r[0] != "get_property"] == []


def test_player_restart_with_new_pid_reloads_and_keeps_persisted_state(env):
    env.fp.reconcile()
    env.fp.set_log_muted(True)
    env.fp.pause()
    saved = env.store.load()
    # each mpv restarted: new process, empty playlist, default properties.
    env.video.props.update({"pid": 999, "playlist-count": 0, "mute": False, "volume": 100})
    env.music.props.update({"pid": 555, "playlist-count": 0, "pause": False, "volume": 100})
    env.video.commands.clear()
    env.music.commands.clear()
    env.fp.reconcile()
    assert [c[2] for c in env.video.commands if c[0] == "loadfile"] == ["replace", "append"]
    assert any(c[0] == "loadfile" for c in env.music.commands)
    assert env.video.props["mute"] is True and env.video.props["volume"] == 40
    assert env.music.props["pause"] is True and env.music.props["volume"] == 60
    assert env.store.load() == saved


def test_music_restart_keeps_persisted_pause(env):
    env.fp.reconcile()
    env.fp.pause()
    env.music.props.update({"pid": 555, "playlist-count": 0, "pause": False})
    env.fp.reconcile()
    assert env.music.props["pause"] is True and env.store.load().music_playing is False
    assert any(c[0] == "loadfile" for c in env.music.commands)


def test_controller_restart_keeps_persisted_pause_and_mute(env):
    env.marker.touch()  # not a boot: the controller restarted
    env.store.save(replace(env.defaults, log_muted=True, music_playing=False, music_volume=15))
    env.video.props["playlist-count"] = 2
    env.music.props["playlist-count"] = 7
    env.fp.reconcile()
    assert env.video.props["mute"] is True
    assert env.music.props["pause"] is True and env.music.props["volume"] == 15
    assert not any(c[0] == "loadfile" for c in env.video.commands + env.music.commands)
    st = env.store.load()
    assert st.log_muted is True and st.music_playing is False


def test_controller_restart_loads_empty_players_with_persisted_settings(env):
    env.marker.touch()
    env.store.save(replace(env.defaults, log_muted=True, music_playing=False))
    env.fp.reconcile()
    assert any(c[0] == "loadfile" for c in env.video.commands)
    assert any(c[0] == "loadfile" for c in env.music.commands)
    assert env.video.props["mute"] is True and env.music.props["pause"] is True


def test_player_down_at_first_then_up_is_reconciled_later(env):
    env.music.stop()
    env.fp.reconcile()
    music = FakeMpv(env.fp.config.music_socket).__enter__()
    try:
        music.props.update({"playlist-count": 0, "volume": 0, "pause": True, "mute": False, "pid": 7})
        env.fp.reconcile()
        assert any(c[0] == "loadfile" for c in music.commands)
        assert music.props["pause"] is False and music.props["volume"] == 60
        assert env.marker.exists()
    finally:
        music.stop()


def test_reconcile_does_not_save_when_nothing_changed(env, monkeypatch):
    env.fp.reconcile()
    saves = []
    monkeypatch.setattr(env.store, "save", lambda st: saves.append(st))
    env.fp.reconcile()
    env.video.props["pid"] = 12345  # restart, but the persisted state is already applied
    env.fp.reconcile()
    assert saves == []


def test_reconcile_first_round_does_not_save_if_state_already_matches(env, monkeypatch):
    saves = []
    monkeypatch.setattr(env.store, "save", lambda st: saves.append(st))
    env.fp.reconcile()
    assert saves == []


def test_no_moods_leaves_music_idle(env_factory):
    env = env_factory(music_tree={})
    env.fp.reconcile()
    s = env.fp.status()
    assert s["mood"] is None and s["moods"] == []
    assert not any(c[0] == "loadfile" for c in env.music.commands)


def test_set_mood_unknown_raises_and_keeps_state(env):
    env.fp.reconcile()
    with pytest.raises(UnknownMood):
        env.fp.set_mood("polka")
    assert env.store.load().mood == "classic-swing"


def test_set_mood_loads_odd_filenames_verbatim(env):
    env.fp.reconcile()
    env.fp.set_mood("modern")
    loaded = [c[1] for c in env.music.commands if c[0] == "loadfile"]
    assert ODD in [Path(p).name for p in loaded]


def test_set_mood_replaces_then_appends_unpauses_and_persists(env):
    env.fp.reconcile()
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
    env.fp.reconcile()
    env.fp.set_music_volume(11)
    env.fp.set_log_volume(22)
    st = env.store.load()
    assert (st.music_volume, st.log_volume) == (11, 22)
    assert env.music.props["volume"] == 11 and env.video.props["volume"] == 22


def test_next_track(env):
    env.fp.next_track()
    assert ["playlist-next"] in env.music.commands


def test_status_shape(env):
    env.fp.reconcile()
    s = env.fp.status()
    assert s == {
        "moods": [{"name": "classic-swing", "label": "Classic Swing", "tracks": 2},
                  {"name": "modern", "label": "Modern", "tracks": 3}],
        "mood": "classic-swing",
        "music": {"available": True, "playing": True, "volume": 60, "now_playing": "Song"},
        "log": {"available": True, "volume": 40, "muted": False, "videos": 2},
    }


def test_status_music_not_playing_when_player_idle(env_factory):
    env = env_factory(music_tree={})
    env.fp.reconcile()
    assert env.music.props["pause"] is False
    assert env.fp.status()["music"]["playing"] is False


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
    env.fp.reconcile()
    env.music.stop()
    with pytest.raises(MpvUnavailable):
        env.fp.set_music_volume(10)
    assert env.store.load().music_volume == 60


def test_pause_play_and_mute_persist(env):
    env.fp.reconcile()
    env.fp.pause()
    env.fp.set_log_muted(True)
    st = env.store.load()
    assert st.music_playing is False and st.log_muted is True
    assert env.music.props["pause"] is True and env.video.props["mute"] is True
    env.fp.play()
    assert env.store.load().music_playing is True and env.music.props["pause"] is False


def test_rescan_reloads_videos_and_current_mood(env):
    env.fp.reconcile()
    env.video.commands.clear()
    env.music.commands.clear()
    env.fp.rescan()
    assert [c[2] for c in env.video.commands] == ["replace", "append"]
    assert [c[2] for c in env.music.commands] == ["replace", "append"]


def test_rescan_current_mood_vanished_falls_back(env):
    env.fp.reconcile()
    env.fp.set_mood("modern")
    for p in env.modern_tracks:
        p.unlink()
    env.music.commands.clear()
    env.fp.rescan()
    assert env.store.load().mood == "classic-swing"
    assert env.music.commands and env.music.commands[0][0] == "loadfile"


def test_rescan_with_no_videos_stops_the_video_player(env):
    env.fp.reconcile()
    for p in env.fp.config.video_dir.iterdir():
        p.unlink()
    env.video.commands.clear()
    env.fp.rescan()
    assert env.video.commands == [["stop"]]
    assert env.video.props["playlist-count"] == 0


def test_rescan_with_no_moods_left_stops_the_music_player(env):
    env.fp.reconcile()
    for p in sorted(env.fp.config.music_dir.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    env.music.commands.clear()
    env.fp.rescan()
    assert env.music.commands == [["stop"]]
    assert env.store.load().mood is None
    assert env.fp.status()["music"]["playing"] is False


def test_reconcile_empty_player_with_no_videos_sends_stop_not_loadfile(env_factory):
    env = env_factory(video_tree={".hidden.mp4": b""})
    env.fp.reconcile()
    assert env.video.commands == [["stop"]]


# -- seasons ---------------------------------------------------------------

SEASON_TREE = {"harvest/a.mp3": b"", "thanksgiving/b.mp3": b"", "modern/c.mp3": b"",
               "extra/d.mp3": b""}
FALL = Season("fall", "Fall", ("harvest", "thanksgiving"))
WINTER = Season("winter", "Winter", ("modern",))


def test_status_without_seasons_is_unchanged(env):
    s = env.fp.status()
    assert s["seasons"] == [] and s["season"] is None
    assert set(s) == {"moods", "mood", "music", "log", "season", "seasons"}
    assert s["mood"] == "classic-swing"


def test_status_reports_season_of_current_mood(env_factory):
    e = env_factory(music_tree={"harvest/a.mp3": b"", "modern/c.mp3": b""},
                    seasons=[FALL, WINTER], default_mood="harvest")
    e.store.save(replace(e.defaults, mood="modern"))
    s = e.fp.status()
    assert s["season"] == "winter"
    assert s["seasons"] == [{"name": "fall", "label": "Fall", "moods": ["harvest", "thanksgiving"]},
                            {"name": "winter", "label": "Winter", "moods": ["modern"]}]


def test_status_season_follows_default_mood_when_none_saved(env_factory):
    e = env_factory(music_tree=SEASON_TREE, seasons=[FALL, WINTER], default_mood="modern")
    e.store.save(replace(e.defaults, mood=None))
    assert e.fp.status()["season"] == "winter"


def test_status_puts_unlisted_mood_in_other_season(env_factory):
    e = env_factory(music_tree=SEASON_TREE, seasons=[FALL, WINTER])
    e.store.save(replace(e.defaults, mood="extra"))
    s = e.fp.status()
    assert s["seasons"][-1] == {"name": "other", "label": "Other", "moods": ["extra"]}
    assert s["season"] == "other"


def test_set_season_starts_first_mood(env_factory):
    e = env_factory(music_tree=SEASON_TREE, seasons=[FALL, WINTER])
    e.store.save(replace(e.defaults, music_playing=False))
    e.fp.set_season("fall")
    st = e.store.load()
    assert st.mood == "harvest" and st.music_playing is True
    assert e.music.props["pause"] is False


def test_set_season_skips_empty_first_mood(env_factory):
    tree = {k: v for k, v in SEASON_TREE.items() if not k.startswith("harvest")}
    e = env_factory(music_tree=tree, seasons=[FALL, WINTER])
    e.fp.set_season("fall")
    assert e.store.load().mood == "thanksgiving"


def test_set_season_unknown_and_empty(env_factory):
    e = env_factory(music_tree={"modern/c.mp3": b""}, seasons=[FALL, WINTER])
    with pytest.raises(UnknownSeason):
        e.fp.set_season("spring")
    with pytest.raises(EmptySeason) as ei:
        e.fp.set_season("fall")
    assert str(ei.value) == "Fall"
    assert e.store.load().mood == "classic-swing"
