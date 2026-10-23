"""Text-level checks of the systemd units, polkit rule and playbook (no Pi needed)."""

import re
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


HARDENING = {
    "ProtectProc": "invisible",
    "SystemCallArchitectures": "native",
    "RestrictNamespaces": "yes",
    "RestrictRealtime": "yes",
    "ProtectClock": "yes",
    "ProtectHostname": "yes",
    "ProtectKernelLogs": "yes",
    "UMask": "0077",
}


@pytest.mark.parametrize("name", SYSTEM_UNITS)
def test_system_units_are_hardened(name):
    service = unit(name)["Service"]
    assert {k: service.get(k, [None])[-1] for k in HARDENING} == HARDENING


@pytest.mark.parametrize("name", SYSTEM_UNITS + USER_UNITS)
def test_units_never_give_up_restarting(name):
    assert unit(name)["Unit"]["StartLimitIntervalSec"] == ["0"]
    assert unit(name)["Service"]["Restart"] == ["always"]


@pytest.mark.parametrize("name", USER_UNITS)
def test_player_units_only_run_for_the_pireplace_user(name):
    assert unit(name)["Unit"]["ConditionUser"] == ["pireplace"]


def test_polkit_rule_allows_only_the_needed_networkmanager_actions():
    rules = (ROOT / "deploy" / "polkit" / "50-pireplace.rules").read_text()
    actions = set(re.findall(r'"(org\.freedesktop\.NetworkManager\.[a-z.-]+)"', rules))
    assert actions == {
        "org.freedesktop.NetworkManager.network-control",
        "org.freedesktop.NetworkManager.settings.modify.system",
        "org.freedesktop.NetworkManager.wifi.scan",
        "org.freedesktop.NetworkManager.wifi.share.protected",
        "org.freedesktop.NetworkManager.wifi.share.open",
    }
    assert "action.id.indexOf(" not in rules and "startsWith" not in rules  # no prefix match
    assert 'subject.user === "pireplace"' in rules


SITE = (ROOT / "deploy" / "ansible" / "site.yml").read_text()


def test_playbook_installs_polkit_and_user_dbus():
    packages = SITE.split("- name: Install packages", 1)[1].split("- name:", 1)[0]
    assert "- polkitd" in packages and "- dbus-user-session" in packages


def test_unattended_upgrades_include_raspberry_pi_origin():
    assert ('"origin=Raspberry Pi Foundation,codename=${distro_codename},'
            'label=Raspberry Pi Foundation";') in SITE


def test_playbook_enables_nothing_for_every_user():
    assert not re.search(r"enabled: true\n\s+scope: global", SITE)


def test_playbook_says_reboot_required_when_boot_files_change():
    assert "register: pireplace_cmdline" in SITE and "register: pireplace_configtxt" in SITE
    last_task = SITE.split("\n  handlers:", 1)[0].rsplit("- name:", 1)[1]
    assert "REBOOT REQUIRED" in last_task
    assert "pireplace_cmdline is changed or pireplace_configtxt is changed" in last_task
