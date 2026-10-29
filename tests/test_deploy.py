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
# Root oneshots: the hardening applies, but they never restart (no Restart=always).
NETBACKUP_SERVICES = ["pireplace-netrestore.service", "pireplace-netbackup.service"]
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


@pytest.mark.parametrize("name", NETBACKUP_SERVICES)
def test_netbackup_oneshots_are_hardened_and_offline(name):
    service = unit(name)["Service"]
    assert {k: service.get(k, [None])[-1] for k in HARDENING} == HARDENING
    assert service["Type"] == ["oneshot"]
    assert service["ProtectSystem"] == ["strict"]
    assert service["PrivateNetwork"] == ["yes"]
    assert service["CapabilityBoundingSet"] == [""]
    assert service["TimeoutStartSec"] == ["30s" if "restore" in name else "60s"]
    writable = set(service["ReadWritePaths"][0].split())
    if "restore" in name:  # rewrites NM's directory; '-' so a missing dir cannot fail the sandbox
        assert writable == {"-/etc/NetworkManager/system-connections", "/var/lib/pireplace-netbackup"}
    else:  # only reads NM's directory
        assert writable == {"/var/lib/pireplace-netbackup"}
        assert service["ReadOnlyPaths"] == ["-/etc/NetworkManager/system-connections"]
    assert service["StateDirectoryMode"] == ["0700"]
    assert "Restart" not in service  # a oneshot must not loop


def test_netrestore_runs_before_networkmanager_which_never_requires_it():
    parsed = unit("pireplace-netrestore.service")
    assert parsed["Unit"]["Before"] == ["NetworkManager.service"]
    assert parsed["Unit"]["DefaultDependencies"] == ["no"]
    assert parsed["Unit"]["After"] == ["local-fs.target"]
    assert parsed["Install"]["WantedBy"] == ["NetworkManager.service"]
    for key in ("Requires", "BindsTo", "Requisite"):
        assert key not in parsed["Unit"]
    for name in NETBACKUP_SERVICES:  # nothing in the units makes NM depend on a backup unit
        assert "RequiredBy" not in unit(name).get("Install", {})


def test_netbackup_runs_after_networkmanager_on_changes_and_once_per_boot():
    service = unit("pireplace-netbackup.service")
    assert service["Unit"]["After"] == ["NetworkManager.service pireplace-netrestore.service"]
    assert service["Service"]["ExecStartPre"] == ["/bin/sleep 2"]
    assert service["Install"]["WantedBy"] == ["multi-user.target"]
    path = unit("pireplace-netbackup.path")
    assert path["Path"]["PathChanged"] == ["/etc/NetworkManager/system-connections"]
    assert path["Path"]["Unit"] == ["pireplace-netbackup.service"]
    assert path["Path"]["TriggerLimitIntervalSec"] == ["0"]
    assert path["Install"]["WantedBy"] == ["multi-user.target"]


def test_cloud_init_dropin_disables_network_config():
    text = (ROOT / "deploy" / "cloud-init" / "99-pireplace-no-network.cfg").read_text()
    assert "network: {config: disabled}" in text.splitlines()


def test_journald_dropin_is_persistent_capped_and_sorts_after_the_pi_default():
    path = ROOT / "deploy" / "journald" / "90-pireplace-persistent.conf"
    assert path.name > "40-rpi-volatile-storage.conf"
    settings = dict(
        line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#")
    )
    assert settings["Storage"] == "persistent"
    assert settings["SystemMaxUse"] == "64M"
    assert settings["SystemMaxFileSize"] == "8M"
    assert settings["SystemKeepFree"] == "1G"
    assert settings["MaxRetentionSec"] == "1month"


def test_playbook_installs_the_safety_net_files():
    for name in ("pireplace-netrestore.service", "pireplace-netbackup.service", "pireplace-netbackup.path"):
        assert (UNITS / name).is_file() and name in SITE
    for src, dest in (
        ("cloud-init/99-pireplace-no-network.cfg", "/etc/cloud/cloud.cfg.d/99-pireplace-no-network.cfg"),
        ("journald/90-pireplace-persistent.conf", "/etc/systemd/journald.conf.d/90-pireplace-persistent.conf"),
    ):
        assert (ROOT / "deploy" / src).is_file() and src in SITE and dest in SITE
    assert "nmcli" in SITE and "migrate" in SITE and "/etc/netplan/" in SITE
    assert "/run/NetworkManager/system-connections/netplan-" in SITE
    play = yaml.safe_load(SITE)[1]
    assert "pireplace-netbackup" not in " ".join(play["vars"]["pireplace_system_units"])


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


def _flatten(tasks):
    for t in tasks:
        yield t
        for part in ("block", "rescue", "always"):
            yield from _flatten(t.get(part, []))


def _tasks():
    """Every task of every play, including those nested in blocks."""
    return [t for play in yaml.safe_load(SITE) for t in _flatten(play.get("tasks", []))]


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
    assert "Restart" not in service or service["Restart"] == ["on-failure"]
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
    # root-only: the sync unit receives it through LoadCredential, nothing else can read it
    assert copy["mode"] == "0600" and copy["owner"] == "root" and copy["group"] == "root"
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


def _ts_block():
    play = yaml.safe_load(SITE)[1]
    return next(t for t in play["tasks"] if "block" in t and "Tailscale" in t["name"])


def test_all_tailscale_tasks_are_one_block_gated_on_the_sync():
    block = _ts_block()
    assert block["when"] == "pireplace_jf_enabled"
    inside = {t["name"] for t in _flatten(block["block"])}
    for part in ("signing key", "apt source", "Install Tailscale", "tailscaled", "login state",
                 "Join the tailnet", "Tell the owner", "Pin the Tailscale repository"):
        assert any(part in name for name in inside), part
    acting = ("command", "apt", "get_url", "lineinfile", "systemd_service", "stat", "copy", "file")
    outside = [
        t["name"] for t in _tasks()
        if t["name"] not in inside and t is not block
        and any("tailscale" in str(t.get(f"ansible.builtin.{m}", "")).lower() for m in acting)
    ]
    # only the unattended-upgrades origin list (harmless without the repository)
    assert outside == ["Limit unattended-upgrades to security updates (Debian, Raspberry Pi, Tailscale)"]


def test_tailscale_join_reads_the_key_from_a_root_only_file():
    join = _task("Join the tailnet")
    guard = " ".join(join["when"])
    assert "pireplace_ts_backend_state == 'NeedsLogin'" in guard and "pireplace_ts_have_key" in guard
    write, up = join["block"]
    assert write["no_log"] is True and up["no_log"] is True
    copy = write["ansible.builtin.copy"]
    assert copy["dest"] == "{{ pireplace_ts_authkey_file }}"
    assert copy["mode"] == "0600" and copy["owner"] == "root" and copy["group"] == "root"
    assert yaml.safe_load(SITE)[1]["vars"]["pireplace_ts_authkey_file"].startswith("/run/")
    argv = up["ansible.builtin.command"]["argv"]
    assert argv == [
        "tailscale", "up", "--hostname=pireplace", "--accept-dns=false", "--shields-up",
        "--advertise-tags=tag:pireplace", "--timeout=60s",
        "--auth-key=file:{{ pireplace_ts_authkey_file }}",
    ]
    assert "pireplace_tailscale_authkey" not in str(argv)  # never on the command line
    fail = join["rescue"][0]["ansible.builtin.fail"]["msg"]
    assert "auth key rejected" in fail and "{{ pireplace_ts_up_hint }}" in fail
    hint = " ".join(yaml.safe_load(SITE)[1]["vars"]["pireplace_ts_up_hint"].split())
    assert hint == ("sudo tailscale up --hostname=pireplace --accept-dns=false --shields-up "
                    "--advertise-tags=tag:pireplace")
    cleanup = join["always"][0]["ansible.builtin.file"]
    assert cleanup == {"path": "{{ pireplace_ts_authkey_file }}", "state": "absent"}


def test_tailscale_note_matches_the_actual_state():
    play = yaml.safe_load(SITE)[1]
    notes = play["vars"]["pireplace_ts_notes"]
    assert set(notes) == {"NeedsLogin", "NeedsMachineAuth", "Stopped", "other"}
    assert "{{ pireplace_ts_up_hint }}" in notes["NeedsLogin"]
    assert "approve" in notes["NeedsMachineAuth"].lower()
    assert "sudo tailscale up" in notes["Stopped"]
    note = _task("Tell the owner what Tailscale needs")
    assert "pireplace_ts_notes" in note["ansible.builtin.debug"]["msg"]
    assert "pireplace_ts_backend_state != 'Running'" in " ".join(note["when"])


def test_tailscale_updates_come_only_for_tailscale_packages():
    assert '"origin=Tailscale,codename=${distro_codename},label=Tailscale";' in SITE
    pin_task = _task("Pin the Tailscale repository")
    pin = pin_task["ansible.builtin.copy"]
    assert pin["dest"] == "/etc/apt/preferences.d/tailscale"
    stanzas = [s.strip() for s in pin["content"].split("\n\n") if "Package:" in s]
    assert [s for s in stanzas[0].splitlines() if not s.startswith("#")] == [
        "Package: tailscale*", "Pin: origin pkgs.tailscale.com", "Pin-Priority: 500"]
    assert stanzas[1].splitlines() == ["Package: *", "Pin: origin pkgs.tailscale.com", "Pin-Priority: -1"]
    names = [t["name"] for t in _flatten(_ts_block()["block"])]
    assert names.index(pin_task["name"]) < names.index("Install Tailscale")


def test_playbook_refuses_placeholder_secrets():
    play = yaml.safe_load(SITE)[0]
    check = next(t for t in play["tasks"] if "placeholder" in t["name"])
    that = " ".join(check["ansible.builtin.assert"]["that"])
    assert "pireplace_jellyfin_api_key" in that and "pireplace_tailscale_authkey" in that
    assert "CHANGE_ME" in that


def test_inventory_example_has_no_inline_comments():
    # Ansible's INI inventory keeps a " # ..." after a value as part of the value.
    text = (ROOT / "deploy" / "ansible" / "inventory.example.ini").read_text()
    for line in text.splitlines():
        if line.startswith("#"):
            body = line.lstrip("#").strip()
            if body.startswith("pireplace_") and "=" in body:
                assert "#" not in body, line
        elif "=" in line:
            assert "#" not in line, line


README = (ROOT / "README.md").read_text()


def test_readme_has_a_complete_minimal_tailnet_policy():
    import json

    block = README.split("```json\n", 1)[1].split("```", 1)[0]
    policy = json.loads(block)
    assert set(policy["tagOwners"]) == {"tag:pireplace", "tag:jellyfin"}
    grants = policy["grants"]
    assert {"src": ["tag:pireplace"], "dst": ["tag:jellyfin"], "ip": ["tcp:8096"]} in grants
    to_pi = [g for g in grants if any(d in ("tag:pireplace", "*") for d in g["dst"])]
    assert to_pi == [{"src": ["autogroup:member"], "dst": ["tag:pireplace"], "ip": ["tcp:22"]}]
    assert "default allow-all" in README
    assert "nc -zvw3 100.88.8.6 22" in README
    assert "http://100.88.8.6:8096/System/Info/Public" in README
    assert "curl http://100.83.117.48/" in README
    assert "Edit ACL tags" in README


def test_readme_uses_a_non_admin_jellyfin_user():
    assert "scripts/jellyfin_token.sh" in README
    assert "API Keys" not in README  # no admin-wide dashboard key any more
    assert "pireplace_jellyfin_user_id" in README
    assert "music-streaming" in README
    assert '"Users":[{"UserId":"<sync user id>","CanEdit":false}]' in README
    assert "Known limitations" in README


FAKE_TAILSCALE = """#!/bin/sh
# Records argv and the auth key file's contents; exit status from $FAKE_TS_RC.
printf '%s\\n' "$@" > "$FAKE_TS_LOG"
for arg in "$@"; do
    case "$arg" in --auth-key=file:*) cat "${arg#--auth-key=file:}" > "$FAKE_TS_LOG.key" ;; esac
done
exit "${FAKE_TS_RC:-0}"
"""


def _run_join(tmp_path: Path, rc: int) -> subprocess.CompletedProcess:
    """Run the real join block locally, with a fake `tailscale` and no root-only bits."""
    ansible = Path(sys.executable).with_name("ansible-playbook")
    if not ansible.exists():
        pytest.skip("ansible-playbook not installed")
    play = yaml.safe_load(SITE)[1]
    join = _task("Join the tailnet")
    fake = tmp_path / "tailscale"
    fake.write_text(FAKE_TAILSCALE)
    fake.chmod(0o755)
    write, up = (dict(t) for t in join["block"])
    write["ansible.builtin.copy"] = {
        k: v for k, v in write["ansible.builtin.copy"].items() if k not in ("owner", "group")}
    up["ansible.builtin.command"] = {"argv": [str(fake)] + up["ansible.builtin.command"]["argv"][1:]}
    block = {**join, "block": [write, up]}
    keep = ("pireplace_ts_up_hint", "pireplace_ts_have_key")
    variables = {k: play["vars"][k] for k in keep}
    variables.update(
        pireplace_ts_authkey_file=str(tmp_path / "authkey"),
        pireplace_ts_backend_state="NeedsLogin",
        pireplace_tailscale_authkey="tskey-auth-SECRET-123",
    )
    playbook = tmp_path / "join.yml"
    playbook.write_text(yaml.safe_dump([{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": variables, "tasks": [block],
    }]))
    env = {**os.environ, "ANSIBLE_LOCALHOST_WARNING": "False", "ANSIBLE_HOME": str(tmp_path / ".ansible"),
           "FAKE_TS_LOG": str(tmp_path / "ts.log"), "FAKE_TS_RC": str(rc)}
    return subprocess.run([str(ansible), "-i", "localhost,", str(playbook)],
                          capture_output=True, text=True, env=env, cwd=tmp_path)


def test_tailscale_join_passes_the_key_by_file_and_removes_it(tmp_path):
    done = _run_join(tmp_path, rc=0)
    assert done.returncode == 0, done.stdout
    assert (tmp_path / "ts.log.key").read_text() == "tskey-auth-SECRET-123"
    argv = (tmp_path / "ts.log").read_text()
    assert "SECRET" not in argv and f"--auth-key=file:{tmp_path / 'authkey'}" in argv
    assert not (tmp_path / "authkey").exists()
    assert "SECRET" not in done.stdout + done.stderr


def test_tailscale_join_failure_explains_and_still_removes_the_key(tmp_path):
    failed = _run_join(tmp_path, rc=1)
    assert failed.returncode != 0
    assert "auth key rejected" in failed.stdout
    assert "sudo tailscale up --hostname=pireplace" in failed.stdout
    assert not (tmp_path / "authkey").exists()
    assert "SECRET" not in failed.stdout + failed.stderr


def _templar(extra: dict):
    pytest.importorskip("ansible")
    try:
        from ansible.parsing.dataloader import DataLoader
        from ansible.template import Templar, trust_as_template
    except ImportError:  # trust_as_template is ansible-core 2.19+
        pytest.skip("ansible-core too old for trust_as_template")

    # Values read with yaml.safe_load are untrusted; mark the play vars as templates.
    play_vars = {
        k: trust_as_template(v) if isinstance(v, str) else v
        for k, v in yaml.safe_load(SITE)[1]["vars"].items()
    }
    play_vars.update(hotspot_password="a-test-password", **extra)
    return Templar(loader=DataLoader(), variables=play_vars), trust_as_template


def _render_config(extra: dict, directory: Path) -> Path:
    templar, trust_as_template = _templar(extra)
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
    assert cfg.user_id == "4640835a50a94bc69709d00d6f332fac"
    assert str(cfg.api_key_file) == "/etc/pireplace/jellyfin.key"
    assert cfg.playlists == {
        "modern": "dc8d8d54ecad68b2546b5c7c9b4b4766",
        "classic-swing": "f4b96696936ec335bfeca0c623ad0669",
        "soul-motown": "79525d419fccb3bec7c38cef5fe07b11",
        "instrumental": "30bf4d0f66600b1d1dd8cded00e25705",
    }

    for n, extra in enumerate(({}, {"pireplace_jellyfin_api_key": ""})):
        directory = tmp_path / f"without{n}"
        directory.mkdir()
        plain = _render_config(extra, directory)
        assert "jellyfin" not in tomllib.loads(plain.read_text())
        assert load_jellyfin_config(plain) is None


def test_sync_service_reads_the_key_as_a_systemd_credential():
    service = unit("pireplace-sync.service")["Service"]
    assert service["LoadCredential"] == ["jellyfin.key:/etc/pireplace/jellyfin.key"]


def test_sync_service_network_is_confined_to_loopback_and_the_tailnet():
    service = unit("pireplace-sync.service")["Service"]
    assert service["IPAddressDeny"] == ["any"]
    assert service["IPAddressAllow"] == ["localhost"]  # the rescan call; Jellyfin via the drop-in
    assert service["RestrictNetworkInterfaces"] == ["lo tailscale0"]
    assert service["CapabilityBoundingSet"] == [""]
    assert service["PrivateIPC"] == ["yes"]
    assert service["SystemCallFilter"] == ["@system-service"]


def test_sync_dropin_allows_only_the_jellyfin_host():
    play = yaml.safe_load(SITE)[1]
    assert play["vars"]["pireplace_jf_host"] == "{{ pireplace_jf_url | urlsplit('hostname') }}"
    dropin = _task("Allow the sync service to reach the Jellyfin server")
    copy = dropin["ansible.builtin.copy"]
    assert copy["dest"] == "/etc/systemd/system/pireplace-sync.service.d/50-jellyfin-address.conf"
    assert "[Service]\nIPAddressAllow={{ pireplace_jf_host }}/32\n" in copy["content"]
    assert dropin["when"] == "pireplace_jf_enabled"
    assert dropin["notify"] == "Reload systemd"
    removal = _task("Remove the sync unit drop-ins")
    assert removal["ansible.builtin.file"] == {
        "path": "/etc/systemd/system/pireplace-sync.service.d", "state": "absent"}
    assert removal["when"] == "not pireplace_jf_enabled"
    check = _task("Require an IPv4 address in the Jellyfin URL")
    assert check["when"] == "pireplace_jf_enabled"
    pattern = re.search(r"match\('(.+)'\)", check["ansible.builtin.assert"]["that"][0]).group(1)
    assert re.match(pattern, "100.88.8.6") and not re.match(pattern, "omarchy.tail1234.ts.net")


def test_control_page_refuses_the_tailnet():
    service = unit("pireplace-control.service")["Service"]
    assert service["IPAddressDeny"] == ["100.64.0.0/10 fd7a:115c:a1e0::/48"]
    assert "IPAddressAllow" not in service  # hotspot (10.42.0.0/24) and home LAN stay reachable


def test_sync_service_retries_a_failed_night_but_not_a_config_error():
    parsed = unit("pireplace-sync.service")
    service, unit_section = parsed["Service"], parsed["Unit"]
    assert service["Restart"] == ["on-failure"]
    assert service["RestartSec"] == ["10min"]
    assert service["RestartPreventExitStatus"] == ["2"]
    assert service["TimeoutStartSec"] == ["6h"]
    assert unit_section["StartLimitIntervalSec"] == ["6h"]
    assert unit_section["StartLimitBurst"] == ["4"]
    assert unit_section["Wants"] == ["network-online.target"]
    after = unit_section["After"][0].split()
    assert "network-online.target" in after and "tailscaled.service" in after


def test_enabled_check_is_one_expression_used_by_the_template():
    play = yaml.safe_load(SITE)[1]
    assert play["vars"]["pireplace_jf_enabled"] == (
        "{{ (pireplace_jellyfin_api_key | default('', true) | string | trim | length) > 0 }}")
    template = (ROOT / "deploy" / "ansible" / "templates" / "config.toml.j2").read_text()
    assert "{% if pireplace_jf_enabled | bool %}" in template
    assert "pireplace_jellyfin_api_key" not in template


def test_whitespace_or_null_token_means_disabled(tmp_path):
    import tomllib

    for n, token in enumerate(("   ", None)):
        directory = tmp_path / f"t{n}"
        directory.mkdir()
        plain = _render_config({"pireplace_jellyfin_api_key": token}, directory)
        assert "jellyfin" not in tomllib.loads(plain.read_text())


def test_inventory_overrides_reach_the_template(tmp_path):
    from pireplace.config import load_jellyfin_config

    rendered = _render_config({
        "pireplace_jellyfin_api_key": "tok",
        "pireplace_jellyfin_url": "http://100.64.1.2:8096",
        "pireplace_jellyfin_user_id": "c" * 32,
        "pireplace_jellyfin_playlists": {"christmas": "d" * 32},
    }, tmp_path)
    cfg = load_jellyfin_config(rendered)
    assert cfg.url == "http://100.64.1.2:8096"
    assert cfg.user_id == "c" * 32
    assert cfg.playlists == {"christmas": "d" * 32}


def test_dropin_host_comes_from_the_url():
    templar, trust = _templar({"pireplace_jellyfin_url": "http://100.64.1.2:8096"})
    assert templar.template(trust("{{ pireplace_jf_host }}")) == "100.64.1.2"
    templar, trust = _templar({})
    assert templar.template(trust("{{ pireplace_jf_host }}")) == "100.88.8.6"


def _run_copy_tasks(tmp_path: Path, names: list[str], extra_vars: dict) -> list[str]:
    """Run the named copy tasks locally (dest moved into tmp_path) twice; return stdouts."""
    ansible = Path(sys.executable).with_name("ansible-playbook")
    if not ansible.exists():
        pytest.skip("ansible-playbook not installed")
    play = yaml.safe_load(SITE)[1]
    tasks = []
    for name in names:
        task = dict(_task(name))
        copy = {k: v for k, v in task["ansible.builtin.copy"].items() if k not in ("owner", "group")}
        copy["dest"] = str(tmp_path / Path(copy["dest"]).name)
        task["ansible.builtin.copy"] = copy
        task.pop("notify", None)
        task.pop("when", None)
        tasks.append(task)
    variables = {**play["vars"], **extra_vars}
    playbook = tmp_path / "copy.yml"
    playbook.write_text(yaml.safe_dump([{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": variables, "tasks": tasks,
    }]))
    env = {**os.environ, "ANSIBLE_LOCALHOST_WARNING": "False", "ANSIBLE_HOME": str(tmp_path / ".ansible")}
    return [
        subprocess.run([str(ansible), "-i", "localhost,", str(playbook)],
                       capture_output=True, text=True, env=env, cwd=tmp_path, check=True).stdout
        for _ in range(2)
    ]


def test_dropin_and_pin_render_with_ansible_and_are_idempotent(tmp_path):
    first, second = _run_copy_tasks(
        tmp_path,
        ["Allow the sync service to reach the Jellyfin server", "Pin the Tailscale repository"],
        {"pireplace_jellyfin_api_key": "tok", "pireplace_jellyfin_url": "http://100.64.1.2:8096"},
    )
    assert "changed=2" in first and "changed=0" in second
    dropin = (tmp_path / "50-jellyfin-address.conf").read_text()
    assert "[Service]\nIPAddressAllow=100.64.1.2/32\n" in dropin
    assert "Pin-Priority: -1" in (tmp_path / "tailscale").read_text()


def _run_validation_play(tmp_path: Path, extra: dict) -> subprocess.CompletedProcess:
    ansible = Path(sys.executable).with_name("ansible-playbook")
    if not ansible.exists():
        pytest.skip("ansible-playbook not installed")
    play = {**yaml.safe_load(SITE)[0], "hosts": "localhost", "connection": "local"}
    playbook = tmp_path / "validate.yml"
    playbook.write_text(yaml.safe_dump([play]))
    extra_vars = tmp_path / "extra.yml"
    extra_vars.write_text(yaml.safe_dump({"hotspot_password": "a-real-password", **extra}))
    env = {**os.environ, "ANSIBLE_LOCALHOST_WARNING": "False", "ANSIBLE_HOME": str(tmp_path / ".ansible")}
    return subprocess.run([str(ansible), "-i", "localhost,", "-e", f"@{extra_vars}", str(playbook)],
                          capture_output=True, text=True, env=env, cwd=tmp_path)


@pytest.mark.parametrize("extra,ok", [
    ({}, True),
    ({"pireplace_jellyfin_api_key": "real-token", "pireplace_tailscale_authkey": None}, True),
    ({"pireplace_jellyfin_api_key": "CHANGE_ME_jellyfin_access_token"}, False),
    ({"pireplace_tailscale_authkey": " CHANGE_ME_tskey-auth-..."}, False),
    ({"pireplace_jellyfin_user_id": "CHANGE_ME_pireplace_user_id"}, False),
])
def test_validation_play_refuses_placeholders(tmp_path, extra, ok):
    result = _run_validation_play(tmp_path, extra)
    assert (result.returncode == 0) is ok, result.stdout
    if not ok:
        assert "CHANGE_ME placeholder" in result.stdout


# ---- Wi-Fi power saving --------------------------------------------------

POWERSAVE_CONF = ROOT / "deploy" / "networkmanager" / "pireplace-wifi-powersave.conf"


def test_powersave_conf_turns_wifi_power_saving_off():
    lines = [l for l in POWERSAVE_CONF.read_text().splitlines() if l and not l.startswith("#")]
    assert lines == ["[connection]", "wifi.powersave = 2"]


def test_playbook_installs_iw():
    packages = SITE.split("- name: Install packages", 1)[1].split("- name:", 1)[0]
    assert "- iw" in packages


def test_playbook_installs_powersave_conf_without_restarting_networkmanager():
    task = next(t for t in _tasks() if "pireplace-wifi-powersave.conf" in str(t.get("ansible.builtin.copy", {})))
    copy = task["ansible.builtin.copy"]
    assert copy["src"].endswith("/../networkmanager/pireplace-wifi-powersave.conf")
    assert copy["dest"] == "/etc/NetworkManager/conf.d/pireplace-wifi-powersave.conf"
    assert (copy["owner"], copy["group"], copy["mode"]) == ("root", "root", "0644")
    assert task["notify"] == "Apply Wi-Fi power saving"
    # A restart would drop the SSH session the playbook runs over.
    handlers = {h["name"]: h for h in yaml.safe_load(SITE)[1]["handlers"]}
    assert "ansible.builtin.systemd_service" not in str(handlers["Apply Wi-Fi power saving"])
    assert "restart" not in str(handlers["Apply Wi-Fi power saving"]).lower()


def test_powersave_handler_reloads_conf_and_tolerates_a_missing_wlan0():
    handlers = {h["name"]: h for h in yaml.safe_load(SITE)[1]["handlers"]}
    steps = handlers["Apply Wi-Fi power saving"]["ansible.builtin.shell"]["cmd"].splitlines()
    cmds = [l.strip() for l in steps if l.strip()]
    assert cmds.pop(0) == "set -e"  # a failed reload must fail the play; only the iw step is optional
    assert cmds[0] == "nmcli general reload conf"
    assert cmds[1].startswith("iw dev wlan0 set power_save off") and cmds[1].endswith("|| true")


def test_netplan_profiles_are_reported_never_migrated_automatically():
    task = next(t for t in _tasks() if "Report profiles still stored as netplan" in t["name"])
    assert "ansible.builtin.debug" in task and "nmcli connection migrate --plugin keyfile" in task["ansible.builtin.debug"]["msg"]
    assert "console" in task["ansible.builtin.debug"]["msg"]
    for t in _tasks():  # nothing runs a migrate
        assert "migrate" not in str(t.get("ansible.builtin.command", "")) + str(t.get("ansible.builtin.shell", ""))
    pattern = re.search(r"select\('match', '([^']+)'\)", str(task["vars"]["pireplace_netplan_profiles"])).group(1)
    for line, listed in (
        ("u1:/etc/netplan/90-NM-x.yaml", True),
        ("u2:/run/NetworkManager/system-connections/netplan-NM-x.nmconnection", True),
        ("u3:/run/NetworkManager/system-connections/lo.nmconnection", False),
        ("u4:/run/NetworkManager/system-connections/tailscale0.nmconnection", False),
        ("u5:/etc/NetworkManager/system-connections/home.nmconnection", False),
    ):
        assert bool(re.match(pattern, line)) is listed
    assert "is defined" not in task["when"] and "length > 0" in task["when"]


def test_first_backup_warning_survives_check_mode():
    task = next(t for t in _tasks() if "Warn when the first profile backup failed" in t["name"])
    assert "default(0)" in task["when"]


def test_netbackup_service_cannot_trip_the_start_limit():
    assert unit("pireplace-netbackup.service")["Unit"]["StartLimitIntervalSec"] == ["0"]


def test_first_profile_backup_cannot_fail_the_play_and_runs_after_the_services():
    tasks = _tasks()
    names = [t["name"] for t in tasks]
    first = names.index("Back up the NetworkManager profiles once now")
    assert first > names.index("Enable and start the control page and Wi-Fi watcher")
    assert tasks[first]["failed_when"] is False and "register" in tasks[first]
