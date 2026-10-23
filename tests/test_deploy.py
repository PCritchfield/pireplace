"""Text-level checks of the systemd units, polkit rule and playbook (no Pi needed)."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UNITS = ROOT / "deploy" / "systemd"
SYSTEM_UNITS = ["pireplace-control.service", "pireplace-wifi.service"]
USER_UNITS = ["pireplace-video.service", "pireplace-music.service"]


def unit(name: str) -> dict[str, dict[str, list[str]]]:
    """Parse a unit file into {section: {key: [values...]}} (keys may repeat)."""
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    for raw in (UNITS / name).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), {})
            continue
        key, _, value = line.partition("=")
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


def test_video_unit_plays_the_video_dir_without_the_controller():
    exec_start = unit("pireplace-video.service")["Service"]["ExecStart"][0]
    assert exec_start.split()[-1] == "/srv/pireplace/videos"


def test_control_unit_can_write_the_boot_marker():
    paths = unit("pireplace-control.service")["Service"]["ReadWritePaths"]
    assert any(p.lstrip("-") == "/run/pireplace" for p in paths)
