import threading
from unittest.mock import Mock

import pytest

from pireplace.__main__ import main, startup_with_retry
from pireplace.mpv import MpvError, MpvUnavailable


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


def test_startup_retries_until_players_answer():
    fp = Mock()
    fp.startup.side_effect = [MpvUnavailable("a"), MpvError("b"), None]
    sleeps = []
    startup_with_retry(fp, threading.Event(), threading.Lock(), sleep=lambda s: sleeps.append(s) or False)
    assert fp.startup.call_count == 3
    assert sleeps == [2, 2]


def test_startup_retry_stops_when_asked():
    fp = Mock()
    fp.startup.side_effect = MpvUnavailable("down")
    stop = threading.Event()

    def sleep(_s):
        stop.set()
        return True

    startup_with_retry(fp, stop, threading.Lock(), sleep=sleep)
    assert fp.startup.call_count == 1


def test_startup_attempts_hold_the_lock_but_sleep_does_not():
    lock = threading.Lock()
    held_during = []
    fp = Mock()

    def startup():
        held_during.append(lock.locked())
        if len(held_during) == 1:
            raise MpvUnavailable("down")

    fp.startup.side_effect = startup

    def sleep(_s):
        held_during.append(("sleep", lock.locked()))
        return False

    startup_with_retry(fp, threading.Event(), lock, sleep=sleep)
    assert held_during == [True, ("sleep", False), True]


def test_startup_retries_on_oserror_and_keeps_going():
    fp = Mock()
    fp.startup.side_effect = [FileNotFoundError("music dir"), None]
    startup_with_retry(fp, threading.Event(), threading.Lock(), sleep=lambda s: False)
    assert fp.startup.call_count == 2
