"""NetworkManager profile backup and boot-time restore (tmp dirs only)."""

import logging
import os
import stat

import pytest

from pireplace import netbackup
from pireplace.__main__ import main

SECRET = "hunter2-very-secret-psk"


def profile(ident="home", ctype="wifi", psk=SECRET, uuid=None, extra=""):
    uuid = uuid or f"uuid-{ident}"
    text = f"[connection]\nid={ident}\ntype={ctype}\nuuid={uuid}\n\n"
    if ctype == "wifi":
        text += f"[wifi]\nssid={ident}\n\n[wifi-security]\npsk={psk}\n"
    return text + extra


@pytest.fixture
def dirs(tmp_path):
    live, backup = tmp_path / "live", tmp_path / "backup"
    live.mkdir()
    return live, backup


def put(directory, name, text):
    (directory / name).write_text(text)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def names(directory):
    return sorted(p.name for p in directory.iterdir())


def two_wifi(live, backup):
    put(live, "home.nmconnection", profile())
    put(live, "other.nmconnection", profile("other"))
    netbackup.sync(live, backup)


# --- sync ---------------------------------------------------------------------


def test_sync_copies_valid_profiles_private(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    assert netbackup.sync(live, backup) == []
    assert (backup / "home.nmconnection").read_text() == profile()
    assert mode(backup / "home.nmconnection") == 0o600
    assert mode(backup) == 0o700
    assert names(backup) == ["home.nmconnection"]


def test_sync_skips_empty_unparseable_and_incomplete(dirs):
    live, backup = dirs
    put(live, "empty.nmconnection", "")
    put(live, "junk.nmconnection", "not a keyfile at all\n")
    put(live, "noid.nmconnection", "[connection]\ntype=wifi\nuuid=u\n")
    put(live, "notype.nmconnection", "[connection]\nid=x\nuuid=u\n")
    put(live, "nouuid.nmconnection", "[connection]\nid=x\ntype=ethernet\n")
    put(live, "nossid.nmconnection", "[connection]\nid=x\ntype=wifi\nuuid=u\n")
    put(live, "other.txt", profile())
    (live / "link.nmconnection").symlink_to(live / "other.txt")
    netbackup.sync(live, backup)
    assert names(backup) == []


def test_sync_updates_changed_keeps_prev_and_leaves_unchanged_alone(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    netbackup.sync(live, backup)
    os.utime(backup / "home.nmconnection", (1000, 1000))
    netbackup.sync(live, backup)
    assert os.stat(backup / "home.nmconnection").st_mtime == 1000
    assert names(backup) == ["home.nmconnection"]
    put(live, "home.nmconnection", profile(psk="changed"))
    netbackup.sync(live, backup)
    assert "psk=changed" in (backup / "home.nmconnection").read_text()
    assert (backup / "home.nmconnection.prev.1").read_text() == profile()
    assert mode(backup / "home.nmconnection.prev.1") == 0o600


def test_sync_prunes_deleted_profile_when_other_wifi_remains(dirs):
    live, backup = dirs
    two_wifi(live, backup)
    (live / "other.nmconnection").unlink()
    put(live, "other.nmconnection", "")  # exists but empty: kept
    netbackup.sync(live, backup)
    assert "other.nmconnection" in names(backup)
    (live / "other.nmconnection").unlink()
    netbackup.sync(live, backup)
    assert names(backup) == ["home.nmconnection"]


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
    assert len(names(backup)) == 2


def test_sync_keeps_backup_of_emptied_live_file(dirs):
    live, backup = dirs
    two_wifi(live, backup)
    put(live, "home.nmconnection", "")
    netbackup.sync(live, backup)
    assert (backup / "home.nmconnection").read_text() == profile()


def test_incomplete_connection_only_live_profile_never_replaces_the_backup(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    netbackup.sync(live, backup)
    put(live, "home.nmconnection", "[connection]\nid=home\ntype=wifi\nuuid=uuid-home\n")
    assert netbackup.sync(live, backup) == []
    put(live, "home.nmconnection", "[connection]\nid=home\ntype=wifi\nuuid=uuid-home\n\n[wifi]\n")
    assert netbackup.sync(live, backup) == []
    assert (backup / "home.nmconnection").read_text() == profile()


def test_complete_open_network_switch_replaces_and_keeps_prev(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    netbackup.sync(live, backup)
    put(live, "home.nmconnection", profile(psk="x").split("[wifi-security]")[0])
    assert netbackup.sync(live, backup) == []
    assert "wifi-security" not in (backup / "home.nmconnection").read_text()
    assert (backup / "home.nmconnection.prev.1").read_text() == profile()


def test_same_name_new_uuid_replaces_and_keeps_prev(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    netbackup.sync(live, backup)
    put(live, "home.nmconnection", profile(uuid="another"))
    assert netbackup.sync(live, backup) == []
    assert "uuid=another" in (backup / "home.nmconnection").read_text()
    assert (backup / "home.nmconnection.prev.1").read_text() == profile()


def test_wpa_psk_without_psk_keeps_backup_warns_exits_zero_and_still_prunes(dirs, caplog):
    live, backup = dirs
    two_wifi(live, backup)
    put(live, "third.nmconnection", profile("third"))
    netbackup.sync(live, backup)
    (live / "third.nmconnection").unlink()
    broken = profile().replace("psk=" + SECRET, "key-mgmt=wpa-psk")
    put(live, "home.nmconnection", broken)
    with caplog.at_level(logging.WARNING):
        assert netbackup.sync(live, backup) == []
    assert (backup / "home.nmconnection").read_text() == profile()
    assert "kept backup of home.nmconnection" in caplog.text
    assert "netbackup-forget home.nmconnection" in caplog.text
    assert SECRET not in caplog.text
    assert names(backup) == ["home.nmconnection", "other.nmconnection"]  # third was pruned
    assert main(["netbackup-sync", "--live", str(live), "--backup", str(backup)]) == 0


def test_incomplete_live_file_is_left_alone_at_warning_level(dirs, caplog):
    live, backup = dirs
    two_wifi(live, backup)
    put(live, "home.nmconnection", "[connection]\nid=home\n")
    with caplog.at_level(logging.DEBUG):
        assert netbackup.restore(live, backup) == []
    records = [r for r in caplog.records if "home.nmconnection" in r.getMessage()]
    assert records and all(r.levelno == logging.WARNING for r in records)


def test_only_the_five_newest_corrupt_copies_are_kept(dirs):
    live, backup = dirs
    two_wifi(live, backup)
    for ts in range(100, 108):
        (backup / f"home.nmconnection.corrupt-{ts}").write_text("x")
    (backup / "other.nmconnection.corrupt-1").write_text("x")
    netbackup.sync(live, backup)
    kept = [n for n in names(backup) if n.startswith("home.nmconnection.corrupt-")]
    assert kept == [f"home.nmconnection.corrupt-{ts}" for ts in range(103, 108)]
    assert "other.nmconnection.corrupt-1" in names(backup)


def test_restore_still_happens_when_the_corrupt_copy_cannot_be_saved(dirs, monkeypatch, caplog):
    live, backup = dirs
    two_wifi(live, backup)
    put(live, "home.nmconnection", "no header here\n")
    real = netbackup._write_atomic

    def no_corrupt(directory, name, data):
        if ".corrupt-" in name:
            raise OSError(28, "full")
        return real(directory, name, data)

    monkeypatch.setattr(netbackup, "_write_atomic", no_corrupt)
    with caplog.at_level(logging.WARNING):
        assert netbackup.restore(live, backup) == ["home.nmconnection"]
    assert "could not keep a copy" in caplog.text


def test_failed_copy_skips_pruning(dirs, monkeypatch):
    live, backup = dirs
    two_wifi(live, backup)
    (live / "other.nmconnection").unlink()
    put(live, "new.nmconnection", profile("new"))
    real = netbackup._write_atomic

    def fail_new(directory, name, data):
        if name == "new.nmconnection":
            raise OSError(28, "x")
        return real(directory, name, data)

    monkeypatch.setattr(netbackup, "_write_atomic", fail_new)
    assert netbackup.sync(live, backup) == ["new.nmconnection"]
    assert "other.nmconnection" in names(backup)


def test_rename_drops_the_old_backup_name(dirs):
    live, backup = dirs
    two_wifi(live, backup)
    (live / "home.nmconnection").rename(live / "renamed.nmconnection")
    netbackup.sync(live, backup)
    assert names(backup) == ["other.nmconnection", "renamed.nmconnection"]


def test_rename_of_the_only_wifi_profile_still_drops_the_old_name(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    netbackup.sync(live, backup)
    (live / "home.nmconnection").rename(live / "renamed.nmconnection")
    netbackup.sync(live, backup)
    assert names(backup) == ["renamed.nmconnection"]


def test_sync_removes_stale_tmp_files(dirs):
    live, backup = dirs
    backup.mkdir(mode=0o700)
    (backup / ".tmp-abc").write_text("x")
    (live / ".tmp-def").write_text("x")
    (live / ".tmp-keepme-not").write_text("x")
    (live / "tmp-unrelated").write_text("x")
    netbackup.sync(live, backup)
    assert names(backup) == []
    assert names(live) == ["tmp-unrelated"]


def test_sync_unusable_backup_dir_is_a_short_error(dirs, tmp_path):
    live, _ = dirs
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(netbackup.NetbackupError):
        netbackup.sync(live, blocker / "sub")


# --- restore ------------------------------------------------------------------


def test_restore_replaces_empty_live_file(dirs):
    live, backup = dirs
    two_wifi(live, backup)
    put(live, "home.nmconnection", "")
    assert netbackup.restore(live, backup) == ["home.nmconnection"]
    assert (live / "home.nmconnection").read_text() == profile()
    assert mode(live / "home.nmconnection") == 0o600
    assert not [n for n in names(backup) if ".corrupt-" in n]  # nothing to keep from 0 bytes


def test_restore_replaces_headerless_file_and_keeps_a_corrupt_copy(dirs):
    live, backup = dirs
    two_wifi(live, backup)
    put(live, "home.nmconnection", "garbage without a header\n")
    assert netbackup.restore(live, backup) == ["home.nmconnection"]
    assert (live / "home.nmconnection").read_text() == profile()
    kept = [n for n in names(backup) if n.startswith("home.nmconnection.corrupt-")]
    assert len(kept) == 1 and mode(backup / kept[0]) == 0o600
    assert (backup / kept[0]).read_text() == "garbage without a header\n"


def test_restore_never_overwrites_a_valid_live_file(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    netbackup.sync(live, backup)
    put(live, "home.nmconnection", profile(psk="newer"))
    assert netbackup.restore(live, backup) == []
    assert "psk=newer" in (live / "home.nmconnection").read_text()


def test_restore_leaves_unreadable_live_file_alone(dirs, monkeypatch, caplog):
    live, backup = dirs
    two_wifi(live, backup)
    real = netbackup.Path.read_bytes

    def deny(self):
        if self.parent == live and self.name == "home.nmconnection":
            raise PermissionError(13, "denied")
        return real(self)

    monkeypatch.setattr(netbackup.Path, "read_bytes", deny)
    with caplog.at_level(logging.WARNING):
        assert netbackup.restore(live, backup) == []
    assert (live / "home.nmconnection").read_text() == profile()
    assert "left home.nmconnection alone" in caplog.text


def test_restore_leaves_non_utf8_and_odd_files_alone(dirs):
    live, backup = dirs
    two_wifi(live, backup)
    (live / "home.nmconnection").write_bytes(b"[connection]\nid=\xff\xfe\n")
    put(live, "other.nmconnection", "[connection]\nid=x\n[connection\n")
    before = {n: (live / n).read_bytes() for n in names(live)}
    assert netbackup.restore(live, backup) == []
    assert {n: (live / n).read_bytes() for n in names(live)} == before


def test_restore_leaves_incomplete_profile_alone(dirs):
    live, backup = dirs
    two_wifi(live, backup)
    put(live, "home.nmconnection", "[connection]\nid=home\n")
    assert netbackup.restore(live, backup) == []


def test_restore_aborts_without_writing_when_live_cannot_be_listed(dirs, monkeypatch):
    live, backup = dirs
    two_wifi(live, backup)
    put(live, "home.nmconnection", "")
    real = os.listdir

    def fail(path):
        if os.fspath(path) == os.fspath(live):
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(netbackup.os, "listdir", fail)
    with pytest.raises(netbackup.NetbackupError):
        netbackup.restore(live, backup)
    assert (live / "home.nmconnection").read_text() == ""


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
    two_wifi(live, backup)
    (live / "other.nmconnection").unlink()
    assert netbackup.restore(live, backup) == []
    assert not (live / "other.nmconnection").exists()


def test_restore_skips_a_backup_whose_uuid_lives_under_another_name(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    netbackup.sync(live, backup)
    (live / "home.nmconnection").rename(live / "renamed.nmconnection")
    assert netbackup.restore(live, backup) == []
    assert names(live) == ["renamed.nmconnection"]


def test_restore_without_backup_dir_does_nothing(dirs):
    live, backup = dirs
    assert netbackup.restore(live, backup) == []


def test_restore_removes_stale_tmp_files(dirs):
    live, backup = dirs
    two_wifi(live, backup)
    (backup / ".tmp-abc").write_text("x")
    (live / ".tmp-def").write_text("x")
    netbackup.restore(live, backup)
    assert ".tmp-abc" not in names(backup) and ".tmp-def" not in names(live)


# --- forget -------------------------------------------------------------------


def test_forget_removes_backup_and_prev(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    netbackup.sync(live, backup)
    put(live, "home.nmconnection", profile(psk="b"))
    netbackup.sync(live, backup)
    assert (backup / "home.nmconnection.prev.1").exists()
    assert main(["netbackup-forget", "home.nmconnection", "--backup", str(backup)]) == 0
    assert names(backup) == []
    assert main(["netbackup-forget", "home.nmconnection", "--backup", str(backup)]) == 1
    assert main(["netbackup-forget", "../x.nmconnection", "--backup", str(backup)]) == 1


# --- secrets and CLI ----------------------------------------------------------


def test_secrets_never_reach_logs_or_errors(dirs, caplog, monkeypatch, capsys):
    live, backup = dirs
    caplog.set_level(logging.DEBUG)
    two_wifi(live, backup)
    put(live, "junk.nmconnection", f"psk={SECRET}\n")
    put(live, "home.nmconnection", f"[connection]\nid=home\ntype=wifi\nuuid=uuid-home\npsk={SECRET}\n")
    netbackup.sync(live, backup)  # truncated: kept
    put(live, "home.nmconnection", f"{SECRET} no header\n")
    netbackup.restore(live, backup)  # headerless: corrupt copy
    (live / "home.nmconnection").write_bytes(b"[connection]\nid=\xff" + SECRET.encode())
    netbackup.restore(live, backup)  # non-UTF-8: left alone

    def boom(*a, **k):
        raise OSError(28, "No space left")

    monkeypatch.setattr(netbackup.os, "replace", boom)
    put(live, "other.nmconnection", profile("other", psk=SECRET + "2"))
    netbackup.sync(live, backup)
    assert SECRET not in caplog.text
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
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


def test_cli_errors_exit_one_with_a_short_message(dirs, tmp_path, capsys):
    live, _ = dirs
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert main(["netbackup-sync", "--live", str(live), "--backup", str(blocker / "sub")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("pireplace: cannot create") and "Traceback" not in err


def test_three_generations_are_kept_and_a_fourth_drops_the_oldest(dirs):
    live, backup = dirs
    for i in range(5):
        put(live, "home.nmconnection", profile(psk=f"v{i}"))
        netbackup.sync(live, backup)
    read = lambda suffix: (backup / f"home.nmconnection{suffix}").read_text()  # noqa: E731
    assert "psk=v4" in read("") and "psk=v3" in read(".prev.1")
    assert "psk=v2" in read(".prev.2") and "psk=v1" in read(".prev.3")
    assert not (backup / "home.nmconnection.prev.4").exists()
    for n in (1, 2, 3):
        assert mode(backup / f"home.nmconnection.prev.{n}") == 0o600


def test_legacy_prev_migrates_to_prev_1(dirs):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    netbackup.sync(live, backup)
    (backup / "home.nmconnection.prev").write_text("legacy")
    netbackup.sync(live, backup)
    assert not (backup / "home.nmconnection.prev").exists()
    assert (backup / "home.nmconnection.prev.1").read_text() == "legacy"


def test_forget_removes_every_generation(dirs):
    live, backup = dirs
    for i in range(4):
        put(live, "home.nmconnection", profile(psk=f"v{i}"))
        netbackup.sync(live, backup)
    (backup / "home.nmconnection.prev").write_text("legacy")
    assert main(["netbackup-forget", "home.nmconnection", "--backup", str(backup)]) == 0
    assert names(backup) == []


def test_dropping_the_wifi_security_section_warns_but_replaces(dirs, caplog):
    live, backup = dirs
    put(live, "home.nmconnection", profile())
    netbackup.sync(live, backup)
    put(live, "home.nmconnection", profile().split("[wifi-security]")[0])
    with caplog.at_level(logging.WARNING):
        assert netbackup.sync(live, backup) == []
    assert "home.nmconnection: Wi-Fi password section removed; previous copies kept as " \
           "home.nmconnection.prev.1..3" in caplog.text
    assert SECRET not in caplog.text
    assert (backup / "home.nmconnection.prev.1").read_text() == profile()
