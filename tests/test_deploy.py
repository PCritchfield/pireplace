"""Text-level checks of the systemd units, polkit rule and playbook (no Pi needed)."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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
    assert "--volume=40" in exec_start.split()


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
    assert "register: pireplace_console" in SITE
    for name in ("pireplace_cmdline", "pireplace_console", "pireplace_configtxt"):
        assert f"{name} is changed" in last_task


REAL_CMDLINE = (
    "console=serial0,115200 console=tty1 root=PARTUUID=decadd98-02 rootfstype=ext4 "
    "fsck.repair=yes rootwait ds=nocloud;i=rpi-imager-1792976701367 "
    "cfg80211.ieee80211_regdom=US quiet loglevel=3 logo.nologo "
    "vt.global_cursor_default=0 consoleblank=0"
)


def _console_tasks():
    tasks = [
        t["ansible.builtin.replace"]
        for play in yaml.safe_load(SITE)
        for t in play.get("tasks", [])
        if "ansible.builtin.replace" in t
        and t["ansible.builtin.replace"]["path"] == "/boot/firmware/cmdline.txt"
    ]
    assert len(tasks) == 3
    return tasks


def _run_console_tasks(line):
    # cmdline.txt ends with a newline; ^ in MULTILINE would also match after it.
    changed = False
    line += "\n"
    for t in _console_tasks():
        new = re.sub(t["regexp"], t["replace"], line, flags=re.MULTILINE)
        changed |= new != line
        line = new
    assert line.endswith("\n") and line.count("\n") == 1
    return line[:-1], changed


def test_console_tty1_becomes_tty3_on_real_pi_cmdline():
    out, changed = _run_console_tasks(REAL_CMDLINE)
    assert changed
    assert out == REAL_CMDLINE.replace("console=tty1", "console=tty3")
    assert "\n" not in out


def test_console_task_is_idempotent_on_fixed_cmdline():
    fixed = REAL_CMDLINE.replace("console=tty1", "console=tty3")
    assert _run_console_tasks(fixed) == (fixed, False)
    once, _ = _run_console_tasks(REAL_CMDLINE)
    assert _run_console_tasks(once) == (once, False)


def test_console_task_never_leaves_no_tty_console():
    cases = [
        REAL_CMDLINE,
        "console=serial0,115200 root=/dev/x rootwait",
        "root=/dev/x rootwait",
        "console=tty10 root=/dev/x",
        "console=tty1",
    ]
    for line in cases:
        out, _ = _run_console_tasks(line)
        assert "console=tty3" in out.split()
        assert "console=tty1" not in out.split()
        assert "\n" not in out
    out, _ = _run_console_tasks("console=serial0,115200 root=/dev/x rootwait")
    assert out == "console=serial0,115200 console=tty3 root=/dev/x rootwait"
    out, _ = _run_console_tasks("root=/dev/x rootwait")
    assert out == "console=tty3 root=/dev/x rootwait"


def test_playbook_warns_about_last_console_and_disables_cloud_init():
    assert "never remove the last usable console" in SITE
    assert "path: /etc/cloud/cloud-init.disabled" in SITE


def _tasks():
    return [t for play in yaml.safe_load(SITE) for t in play.get("tasks", [])]


def test_playbook_installs_hdmi_sink_dropin_for_the_pireplace_user():
    play = yaml.safe_load(SITE)[1]
    assert play["vars"]["pireplace_hdmi_card"] == "vc4hdmi0"
    task = next(t for t in _tasks() if "50-pireplace-hdmi.conf" in str(t.get("ansible.builtin.copy", {})))
    copy = task["ansible.builtin.copy"]
    assert copy["dest"].endswith("/.config/pipewire/pipewire.conf.d/50-pireplace-hdmi.conf")
    assert copy["owner"] == "{{ pireplace_user }}"
    content = copy["content"]
    assert 'api.alsa.path    = "hdmi:CARD={{ pireplace_hdmi_card }},DEV=0"' in content
    assert "factory.name     = api.alsa.pcm.sink" in content
    assert "media.class      = Audio/Sink" in content
    assert "priority.session = 2000" in content and "priority.driver  = 2000" in content
    assert task["notify"] == "Restart pireplace audio and player units"


def test_audio_handler_restarts_pipewire_and_players_without_machined():
    handlers = {h["name"]: h for h in yaml.safe_load(SITE)[1]["handlers"]}
    cmd = handlers["Restart pireplace audio and player units"]["ansible.builtin.shell"]["cmd"]
    assert "--machine={{ pireplace_user }}@.host restart pipewire.service wireplumber.service" in cmd
    assert "{{ pireplace_user_units | join(' ') }}" in cmd
    assert "-M " not in cmd


def test_headphone_audio_off_is_registered_for_reboot_required():
    last_task = SITE.split("\n  handlers:", 1)[0].rsplit("- name:", 1)[1]
    assert "pireplace_audio_off is changed" in last_task


def _run_audio_off(path: Path) -> subprocess.CompletedProcess:
    task = next(t for t in _tasks() if t.get("register") == "pireplace_audio_off")
    task = {**task, "ansible.builtin.lineinfile": {**task["ansible.builtin.lineinfile"], "path": str(path)}}
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "tasks": [task]}]
    playbook = path.with_name("audio.yml")
    playbook.write_text(yaml.safe_dump(play))
    ansible = Path(sys.executable).with_name("ansible-playbook")
    if not ansible.exists():
        pytest.skip("ansible-playbook not installed")
    env = {**os.environ, "ANSIBLE_LOCALHOST_WARNING": "False", "ANSIBLE_HOME": str(path.parent / ".ansible")}
    return subprocess.run(
        [str(ansible), "-i", "localhost,", str(playbook)],
        capture_output=True, text=True, env=env, cwd=path.parent, check=True,
    )


def test_dtparam_audio_off_replaces_on_once_and_is_idempotent(tmp_path):
    config = tmp_path / "config.txt"
    config.write_text("# comment\ndtparam=i2c_arm=on\ndtparam=audio=on\ndtoverlay=vc4-kms-v3d\n[all]\n")
    first = _run_audio_off(config)
    assert "changed=1" in first.stdout
    lines = config.read_text().splitlines()
    assert lines.count("dtparam=audio=off") == 1
    assert not any(line.startswith("dtparam=audio=on") for line in lines)
    assert lines[2] == "dtparam=audio=off" and "dtparam=i2c_arm=on" in lines
    before = config.read_text()
    second = _run_audio_off(config)
    assert "changed=0" in second.stdout and config.read_text() == before


def test_dtparam_audio_off_is_appended_when_absent(tmp_path):
    config = tmp_path / "config.txt"
    config.write_text("dtoverlay=vc4-kms-v3d\n")
    _run_audio_off(config)
    assert config.read_text().splitlines().count("dtparam=audio=off") == 1


# --- Jellyfin sync and Tailscale -----------------------------------------------------


def _task(name_part: str) -> dict:
    return next(t for t in _tasks() if name_part in t["name"])


def test_sync_service_is_a_hardened_oneshot_that_writes_only_the_music_dir():
    service = unit("pireplace-sync.service")["Service"]
    assert service["Type"] == ["oneshot"]
    assert service["User"] == ["pireplace"] and service["Group"] == ["pireplace"]
    assert service["UMask"] == ["0002"]
    assert service["ReadWritePaths"] == ["/srv/pireplace/music"]
    assert service["ExecStart"] == [
        "/usr/bin/python3 -m pireplace sync --config /etc/pireplace/config.toml"
    ]
    assert service["ProtectSystem"] == ["strict"] and service["ProtectHome"] == ["yes"]
    assert service["RestrictAddressFamilies"] == ["AF_INET AF_INET6 AF_UNIX"]
    assert service["Nice"] == ["10"] and service["IOSchedulingClass"] == ["idle"]
    assert "Restart" not in service and "RestartPreventExitStatus" not in service
    for key, value in HARDENING.items():
        if key != "UMask":
            assert service[key] == [value], key


def test_sync_timer_runs_nightly_and_catches_up():
    parsed = unit("pireplace-sync.timer")
    assert parsed["Timer"]["OnCalendar"] == ["*-*-* 04:30:00"]
    assert parsed["Timer"]["Persistent"] == ["true"]
    assert parsed["Timer"]["RandomizedDelaySec"] == ["5min"]
    assert parsed["Install"]["WantedBy"] == ["timers.target"]


def test_jellyfin_key_file_is_secret_and_only_present_with_a_key():
    present = next(t for t in _tasks() if "ansible.builtin.copy" in t
                   and t["ansible.builtin.copy"].get("dest") == "/etc/pireplace/jellyfin.key")
    copy = present["ansible.builtin.copy"]
    assert present["no_log"] is True
    assert copy["mode"] == "0640" and copy["owner"] == "root" and copy["group"] == "{{ pireplace_user }}"
    assert present["when"] == "pireplace_jf_enabled"
    absent = next(t for t in _tasks() if "ansible.builtin.file" in t
                  and t["ansible.builtin.file"].get("path") == "/etc/pireplace/jellyfin.key")
    assert absent["ansible.builtin.file"]["state"] == "absent"
    assert absent["when"] == "not pireplace_jf_enabled"


def test_sync_units_and_timer_depend_on_the_api_key():
    play = yaml.safe_load(SITE)[1]
    assert "pireplace_jellyfin_api_key" in play["vars"]["pireplace_jf_enabled"]
    install = _task("Install the Jellyfin sync units")
    assert install["when"] == "pireplace_jf_enabled"
    enable = _task("Enable and start the Jellyfin sync timer")
    assert enable["when"] == "pireplace_jf_enabled"
    assert enable["ansible.builtin.systemd_service"]["name"] == "pireplace-sync.timer"
    removal = [t for t in _tasks() if "Jellyfin sync" in t["name"] and t.get("when") != "pireplace_jf_enabled"]
    assert removal, "a key-less deploy must disable and remove the timer"
    assert all("not pireplace_jf_enabled" in str(t["when"]) for t in removal)
    # the sync units are not part of the always-restarted controller set
    assert "pireplace-sync" not in " ".join(play["vars"]["pireplace_system_units"])


def test_mood_folders_follow_the_playlists_when_syncing():
    loop = _task("Create media directories")["loop"]
    assert "pireplace_jf_enabled" in loop and "pireplace_jf_playlists" in loop and "pireplace_moods" in loop


def test_tailscale_matches_the_hand_installed_state():
    keyring = "/usr/share/keyrings/tailscale-archive-keyring.gpg"
    fetch = _task("Fetch the Tailscale signing key")
    assert fetch["ansible.builtin.get_url"]["dest"] == keyring
    assert fetch["ansible.builtin.get_url"]["url"] == (
        "https://pkgs.tailscale.com/stable/debian/{{ ansible_distribution_release }}.noarmor.gpg")
    assert "not" in fetch["when"] and "exists" in fetch["when"]
    source = _task("Add the Tailscale apt source")["ansible.builtin.lineinfile"]
    assert source["path"] == "/etc/apt/sources.list.d/tailscale.list"
    assert source["line"] == (f"deb [signed-by={keyring}] https://pkgs.tailscale.com/stable/debian "
                              "{{ ansible_distribution_release }} main")
    assert _task("Install Tailscale")["ansible.builtin.apt"]["name"] == [
        "tailscale", "tailscale-archive-keyring"]
    assert _task("Enable and start tailscaled")["ansible.builtin.systemd_service"]["name"] == "tailscaled.service"
    assert SITE.count("sources.list.d/tailscale") == 1  # never a second source file


def test_tailscale_up_is_secret_and_only_runs_when_not_running():
    state = _task("Read the Tailscale login state")
    assert "BackendState" in str(state["ansible.builtin.set_fact"])
    up = _task("Join the tailnet")
    assert up["no_log"] is True
    argv = up["ansible.builtin.command"]["argv"]
    assert argv[:4] == ["tailscale", "up", "--hostname=pireplace", "--accept-dns=false"]
    assert argv[4].startswith("--authkey=")
    guard = " ".join(up["when"])
    assert "pireplace_ts_backend_state != 'Running'" in guard and "pireplace_tailscale_authkey" in guard
    note = _task("Tell the owner to log Tailscale in")
    assert "sudo tailscale up --hostname=pireplace --accept-dns=false" in note["ansible.builtin.debug"]["msg"]
    assert "pireplace_tailscale_authkey" in " ".join(note["when"])


def _render_config(extra: dict, directory: Path) -> Path:
    pytest.importorskip("ansible")
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar, trust_as_template

    # Values read with yaml.safe_load are untrusted; mark the play vars as templates.
    play_vars = {
        k: trust_as_template(v) if isinstance(v, str) else v
        for k, v in yaml.safe_load(SITE)[1]["vars"].items()
    }
    play_vars.update(hotspot_password="a-test-password", **extra)
    templar = Templar(loader=DataLoader(), variables=play_vars)
    text = (ROOT / "deploy" / "ansible" / "templates" / "config.toml.j2").read_text()
    out = directory / "etc" / "config.toml"
    out.parent.mkdir(exist_ok=True)
    out.write_text(templar.template(trust_as_template(text)))
    return out


def test_template_renders_a_loadable_jellyfin_section_only_with_a_key(tmp_path):
    import tomllib

    from pireplace.config import load_jellyfin_config

    with_key = _render_config({"pireplace_jellyfin_api_key": "s3cret-key"}, tmp_path)
    data = tomllib.loads(with_key.read_text())
    assert "s3cret-key" not in with_key.read_text()
    assert set(data["jellyfin"]) == {"url", "user_id", "api_key_file", "playlists"}
    cfg = load_jellyfin_config(with_key)
    assert cfg.url == "http://100.88.8.6:8096"
    assert cfg.user_id == "1b7ed00cbdcc4a1eb624738191ca4efd"
    assert str(cfg.api_key_file) == "/etc/pireplace/jellyfin.key"
    assert cfg.playlists == {
        "modern": "385d9e4b0602d3d75a00bc6d31dc5aa6",
        "classic-swing": "dfbf231b1aaa92df17ff5248fbde6fbf",
        "soul-motown": "352617a64fc727e324fd1bfa625cb0f6",
        "instrumental": "a371cfe3e0f01be7542358ad2f50f7b5",
    }

    for n, extra in enumerate(({}, {"pireplace_jellyfin_api_key": ""})):
        directory = tmp_path / f"without{n}"
        directory.mkdir()
        plain = _render_config(extra, directory)
        assert "jellyfin" not in tomllib.loads(plain.read_text())
        assert load_jellyfin_config(plain) is None
