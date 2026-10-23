"""Fireplace controller: orchestrates the video and music mpv players and persisted state."""

from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path
from typing import Callable

from pireplace.config import Config
from pireplace.library import find_moods, find_videos, mood_label
from pireplace.mpv import MpvClient, MpvError, MpvUnavailable
from pireplace.state import StateStore


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
        self.store.save(replace(self.store.load(), **changes))

    # -- lifecycle -------------------------------------------------------

    def startup(self) -> None:
        state = self.store.load()
        if self.video.get("playlist-count") == 0:
            self._load_videos()
        self.video.set("volume", state.log_volume)
        self.video.set("mute", state.log_muted)

        moods = find_moods(self.config.music_dir)
        mood = self._resolve_mood(state.mood, moods)
        if mood is not None and self.music.get("playlist-count") == 0:
            self._load_music(moods[mood])
        self.music.set("volume", state.music_volume)
        self.music.set("pause", not state.music_playing)
        self.store.save(replace(state, mood=mood))

    def rescan(self) -> None:
        self._load_videos()
        moods = find_moods(self.config.music_dir)
        mood = self._resolve_mood(self.store.load().mood, moods)
        if mood is not None:
            self._load_music(moods[mood])
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
            music = {
                "available": True,
                "playing": not self.music.get("pause"),
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
