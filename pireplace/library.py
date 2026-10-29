"""Media discovery: find videos and mood (music) folders."""

from pathlib import Path

VIDEO_EXTS = frozenset({".mp4", ".mkv", ".webm", ".mov", ".m4v"})
AUDIO_EXTS = frozenset({".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav"})


def _scan(root: Path, exts: frozenset[str]) -> list[Path]:
    """Recursively list matching files under root, skipping hidden files and
    folders (any path part starting with '.'), sorted by path relative to root."""
    if not root.is_dir():
        return []
    found = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if p.suffix.lower() in exts and p.is_file():
            found.append(rel)
    return [root / rel for rel in sorted(found)]


def find_videos(video_dir: Path) -> list[Path]:
    return _scan(video_dir, VIDEO_EXTS)


def find_moods(music_dir: Path) -> dict[str, list[Path]]:
    if not music_dir.is_dir():
        return {}
    moods = {}
    for d in sorted(music_dir.iterdir()):
        if d.name.startswith(".") or not d.is_dir():
            continue
        tracks = _scan(d, AUDIO_EXTS)
        if tracks:
            moods[d.name] = tracks
    return moods


def mood_label(name: str) -> str:
    return name.replace("-", " ").replace("_", " ").title()
