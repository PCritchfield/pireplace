from dataclasses import replace
from pathlib import Path

import pytest

from pireplace.config import (
    ConfigError, Season, audio_profile, load_config, load_jellyfin_config, read_api_key,
)


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


def test_web_and_hotspot_address_defaults(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("")
    c = load_config(p)
    assert c.hotspot_address == "10.42.0.1"
    assert c.allowed_hosts == ("pireplace.local", "pireplace", "localhost", "127.0.0.1")


def test_allowed_hosts_override_is_lowercased(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[web]\nallowed_hosts = ["Fire.Local", "192.168.1.5"]\n[wifi]\nhotspot_address = "10.0.0.1"\n')
    c = load_config(p)
    assert c.allowed_hosts == ("fire.local", "192.168.1.5") and c.hotspot_address == "10.0.0.1"


@pytest.mark.parametrize("body", ['[web]\nallowed_hosts = "pireplace.local"\n',
                                  '[web]\nallowed_hosts = ["ok", 3]\n',
                                  '[wifi]\nhotspot_address = 10\n'])
def test_invalid_host_settings_raise(tmp_path, body):
    p = tmp_path / "c.toml"
    p.write_text(body)
    with pytest.raises(ConfigError):
        load_config(p)


# --- [jellyfin] section ---

JF_TOML = """\
[jellyfin]
url = "http://100.88.8.6:8096"
user_id = "1b7ed00cbdcc4a1eb624738191ca4efd"
api_key_file = "/etc/pireplace/jellyfin.key"

[jellyfin.playlists]
modern = "385d9e4b0602d3d75a00bc6d31dc5aa6"
classic-swing = "dfbf231b1aaa92df17ff5248fbde6fbf"
"""


def _write(tmp_path, body):
    p = tmp_path / "c.toml"
    p.write_text(body)
    return p


def test_no_jellyfin_section_returns_none(tmp_path):
    assert load_jellyfin_config(_write(tmp_path, "[web]\nport = 80\n")) is None


def test_full_section(tmp_path):
    j = load_jellyfin_config(_write(tmp_path, JF_TOML))
    assert j.url == "http://100.88.8.6:8096"
    assert j.user_id == "1b7ed00cbdcc4a1eb624738191ca4efd"
    assert list(j.playlists) == ["modern", "classic-swing"]
    assert j.playlists["modern"] == "385d9e4b0602d3d75a00bc6d31dc5aa6"
    assert j.api_key_file == Path("/etc/pireplace/jellyfin.key")


def test_api_key_file_defaults(tmp_path):
    body = "\n".join(l for l in JF_TOML.splitlines() if not l.startswith("api_key_file"))
    j = load_jellyfin_config(_write(tmp_path, body))
    assert j.api_key_file == Path("/etc/pireplace/jellyfin.key")


def test_relative_api_key_file_resolves_like_other_paths(tmp_path):
    (tmp_path / "etc").mkdir()
    body = JF_TOML.replace('"/etc/pireplace/jellyfin.key"', '"secrets/jf.key"')
    p = tmp_path / "etc" / "c.toml"
    p.write_text(body)
    assert load_jellyfin_config(p).api_key_file == tmp_path.resolve() / "secrets" / "jf.key"


def test_trailing_slash_stripped(tmp_path):
    body = JF_TOML.replace('8096"', '8096/"')
    assert load_jellyfin_config(_write(tmp_path, body)).url == "http://100.88.8.6:8096"


@pytest.mark.parametrize("mutation", [
    ('url = "http://100.88.8.6:8096"', 'url = "ftp://x"'),
    ('user_id = "1b7ed00cbdcc4a1eb624738191ca4efd"', 'user_id = "nothex"'),
    ('modern = "385d9e4b0602d3d75a00bc6d31dc5aa6"', 'modern = "short"'),
    ('modern =', '"Bad Mood!" ='),
])
def test_jellyfin_invalid_values_raise(tmp_path, mutation):
    old, new = mutation
    assert old in JF_TOML
    with pytest.raises(ConfigError):
        load_jellyfin_config(_write(tmp_path, JF_TOML.replace(old, new)))


@pytest.mark.parametrize("missing", ["url", "user_id"])
def test_jellyfin_missing_required_raises(tmp_path, missing):
    body = "\n".join(l for l in JF_TOML.splitlines() if not l.startswith(missing))
    with pytest.raises(ConfigError):
        load_jellyfin_config(_write(tmp_path, body))


def test_empty_playlists_raises(tmp_path):
    body = JF_TOML.split("[jellyfin.playlists]")[0] + "[jellyfin.playlists]\n"
    with pytest.raises(ConfigError):
        load_jellyfin_config(_write(tmp_path, body))


def test_missing_playlists_table_raises(tmp_path):
    body = JF_TOML.split("[jellyfin.playlists]")[0]
    with pytest.raises(ConfigError):
        load_jellyfin_config(_write(tmp_path, body))


def test_read_api_key_strips_and_never_leaks(tmp_path):
    cfg = load_jellyfin_config(_write(tmp_path, JF_TOML))
    k = tmp_path / "k"
    k.write_text("s3cret\n")
    assert read_api_key(replace(cfg, api_key_file=k)) == "s3cret"
    k.write_text("  \n")
    with pytest.raises(ConfigError) as e:
        read_api_key(replace(cfg, api_key_file=k))
    assert "s3cret" not in str(e.value)
    assert str(e.value) == f"jellyfin: cannot read api_key_file {k}"


def test_read_api_key_missing_file(tmp_path):
    cfg = load_jellyfin_config(_write(tmp_path, JF_TOML))
    with pytest.raises(ConfigError, match="cannot read api_key_file"):
        read_api_key(replace(cfg, api_key_file=tmp_path / "nope"))


# --- systemd credential (LoadCredential=jellyfin.key:...) ---


def test_read_api_key_prefers_the_systemd_credential(tmp_path):
    cfg = load_jellyfin_config(_write(tmp_path, JF_TOML))
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "jellyfin.key").write_text("from-credential\n")
    fallback = tmp_path / "k"
    fallback.write_text("from-file\n")
    cfg = replace(cfg, api_key_file=fallback)
    assert read_api_key(cfg, environ={"CREDENTIALS_DIRECTORY": str(creds)}) == "from-credential"


def test_read_api_key_falls_back_without_the_credential(tmp_path):
    cfg = load_jellyfin_config(_write(tmp_path, JF_TOML))
    fallback = tmp_path / "k"
    fallback.write_text("from-file\n")
    cfg = replace(cfg, api_key_file=fallback)
    empty = tmp_path / "creds"
    empty.mkdir()  # set, but holds no jellyfin.key
    assert read_api_key(cfg, environ={"CREDENTIALS_DIRECTORY": str(empty)}) == "from-file"
    assert read_api_key(cfg, environ={}) == "from-file"
    assert read_api_key(cfg, environ={"CREDENTIALS_DIRECTORY": ""}) == "from-file"


def test_read_api_key_empty_credential_is_an_error_naming_it(tmp_path):
    cfg = load_jellyfin_config(_write(tmp_path, JF_TOML))
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "jellyfin.key").write_text("\n")
    with pytest.raises(ConfigError) as e:
        read_api_key(cfg, environ={"CREDENTIALS_DIRECTORY": str(creds)})
    assert str(e.value) == f"jellyfin: cannot read api_key_file {creds / 'jellyfin.key'}"


def test_read_api_key_uses_the_process_environment_by_default(tmp_path, monkeypatch):
    cfg = load_jellyfin_config(_write(tmp_path, JF_TOML))
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "jellyfin.key").write_text("env-key")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    assert read_api_key(replace(cfg, api_key_file=tmp_path / "nope")) == "env-key"


# --- [seasons] ---

def test_seasons_parsed_in_file_order(tmp_path):
    p = _write(tmp_path, '[seasons.fall]\nlabel = "Fall"\nmoods = ["harvest", "thanksgiving"]\n'
                         '[seasons.winter]\nlabel = "Winter"\nmoods = ["modern"]\n')
    assert load_config(p).seasons == (
        Season("fall", "Fall", ("harvest", "thanksgiving")),
        Season("winter", "Winter", ("modern",)),
    )


def test_no_seasons_section_gives_empty_tuple(tmp_path):
    assert load_config(_write(tmp_path, "")).seasons == ()


def test_season_rejects_mood_in_two_seasons(tmp_path):
    p = _write(tmp_path, '[seasons.fall]\nlabel = "Fall"\nmoods = ["cozy"]\n'
                         '[seasons.winter]\nlabel = "Winter"\nmoods = ["cozy"]\n')
    with pytest.raises(ConfigError, match="cozy"):
        load_config(p)


@pytest.mark.parametrize("body", [
    '[seasons.fall]\nlabel = "Fall"\nmoods = []\n',
    '[seasons.fall]\nlabel = ""\nmoods = ["a"]\n',
])
def test_season_rejects_empty_moods_and_label(tmp_path, body):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, body))


@pytest.mark.parametrize("body", [
    '[seasons."Fall!"]\nlabel = "Fall"\nmoods = ["a"]\n',
    '[seasons.fall]\nlabel = "Fall"\nmoods = ["Bad Name"]\n',
])
def test_season_rejects_bad_names(tmp_path, body):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, body))


# --- jellyfin audio profile ---

def _jf(tmp_path, extra=""):
    body = JF_TOML.replace('api_key_file =', extra + '\napi_key_file =', 1)
    return load_jellyfin_config(_write(tmp_path, body))


def test_jellyfin_audio_defaults_to_opus_160(tmp_path):
    j = load_jellyfin_config(_write(tmp_path, JF_TOML))
    assert j.audio_format == "opus" and j.audio_bitrate_kbps == 160
    assert audio_profile(j) == "opus-160"


def test_jellyfin_audio_original_and_range(tmp_path):
    assert audio_profile(_jf(tmp_path, 'audio_format = "original"')) == "original"
    assert audio_profile(_jf(tmp_path, "audio_bitrate_kbps = 96")) == "opus-96"
    for bad in ('audio_format = "mp3"', "audio_bitrate_kbps = 32", "audio_bitrate_kbps = 321"):
        with pytest.raises(ConfigError):
            _jf(tmp_path, bad)
