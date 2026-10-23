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
