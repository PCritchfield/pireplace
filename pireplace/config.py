"""Load and validate the TOML configuration file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ALLOWED_HOSTS = ("pireplace.local", "pireplace", "localhost", "127.0.0.1")
DEFAULT_HOTSPOT_ADDRESS = "10.42.0.1"  # NetworkManager's address for a shared (hotspot) connection


class ConfigError(Exception):
    """Raised when the config file is unreadable or contains invalid values."""


@dataclass(frozen=True)
class Config:
    video_dir: Path
    music_dir: Path
    video_socket: Path
    music_socket: Path
    state_file: Path
    start_music: bool
    default_mood: str | None
    music_volume: int
    log_volume: int
    host: str
    port: int
    hotspot_ssid: str
    hotspot_password: str
    hotspot_after_s: int
    hotspot_retry_s: int
    # Defaults here too so code that builds a Config by hand need not repeat them.
    hotspot_address: str = DEFAULT_HOTSPOT_ADDRESS
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS


# field -> (section, key, kind, default, min, max)
# kind is one of: path, bool, str, opt_str, int, hosts
_SPEC = {
    "video_dir": ("media", "video_dir", "path", "/srv/pireplace/videos", None, None),
    "music_dir": ("media", "music_dir", "path", "/srv/pireplace/music", None, None),
    "video_socket": ("players", "video_socket", "path", "/run/pireplace/video.sock", None, None),
    "music_socket": ("players", "music_socket", "path", "/run/pireplace/music.sock", None, None),
    "state_file": ("state", "file", "path", "/var/lib/pireplace/state.json", None, None),
    "start_music": ("playback", "start_music", "bool", True, None, None),
    "default_mood": ("playback", "default_mood", "opt_str", "classic-swing", None, None),
    "music_volume": ("playback", "music_volume", "int", 60, 0, 100),
    "log_volume": ("playback", "log_volume", "int", 40, 0, 100),
    "host": ("web", "host", "str", "0.0.0.0", None, None),
    "port": ("web", "port", "int", 80, 1, 65535),
    "allowed_hosts": ("web", "allowed_hosts", "hosts", list(DEFAULT_ALLOWED_HOSTS), None, None),
    "hotspot_ssid": ("wifi", "hotspot_ssid", "str", "pireplace-setup", None, None),
    "hotspot_password": ("wifi", "hotspot_password", "str", "", None, None),
    "hotspot_after_s": ("wifi", "hotspot_after_s", "int", 300, 0, None),
    "hotspot_retry_s": ("wifi", "hotspot_retry_s", "int", 300, 0, None),
    "hotspot_address": ("wifi", "hotspot_address", "str", DEFAULT_HOTSPOT_ADDRESS, None, None),
}


def _coerce(label: str, value: object, kind: str, lo: int | None, hi: int | None):
    if kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{label}: expected true/false, got {value!r}")
        return value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{label}: expected an integer, got {value!r}")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise ConfigError(f"{label}: {value} is out of range (min {lo}, max {hi})")
        return value
    if kind == "hosts":
        if not isinstance(value, list) or not all(isinstance(h, str) and h for h in value):
            raise ConfigError(f"{label}: expected a list of host names, got {value!r}")
        return tuple(h.lower() for h in value)
    if not isinstance(value, str):
        raise ConfigError(f"{label}: expected a string, got {value!r}")
    if kind == "path":
        return Path(value)
    if kind == "opt_str":
        return value or None
    return value


def load_config(path: Path) -> Config:
    """Read `path`. Relative paths in it resolve against the parent of its directory."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    except OSError as e:
        raise ConfigError(f"{path}: cannot read: {e}") from e

    values = {}
    for name, (section, key, kind, default, lo, hi) in _SPEC.items():
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise ConfigError(f"[{section}]: expected a table")
        value = _coerce(f"{section}.{key}", table.get(key, default), kind, lo, hi)
        if kind == "path" and not value.is_absolute():
            value = path.resolve().parent.parent / value
        values[name] = value
    return Config(**values)
