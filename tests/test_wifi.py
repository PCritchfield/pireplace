import http.client
import json
import logging
import subprocess
import threading

import pytest

from pireplace.__main__ import main
from pireplace.config import Config
from pireplace.web import make_server
from pireplace.wifi import Nmcli, WifiError, decide, run_command, setup_routes, watch

SECRET = "hunter2-secret"


@pytest.mark.parametrize("args,expected", [
    (dict(now=100, last_connected=0, hotspot_since=None, connected=True), "none"),
    (dict(now=299, last_connected=0, hotspot_since=None, connected=False), "none"),
    (dict(now=300, last_connected=0, hotspot_since=None, connected=False), "start_hotspot"),
    (dict(now=500, last_connected=0, hotspot_since=400, connected=False), "none"),
    (dict(now=700, last_connected=0, hotspot_since=400, connected=False), "retry_known"),
    (dict(now=700, last_connected=0, hotspot_since=400, connected=True), "stop_hotspot"),
])
def test_decide(args, expected):
    assert decide(**args, after_s=300, retry_s=300) == expected


def test_router_reboot_under_grace_never_starts_hotspot():
    assert decide(now=1000 + 120, last_connected=1000, hotspot_since=None,
                  connected=False, after_s=300, retry_s=300) == "none"


def test_scan_parses_and_dedupes():
    out = "HomeNet:80\n:60\nHomeNet:40\nCafé \\: Guest:70\n"
    n = Nmcli(lambda cmd: out)
    assert n.scan() == ["HomeNet", "Café : Guest"]


def test_scan_orders_strongest_first():
    n = Nmcli(lambda cmd: "Weak:20\nStrong:90\nMid:50\n")
    assert n.scan() == ["Strong", "Mid", "Weak"]


def test_connect_passes_args_as_list_not_shell():
    calls = []
    Nmcli(lambda c: calls.append(c) or "").connect("My Net; rm -rf /", "pa ss")
    assert calls[-1][:4] == ["nmcli", "device", "wifi", "connect"]
    assert "My Net; rm -rf /" in calls[-1] and "pa ss" in calls[-1]


def test_connect_open_network_omits_password():
    calls = []
    Nmcli(lambda c: calls.append(c) or "").connect("Cafe", "")
    assert "password" not in calls[-1]


def test_stop_hotspot_command():
    calls = []
    Nmcli(lambda c: calls.append(c) or "").stop_hotspot()
    assert calls == [["nmcli", "connection", "down", "pireplace-hotspot"]]


class ProfileNm:
    """A runner that behaves like nmcli for the hotspot profile: tracks one profile."""

    def __init__(self, exists=False, ssid="pireplace-setup", psk="12345678", read_fails=False, up_fails=0,
                 up_removes=True):
        self.exists, self.ssid, self.psk = exists, ssid, psk
        self.read_fails, self.up_fails, self.up_removes = read_fails, up_fails, up_removes
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[:3] == ["nmcli", "-t", "-f"] and cmd[3] in ("NAME", "NAME,TYPE"):
            return "pireplace-hotspot\n" if self.exists else "Wired\n"
        if "-g" in cmd:
            if self.read_fails:
                raise subprocess.CalledProcessError(1, cmd, stderr=f"boom {self.psk}")
            return f"{self.ssid}\n{self.psk}\n"
        if cmd[:3] == ["nmcli", "connection", "add"]:
            self.exists = True
        if cmd[:3] == ["nmcli", "connection", "up"] and self.up_fails:
            self.up_fails -= 1
            self.exists = self.exists and not self.up_removes
            raise subprocess.CalledProcessError(10, cmd)
        return ""

    def writes(self):
        return [c for c in self.calls if c[:3] in (["nmcli", "connection", "add"], ["nmcli", "connection", "modify"])]

    def ups(self):
        return [c for c in self.calls if c[:3] == ["nmcli", "connection", "up"]]


def test_ensure_hotspot_profile_creates_it_when_missing():
    run = ProfileNm(exists=False)
    Nmcli(run).ensure_hotspot_profile("pireplace-setup", "12345678")
    assert run.writes() == [[
        "nmcli", "connection", "add", "type", "wifi", "ifname", "wlan0", "con-name", "pireplace-hotspot",
        "autoconnect", "no", "ssid", "pireplace-setup", "802-11-wireless.mode", "ap",
        "ipv4.method", "shared", "ipv6.method", "ignore",
        "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp",
        "wifi-sec.group", "ccmp", "wifi-sec.psk", "12345678"]]


def test_ensure_hotspot_profile_writes_nothing_when_it_matches():
    run = ProfileNm(exists=True)
    Nmcli(run).ensure_hotspot_profile("pireplace-setup", "12345678")
    assert run.writes() == []
    assert ["nmcli", "-e", "no", "-s", "-g", "802-11-wireless.ssid,802-11-wireless-security.psk",
            "connection", "show", "pireplace-hotspot"] in run.calls


@pytest.mark.parametrize("stored", [dict(psk="old-password"), dict(ssid="old-ssid"), dict(read_fails=True)])
def test_ensure_hotspot_profile_modifies_once_when_it_differs_or_cannot_be_read(stored):
    run = ProfileNm(exists=True, **stored)
    Nmcli(run).ensure_hotspot_profile("pireplace-setup", "12345678")
    assert run.writes() == [["nmcli", "connection", "modify", "pireplace-hotspot",
                             "802-11-wireless.ssid", "pireplace-setup", "wifi-sec.psk", "12345678"]]


def test_ensure_hotspot_profile_errors_never_contain_the_password():
    def run(cmd):
        if cmd[3] == "NAME":
            return ""
        raise subprocess.CalledProcessError(4, cmd, stderr=f"bad {SECRET}")
    with pytest.raises(WifiError) as ei:
        Nmcli(run).ensure_hotspot_profile("x", SECRET)
    assert SECRET not in str(ei.value) and SECRET not in repr(ei.value)


def test_failed_modify_error_never_contains_the_password():
    def run(cmd):
        if cmd[3] == "NAME":
            return "pireplace-hotspot\n"
        if "-g" in cmd:
            return "x\nstale-psk-value\n"
        raise subprocess.CalledProcessError(4, cmd, stderr=f"bad {SECRET} stale-psk-value")
    with pytest.raises(WifiError) as ei:
        Nmcli(run).ensure_hotspot_profile("x", SECRET)
    assert SECRET not in str(ei.value) and "stale-psk-value" not in str(ei.value)


def test_start_hotspot_ensures_once_then_only_activates():
    run = ProfileNm(exists=False)
    n = Nmcli(run)
    n.start_hotspot("pireplace-setup", "12345678")
    n.start_hotspot("pireplace-setup", "12345678")
    assert len(run.writes()) == 1
    assert run.ups() == [["nmcli", "connection", "up", "pireplace-hotspot"]] * 2
    assert not any(c[:3] == ["nmcli", "device", "wifi"] for c in run.calls)


def test_start_hotspot_reensures_after_connection_up_fails():
    run = ProfileNm(exists=False, up_fails=1)  # NM "deletes" the profile when up fails
    n = Nmcli(run)
    with pytest.raises(WifiError):
        n.start_hotspot("pireplace-setup", "12345678")
    n.start_hotspot("pireplace-setup", "12345678")
    assert len(run.writes()) == 2  # created, lost, created again
    assert len(run.ups()) == 2


def test_up_failure_with_the_profile_still_there_does_not_rewrite_it():
    run = ProfileNm(exists=False, up_removes=False)
    n = Nmcli(run)
    n.start_hotspot("pireplace-setup", "12345678")  # creates the profile
    run.up_fails = 1
    with pytest.raises(WifiError):
        n.start_hotspot("pireplace-setup", "12345678")
    run.calls.clear()
    n.start_hotspot("pireplace-setup", "12345678")
    assert run.writes() == [] and not any("-g" in c for c in run.calls)
    assert len(run.ups()) == 1


def test_ensure_compares_a_psk_with_colon_and_backslash_without_writing():
    psk = "pa:ss\\word1"
    run = ProfileNm(exists=True, psk=psk)
    Nmcli(run).ensure_hotspot_profile("pireplace-setup", psk)
    assert run.writes() == []
    assert ["nmcli", "-e", "no"] == [c for c in run.calls if "-g" in c][0][:3]


def test_blank_stored_psk_does_not_mangle_later_error_messages():
    n = Nmcli(ProfileNm(exists=True, psk=""))
    n.ensure_hotspot_profile("pireplace-setup", "12345678")
    assert "" not in n._secrets

    def run(cmd):
        raise subprocess.CalledProcessError(4, cmd, stderr="No suitable device")
    n._run = run
    with pytest.raises(WifiError) as ei:
        n.connect("Home", "12345678")
    assert str(ei.value).endswith("No suitable device")


def test_hotspot_clients_warns_on_the_first_failure_only(caplog):
    def run(cmd):
        raise FileNotFoundError("iw")
    n = Nmcli(run)
    caplog.set_level(logging.DEBUG)
    n.hotspot_clients()
    n.hotspot_clients()
    levels = [r.levelno for r in caplog.records if "hotspot clients" in r.getMessage()]
    assert levels == [logging.WARNING, logging.DEBUG]


IW_DUMP = (
    "Station aa:bb:cc:dd:ee:01 (on wlan0)\n\tinactive time:\t10 ms\n\tsignal:  -40 dBm\n"
    "Station aa:bb:cc:dd:ee:02 (on wlan0)\n\tinactive time:\t20 ms\n"
)


def test_hotspot_clients_counts_stations():
    calls = []
    n = Nmcli(lambda c: calls.append(c) or IW_DUMP)
    assert n.hotspot_clients() == 2
    assert calls == [["iw", "dev", "wlan0", "station", "dump"]]


@pytest.mark.parametrize("exc", [subprocess.CalledProcessError(1, ["iw"]), FileNotFoundError("iw"),
                                 subprocess.TimeoutExpired(["iw"], 30)])
def test_hotspot_clients_is_zero_on_any_error(exc):
    def run(cmd):
        raise exc
    assert Nmcli(run).hotspot_clients() == 0


def test_known_wifi_profiles_skips_the_hotspot_and_ethernet():
    out = "Home:802-11-wireless\nOld:802-11-wireless\npireplace-hotspot:802-11-wireless\nWired:802-3-ethernet\n"
    assert Nmcli(lambda c: out).known_wifi_profiles() == ["Home", "Old"]


ACTIVE = "Home:802-11-wireless:wlan0\nlo:loopback:lo\n"
HOTSPOT = "pireplace-hotspot:802-11-wireless:wlan0\n"


@pytest.mark.parametrize("out,hot", [
    ("", False),
    (ACTIVE, False),
    (HOTSPOT, True),
    (ACTIVE + HOTSPOT, True),
])
def test_hotspot_active(out, hot):
    assert Nmcli(lambda c: out).hotspot_active() is hot


# `nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device` as printed by NetworkManager 1.42+
DEV_IDLE = (
    "eth0:ethernet:unavailable:\n"
    "lo:loopback:connected (externally):lo\n"
    "p2p-dev-wlan0:wifi-p2p:disconnected:\n"
)
DEV_WIFI = "wlan0:wifi:connected:Home Net\n"
DEV_HOTSPOT = "wlan0:wifi:connected:pireplace-hotspot\n"
DEV_WIRED = "eth0:ethernet:connected:Wired connection 1\n"


@pytest.mark.parametrize("out,conn", [
    ("", False),
    (DEV_IDLE, False),
    (DEV_IDLE + "wlan0:wifi:disconnected:\n", False),
    (DEV_IDLE + DEV_WIFI, True),
    (DEV_IDLE + DEV_HOTSPOT, False),
    (DEV_WIRED + DEV_HOTSPOT, True),
    ("wlan0:wifi:connecting (getting IP configuration):Home\n", False),
    ("wlan0:wifi:connected:Caf\\: Guest\n", True),
    ("tun0:tun:connected (externally):tun0\n", False),
])
def test_connected_uses_device_state(out, conn):
    calls = []
    n = Nmcli(lambda c: calls.append(c) or out)
    assert n.connected() is conn
    assert calls == [["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]]


def test_try_known_networks_tries_profiles_until_one_works():
    calls = []

    def run(cmd):
        calls.append(cmd)
        if "up" in cmd:
            if cmd[-1] == "Home":
                raise subprocess.CalledProcessError(4, cmd)
            return ""
        return "Home:802-11-wireless\nOld:802-11-wireless\npireplace-hotspot:802-11-wireless\nWired:802-3-ethernet\n"
    assert Nmcli(run).try_known_networks() is True
    ups = [c for c in calls if "up" in c]
    assert [c[-1] for c in ups] == ["Home", "Old"]  # hotspot and ethernet profiles skipped
    assert all(c[:3] == ["nmcli", "connection", "up"] and c[3] == "id" for c in ups)


def test_try_known_networks_false_when_all_fail():
    def run(cmd):
        if "up" in cmd:
            raise subprocess.CalledProcessError(4, cmd)
        return "Home:802-11-wireless\n"
    assert Nmcli(run).try_known_networks() is False


def test_try_known_networks_respects_time_budget():
    now = [0.0]
    ups = []

    def run(cmd):
        if "up" in cmd:
            ups.append(cmd[-1])
            now[0] += 20
            raise subprocess.CalledProcessError(4, cmd)
        return "A:802-11-wireless\nB:802-11-wireless\nC:802-11-wireless\n"
    assert Nmcli(run, clock=lambda: now[0]).try_known_networks() is False
    assert ups == ["A", "B"]  # third attempt would start after the 30 s budget


def test_failed_connect_error_never_contains_password():
    def run(cmd):
        raise subprocess.CalledProcessError(4, cmd, stderr=f"bad {SECRET}")
    with pytest.raises(WifiError) as ei:
        Nmcli(run).connect("Home", SECRET)
    assert SECRET not in str(ei.value) and SECRET not in repr(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


def test_timeout_is_wrapped_without_password():
    def run(cmd):
        raise subprocess.TimeoutExpired(cmd, 30)
    with pytest.raises(WifiError) as ei:
        Nmcli(run).start_hotspot("x", SECRET)
    assert SECRET not in str(ei.value)


def test_run_command_uses_argv_without_shell(monkeypatch):
    seen = {}

    def fake(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="out")
    monkeypatch.setattr(subprocess, "run", fake)
    assert run_command(["nmcli", "x"]) == "out"
    assert seen["cmd"] == ["nmcli", "x"] and not seen.get("shell")
    assert seen["check"] is True and seen["timeout"] == 30 and seen["capture_output"] and seen["text"]


# ---- HTTP routes -------------------------------------------------------

class Srv:
    def __init__(self, httpd):
        self.httpd = httpd
        self.port = httpd.server_address[1]


def call(srv, method, path, body=None):
    headers = {"Content-Type": "application/json"} if method == "POST" else {}
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        data = r.read()
        return r.status, data, r.getheader("Content-Type") or ""
    finally:
        conn.close()


def post_raw(srv, path, body):
    code, data, _ = call(srv, "POST", path, body)
    return code, json.loads(data)


def wait_for(cond):
    for _ in range(250):
        if cond():
            return True
        threading.Event().wait(0.02)
    return False


class FakeNm:
    def __init__(self):
        self.calls = []
        self.done = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.connect_error = None
        self.stop_error = None
        self.hotspot = True

    def hotspot_active(self):
        return self.hotspot

    def stop_hotspot(self):
        self.calls.append(("stop_hotspot",))
        if self.stop_error:
            raise self.stop_error

    def connect(self, ssid, password):
        self.release.wait(5)
        self.calls.append(("connect", ssid, password))
        self.done.set()
        if self.connect_error:
            raise self.connect_error


@pytest.fixture
def fake_nm():
    return FakeNm()


@pytest.fixture
def setup_srv(tmp_path, fake_nm):
    cache = tmp_path / "wifi-scan.json"
    httpd = make_server(object(), "127.0.0.1", 0, extra=setup_routes(fake_nm, cache, settle_s=0),
                        hotspot_address="127.0.0.1")  # test servers bind 127.0.0.1
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    s = Srv(httpd)
    s.cache = cache
    yield s
    fake_nm.release.set()
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=5)


def test_setup_page_served_as_html(setup_srv):
    code, body, ctype = call(setup_srv, "GET", "/setup")
    assert code == 200 and "text/html" in ctype and b"<html" in body


def test_networks_missing_cache_is_empty(setup_srv):
    code, body, _ = call(setup_srv, "GET", "/api/wifi/networks")
    assert code == 200 and json.loads(body) == {"networks": []}


def test_networks_reads_cache(setup_srv):
    setup_srv.cache.write_text(json.dumps({"networks": ["A", "B"]}))
    assert json.loads(call(setup_srv, "GET", "/api/wifi/networks")[1]) == {"networks": ["A", "B"]}


@pytest.mark.parametrize("content", ["not json", "[1]", '{"networks": "x"}', '{"networks": [1, "a"]}'])
def test_networks_corrupt_cache_is_empty(setup_srv, content):
    setup_srv.cache.write_text(content)
    assert json.loads(call(setup_srv, "GET", "/api/wifi/networks")[1]) == {"networks": []}


BAD_BODIES = [
    b"{}", b"", b'{"ssid": "", "password": "x"}', b'{"ssid": "   ", "password": "x"}',
    b'{"ssid": 5, "password": "x"}', b'{"ssid": "Home", "password": 5}',
    b'{"ssid": "Home"}', b'{"ssid": "Home", "password": "short"}',
    b'{"ssid": "-h", "password": "12345678"}',
    json.dumps({"ssid": "a" * 33, "password": "12345678"}).encode(),
    json.dumps({"ssid": "Home", "password": "p" * 64}).encode(),
    json.dumps({"ssid": "Ho\x00me", "password": "12345678"}).encode(),
    json.dumps({"ssid": "Home", "password": "1234\x005678"}).encode(),
    json.dumps({"ssid": "é" * 17, "password": "12345678"}).encode(),  # 34 bytes
    b"[]",
]


@pytest.mark.parametrize("body", BAD_BODIES)
def test_connect_route_rejects_bad_input(setup_srv, fake_nm, body):
    assert post_raw(setup_srv, "/api/wifi/connect", body)[0] == 400
    assert fake_nm.calls == []


def test_connect_returns_202_then_stops_hotspot_and_connects(setup_srv, fake_nm):
    fake_nm.release.clear()
    body = json.dumps({"ssid": "Home Net", "password": "correct horse"}).encode()
    code, data = post_raw(setup_srv, "/api/wifi/connect", body)
    assert code == 202 and "correct horse" not in json.dumps(data)
    assert wait_for(lambda: fake_nm.calls == [("stop_hotspot",)])  # connect still pending
    fake_nm.release.set()
    assert fake_nm.done.wait(5)
    assert fake_nm.calls == [("stop_hotspot",), ("connect", "Home Net", "correct horse")]


def test_connect_open_network_allowed(setup_srv, fake_nm):
    body = json.dumps({"ssid": "Cafe", "password": ""}).encode()
    assert post_raw(setup_srv, "/api/wifi/connect", body)[0] == 202
    assert fake_nm.done.wait(5)
    assert fake_nm.calls[-1] == ("connect", "Cafe", "")


def test_second_connect_while_busy_is_409(setup_srv, fake_nm):
    fake_nm.release.clear()
    body = json.dumps({"ssid": "Home", "password": "12345678"}).encode()
    assert post_raw(setup_srv, "/api/wifi/connect", body)[0] == 202
    assert post_raw(setup_srv, "/api/wifi/connect", body)[0] == 409
    fake_nm.release.set()
    assert fake_nm.done.wait(5)


def test_failed_connect_logs_without_password(setup_srv, fake_nm, caplog):
    fake_nm.connect_error = WifiError("nmcli connect failed (exit 4)")
    caplog.set_level(logging.DEBUG)
    body = json.dumps({"ssid": "Home", "password": SECRET + "xx"}).encode()
    assert post_raw(setup_srv, "/api/wifi/connect", body)[0] == 202
    assert fake_nm.done.wait(5)
    assert wait_for(lambda: "connect failed" in caplog.text)
    assert SECRET not in caplog.text


def test_connect_marks_busy_marker_during_connect(setup_srv, fake_nm):
    marker = setup_srv.cache.with_name("wifi-connecting")
    fake_nm.release.clear()
    body = json.dumps({"ssid": "Home", "password": "12345678"}).encode()
    post_raw(setup_srv, "/api/wifi/connect", body)
    assert wait_for(marker.exists)
    fake_nm.release.set()
    assert fake_nm.done.wait(5)
    assert wait_for(lambda: not marker.exists())


# ---- watcher -----------------------------------------------------------

class WatchNm:
    def __init__(self, connected=False, hotspot=False, known_ok=False, profiles=("Home",), clients=0):
        self._connected, self._hotspot, self.known_ok = connected, hotspot, known_ok
        self.profiles, self.clients = list(profiles), clients
        self.calls = []

    def known_wifi_profiles(self):
        return self.profiles

    def hotspot_clients(self):
        return self.clients

    def connected(self):
        return self._connected

    def hotspot_active(self):
        return self._hotspot

    def scan(self):
        self.calls.append("scan")
        return ["Home", "Other"]

    def start_hotspot(self, ssid, password):
        self.calls.append(("start_hotspot", ssid))
        self._hotspot = True

    def stop_hotspot(self):
        self.calls.append("stop_hotspot")
        self._hotspot = False

    def try_known_networks(self):
        self.calls.append("try_known")
        if self.known_ok:
            self._connected = True
        return self.known_ok


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make_config(tmp_path, **kw):
    base = dict(
        video_dir=tmp_path, music_dir=tmp_path, video_socket=tmp_path / "v", music_socket=tmp_path / "m",
        state_file=tmp_path / "state.json", start_music=True, default_mood=None, music_volume=1,
        log_volume=1, host="127.0.0.1", port=1, hotspot_ssid="pireplace-setup",
        hotspot_password="12345678", hotspot_after_s=300, hotspot_retry_s=300,
    )
    base.update(kw)
    return Config(**base)


def run_watch(nm, cfg, cache, clock, iterations, busy=lambda: False):
    watch(nm, cfg, cache, clock=clock, sleep=clock.sleep, busy=busy, iterations=iterations)


def test_watch_disconnected_past_grace_caches_scan_then_starts_hotspot(tmp_path):
    nm, clock, cache = WatchNm(), Clock(), tmp_path / "wifi-scan.json"
    run_watch(nm, make_config(tmp_path), cache, clock, iterations=31)  # t = 0..300
    assert nm.calls == ["scan", ("start_hotspot", "pireplace-setup")]
    assert json.loads(cache.read_text()) == {"networks": ["Home", "Other"]}
    assert "12345678" not in cache.read_text()


def test_watch_before_grace_does_nothing(tmp_path):
    nm, clock = WatchNm(), Clock()
    run_watch(nm, make_config(tmp_path), tmp_path / "s.json", clock, iterations=30)  # t = 0..290
    assert nm.calls == []


def test_watch_connected_resets_grace(tmp_path):
    nm, clock = WatchNm(connected=True), Clock()
    cfg = make_config(tmp_path)
    run_watch(nm, cfg, tmp_path / "s.json", clock, iterations=40)
    nm._connected = False
    run_watch(nm, cfg, tmp_path / "s.json", clock, iterations=20)
    assert nm.calls == []


def test_watch_stops_hotspot_when_connected(tmp_path):
    nm, clock = WatchNm(connected=True, hotspot=True), Clock()
    run_watch(nm, make_config(tmp_path), tmp_path / "s.json", clock, iterations=1)
    assert nm.calls == ["stop_hotspot"]


def test_watch_retry_known_failure_rescans_and_restarts_hotspot(tmp_path):
    nm, clock, cache = WatchNm(), Clock(), tmp_path / "s.json"
    cfg = make_config(tmp_path)
    run_watch(nm, cfg, cache, clock, iterations=31)
    nm.calls.clear()
    run_watch(nm, cfg, cache, clock, iterations=31)  # 300 s later
    assert nm.calls == ["stop_hotspot", "try_known", "scan", ("start_hotspot", "pireplace-setup")]


def test_watch_retry_known_success_leaves_hotspot_down(tmp_path):
    nm, clock, cache = WatchNm(known_ok=True), Clock(), tmp_path / "s.json"
    cfg = make_config(tmp_path)
    run_watch(nm, cfg, cache, clock, iterations=31)
    nm.calls.clear()
    run_watch(nm, cfg, cache, clock, iterations=40)
    assert nm.calls == ["stop_hotspot", "try_known"]


def test_watch_skips_while_connect_in_progress(tmp_path):
    nm, clock = WatchNm(), Clock()
    run_watch(nm, make_config(tmp_path), tmp_path / "s.json", clock, iterations=40, busy=lambda: True)
    assert nm.calls == []


def test_watch_survives_nmcli_errors(tmp_path, caplog):
    class Boom(WatchNm):
        def connected(self):
            raise WifiError("nmcli exploded")
    clock = Clock()
    run_watch(Boom(), make_config(tmp_path), tmp_path / "s.json", clock, iterations=3)
    assert "nmcli exploded" in caplog.text


def test_short_hotspot_password_exits_2(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[wifi]\nhotspot_password = "short"\n')
    assert main(["wifi-watch", "--config", str(p)]) == 2


def test_empty_default_hotspot_password_exits_2(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("")
    assert main(["wifi-watch", "--config", str(p)]) == 2


def test_failed_stop_hotspot_does_not_abort_connect(setup_srv, fake_nm, caplog):
    fake_nm.stop_error = WifiError("nmcli connection down failed (exit 10)")
    caplog.set_level(logging.DEBUG)
    body = json.dumps({"ssid": "Home", "password": "12345678"}).encode()
    assert post_raw(setup_srv, "/api/wifi/connect", body)[0] == 202
    assert fake_nm.done.wait(5)
    assert fake_nm.calls == [("stop_hotspot",), ("connect", "Home", "12345678")]


def test_connect_is_409_when_the_hotspot_is_not_active(setup_srv, fake_nm):
    fake_nm.hotspot = False
    body = json.dumps({"ssid": "Home", "password": "12345678"}).encode()
    code, data = post_raw(setup_srv, "/api/wifi/connect", body)
    assert code == 409 and "hotspot" in data["error"]
    assert not fake_nm.done.wait(0.2) and fake_nm.calls == []


def test_connect_is_503_when_hotspot_state_is_unknown(setup_srv, fake_nm):
    def boom():
        raise WifiError("nmcli connection show failed (exit 8)")

    fake_nm.hotspot_active = boom
    body = json.dumps({"ssid": "Home", "password": "12345678"}).encode()
    assert post_raw(setup_srv, "/api/wifi/connect", body)[0] == 503
    assert fake_nm.calls == []


def test_hotspot_gone_by_connect_time_is_not_stopped_but_connect_runs(tmp_path, fake_nm):
    # The watcher may drop the hotspot between the request and the background connect.
    routes = setup_routes(fake_nm, tmp_path / "s.json", settle_s=0,
                          sleep=lambda s: setattr(fake_nm, "hotspot", False))
    code, _ = routes[("POST", "/api/wifi/connect")]({"ssid": "Home", "password": "12345678"})
    assert code == 202 and fake_nm.done.wait(5)
    assert fake_nm.calls == [("connect", "Home", "12345678")]


def test_wifi_routes_are_404_off_the_hotspot_address(tmp_path, fake_nm):
    httpd = make_server(object(), "127.0.0.1", 0, extra=setup_routes(fake_nm, tmp_path / "s.json", settle_s=0),
                        hotspot_address="10.42.0.1")
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    try:
        srv = Srv(httpd)
        body = json.dumps({"ssid": "Home", "password": "12345678"}).encode()
        assert call(srv, "GET", "/setup")[0] == 404
        assert call(srv, "GET", "/api/wifi/networks")[0] == 404
        assert call(srv, "POST", "/api/wifi/connect", body)[0] == 404
        assert fake_nm.calls == []
    finally:
        httpd.shutdown(); httpd.server_close(); t.join(timeout=5)


def test_connect_waits_settle_time_before_touching_hotspot(tmp_path, fake_nm):
    slept = []
    routes = setup_routes(fake_nm, tmp_path / "s.json", settle_s=1.5,
                          sleep=lambda s: slept.append((s, list(fake_nm.calls))))
    code, _ = routes[("POST", "/api/wifi/connect")]({"ssid": "Home", "password": "12345678"})
    assert code == 202 and fake_nm.done.wait(5)
    assert slept == [(1.5, [])]  # slept before any nmcli call


def test_real_error_path_never_leaks_password_to_logs_or_responses(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    done = threading.Event()

    def run(cmd):
        if "connect" in cmd:
            done.set()
        if cmd[1:3] == ["-t", "-f"]:
            return "pireplace-hotspot:802-11-wireless:wlan0\n"
        raise subprocess.CalledProcessError(10, ["nmcli", "x", SECRET], stderr=f"Error {SECRET}")
    cache = tmp_path / "s.json"
    httpd = make_server(object(), "127.0.0.1", 0, extra=setup_routes(Nmcli(run), cache, settle_s=0),
                        hotspot_address="127.0.0.1")
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    try:
        srv = Srv(httpd)
        body = json.dumps({"ssid": "Home", "password": SECRET}).encode()
        code, data, _ = call(srv, "POST", "/api/wifi/connect", body)
        assert code == 202 and SECRET not in data.decode()
        assert done.wait(5) and wait_for(lambda: "connecting to" in caplog.text)
    finally:
        httpd.shutdown(); httpd.server_close(); t.join(timeout=5)
    assert SECRET not in caplog.text


def test_non_wifi_error_in_connect_logs_only_type_name(setup_srv, fake_nm, caplog):
    caplog.set_level(logging.DEBUG)
    fake_nm.connect_error = subprocess.CalledProcessError(10, ["nmcli", "x", SECRET + "xx"])
    body = json.dumps({"ssid": "Home", "password": SECRET + "xx"}).encode()
    code, data, _ = call(setup_srv, "POST", "/api/wifi/connect", body)
    assert code == 202 and SECRET not in data.decode()
    assert fake_nm.done.wait(5) and wait_for(lambda: "CalledProcessError" in caplog.text)
    assert SECRET not in caplog.text


# ---- calmer retries ----------------------------------------------------

def retry_calls(calls):
    return [c for c in calls if c in ("stop_hotspot", "try_known")]


def test_watch_keeps_the_hotspot_when_no_wifi_is_saved(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    nm, clock, cache = WatchNm(profiles=[]), Clock(), tmp_path / "s.json"
    run_watch(nm, make_config(tmp_path), cache, clock, iterations=31)
    nm.calls.clear()
    run_watch(nm, make_config(tmp_path), cache, clock, iterations=31)  # retry timer fires
    assert nm.calls == []  # no stop, no start, no scan
    assert caplog.text.count("hotspot kept: no saved Wi-Fi networks") == 1


def test_watch_keeps_the_hotspot_while_a_client_is_connected(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    nm, clock, cache = WatchNm(clients=1), Clock(), tmp_path / "s.json"
    run_watch(nm, make_config(tmp_path), cache, clock, iterations=31)
    nm.calls.clear()
    run_watch(nm, make_config(tmp_path), cache, clock, iterations=31)
    assert nm.calls == []
    assert caplog.text.count("hotspot kept: 1 client(s) connected") == 1


def test_a_client_that_never_leaves_cannot_block_the_retry_forever(tmp_path):
    nm, clock = WatchNm(clients=1), Clock()
    stops = stamp(nm, clock, "stop_hotspot")
    run_watch(nm, make_config(tmp_path), tmp_path / "s.json", clock, iterations=130)  # t = 0..1290
    # hotspot at 300; due at 600 and 900 (skipped), 3rd expiry at 1200 retries anyway
    assert stops == [1200]
    assert "try_known" in nm.calls


class TimedClock(Clock):
    """A Clock that runs scripted changes when time passes a given second."""

    def __init__(self, script):
        super().__init__()
        self.script = sorted(script, key=lambda x: x[0])

    def sleep(self, s):
        super().sleep(s)
        while self.script and self.t >= self.script[0][0]:
            self.script.pop(0)[1]()


def stamp(nm, clock, name):
    """Record the time of each call to nm.<name>."""
    stamps, orig = [], getattr(nm, name)
    setattr(nm, name, lambda *a: stamps.append(clock.t) or orig(*a))
    return stamps


def test_watch_skip_resets_the_timer_and_retries_once_the_client_leaves(tmp_path):
    nm = WatchNm(clients=1)
    clock = TimedClock([(700, lambda: setattr(nm, "clients", 0))])
    stops = stamp(nm, clock, "stop_hotspot")
    run_watch(nm, make_config(tmp_path), tmp_path / "s.json", clock, iterations=100)  # t = 0..990
    # hotspot at 300; due at 600 but a client is there; timer restarts at 600; due again at 900
    assert stops == [900]


def test_watch_retry_wait_doubles_after_each_failure_up_to_1800(tmp_path):
    nm, clock = WatchNm(), Clock()
    stamps = []
    orig = nm.try_known_networks
    nm.try_known_networks = lambda: stamps.append(clock.t) or orig()
    run_watch(nm, make_config(tmp_path), tmp_path / "s.json", clock, iterations=800)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert gaps[:3] == [600, 1200, 1800]
    assert all(g == 1800 for g in gaps[2:]) and len(gaps) >= 4


def test_watch_retry_wait_resets_after_reconnect(tmp_path):
    nm = WatchNm()

    def reconnect():
        nm._connected, nm._hotspot = True, True

    clock = TimedClock([(700, reconnect), (710, lambda: setattr(nm, "_connected", False))])
    tries, starts = stamp(nm, clock, "try_known_networks"), stamp(nm, clock, "start_hotspot")
    run_watch(nm, make_config(tmp_path), tmp_path / "s.json", clock, iterations=200)  # t = 0..1990
    # hotspot 300, retry fails at 600 (wait now 600); reconnect at 700 resets the wait;
    # hotspot again after the grace; the next retry comes 300 s later, not 600
    assert tries[0] == 600 and starts[:2] == [300, 600]
    assert starts[2] - 0 > 710 and tries[1] - starts[2] == 300


def test_decide_uses_the_wait_it_is_given():
    assert decide(now=700, last_connected=0, hotspot_since=400, connected=False,
                  after_s=300, retry_s=600) == "none"
    assert decide(now=1000, last_connected=0, hotspot_since=400, connected=False,
                  after_s=300, retry_s=600) == "retry_known"


class SimNm:
    """Real Nmcli over a fake nmcli: no Wi-Fi, no saved profiles, hotspot toggles on up/down."""

    def __init__(self):
        self.profile = False
        self.hotspot = False
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[:4] == ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"]:
            return "wlan0:wifi:connected:pireplace-hotspot\n" if self.hotspot else "wlan0:wifi:disconnected:\n"
        if cmd[:4] == ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE"]:
            return "pireplace-hotspot:802-11-wireless:wlan0\n" if self.hotspot else ""
        if cmd[:4] == ["nmcli", "-t", "-f", "NAME"]:
            return "pireplace-hotspot\n" if self.profile else ""
        if cmd[:3] == ["nmcli", "connection", "add"]:
            self.profile = True
        if cmd[:3] == ["nmcli", "connection", "up"]:
            self.hotspot = True
        if cmd[:3] == ["nmcli", "connection", "down"]:
            self.hotspot = False
        return ""


def test_an_hour_without_wifi_writes_the_hotspot_profile_at_most_once(tmp_path):
    sim = SimNm()
    clock = Clock()
    nm = Nmcli(sim, clock=clock)
    run_watch(nm, make_config(tmp_path), tmp_path / "s.json", clock, iterations=360)
    writes = [c for c in sim.calls if c[:3] in (["nmcli", "connection", "add"], ["nmcli", "connection", "modify"])]
    assert len(writes) == 1
    assert sim.hotspot
    assert not any(c[:3] == ["nmcli", "connection", "down"] for c in sim.calls)
