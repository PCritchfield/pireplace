"""Mirror a Jellyfin playlist into a local mood folder, tracked by a manifest.

Safety rules: only files recorded in the manifest are ever deleted, only by a
bare filename resolved inside the mood dir; anything else in the folder
(manual rips, dotfiles) is never touched.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pireplace.config import Config, JellyfinConfig
from pireplace.jellyfin import JellyfinError, Track

log = logging.getLogger("pireplace.sync")

MANIFEST = ".pireplace-sync.json"
MIN_FREE_START = 2 * 1024**3
MIN_FREE_RUN = 1024**3
MAX_NAME_BYTES = 200

_BAD_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')


def default_free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


@dataclass
class MoodResult:
    mood: str
    added: int = 0
    removed: int = 0
    failed: int = 0
    error: str | None = None


def _clean(text: str | None, default: str) -> str:
    cleaned = _BAD_CHARS.sub("_", text or "").lstrip(". ")
    return cleaned or default


def _truncate(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[: max(max_bytes, 0)].decode("utf-8", "ignore")


def track_filename(track: Track) -> str:
    """``<Artist> - <Title> [<id[:8]>].<ext>``, always a bare, non-hidden filename."""
    artist = _clean(track.artist, "Unknown Artist")
    title = _clean(track.title, "Untitled")
    # The id comes from the server unvalidated: same rules, and no dots in the extension.
    ident = _clean(str(track.id)[:8], "unknown")
    ext = _clean((track.container or "").replace(".", "_"), "audio")
    fixed = f" -   [{ident}].{ext}"
    budget = MAX_NAME_BYTES - len(fixed.encode("utf-8"))
    artist = _truncate(artist, budget // 2)
    title = _truncate(title, budget - len(artist.encode("utf-8")))
    return f"{artist} - {title} [{ident}].{ext}"


def _is_safe_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and not name.startswith(".")
        and "\0" not in name
        and "/" not in name
        and "\\" not in name
        and Path(name).name == name
    )


def _load_manifest(mood_dir: Path) -> tuple[dict[str, str], bool]:
    """Return (items, dirty). Unsafe entries are dropped (never deleted)."""
    try:
        raw = json.loads((mood_dir / MANIFEST).read_text(encoding="utf-8"))
        items = raw["items"]
        if not isinstance(items, dict):
            return {}, False
    except (OSError, ValueError, KeyError, TypeError):
        return {}, False
    clean = {k: v for k, v in items.items() if isinstance(k, str) and _is_safe_name(v)}
    return clean, len(clean) != len(items)


def _write_manifest(mood_dir: Path, items: dict[str, str]) -> None:
    tmp = mood_dir / f".{MANIFEST}.tmp"
    try:
        tmp.write_text(json.dumps({"items": items}, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, mood_dir / MANIFEST)
    finally:
        tmp.unlink(missing_ok=True)


def sync_mood(
    client,
    mood: str,
    playlist_id: str,
    mood_dir: Path,
    free_bytes: Callable[[Path], int] = default_free_bytes,
) -> MoodResult:
    result = MoodResult(mood=mood, added=0, removed=0, failed=0, error=None)
    mood_dir = Path(mood_dir)
    mood_dir.mkdir(parents=True, exist_ok=True)
    manifest, changed = _load_manifest(mood_dir)

    try:
        listed = client.playlist_tracks(playlist_id)
    except JellyfinError as e:
        result.error = f"cannot list playlist: {e}"
        return result
    if not listed and manifest:
        result.error = "playlist is empty; refusing to delete synced tracks"
        return result

    tracks: dict[str, Track] = {}
    for t in listed:
        tracks.setdefault(t.id, t)

    to_delete = {i: n for i, n in manifest.items() if i not in tracks}
    to_fetch: list[tuple[Track, str]] = []
    adopted: dict[str, str] = {}
    for t in tracks.values():
        if t.id in manifest:
            continue
        name = track_filename(t)
        path = mood_dir / name
        if path.is_file() and not path.is_symlink():
            adopted[t.id] = name
        else:
            to_fetch.append((t, name))

    needed = sum(t.size or 0 for t, _ in to_fetch)
    if to_fetch and needed + MIN_FREE_START > free_bytes(mood_dir):
        result.error = "not enough space for downloads; nothing changed"
        return result

    manifest.update(adopted)
    result.added += len(adopted)

    # Delete only manifest-recorded bare filenames, and never one still in use.
    keep = {manifest[i] for i in manifest if i not in to_delete} | {n for _, n in to_fetch}
    for item_id, name in to_delete.items():
        if name not in keep:
            path = mood_dir / name
            if path.parent != mood_dir:  # defence in depth; _is_safe_name already ensures this
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                result.failed += 1
                continue
        del manifest[item_id]
        result.removed += 1

    for index, (t, name) in enumerate(to_fetch):
        if free_bytes(mood_dir) - (t.size or 0) < MIN_FREE_RUN:
            result.failed += len(to_fetch) - index
            break
        part = mood_dir / f".{name}.part"
        try:
            client.download(t.id, part)
            os.replace(part, mood_dir / name)
        except (JellyfinError, OSError):
            part.unlink(missing_ok=True)
            result.failed += 1
            continue
        manifest[t.id] = name
        result.added += 1

    if changed or result.added or result.removed:
        try:
            _write_manifest(mood_dir, manifest)
        except OSError:
            result.error = "cannot write manifest"
            return result
    if result.failed:
        result.error = f"{result.failed} downloads failed"
    return result


def trigger_rescan(port: int, timeout: float = 10.0) -> bool:
    """Ask the local control API to rescan the media dirs. False (and a warning) on any error."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/rescan",
        data=b"{}",
        method="POST",
        headers={"Host": "pireplace.local", "Content-Type": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback: never via a proxy
        with opener.open(req, timeout=timeout) as resp:
            resp.read()
        return True
    except Exception as e:  # URLError, HTTPError, timeouts, resets: all just a warning
        log.warning("rescan failed: %s", e)
        return False


def run_sync(config: Config, jf: JellyfinConfig, client, rescan: Callable[[], bool]) -> int:
    """Sync every configured mood; rescan once if anything changed. 0 ok, 1 if any mood errored."""
    changed = False
    failed = False
    for mood, playlist_id in jf.playlists.items():
        result = sync_mood(client, mood, playlist_id, config.music_dir / mood, free_bytes=default_free_bytes)
        line = f"mood={mood} added={result.added} removed={result.removed} failed={result.failed}"
        if result.error:
            line += f" error={result.error}"
            failed = True
        log.info("%s", line)
        changed = changed or bool(result.added or result.removed)
    if changed and not rescan():
        log.warning("rescan did not succeed; new music appears after the next rescan")
    return 1 if failed else 0
