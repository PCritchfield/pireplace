import threading
from unittest.mock import Mock

import pytest

from pireplace.__main__ import main, reconcile_forever
from pireplace.mpv import MpvUnavailable


def test_bad_config_exits_2(tmp_path, capsys):
    p = tmp_path / "c.toml"
    p.write_text("[web]\nport = 'x'\n")
    assert main(["serve", "--config", str(p)]) == 2
    err = capsys.readouterr().err
    assert "port" in err and len(err.strip().splitlines()) == 1


def test_missing_config_exits_2(tmp_path, capsys):
    assert main(["serve", "--config", str(tmp_path / "nope.toml")]) == 2
    assert "nope.toml" in capsys.readouterr().err


def test_no_subcommand_is_usage_error():
    with pytest.raises(SystemExit) as e:
        main([])
    assert e.value.code == 2


def _sleeper(rounds):
    """sleep() stand-in: records intervals and asks to stop after `rounds` calls."""
    slept = []

    def sleep(s):
        slept.append(s)
        return len(slept) >= rounds

    return sleep, slept


def test_reconcile_loop_runs_every_interval_until_stopped():
    fp = Mock()
    sleep, slept = _sleeper(3)
    reconcile_forever(fp, threading.Event(), threading.Lock(), sleep=sleep)
    assert fp.reconcile.call_count == 3
    assert slept == [5, 5, 5]


def test_reconcile_interval_is_injectable():
    fp = Mock()
    sleep, slept = _sleeper(2)
    reconcile_forever(fp, threading.Event(), threading.Lock(), interval=0.5, sleep=sleep)
    assert slept == [0.5, 0.5]


def test_reconcile_loop_survives_any_error(caplog):
    fp = Mock()
    fp.reconcile.side_effect = [MpvUnavailable("a"), OSError("disk"), RuntimeError("bug"), None]
    sleep, _ = _sleeper(4)
    reconcile_forever(fp, threading.Event(), threading.Lock(), sleep=sleep)
    assert fp.reconcile.call_count == 4
    assert "reconcile failed" in caplog.text


def test_reconcile_holds_the_lock_but_sleep_does_not():
    lock = threading.Lock()
    seen = []
    fp = Mock()
    fp.reconcile.side_effect = lambda: seen.append(lock.locked())

    def sleep(_s):
        seen.append(("sleep", lock.locked()))
        return len(seen) >= 4

    reconcile_forever(fp, threading.Event(), lock, sleep=sleep)
    assert seen == [True, ("sleep", False), True, ("sleep", False)]


def test_reconcile_default_sleep_waits_on_stop_event():
    fp = Mock()
    stop = threading.Event()
    stop.set()
    reconcile_forever(fp, stop, threading.Lock())
    assert fp.reconcile.call_count == 1


def test_serve_passes_host_and_hotspot_settings_to_the_server(tmp_path, monkeypatch):
    import pireplace.__main__ as m

    seen = {}

    def fake_make_server(*args, **kw):
        seen.update(kw)
        raise OSError("stop here")

    monkeypatch.setattr(m, "make_server", fake_make_server)
    p = tmp_path / "c.toml"
    p.write_text('[web]\nallowed_hosts = ["fire.local"]\n[wifi]\nhotspot_address = "10.1.1.1"\n')
    assert main(["serve", "--config", str(p)]) == 1
    assert seen["allowed_hosts"] == ("fire.local",) and seen["hotspot_address"] == "10.1.1.1"


def test_serve_gives_players_a_timeout_that_survives_opening_a_large_file(tmp_path, monkeypatch):
    import pireplace.__main__ as m

    clients = []
    real = m.MpvClient

    def spy(*args, **kw):
        clients.append(real(*args, **kw))
        return clients[-1]

    def fake_make_server(*args, **kw):
        raise OSError("stop here")

    monkeypatch.setattr(m, "MpvClient", spy)
    monkeypatch.setattr(m, "make_server", fake_make_server)
    p = tmp_path / "c.toml"
    p.write_text("")
    assert main(["serve", "--config", str(p)]) == 1
    assert len(clients) == 2 and all(c.timeout == 3.0 for c in clients)


# --- sync subcommand -----------------------------------------------------------

import logging

from tests.fake_jellyfin import FakeJellyfin
from tests.test_sync import rescan_server  # noqa: F401  (fixture)

PL_ID = "a" * 32
USER_ID = "b" * 32


def _write_cfg(tmp_path, url="http://127.0.0.1:1", key="k" * 20, port=80, jellyfin=True):
    key_file = tmp_path / "jf.key"
    if key is not None:
        key_file.write_text(key + "\n")
    text = f'[media]\nmusic_dir = "{tmp_path / "music"}"\n[web]\nport = {port}\n'
    if jellyfin:
        text += (
            f'[jellyfin]\nurl = "{url}"\nuser_id = "{USER_ID}"\napi_key_file = "{key_file}"\n'
            f'[jellyfin.playlists]\nmodern = "{PL_ID}"\n'
        )
    cfg = tmp_path / "config.toml"
    cfg.write_text(text)
    return cfg


@pytest.fixture
def fake():
    with FakeJellyfin() as f:
        yield f


@pytest.fixture
def plenty_of_space(monkeypatch):
    monkeypatch.setattr("pireplace.sync.default_free_bytes", lambda p: 10 * 1024**4)


def test_sync_cli_no_jellyfin_section_exits_2(tmp_path, capsys):
    cfg = _write_cfg(tmp_path, jellyfin=False)
    assert main(["sync", "--config", str(cfg)]) == 2
    err = capsys.readouterr().err
    assert f"no [jellyfin] section in {cfg}" in err and len(err.strip().splitlines()) == 1


def test_sync_cli_bad_config_exits_2(tmp_path, capsys):
    assert main(["sync", "--config", str(tmp_path / "nope.toml")]) == 2
    assert "nope.toml" in capsys.readouterr().err


def test_sync_cli_missing_key_exits_2_without_leaking(tmp_path, capsys):
    cfg = _write_cfg(tmp_path, key=None)
    assert main(["sync", "--config", str(cfg)]) == 2
    err = capsys.readouterr().err
    assert "jf.key" in err and len(err.strip().splitlines()) == 1


def _item(i, title):
    return {
        "Id": i, "AlbumArtist": "Art", "Name": title, "Container": "flac",
        "MediaSources": [{"Container": "flac", "Size": 5}],
    }


def test_sync_cli_end_to_end_with_fake_jellyfin(tmp_path, fake, plenty_of_space, rescan_server):
    port, seen = rescan_server  # answers GET /api/status with music not playing
    fake.playlists[PL_ID] = [_item("c" * 32, "One")]
    fake.files["c" * 32] = b"audio"
    cfg = _write_cfg(tmp_path, url=fake.url, key=fake.key, port=port)
    assert main(["sync", "--config", str(cfg)]) == 0
    assert any((tmp_path / "music" / "modern").glob("*.flac"))
    assert [s[0] for s in seen] == ["/api/status", "/api/rescan"]


def test_api_key_never_logged(tmp_path, fake, plenty_of_space, caplog, capsys):
    fake.playlists[PL_ID] = [_item("c" * 32, "One")]
    wrong = "wrong-key-SECRET-9876"
    cfg = _write_cfg(tmp_path, url=fake.url, key=wrong)
    with caplog.at_level(logging.DEBUG):
        assert main(["sync", "--config", str(cfg)]) == 1
    out = capsys.readouterr()
    assert wrong not in caplog.text and wrong not in out.err and wrong not in out.out
    assert "mood=modern" in caplog.text
