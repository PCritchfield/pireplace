from pathlib import Path

import pytest

from pireplace.config import ConfigError, load_config


def test_empty_file_gives_defaults(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("")
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
    p = tmp_path / "c.toml"
    p.write_text(body)
    with pytest.raises(ConfigError):
        load_config(p)


def test_relative_paths_resolve_against_repo_root(tmp_path):
    (tmp_path / "dev").mkdir()
    p = tmp_path / "dev" / "c.toml"
    p.write_text('[media]\nvideo_dir = ".dev/media/videos"\n')
    assert load_config(p).video_dir == tmp_path / ".dev/media/videos"


def test_absolute_paths_are_untouched(tmp_path):
    (tmp_path / "dev").mkdir()
    p = tmp_path / "dev" / "c.toml"
    p.write_text('[media]\nvideo_dir = "/srv/x"\n')
    assert load_config(p).video_dir == Path("/srv/x")
