"""Fireplace controller: orchestrates the video and music mpv players and persisted state."""

from __future__ import annotations

import logging
import random
from dataclasses import replace
from pathlib import Path
from typing import Callable

from pireplace.config import Config
from pireplace.library import find_moods, find_videos, mood_label
from pireplace.mpv import MpvClient, MpvError, MpvUnavailable
from pireplace.state import State, StateStore

log = logging.getLogger(__name__)

BOOT_MARKER = "boot-done"  # lives in the runtime socket dir (tmpfs: gone after a reboot)
PLAYERS = ("video", "music")


class UnknownMood(Exception):
    """Requested mood is not a folder with tracks under the music dir."""


def _check_level(level: object) -> None:
    if type(level) is not int or not 0 <= level <= 100:
        raise ValueError(f"volume must be an integer 0-100, got {level!r}")


class Fireplace:
    def __init__(
        self,
        config: Config,
        store: StateStore,
        video: MpvClient,
        music: MpvClient,
        shuffle: Callable[[list], None] = random.shuffle,
    ) -> None:
        self.config = config
        self.store = store
        self.video = video
        self.music = music
        self._shuffle = shuffle
        self._boot_marker = Path(config.music_socket).parent / BOOT_MARKER
        self._boot: bool | None = None  # decided on the first reconcile
        self._pids: dict[str, object] = {}  # last mpv pid seen per player
        self._down: set[str] = set()  # players whose last reconcile failed (log once)

    # -- helpers ---------------------------------------------------------

    def _load_videos(self) -> None:
        self._load(self.video, find_videos(self.config.video_dir))

    def _load_music(self, tracks: list[Path]) -> None:
        tracks = list(tracks)
        self._shuffle(tracks)
        self._load(self.music, tracks)

    @staticmethod
    def _load(player: MpvClient, files: list[Path]) -> None:
        if not files:
            player.command("stop")  # clear a stale playlist rather than keep playing it
            return
        player.command("loadfile", str(files[0]), "replace")
        for f in files[1:]:
            player.command("loadfile", str(f), "append")

    def _resolve_mood(self, saved: str | None, moods: dict[str, list[Path]]) -> str | None:
        for candidate in (saved, self.config.default_mood):
            if candidate is not None and candidate in moods:
                return candidate
        return next(iter(moods), None)

    def _update(self, **changes) -> None:
        old = self.store.load()
        self._save_if_changed(old, replace(old, **changes))

    def _save_if_changed(self, old: State, new: State) -> None:
        if new != old:  # no SD-card write when nothing changed
            self.store.save(new)

    # -- lifecycle -------------------------------------------------------

    def reconcile(self) -> None:
        """Bring each player in line with the saved state. Safe to call every few seconds.

        Per player, keyed on mpv's `pid`:
        - same pid as last round: nothing to do (no commands, no writes);
        - first sight during a boot (no boot marker): boot defaults - log sound on,
          music playing unless start_music is false;
        - otherwise (controller restart, or a new pid because mpv restarted): the
          persisted state is applied again.
        Either way an empty player is (re)loaded. A player that is down or errors is
        skipped this round; the other is still handled.
        """
        if self._boot is None:
            self._boot = not self._boot_marker.exists()
        before = state = self.store.load()
        for name, player, apply in (
            ("video", self.video, self._apply_video),
            ("music", self.music, self._apply_music),
        ):
            try:
                pid = player.get("pid")
                self._down.discard(name)
                last = self._pids.get(name)
                if last is not None and pid == last:
                    continue
                restarted = last is not None
                state = apply(state, boot_defaults=self._boot)
            except (MpvUnavailable, MpvError, OSError) as e:
                if name not in self._down:
                    log.warning("%s player not ready, will retry: %s", name, e)
                    self._down.add(name)
                continue
            self._pids[name] = pid
            log.info("%s player %s (pid %s)", name, "restarted, set up again" if restarted else "set up", pid)
        self._save_if_changed(before, state)
        if self._boot and all(p in self._pids for p in PLAYERS):
            self._boot = False
            try:
                self._boot_marker.touch()
            except OSError as e:
                log.warning("cannot write boot marker %s: %s", self._boot_marker, e)

    def _apply_video(self, state: State, boot_defaults: bool) -> State:
        if boot_defaults:
            state = replace(state, log_muted=False)
        self.video.set("volume", state.log_volume)
        self.video.set("mute", state.log_muted)
        if self.video.get("playlist-count") == 0:
            self._load_videos()
        return state

    def _apply_music(self, state: State, boot_defaults: bool) -> State:
        moods = find_moods(self.config.music_dir)
        mood = self._resolve_mood(state.mood, moods)
        state = replace(state, mood=mood)
        if boot_defaults:
            state = replace(state, music_playing=self.config.start_music)
        self.music.set("volume", state.music_volume)
        self.music.set("pause", not state.music_playing)
        if self.music.get("playlist-count") == 0:
            self._load_music(moods[mood] if mood is not None else [])
        return state

    def rescan(self) -> None:
        self._load_videos()
        moods = find_moods(self.config.music_dir)
        mood = self._resolve_mood(self.store.load().mood, moods)
        self._load_music(moods[mood] if mood is not None else [])
        self._update(mood=mood)

    # -- status ----------------------------------------------------------

    def status(self) -> dict:
        try:
            moods = find_moods(self.config.music_dir)
        except OSError:
            moods = {}
        try:
            videos = len(find_videos(self.config.video_dir))
        except OSError:
            videos = 0
        state = self.store.load()
        try:
            try:
                title = self.music.get("media-title")
            except MpvError:  # idle mpv has no title
                title = None
            idle = self.music.get("playlist-count") == 0
            music = {
                "available": True,
                "playing": not idle and not self.music.get("pause"),
                "volume": round(self.music.get("volume")),
                "now_playing": title or None,
            }
        except (MpvUnavailable, MpvError):
            music = {"available": False}
        try:
            log = {
                "available": True,
                "volume": round(self.video.get("volume")),
                "muted": bool(self.video.get("mute")),
                "videos": videos,
            }
        except (MpvUnavailable, MpvError):
            log = {"available": False}
        return {
            "moods": [
                {"name": n, "label": mood_label(n), "tracks": len(t)} for n, t in moods.items()
            ],
            "mood": state.mood if state.mood in moods else None,
            "music": music,
            "log": log,
        }

    # -- mutators --------------------------------------------------------

    def set_mood(self, name: str) -> None:
        moods = find_moods(self.config.music_dir)
        if name not in moods:
            raise UnknownMood(name)
        self._load_music(moods[name])
        self.music.set("pause", False)
        self._update(mood=name, music_playing=True)

    def play(self) -> None:
        self.music.set("pause", False)
        self._update(music_playing=True)

    def pause(self) -> None:
        self.music.set("pause", True)
        self._update(music_playing=False)

    def next_track(self) -> None:
        self.music.command("playlist-next")

    def set_music_volume(self, level: int) -> None:
        _check_level(level)
        self.music.set("volume", level)
        self._update(music_volume=level)

    def set_log_volume(self, level: int) -> None:
        _check_level(level)
        self.video.set("volume", level)
        self._update(log_volume=level)

    def set_log_muted(self, muted: bool) -> None:
        self.video.set("mute", bool(muted))
        self._update(log_muted=bool(muted))
