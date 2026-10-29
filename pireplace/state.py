"""Persisted runtime state, stored as JSON with atomic writes."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass
class State:
    mood: str | None
    music_volume: int
    log_volume: int
    log_muted: bool
    music_playing: bool


def _valid(name: str, value: object) -> bool:
    if name == "mood":
        return value is None or isinstance(value, str)
    if name in ("music_volume", "log_volume"):
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100
    return isinstance(value, bool)  # log_muted, music_playing


class StateStore:
    def __init__(self, path: Path, defaults: State):
        self.path = Path(path)
        self.defaults = defaults

    def load(self) -> State:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return self.defaults
        if not isinstance(data, dict):
            return self.defaults
        merged = {}
        for f in fields(State):
            value = data.get(f.name, getattr(self.defaults, f.name))
            if not _valid(f.name, value):
                value = getattr(self.defaults, f.name)
            merged[f.name] = value
        return State(**merged)

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(state), f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
