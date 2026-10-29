"""NetworkManager profile backup and boot-time restore (tmp dirs only)."""

import logging
import os
import stat

import pytest

from pireplace import netbackup
from pireplace.__main__ import main

SECRET = "hunter2-very-secret-psk"


def profile(ident="home", ctype="wifi", psk=SECRET):
    return f"[connection]\nid={ident}\ntype={ctype}\n\n[wifi-security]\npsk={psk}\n"


@pytest.fixture
def dirs(tmp_path):
    live, backup = tmp_path / "live", tmp_path / "backup"
    live.mkdir()
    return live, backup


def put(directory, name, text):
    (directory / name).write_text(text)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_sync_copies_valid_profiles_private(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    netbackup.sync(live, backup)
    assert (backup / "home.nmconnection").read_text() == profile()
    assert mode(backup / "home.nmconnection") == 0o600
    assert mode(backup) == 0o700
    assert [p.name for p in backup.iterdir()] == ["home.nmconnection"]


def test_sync_skips_empty_unparseable_and_incomplete(dirs):
    live, backup = dirs
    put(live, "empty.nmconnection", "")
    put(live, "junk.nmconnection", "not a keyfile at all\n")
    put(live, "noid.nmconnection", "[connection]\ntype=wifi\n")
    put(live, "notype.nmconnection", "[connection]\nid=x\n")
    put(live, "other.txt", profile())
    (live / "link.nmconnection").symlink_to(live / "other.txt")
    netbackup.sync(live, backup)
    assert list(backup.iterdir()) == []


def test_sync_updates_changed_and_leaves_unchanged_alone(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    netbackup.sync(live, backup)
    os.utime(backup / "home.nmconnection", (1000, 1000))
    netbackup.sync(live, backup)
    assert os.stat(backup / "home.nmconnection").st_mtime == 1000
    put(live, "home.nmconnection", profile(psk="changed"))
    netbackup.sync(live, backup)
    assert "psk=changed" in (backup / "home.nmconnection").read_text()


def test_sync_prunes_deleted_profile_when_other_wifi_remains(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    put(live, "old.nmconnection", profile("old"))
    netbackup.sync(live, backup)
    (live / "old.nmconnection").unlink()
    netbackup.sync(live, backup)
    assert sorted(p.name for p in backup.iterdir()) == ["home.nmconnection"]


def test_sync_does_not_prune_when_no_wifi_remains(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    put(live, "pireplace-hotspot.nmconnection", profile("pireplace-hotspot"))
    netbackup.sync(live, backup)
    (live / "home.nmconnection").unlink()
    netbackup.sync(live, backup)
    assert (backup / "home.nmconnection").exists()
    for f in live.iterdir():
        f.unlink()
    netbackup.sync(live, backup)
    assert len(list(backup.iterdir())) == 2


def test_sync_keeps_backup_of_emptied_live_file(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    put(live, "other.nmconnection", profile("other"))
    netbackup.sync(live, backup)
    put(live, "home.nmconnection", "")
    netbackup.sync(live, backup)
    assert (backup / "home.nmconnection").read_text() == profile()


def test_restore_replaces_empty_live_file(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    put(live, "other.nmconnection", profile("other"))
    netbackup.sync(live, backup)
    put(live, "home.nmconnection", "")
    assert netbackup.restore(live, backup) == ["home.nmconnection"]
    assert (live / "home.nmconnection").read_text() == profile()
    assert mode(live / "home.nmconnection") == 0o600


def test_restore_never_overwrites_a_valid_live_file(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    netbackup.sync(live, backup)
    put(live, "home.nmconnection", profile(psk="newer"))
    assert netbackup.restore(live, backup) == []
    assert "psk=newer" in (live / "home.nmconnection").read_text()


def test_restore_brings_back_everything_when_all_wifi_is_gone(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    put(live, "eth.nmconnection", profile("eth", "ethernet"))
    netbackup.sync(live, backup)
    for f in live.iterdir():
        f.unlink()
    assert sorted(netbackup.restore(live, backup)) == ["eth.nmconnection", "home.nmconnection"]


def test_restore_treats_hotspot_only_as_all_wifi_gone(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    netbackup.sync(live, backup)
    (live / "home.nmconnection").unlink()
    put(live, "pireplace-hotspot.nmconnection", profile("pireplace-hotspot"))
    assert netbackup.restore(live, backup) == ["home.nmconnection"]


def test_restore_leaves_a_deleted_profile_when_other_wifi_exists(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    put(live, "old.nmconnection", profile("old"))
    netbackup.sync(live, backup)
    (live / "old.nmconnection").unlink()
    assert netbackup.restore(live, backup) == []
    assert not (live / "old.nmconnection").exists()


def test_restore_without_backup_dir_does_nothing(dirs):
    live, backup = dirs
    assert netbackup.restore(live, backup) == []


def test_secrets_never_reach_logs_or_errors(dirs, caplog, monkeypatch):
    live, backup = dirs
    caplog.set_level(logging.DEBUG)
    put(live, "home.nmconnection", profile())
    put(live, "junk.nmconnection", f"psk={SECRET}\n")
    netbackup.sync(live, backup)
    put(live, "home.nmconnection", "")
    netbackup.restore(live, backup)
    # a failing write must not leak content either
    def boom(*a, **k):
        raise OSError(28, "No space left")
    monkeypatch.setattr(netbackup.os, "replace", boom)
    put(live, "home.nmconnection", profile(psk=SECRET + "2"))
    netbackup.sync(live, backup)
    assert SECRET not in caplog.text
    assert "home.nmconnection" in caplog.text


def test_cli_exit_zero(dirs, capsys):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    args = ["--live", str(live), "--backup", str(backup)]
    assert main(["netbackup-sync", *args]) == 0
    put(live, "home.nmconnection", "")
    assert main(["netbackup-restore", *args]) == 0
    assert (live / "home.nmconnection").read_text() == profile()
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
