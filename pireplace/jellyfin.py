"""Minimal stdlib Jellyfin client: list playlist tracks, download items.

Errors never include the API key or request headers. They are raised outside
any ``except`` block so neither ``__cause__`` nor ``__context__`` carries the
original exception (whose text could echo request details).
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("pireplace.jellyfin")

CHUNK = 1024 * 1024
_ITEM_ID = re.compile(r"[0-9a-fA-F]{32}")  # Jellyfin item Ids; anything else is skipped
# A relay that trickles must not hold a run for hours: give up on a file after this.
DOWNLOAD_DEADLINE = 15 * 60.0


class JellyfinError(Exception):
    pass


@dataclass(frozen=True)
class Track:
    id: str
    artist: str | None
    title: str | None
    container: str | None
    size: int | None


class TrackList(list):
    """playlist_tracks' result: the tracks, plus how many items were skipped."""

    skipped: int = 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: the Authorization header must stay on our server."""

    def redirect_request(self, *args, **kwargs):
        return None


def _str_or_none(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _track(raw: dict) -> Track | None:
    """The item as a Track, or None if its Id is not a Jellyfin Id (32 hex characters)."""
    item_id = raw.get("Id")
    if not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
        return None
    artist = _str_or_none(raw.get("AlbumArtist"))
    if artist is None:
        artists = raw.get("Artists")
        if isinstance(artists, list) and artists:
            artist = _str_or_none(artists[0])
    source = None
    sources = raw.get("MediaSources")
    if isinstance(sources, list) and sources and isinstance(sources[0], dict):
        source = sources[0]
    container = _str_or_none(raw.get("Container"))
    if container is None and source is not None:
        container = _str_or_none(source.get("Container"))
    size = source.get("Size") if source is not None else None
    if isinstance(size, bool) or not isinstance(size, int):
        size = None
    return Track(
        id=item_id,
        artist=artist,
        title=_str_or_none(raw.get("Name")),
        container=container.lower() if container else None,
        size=size,
    )


class JellyfinClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        user_id: str,
        timeout: float = 30.0,
        download_deadline: float = DOWNLOAD_DEADLINE,
    ) -> None:
        """`timeout` bounds each network wait; `download_deadline` a whole file."""
        self._url = url.rstrip("/")
        self._download_deadline = download_deadline
        self._api_key = api_key
        self._user_id = user_id
        self._timeout = timeout
        # ProxyHandler({}): never send the token through a proxy taken from the environment.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def _request(self, path: str, query: str = ""):
        """Open GET path. Returns (response, None) or (None, error message)."""
        req = urllib.request.Request(
            self._url + path + (f"?{query}" if query else ""),
            headers={"Authorization": f'MediaBrowser Token="{self._api_key}"'},
        )
        try:
            resp = self._opener.open(req, timeout=self._timeout)
        except urllib.error.HTTPError as e:
            e.close()
            return None, f"HTTP {e.code} from {path}"
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError):
            return None, f"cannot reach Jellyfin for {path}"
        if resp.status != 200:
            resp.close()
            return None, f"HTTP {resp.status} from {path}"
        return resp, None

    def playlist_tracks(self, playlist_id: str) -> TrackList:
        """The playlist's tracks, duplicates dropped. Items without a valid Id are logged
        and counted in ``.skipped``."""
        path = f"/Playlists/{urllib.parse.quote(playlist_id, safe='')}/Items"
        # fields=MediaSources: the file size and container, for the space check and names.
        query = urllib.parse.urlencode({"userId": self._user_id, "fields": "MediaSources"})
        resp, err = self._request(path, query)
        if resp is None:
            raise JellyfinError(err) from None
        tracks = TrackList()
        try:
            with closing(resp):
                data = json.loads(resp.read())
            seen: set[str] = set()
            for raw in data["Items"]:
                track = _track(raw)
                if track is None:
                    tracks.skipped += 1
                    log.warning("playlist %s: skipping an item with an invalid Id %.40r",
                                playlist_id, raw.get("Id"))
                    continue
                if track.id not in seen:
                    seen.add(track.id)
                    tracks.append(track)
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError):
            err = f"unexpected response from {path}"
        else:
            return tracks
        raise JellyfinError(err) from None

    def download(self, item_id: str, dest: Path) -> int:
        """Write the original file to `dest`, which must not exist (never follows a link).

        The data is fsynced before returning, so a rename into place after a power cut
        never leaves an empty or partial track. On any error `dest` is removed.
        """
        dest = Path(dest)
        path = f"/Items/{urllib.parse.quote(item_id, safe='')}/Download"
        written = 0
        deadline = time.monotonic() + self._download_deadline
        resp, err = self._request(path)
        if resp is not None:
            try:
                with closing(resp), open(dest, "xb") as out:
                    # read1: return what has arrived, so the deadline is checked as data
                    # trickles in rather than after a whole chunk.
                    while chunk := resp.read1(CHUNK):
                        out.write(chunk)
                        written += len(chunk)
                        if time.monotonic() > deadline:
                            err = f"download took longer than {self._download_deadline:g} s for {path}"
                            break
                    if err is None:
                        expected = resp.headers.get("Content-Length")
                        if expected is not None and int(expected) != written:
                            err = f"incomplete download from {path}: got {written} of {expected} bytes"
                    if err is None:
                        out.flush()
                        os.fsync(out.fileno())
            except (OSError, http.client.HTTPException, ValueError):
                err = f"download failed for {path}"
        if err is not None:
            dest.unlink(missing_ok=True)
            raise JellyfinError(err) from None
        return written
