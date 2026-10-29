"""Load and validate the TOML configuration file."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ALLOWED_HOSTS = ("pireplace.local", "pireplace", "localhost", "127.0.0.1")
DEFAULT_HOTSPOT_ADDRESS = "10.42.0.1"  # NetworkManager's address for a shared (hotspot) connection


class ConfigError(Exception):
    """Raised when the config file is unreadable or contains invalid values."""


@dataclass(frozen=True)
class Season:
    name: str
    label: str
    moods: tuple[str, ...]


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
    seasons: tuple[Season, ...] = ()


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
    values["seasons"] = _parse_seasons(data.get("seasons", {}))
    return Config(**values)


def _parse_seasons(raw: object) -> tuple[Season, ...]:
    if not isinstance(raw, dict):
        raise ConfigError("[seasons]: expected a table")
    seasons = []
    seen: dict[str, str] = {}  # mood -> season that claimed it
    for name, table in raw.items():
        if not _MOOD_NAME.fullmatch(name):
            raise ConfigError(f"seasons: invalid season name {name!r}")
        if not isinstance(table, dict):
            raise ConfigError(f"seasons.{name}: expected a table")
        label = _coerce(f"seasons.{name}.label", table.get("label"), "str", None, None)
        if not label.strip():
            raise ConfigError(f"seasons.{name}.label: must not be empty")
        moods = table.get("moods")
        if not isinstance(moods, list) or not moods:
            raise ConfigError(f"seasons.{name}.moods: expected a non-empty list of mood names")
        for mood in moods:
            if not isinstance(mood, str) or not _MOOD_NAME.fullmatch(mood):
                raise ConfigError(f"seasons.{name}.moods: invalid mood name {mood!r}")
            if mood in seen:
                raise ConfigError(
                    f"seasons.{name}.moods: mood {mood!r} is already in season {seen[mood]!r}"
                )
            seen[mood] = name
        seasons.append(Season(name, label, tuple(moods)))
    return tuple(seasons)


DEFAULT_JELLYFIN_KEY_FILE = "/etc/pireplace/jellyfin.key"
_HEX32 = re.compile(r"[0-9a-fA-F]{32}")
_MOOD_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


@dataclass(frozen=True)
class JellyfinConfig:
    url: str  # no trailing slash
    user_id: str
    api_key_file: Path
    playlists: dict[str, str]  # mood -> playlist Id, in file order
    audio_format: str = "opus"  # "opus" (transcode) or "original"
    audio_bitrate_kbps: int = 160  # opus only; 64-320


def load_jellyfin_config(path: Path) -> JellyfinConfig | None:
    """Read the optional [jellyfin] section of `path`; None if it is absent."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    except OSError as e:
        raise ConfigError(f"{path}: cannot read: {e}") from e

    if "jellyfin" not in data:
        return None
    table = data["jellyfin"]
    if not isinstance(table, dict):
        raise ConfigError("[jellyfin]: expected a table")

    for key in ("url", "user_id"):
        if key not in table:
            raise ConfigError(f"jellyfin.{key}: required")
    url = _coerce("jellyfin.url", table["url"], "str", None, None)
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"jellyfin.url: must start with http:// or https://, got {url!r}")
    url = url.rstrip("/")

    user_id = _coerce("jellyfin.user_id", table["user_id"], "str", None, None)
    if not _HEX32.fullmatch(user_id):
        raise ConfigError("jellyfin.user_id: expected 32 hex characters")

    key_file = _coerce(
        "jellyfin.api_key_file", table.get("api_key_file", DEFAULT_JELLYFIN_KEY_FILE),
        "path", None, None,
    )
    if not key_file.is_absolute():
        key_file = path.resolve().parent.parent / key_file

    raw = table.get("playlists")
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("jellyfin.playlists: expected a table with at least one playlist")
    playlists = {}
    for mood, pid in raw.items():
        if not _MOOD_NAME.fullmatch(mood):
            raise ConfigError(f"jellyfin.playlists: invalid mood name {mood!r}")
        pid = _coerce(f"jellyfin.playlists.{mood}", pid, "str", None, None)
        if not _HEX32.fullmatch(pid):
            raise ConfigError(f"jellyfin.playlists.{mood}: expected 32 hex characters")
        playlists[mood] = pid

    audio_format = _coerce("jellyfin.audio_format", table.get("audio_format", "opus"), "str", None, None)
    if audio_format not in ("opus", "original"):
        raise ConfigError(f"jellyfin.audio_format: expected 'opus' or 'original', got {audio_format!r}")
    bitrate = _coerce(
        "jellyfin.audio_bitrate_kbps", table.get("audio_bitrate_kbps", 160), "int", 64, 320
    )

    return JellyfinConfig(
        url=url, user_id=user_id, api_key_file=key_file, playlists=playlists,
        audio_format=audio_format, audio_bitrate_kbps=bitrate,
    )


def audio_profile(jf: JellyfinConfig) -> str:
    """Name of the audio settings in force: "original" or e.g. "opus-160"."""
    return "original" if jf.audio_format == "original" else f"opus-{jf.audio_bitrate_kbps}"


CREDENTIAL_NAME = "jellyfin.key"


def api_key_path(cfg: JellyfinConfig, environ: Mapping[str, str] | None = None) -> Path:
    """Where the key is read from.

    Under systemd (pireplace-sync.service: LoadCredential=jellyfin.key:...) the key is
    $CREDENTIALS_DIRECTORY/jellyfin.key, readable only by this unit; the file in /etc is
    root-only. Elsewhere (a hand run, tests) it is ``api_key_file``.
    """
    environ = os.environ if environ is None else environ
    creds = environ.get("CREDENTIALS_DIRECTORY")
    if creds:
        candidate = Path(creds) / CREDENTIAL_NAME
        if candidate.is_file():
            return candidate
    return cfg.api_key_file


def read_api_key(cfg: JellyfinConfig, environ: Mapping[str, str] | None = None) -> str:
    """Return the stripped key (see api_key_path). Never echoes the contents."""
    path = api_key_path(cfg, environ)
    try:
        key = path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        key = ""
    if not key:
        raise ConfigError(f"jellyfin: cannot read api_key_file {path}")
    return key
