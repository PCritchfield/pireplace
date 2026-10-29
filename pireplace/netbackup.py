"""Back up NetworkManager keyfile profiles and restore them before NM starts.

A profile that NetworkManager left empty (or lost) takes all networking with it, so a
copy of every good profile is kept in a separate directory and put back at boot.

Profile files hold Wi-Fi passwords: nothing in here logs, prints or raises file contents.
Only file names are ever reported, and parse errors (which can quote a line of the file)
are swallowed.
"""

from __future__ import annotations

import configparser
import logging
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("pireplace.netbackup")

DEFAULT_LIVE = "/etc/NetworkManager/system-connections"
DEFAULT_BACKUP = "/var/lib/pireplace-netbackup"
SUFFIX = ".nmconnection"
PREV = ".prev"
TMP_PREFIX = ".tmp-"
HOTSPOT_ID = "pireplace-hotspot"
WIFI_TYPES = ("802-11-wireless", "wifi")


class NetbackupError(Exception):
    """A failure to report briefly (file names and strerror only, never contents)."""


@dataclass(frozen=True)
class Profile:
    ident: str
    ctype: str
    uuid: str
    sections: frozenset[str]

    @property
    def is_wifi(self) -> bool:
        return self.ctype in WIFI_TYPES and self.ident != HOTSPOT_ID


@dataclass(frozen=True)
class Loaded:
    """kind: ok | absent | unreadable | empty | noheader | incomplete | odd."""

    kind: str
    data: bytes = b""
    profile: Profile | None = None


def _classify(data: bytes) -> Loaded:
    if not data:
        return Loaded("empty", data)
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(data.decode("utf-8"))
    except configparser.MissingSectionHeaderError:
        return Loaded("noheader", data)
    except Exception:  # noqa: BLE001 - errors may quote file content; report nothing
        return Loaded("odd", data)
    if not parser.has_section("connection"):
        return Loaded("noheader", data)
    conn = parser["connection"]
    ident, ctype, uuid = (conn.get(k, "").strip() for k in ("id", "type", "uuid"))
    if not (ident and ctype and uuid):
        return Loaded("incomplete", data)
    if ctype in WIFI_TYPES:
        ssid = ""
        for section in ("wifi", "802-11-wireless"):
            if parser.has_section(section):
                ssid = parser[section].get("ssid", "").strip()
                break
        if not ssid:
            return Loaded("incomplete", data)
    return Loaded("ok", data, Profile(ident, ctype, uuid, frozenset(parser.sections())))


def _load(path: Path) -> Loaded:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return Loaded("absent")
    except OSError:
        return Loaded("unreadable")
    if not stat.S_ISREG(mode):
        return Loaded("odd")
    try:
        return _classify(path.read_bytes())
    except OSError:
        return Loaded("unreadable")


def _oserror(e: OSError) -> str:
    return os.strerror(e.errno) if e.errno else "I/O error"


def _listdir(directory: Path, missing_ok: bool = False) -> list[str]:
    try:
        return sorted(os.listdir(directory))
    except FileNotFoundError:
        if missing_ok:
            return []
        raise NetbackupError(f"cannot list {directory}: {os.strerror(2)}") from None
    except OSError as e:
        raise NetbackupError(f"cannot list {directory}: {_oserror(e)}") from None


def _profiles(directory: Path, missing_ok: bool = False) -> dict[str, Loaded]:
    """Valid profiles in `directory`, by file name."""
    found = {}
    for name in _listdir(directory, missing_ok):
        if name.endswith(SUFFIX):
            loaded = _load(directory / name)
            if loaded.kind == "ok":
                found[name] = loaded
    return found


def _has_wifi(profiles: dict[str, Loaded]) -> bool:
    return any(p.profile.is_wifi for p in profiles.values())


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(directory: Path, name: str, data: bytes) -> None:
    """tmp file in `directory`, fsync, chmod 0600, rename over `name`, fsync the directory.

    A failing directory fsync after the rename only warns: the file is in place.
    """
    fd, tmp = tempfile.mkstemp(prefix=TMP_PREFIX, dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, directory / name)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        _fsync_dir(directory)
    except OSError as e:
        log.warning("wrote %s but could not sync %s: %s", name, directory, _oserror(e))


def _ensure_dir(directory: Path, mode: int) -> None:
    try:
        directory.mkdir(mode=mode, parents=True, exist_ok=True)
    except OSError as e:
        raise NetbackupError(f"cannot create {directory}: {_oserror(e)}") from None


def _clean_tmp(directory: Path) -> None:
    """Delete leftover temp files of this module (exact prefix only)."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if name.startswith(TMP_PREFIX):
            try:
                if stat.S_ISREG(os.lstat(directory / name).st_mode):
                    os.unlink(directory / name)
                    log.info("removed leftover %s", name)
            except OSError:
                pass


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def sync(live: Path, backup: Path) -> list[str]:
    """Copy valid live profiles into `backup`, pruning deleted or renamed ones.

    Returns the names of problems (empty on success); contents are never reported.
    Raises NetbackupError when a directory cannot be used at all.
    """
    live, backup = Path(live), Path(backup)
    problems: list[str] = []
    _ensure_dir(backup, 0o700)
    _clean_tmp(backup)
    _clean_tmp(live)
    live_profiles = _profiles(live)
    saved = _profiles(backup)

    for name, new in live_profiles.items():
        old = saved.get(name)
        if old and old.data == new.data:
            continue
        try:
            if old:
                lost = sorted(old.profile.sections - new.profile.sections)
                if old.profile.uuid != new.profile.uuid or lost:
                    why = "uuid changed" if old.profile.uuid != new.profile.uuid else f"live copy lost sections {lost}"
                    log.warning("kept backup of %s: %s", name, why)
                    problems.append(name)
                    continue
                _write_atomic(backup, name + PREV, old.data)
            _write_atomic(backup, name, new.data)
            log.info("backed up %s", name)
        except OSError as e:
            log.error("cannot back up %s: %s", name, _oserror(e))
            problems.append(name)

    if problems:  # an incomplete picture: remove nothing this run
        return problems

    live_uuids = {p.profile.uuid: n for n, p in live_profiles.items()}
    for name in _listdir(backup):
        if not name.endswith(SUFFIX) or os.path.lexists(live / name):
            continue
        old = saved.get(name)
        if old and live_uuids.get(old.profile.uuid, name) != name:
            reason = "renamed in NetworkManager"
        elif _has_wifi(live_profiles):
            reason = "deleted from NetworkManager"
        else:
            continue
        try:
            _unlink(backup / name)
            _unlink(backup / (name + PREV))
            log.info("pruned %s (%s)", name, reason)
        except OSError as e:
            log.error("cannot prune %s: %s", name, _oserror(e))
            problems.append(name)
    return problems


def restore(live: Path, backup: Path) -> list[str]:
    """Put backed-up profiles back where the live one is empty or headerless, or all Wi-Fi is gone.

    Never overwrites a valid live file, nor one it cannot read or understand. Returns the
    restored file names. Raises NetbackupError (writing nothing) if a directory cannot be listed.
    """
    live, backup = Path(live), Path(backup)
    saved = _profiles(backup, missing_ok=True)
    if not saved:
        return []
    live_names = _listdir(live, missing_ok=True)  # any other error aborts before a write
    _clean_tmp(backup)
    _clean_tmp(live)
    live_profiles = _profiles(live, missing_ok=True)
    wifi_gone = not _has_wifi(live_profiles)
    present_uuids = {p.profile.uuid for p in live_profiles.values()}
    restored: list[str] = []
    for name, good in saved.items():
        if name in live_profiles or good.profile.uuid in present_uuids:
            continue
        current = _load(live / name) if name in live_names else Loaded("absent")
        if current.kind == "absent":
            if not wifi_gone:
                continue
        elif current.kind not in ("empty", "noheader"):
            log.warning("left %s alone: live file is %s, not overwriting", name, current.kind)
            continue
        try:
            _ensure_dir(live, 0o755)
            if current.data:
                try:
                    _write_atomic(backup, f"{name}.corrupt-{int(time.time())}", current.data)
                except OSError as e:
                    log.warning("could not keep a copy of the bad %s: %s", name, _oserror(e))
            _write_atomic(live, name, good.data)
        except NetbackupError as e:
            log.error("cannot restore %s: %s", name, e)
            continue
        except OSError as e:
            log.error("cannot restore %s: %s", name, _oserror(e))
            continue
        present_uuids.add(good.profile.uuid)
        log.warning("restored %s from backup", name)
        restored.append(name)
    return restored


def forget(backup: Path, name: str) -> list[str]:
    """Remove a profile (and its .prev) from the backup; returns the removed file names."""
    if os.path.basename(name) != name or not name.endswith(SUFFIX):
        raise NetbackupError("not a profile file name (expected NAME.nmconnection)")
    removed = []
    for victim in (name, name + PREV):
        try:
            if _unlink(Path(backup) / victim):
                removed.append(victim)
        except OSError as e:
            raise NetbackupError(f"cannot remove {victim}: {_oserror(e)}") from None
    for victim in removed:
        log.info("forgot %s", victim)
    return removed
