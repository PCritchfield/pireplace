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
from pathlib import Path

log = logging.getLogger("pireplace.netbackup")

DEFAULT_LIVE = "/etc/NetworkManager/system-connections"
DEFAULT_BACKUP = "/var/lib/pireplace-netbackup"
SUFFIX = ".nmconnection"
HOTSPOT_ID = "pireplace-hotspot"
WIFI_TYPES = ("802-11-wireless", "wifi")


def _parse(data: bytes) -> tuple[str, str] | None:
    """Return (id, type) when `data` is a usable keyfile profile, else None."""
    if not data:
        return None
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(data.decode("utf-8"))
        section = parser["connection"]
        ident, ctype = section.get("id", "").strip(), section.get("type", "").strip()
    except Exception:  # noqa: BLE001 - errors may quote file content; report nothing
        return None
    return (ident, ctype) if ident and ctype else None


def _read_profile(path: Path) -> tuple[bytes, tuple[str, str]] | None:
    """(content, (id, type)) for a valid regular profile file, else None."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        data = path.read_bytes()
    except OSError:
        return None
    parsed = _parse(data)
    return (data, parsed) if parsed else None


def _profiles(directory: Path) -> dict[str, tuple[bytes, tuple[str, str]]]:
    """Valid profiles in `directory`, by file name."""
    found = {}
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return found
    for name in names:
        if name.endswith(SUFFIX):
            profile = _read_profile(directory / name)
            if profile:
                found[name] = profile
    return found


def _has_wifi(profiles: dict[str, tuple[bytes, tuple[str, str]]]) -> bool:
    return any(ctype in WIFI_TYPES and ident != HOTSPOT_ID for _, (ident, ctype) in profiles.values())


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(directory: Path, name: str, data: bytes) -> None:
    """tmp file in `directory`, fsync, chmod 0600, rename over `name`, fsync the directory."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
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
    _fsync_dir(directory)


def _ensure_dir(directory: Path, mode: int) -> None:
    directory.mkdir(mode=mode, parents=True, exist_ok=True)


def _oserror(e: OSError) -> str:
    return os.strerror(e.errno) if e.errno else "I/O error"


def sync(live: Path, backup: Path) -> list[str]:
    """Copy valid live profiles into `backup`, pruning deliberately deleted ones.

    Returns the names of problems (empty on success); contents are never reported.
    """
    live, backup = Path(live), Path(backup)
    problems: list[str] = []
    _ensure_dir(backup, 0o700)
    live_profiles = _profiles(live)
    saved = _profiles(backup)
    for name, (data, _) in live_profiles.items():
        if name in saved and saved[name][0] == data:
            continue
        try:
            _write_atomic(backup, name, data)
            log.info("backed up %s", name)
        except OSError as e:
            log.error("cannot back up %s: %s", name, _oserror(e))
            problems.append(name)
    if _has_wifi(live_profiles):
        for name in sorted(os.listdir(backup)):
            if name.endswith(SUFFIX) and not os.path.lexists(live / name):
                try:
                    (backup / name).unlink()
                    log.info("pruned %s (deleted from NetworkManager)", name)
                except OSError as e:
                    log.error("cannot prune %s: %s", name, _oserror(e))
                    problems.append(name)
    return problems


def restore(live: Path, backup: Path) -> list[str]:
    """Put backed-up profiles back where the live one is empty/invalid, or all Wi-Fi is gone.

    Never overwrites a valid live file. Returns the restored file names.
    """
    live, backup = Path(live), Path(backup)
    saved = _profiles(backup)
    if not saved:
        return []
    live_profiles = _profiles(live)
    wifi_gone = not _has_wifi(live_profiles)
    restored: list[str] = []
    for name, (data, _) in saved.items():
        if name in live_profiles:
            continue
        if not (os.path.lexists(live / name) or wifi_gone):
            continue
        try:
            _ensure_dir(live, 0o755)
            _write_atomic(live, name, data)
        except OSError as e:
            log.error("cannot restore %s: %s", name, _oserror(e))
            continue
        log.warning("restored %s from backup", name)
        restored.append(name)
    return restored
