"""Mirror a Jellyfin playlist into a local mood folder, tracked by a manifest.

Safety rules: only files recorded in the manifest are ever deleted, only by a
bare filename resolved inside the mood dir; anything else in the folder
(manual rips, dotfiles) is never touched.

The manifest also records the audio profile each file was fetched with ("original"
or e.g. "opus-160"). A file whose profile differs from the configured one is fetched
again in the new format, and the old file is deleted only after the new one is in
place (migration, e.g. FLAC to Opus).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import unicodedata
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pireplace.config import Config, JellyfinConfig, audio_profile
from pireplace.jellyfin import JellyfinError, Track

log = logging.getLogger("pireplace.sync")

MANIFEST = ".pireplace-sync.json"
RESCAN_PENDING = ".pireplace-rescan-pending"  # in the music dir; hidden, so never a mood
MIN_FREE_START = 2 * 1024**3
MIN_FREE_RUN = 1024**3
MAX_NAME_BYTES = 200
MAX_EXT_BYTES = 16
ORIGINAL = "original"
UNKNOWN_DURATION_ESTIMATE = 10 * 1024**2  # bytes assumed for a transcode of unknown length
MIN_TRANSCODE_CAP = 1024**2  # floor of the per-file size cap passed to the client

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


def track_filename(track: Track, ext: str | None = None) -> str:
    """``<Artist> - <Title> [<id[:8]>].<ext>``: a bare, non-hidden name of at most 200 bytes.

    `ext` overrides the track's container (transcodes use "ogg").
    """
    artist = _clean(track.artist, "Unknown Artist")
    title = _clean(track.title, "Untitled")
    # The id comes from the server unvalidated: same rules, and no dots in the extension.
    ident = _clean(str(track.id)[:8], "unknown")
    container = track.container if ext is None else ext
    ext = _truncate(_clean((container or "").replace(".", "_"), "audio"), MAX_EXT_BYTES) or "audio"
    fixed = f" -   [{ident}].{ext}"
    budget = MAX_NAME_BYTES - len(fixed.encode("utf-8"))
    artist = _truncate(artist, budget // 2)
    title = _truncate(title, budget - len(artist.encode("utf-8")))
    return f"{artist} - {title} [{ident}].{ext}"


def _expected_bytes(duration_s: float, bitrate_kbps: int) -> float:
    return duration_s * bitrate_kbps * 1000 / 8


def estimate_bytes(track: Track, bitrate_kbps: int) -> int:
    """Space to set aside for a transcode: duration x bitrate plus 50%; 10 MiB if unknown."""
    if track.duration_s is None:
        return UNKNOWN_DURATION_ESTIMATE
    return math.ceil(_expected_bytes(track.duration_s, bitrate_kbps) * 1.5)


def plausible_size(size: int, duration_s: float | None, bitrate_kbps: int) -> bool:
    """Is `size` 25%..300% of duration x bitrate? With no duration, any non-empty size is."""
    if duration_s is None:
        return size > 0
    expected = _expected_bytes(duration_s, bitrate_kbps)
    return 0.25 * expected <= size <= 3 * expected


def _transcode_cap(track: Track, bitrate_kbps: int) -> int | None:
    """max_bytes for download_transcoded; None means the client default."""
    if track.duration_s is None:
        return None
    return max(MIN_TRANSCODE_CAP, math.ceil(3 * _expected_bytes(track.duration_s, bitrate_kbps)))


def _is_adoptable_transcode(path: Path, track: Track, bitrate_kbps: int) -> bool:
    """A transcode left by a crashed run: a regular Ogg file of a plausible size."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as f:
            head = f.read(4)
            size = os.fstat(f.fileno()).st_size
    except OSError:
        return False
    return head == b"OggS" and plausible_size(size, track.duration_s, bitrate_kbps)


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


def _load_manifest(mood_dir: Path) -> tuple[dict[str, str], dict[str, str], bool, str | None]:
    """Return (items, profiles, dirty, problem); problem is "missing", "corrupt" or None.

    Unsafe entries are dropped (never deleted). ``profiles`` has an entry for every
    item; one missing from the file (an old manifest) or not a string is "original".
    A manifest that exists but cannot be read raises OSError: guessing it empty would
    orphan every synced file.
    """
    try:
        text = (mood_dir / MANIFEST).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, {}, False, "missing"
    except UnicodeDecodeError:
        return {}, {}, False, "corrupt"
    try:
        data = json.loads(text)
        items = data["items"]
        if not isinstance(items, dict):
            raise TypeError
        raw_profiles = data.get("profiles", {})
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}, {}, False, "corrupt"
    if not isinstance(raw_profiles, dict):
        raw_profiles = {}
    clean = {k: v for k, v in items.items() if isinstance(k, str) and _is_safe_name(v)}
    profiles = {}
    for k in clean:
        p = raw_profiles.get(k)
        profiles[k] = p if isinstance(p, str) and p else ORIGINAL
    return clean, profiles, len(clean) != len(items), None


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


def _write_manifest(mood_dir: Path, items: dict[str, str], profiles: dict[str, str]) -> None:
    data = {"items": items, "profiles": {i: profiles.get(i, ORIGINAL) for i in items}}
    tmp = mood_dir / f".{MANIFEST}.tmp"
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, mood_dir / MANIFEST)
    finally:
        tmp.unlink(missing_ok=True)


def sync_mood(
    client,
    mood: str,
    playlist_id: str,
    mood_dir: Path,
    free_bytes: Callable[[Path], int] = default_free_bytes,
    profile: str = ORIGINAL,
    bitrate_kbps: int = 160,
) -> MoodResult:
    """Sync one mood. Any error, filesystem ones included, ends up in ``.error``.

    `profile` is "original" (fetch the file as stored) or "opus-<kbps>" (fetch a
    transcode at `bitrate_kbps`, never the original).
    """
    result = MoodResult(mood=mood)
    try:
        _sync_mood(client, mood, playlist_id, Path(mood_dir), free_bytes, result, profile, bitrate_kbps)
    except OSError as e:
        result.error = f"filesystem error: {e}"
    return result


def _sync_mood(
    client, mood: str, playlist_id: str, mood_dir: Path, free_bytes, result: MoodResult,
    profile: str, bitrate_kbps: int,
) -> None:
    transcode = profile != ORIGINAL
    ext = "ogg" if transcode else None
    mood_dir.mkdir(parents=True, exist_ok=True)
    manifest, profiles, changed, problem = _load_manifest(mood_dir)
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
        old = manifest.get(t.id)
        if old is not None and profiles[t.id] == profile:
            continue  # current
        name = track_filename(t, ext)
        path = mood_dir / name
        # A file already at the name (a crashed run, a lost manifest) is adopted if it
        # checks out; otherwise it is downloaded again. The track's own old-profile
        # file is never adopted as the new one, even when the names match.
        if name != old and path.is_file() and not path.is_symlink() and (
            _is_adoptable_transcode(path, t, bitrate_kbps) if transcode
            else t.size in (None, path.stat().st_size)
        ):
            adopted[t.id] = name
        else:
            to_fetch.append((t, name))

    def need(t: Track) -> int:
        return estimate_bytes(t, bitrate_kbps) if transcode else (t.size or 0)

    # J4: with nothing to fetch there is no space check. Deleting only frees space, so a
    # run that only deletes or adopts cannot fill the card.
    needed = sum(need(t) for t, _ in to_fetch)
    if to_fetch and needed + MIN_FREE_START > free_bytes(mood_dir):
        result.error = "not enough space for downloads; nothing changed"
        return

    # Old-profile files replaced by a new one: deleted with the others, after every
    # download, and only once the new file is in place and recorded.
    superseded: list[tuple[str, str]] = []

    def record(item_id: str, name: str) -> None:
        old = manifest.get(item_id)
        if old is not None and old != name:
            superseded.append((item_id, old))
        manifest[item_id] = name
        profiles[item_id] = profile
        result.added += 1

    for item_id, name in adopted.items():
        record(item_id, name)

    # J8: download first, then delete, so a run cut short never leaves a mood with
    # fewer tracks than it had.
    for index, (t, name) in enumerate(to_fetch):
        if free_bytes(mood_dir) - need(t) < MIN_FREE_RUN:
            left = len(to_fetch) - index
            result.failed += left
            log.warning("mood=%s: free space would drop below 1 GB; %d downloads left for later", mood, left)
            break
        part = mood_dir / f".{name}.part"
        try:
            part.unlink(missing_ok=True)  # a stale (or planted symlink) .part is never written through
            if transcode:
                # Never the original: a refused or ignored transcode fails the track.
                cap = _transcode_cap(t, bitrate_kbps)
                kwargs = {} if cap is None else {"max_bytes": cap}
                client.download_transcoded(t.id, part, bitrate_kbps, **kwargs)
                size = part.stat().st_size
                if not plausible_size(size, t.duration_s, bitrate_kbps):
                    raise JellyfinError(
                        f"transcode of {size} bytes is implausible for {t.duration_s} s at {bitrate_kbps} kbps"
                    )
            else:
                client.download(t.id, part)
            os.replace(part, mood_dir / name)
        except (JellyfinError, OSError) as e:
            with suppress(OSError):
                part.unlink(missing_ok=True)
            result.failed += 1
            log.warning("mood=%s item=%s: %s", mood, t.id, e)
            continue
        record(t.id, name)
        log.info("mood=%s downloaded %s", mood, name)

    # Delete only manifest-recorded bare filenames, and never one still in use.
    keep = {manifest[i] for i in manifest if i not in to_delete} | {n for _, n in to_fetch}
    doomed = [(i, n, True) for i, n in to_delete.items()] + [(i, n, False) for i, n in superseded]
    for item_id, name, forget in doomed:
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
        if forget:
            del manifest[item_id]
            profiles.pop(item_id, None)
        result.removed += 1

    if changed or result.added or result.removed:
        try:
            _write_manifest(mood_dir, manifest, profiles)
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
    profile = audio_profile(jf)
    for mood, playlist_id in jf.playlists.items():
        result = sync_mood(
            client, mood, playlist_id, config.music_dir / mood, free_bytes=default_free_bytes,
            profile=profile, bitrate_kbps=jf.audio_bitrate_kbps,
        )
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
