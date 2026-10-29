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
import stat
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
DURATION_SLACK_S = 2.0  # a transcode may differ from the track's duration by this much,
DURATION_SLACK = 0.02  # or by this fraction of it, whichever is larger
OGG_HEADER = 27  # bytes of an Ogg page header before its segment table
OPUS_HEAD_LEN = 19  # the OpusHead packet
OGG_TAIL = 64 * 1024  # an Ogg page is at most 65307 bytes; the last one is in this tail

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


def _open_nofollow(path: Path):
    return os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb")


def _last_page(tail: bytes) -> tuple[int, bytes] | None:
    """(granule, serial) of the Ogg page that ends exactly at the end of `tail`."""
    end = len(tail)
    while (i := tail.rfind(b"OggS", 0, end)) >= 0:
        end = i
        nseg_at = i + 26
        if nseg_at >= len(tail) or tail[i + 4] != 0:  # incomplete header or not version 0
            continue
        table = tail[nseg_at + 1:nseg_at + 1 + tail[nseg_at]]
        if len(table) == tail[nseg_at] and nseg_at + 1 + len(table) + sum(table) == len(tail):
            granule = int.from_bytes(tail[i + 6:i + 14], "little", signed=True)
            return (granule, tail[i + 14:i + 18]) if granule >= 0 else None
    return None


def ogg_duration_s(path: Path) -> float | None:
    """Length of an Opus-in-Ogg file from its headers; None if it cannot be read.

    (granule of the last page - OpusHead pre-skip) / 48000. The last page is the one
    that ends exactly at the end of the file, so a stream cut mid-page has none, and it
    must belong to the OpusHead's logical stream (same serial): a chained file is refused.
    """
    try:
        with _open_nofollow(path) as f:
            head = f.read(OGG_HEADER + 255 + OPUS_HEAD_LEN)
            size = os.fstat(f.fileno()).st_size
            f.seek(max(0, size - OGG_TAIL))
            tail = f.read(OGG_TAIL)
    except OSError:
        return None
    if len(head) < OGG_HEADER or head[:4] != b"OggS" or head[4] != 0:
        return None
    start = OGG_HEADER + head[26]
    packet = head[start:start + OPUS_HEAD_LEN]
    if len(packet) < OPUS_HEAD_LEN or packet[:8] != b"OpusHead":
        return None
    pre_skip = int.from_bytes(packet[10:12], "little")
    last = _last_page(tail)
    if last is None or last[1] != head[14:18]:
        return None
    granule = last[0]
    if granule <= pre_skip:
        return None
    return (granule - pre_skip) / 48000


def _transcode_problem(path: Path, track: Track, bitrate_kbps: int) -> str | None:
    """Why the transcode at `path` must not be used for `track`; None if it may.

    An Ogg stream of a plausible size and, when Jellyfin gave the track's duration,
    an Opus duration within max(2 s, 2%) of it: a cut-short stream is never accepted.
    """
    try:
        with _open_nofollow(path) as f:
            head = f.read(4)
            size = os.fstat(f.fileno()).st_size
    except OSError as e:
        return f"cannot read the transcode: {e.strerror}"
    if head != b"OggS":
        return "transcode is not an Ogg stream"
    duration = track.duration_s
    if not plausible_size(size, duration, bitrate_kbps):
        return f"transcode of {size} bytes is implausible for {duration} s at {bitrate_kbps} kbps"
    if duration is not None:
        got = ogg_duration_s(path)
        if got is None:
            return "cannot read the transcode's duration"
        if abs(got - duration) > max(DURATION_SLACK_S, DURATION_SLACK * duration):
            return f"transcode lasts {got:.1f} s, the track {duration:.1f} s"
    return None


def _regular_size(path: Path) -> int | None:
    """Size of a regular file (never through a symlink); None otherwise."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_size if stat.S_ISREG(st.st_mode) else None


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


def _load_manifest(
    mood_dir: Path,
) -> tuple[dict[str, str], dict[str, str], set[str], bool, str | None]:
    """Return (items, profiles, stale, dirty, problem); problem is "missing", "corrupt" or None.

    Unsafe entries are dropped (never deleted). ``profiles`` has an entry for every
    item; one missing from the file (an old manifest) or not a string is "original".
    ``stale`` holds bare names of replaced files whose deletion is still owed.
    A manifest that exists but cannot be read raises OSError: guessing it empty would
    orphan every synced file.
    """
    try:
        text = (mood_dir / MANIFEST).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, {}, set(), False, "missing"
    except UnicodeDecodeError:
        return {}, {}, set(), False, "corrupt"
    try:
        data = json.loads(text)
        items = data["items"]
        if not isinstance(items, dict):
            raise TypeError
        raw_profiles = data.get("profiles", {})
        raw_stale = data.get("stale", [])
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}, {}, set(), False, "corrupt"
    if not isinstance(raw_profiles, dict):
        raw_profiles = {}
    if not isinstance(raw_stale, list):
        raw_stale = []
    clean = {k: v for k, v in items.items() if isinstance(k, str) and _is_safe_name(v)}
    profiles = {}
    for k in clean:
        p = raw_profiles.get(k)
        profiles[k] = p if isinstance(p, str) and p else ORIGINAL
    stale = {n for n in raw_stale if _is_safe_name(n)}
    dirty = len(clean) != len(items) or len(stale) != len(raw_stale)
    return clean, profiles, stale, dirty, None


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


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_manifest(
    mood_dir: Path, items: dict[str, str], profiles: dict[str, str], stale: set[str],
) -> None:
    """Durably replace the manifest: fsynced tmp file, rename, fsynced directory."""
    data = {
        "items": items,
        "profiles": {i: profiles.get(i, ORIGINAL) for i in items},
        "stale": sorted(stale),
    }
    tmp = mood_dir / f".{MANIFEST}.tmp"
    try:
        tmp.unlink(missing_ok=True)  # never written through a planted symlink
        with open(tmp, "x", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, mood_dir / MANIFEST)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(mood_dir)


def _delete_stale(mood: str, mood_dir: Path, stale: set[str], items: dict[str, str], result: MoodResult) -> set[str]:
    """Delete owed old files; return the names still owed (their delete failed).

    A name some item still records is never deleted, only dropped from the list.
    """
    in_use = set(items.values())
    left = set()
    for name in sorted(stale):
        if name in in_use:
            continue
        path = mood_dir / name
        if path.parent != mood_dir:  # defence in depth; _is_safe_name already ensures this
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            result.delete_failed += 1
            left.add(name)
            log.warning("mood=%s: cannot delete replaced file %s (retried next run): %s", mood, name, e)
            continue
        result.removed += 1
        log.info("mood=%s deleted replaced file %s", mood, name)
    return left


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
    manifest, profiles, stale, changed, problem = _load_manifest(mood_dir)
    _warn_if_ownership_reset(mood, mood_dir, problem)

    # Old files an earlier run replaced but could not delete: retried first.
    if stale:
        left = _delete_stale(mood, mood_dir, stale, manifest, result)
        if left != stale:
            stale = left
            try:
                _write_manifest(mood_dir, manifest, profiles, stale)
            except OSError:
                result.error = "cannot write manifest"
                return

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
        name = track_filename(t, ext)
        if transcode and t.id not in manifest:
            # After an ownership reset, an original file of the size Jellyfin reports is
            # ours: owned again as "original", so it is migrated and then deleted.
            orig = track_filename(t)
            if orig != name and t.size is not None and _regular_size(mood_dir / orig) == t.size:
                manifest[t.id] = orig
                profiles[t.id] = ORIGINAL
                changed = True
                log.info("mood=%s item=%s: adopted %s as original", mood, t.id, orig)
        old = manifest.get(t.id)
        if old is not None and profiles[t.id] == profile:
            continue  # current
        if transcode and old is not None and t.duration_s is None:
            # A transcode of unknown length cannot be checked: never trade a good file for it.
            log.info("mood=%s item=%s: duration unknown; keeping original %s", mood, t.id, old)
            continue
        path = mood_dir / name
        # A file already at the name (a crashed run, a lost manifest) is adopted if it
        # checks out; otherwise it is downloaded again. The track's own old-profile
        # file is never adopted as the new one, even when the names match.
        if name != old and path.is_file() and not path.is_symlink() and (
            _transcode_problem(path, t, bitrate_kbps) is None if transcode
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

    # Old-profile files replaced by a new one. They become "stale" (owed deletes, kept
    # in the manifest until done) only once the new file is in place and recorded.
    superseded: list[str] = []

    def record(item_id: str, name: str) -> None:
        old = manifest.get(item_id)
        if old is not None and old != name:
            superseded.append(old)
        manifest[item_id] = name
        profiles[item_id] = profile
        result.added += 1

    for item_id, name in adopted.items():
        record(item_id, name)

    # J8: download first, then delete, so a run cut short never leaves a mood with
    # fewer tracks than it had.
    reasons: list[str] = []
    for index, (t, name) in enumerate(to_fetch):
        if free_bytes(mood_dir) - need(t) < MIN_FREE_RUN:
            left_over = len(to_fetch) - index
            result.failed += left_over
            reasons.append("free space would drop below 1 GB")
            log.warning("mood=%s: free space would drop below 1 GB; %d downloads left for later", mood, left_over)
            break
        part = mood_dir / f".{name}.part"
        try:
            part.unlink(missing_ok=True)  # a stale (or planted symlink) .part is never written through
            if transcode:
                # Never the original: a refused or ignored transcode fails the track.
                cap = _transcode_cap(t, bitrate_kbps)
                kwargs = {} if cap is None else {"max_bytes": cap}
                client.download_transcoded(t.id, part, bitrate_kbps, **kwargs)
                why = _transcode_problem(part, t, bitrate_kbps)
                if why is not None:
                    raise JellyfinError(why)
            else:
                client.download(t.id, part)
            os.replace(part, mood_dir / name)
        except (JellyfinError, OSError) as e:
            with suppress(OSError):
                part.unlink(missing_ok=True)
            result.failed += 1
            reasons.append(str(e))
            log.warning("mood=%s item=%s: %s", mood, t.id, e)
            continue
        record(t.id, name)
        log.info("mood=%s downloaded %s", mood, name)

    keep = {manifest[i] for i in manifest if i not in to_delete} | {n for _, n in to_fetch}
    replaced = {n for n in superseded if n not in keep}
    stale |= replaced

    # The new files and the manifest naming them are on disk before anything is deleted,
    # so a power cut cannot leave a track with neither its old nor its new file.
    if (changed or result.added) and (stale or to_delete):
        try:
            _fsync_dir(mood_dir)
            _write_manifest(mood_dir, manifest, profiles, stale)
        except OSError:
            result.error = "cannot write manifest"
            return

    # Delete only manifest-recorded bare filenames, and never one still in use.
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
        profiles.pop(item_id, None)
        result.removed += 1
    if replaced:
        stale = (stale - replaced) | _delete_stale(mood, mood_dir, replaced, manifest, result)

    if changed or result.added or result.removed or superseded:
        try:
            _write_manifest(mood_dir, manifest, profiles, stale)
        except OSError:
            result.error = "cannot write manifest"
            return
    problems = []
    if result.failed:
        problems.append(f"{result.failed} downloads failed (first: {reasons[0]})")
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

