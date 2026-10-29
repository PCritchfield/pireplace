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
import unicodedata
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pireplace.config import Config, JellyfinConfig
from pireplace.jellyfin import JellyfinError, Track

log = logging.getLogger("pireplace.sync")

MANIFEST = ".pireplace-sync.json"
RESCAN_PENDING = ".pireplace-rescan-pending"  # in the music dir; hidden, so never a mood
MIN_FREE_START = 2 * 1024**3
MIN_FREE_RUN = 1024**3
MAX_NAME_BYTES = 200
MAX_EXT_BYTES = 16

_BAD_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
# The tail of a name track_filename() makes: " [<8 chars of the Id>].<ext>".
_SYNCED_NAME = re.compile(r" \[[^\]]{1,8}\]\.[^.]+$")


def default_free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


@dataclass
class MoodResult:
    mood: str
    added: int = 0
    removed: int = 0
    failed: int = 0  # downloads
    error: str | None = None
    delete_failed: int = 0
    skipped: int = 0  # playlist items without a valid Jellyfin Id


def _clean(text: str | None, default: str) -> str:
    # Format characters (Unicode Cf: bidi overrides, zero-width marks) are dropped so a
    # name cannot display as something else.
    text = "".join(ch for ch in (text or "") if unicodedata.category(ch) != "Cf")
    cleaned = _BAD_CHARS.sub("_", text).lstrip(". ")
    return cleaned or default


def _truncate(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[: max(max_bytes, 0)].decode("utf-8", "ignore")


def track_filename(track: Track) -> str:
    """``<Artist> - <Title> [<id[:8]>].<ext>``: a bare, non-hidden name of at most 200 bytes."""
    artist = _clean(track.artist, "Unknown Artist")
    title = _clean(track.title, "Untitled")
    # The id comes from the server unvalidated: same rules, and no dots in the extension.
    ident = _clean(str(track.id)[:8], "unknown")
    ext = _truncate(_clean((track.container or "").replace(".", "_"), "audio"), MAX_EXT_BYTES) or "audio"
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


def _load_manifest(mood_dir: Path) -> tuple[dict[str, str], bool, str | None]:
    """Return (items, dirty, problem); problem is "missing", "corrupt" or None.

    Unsafe entries are dropped (never deleted). A manifest that exists but cannot be
    read raises OSError: guessing it empty would orphan every synced file.
    """
    try:
        text = (mood_dir / MANIFEST).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, False, "missing"
    except UnicodeDecodeError:
        return {}, False, "corrupt"
    try:
        items = json.loads(text)["items"]
        if not isinstance(items, dict):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return {}, False, "corrupt"
    clean = {k: v for k, v in items.items() if isinstance(k, str) and _is_safe_name(v)}
    return clean, len(clean) != len(items), None


def _warn_if_ownership_reset(mood: str, mood_dir: Path, problem: str | None) -> None:
    """A lost manifest means the sync no longer knows which files it owns."""
    if problem == "corrupt":
        why = "the manifest is corrupt"
    elif problem == "missing" and any(
        _SYNCED_NAME.search(p.name) for p in mood_dir.iterdir() if not p.name.startswith(".")
    ):
        why = "the manifest is missing but synced files are present"
    else:
        return
    log.warning(
        "mood=%s: %s; ownership reset: files still in the playlist are adopted again by "
        "name, any others are left alone and never deleted", mood, why,
    )


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
    """Sync one mood. Any error, filesystem ones included, ends up in ``.error``."""
    result = MoodResult(mood=mood)
    try:
        _sync_mood(client, mood, playlist_id, Path(mood_dir), free_bytes, result)
    except OSError as e:
        result.error = f"filesystem error: {e}"
    return result


def _sync_mood(client, mood: str, playlist_id: str, mood_dir: Path, free_bytes, result: MoodResult) -> None:
    mood_dir.mkdir(parents=True, exist_ok=True)
    manifest, changed, problem = _load_manifest(mood_dir)
    _warn_if_ownership_reset(mood, mood_dir, problem)

    try:
        listed = client.playlist_tracks(playlist_id)
    except JellyfinError as e:
        result.error = f"cannot list playlist: {e}"
        return
    result.skipped = getattr(listed, "skipped", 0)
    if not listed and manifest:
        result.error = "playlist is empty; refusing to delete synced tracks"
        return

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
        # A file already at the name (a crashed run, a lost manifest) is adopted unless
        # Jellyfin says it should be a different size; then it is downloaded again.
        if path.is_file() and not path.is_symlink() and t.size in (None, path.stat().st_size):
            adopted[t.id] = name
        else:
            to_fetch.append((t, name))

    # J4: with nothing to fetch there is no space check. Deleting only frees space, so a
    # run that only deletes or adopts cannot fill the card.
    needed = sum(t.size or 0 for t, _ in to_fetch)
    if to_fetch and needed + MIN_FREE_START > free_bytes(mood_dir):
        result.error = "not enough space for downloads; nothing changed"
        return

    manifest.update(adopted)
    result.added += len(adopted)

    # J8: download first, then delete, so a run cut short never leaves a mood with
    # fewer tracks than it had.
    for index, (t, name) in enumerate(to_fetch):
        if free_bytes(mood_dir) - (t.size or 0) < MIN_FREE_RUN:
            left = len(to_fetch) - index
            result.failed += left
            log.warning("mood=%s: free space would drop below 1 GB; %d downloads left for later", mood, left)
            break
        part = mood_dir / f".{name}.part"
        try:
            part.unlink(missing_ok=True)  # a stale (or planted symlink) .part is never written through
            client.download(t.id, part)
            os.replace(part, mood_dir / name)
        except (JellyfinError, OSError) as e:
            with suppress(OSError):
                part.unlink(missing_ok=True)
            result.failed += 1
            log.warning("mood=%s item=%s: %s", mood, t.id, e)
            continue
        manifest[t.id] = name
        result.added += 1
        log.info("mood=%s downloaded %s", mood, name)

    # Delete only manifest-recorded bare filenames, and never one still in use.
    keep = {manifest[i] for i in manifest if i not in to_delete} | {n for _, n in to_fetch}
    for item_id, name in to_delete.items():
        if name not in keep:
            path = mood_dir / name
            if path.parent != mood_dir:  # defence in depth; _is_safe_name already ensures this
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                result.delete_failed += 1
                log.warning("mood=%s item=%s: cannot delete %s: %s", mood, item_id, name, e)
                continue
        del manifest[item_id]
        result.removed += 1

    if changed or result.added or result.removed:
        try:
            _write_manifest(mood_dir, manifest)
        except OSError:
            result.error = "cannot write manifest"
            return
    problems = []
    if result.failed:
        problems.append(f"{result.failed} downloads failed")
    if result.delete_failed:
        problems.append(f"{result.delete_failed} deletes failed")
    if result.skipped:
        problems.append(f"{result.skipped} playlist items skipped (invalid Id)")
    if problems:
        result.error = "; ".join(problems)


def _local(port: int, path: str, timeout: float, data: bytes | None = None) -> bytes:
    """One request to the local control API. Raises on any error.

    No Host header of our own: urllib sends ``127.0.0.1:<port>``, and the control page
    always accepts the address a request arrived on.
    """
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback: never via a proxy
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def trigger_rescan(port: int, timeout: float = 10.0) -> bool:
    """Ask the local control API to rescan the media dirs. False (and a warning) on any error."""
    try:
        _local(port, "/api/rescan", timeout, data=b"{}")
        return True
    except Exception as e:  # URLError, HTTPError, timeouts, resets: all just a warning
        log.warning("rescan failed: %s", e)
        return False


def music_playing(port: int, timeout: float = 10.0) -> bool | None:
    """Is music playing right now (GET /api/status)? None (and a warning) if unknown."""
    try:
        music = json.loads(_local(port, "/api/status", timeout))["music"]
        return music.get("playing") is True
    except Exception as e:  # unreachable, HTTP error, not JSON, no "music" object
        log.warning("cannot read the control page status: %s", e)
        return None


def _rescan_when_quiet(music_dir: Path, changed: bool, rescan, playing) -> None:
    """Rescan if something changed or a rescan is pending, but never under playing music.

    A rescan reshuffles the current mood, so while music plays (or when that cannot be
    told) it is deferred: a marker file asks a later run to do it.
    """
    marker = music_dir / RESCAN_PENDING
    if not (changed or marker.exists()):
        return
    state = playing()
    if state is False and rescan():
        marker.unlink(missing_ok=True)
        return
    if state is False:
        log.warning("rescan did not succeed; it is retried on the next run")
    else:
        why = "music is playing" if state else "the control page status is unknown"
        log.info("rescan deferred (%s); the next run retries", why)
    try:
        marker.touch()
    except OSError as e:
        log.warning("cannot write the rescan-pending marker %s: %s", marker, e)


def run_sync(
    config: Config,
    jf: JellyfinConfig,
    client,
    rescan: Callable[[], bool],
    playing: Callable[[], bool | None],
) -> int:
    """Sync every configured mood, then rescan if due (see _rescan_when_quiet).

    0 if every mood synced, 1 if any mood errored; rescan problems are only warnings.
    """
    changed = False
    failed = False
    for mood, playlist_id in jf.playlists.items():
        result = sync_mood(client, mood, playlist_id, config.music_dir / mood, free_bytes=default_free_bytes)
        line = (
            f"mood={mood} added={result.added} removed={result.removed} failed={result.failed}"
            f" delete_failed={result.delete_failed} skipped={result.skipped}"
        )
        if result.error:
            line += f" error={result.error}"
            failed = True
        log.info("%s", line)
        changed = changed or bool(result.added or result.removed)
    _rescan_when_quiet(config.music_dir, changed, rescan, playing)
    return 1 if failed else 0
