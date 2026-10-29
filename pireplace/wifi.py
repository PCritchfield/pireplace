"""Wi-Fi onboarding: NetworkManager wrapper, hotspot watchdog, and setup page routes.

Security notes:
- Every command is an argv list; nothing goes through a shell.
- The Wi-Fi password must never reach logs, HTTP responses, or the scan cache. It is
  unavoidably in nmcli's argv, so errors from the runner are re-raised as `WifiError`
  with the command line dropped (CalledProcessError's str() would include it).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Literal

from pireplace.config import Config
from pireplace.web import BadRequest, Handler

log = logging.getLogger(__name__)

Runner = Callable[[list[str]], str]
Action = Literal["none", "start_hotspot", "retry_known", "stop_hotspot"]

HOTSPOT_CON = "pireplace-hotspot"
HOTSPOT_IFACE = "wlan0"
WIFI_TYPE = "802-11-wireless"  # connection TYPE (nmcli connection show)
UPLINK_DEVICE_TYPES = ("wifi", "ethernet")  # device TYPE (nmcli device)
TICK_S = 10
KNOWN_BUDGET_S = 30
CONNECTING_MAX_AGE_S = 120
SCAN_CACHE_NAME = "wifi-scan.json"
CONNECTING_NAME = "wifi-connecting"
MIN_PSK, MAX_PSK = 8, 63  # WPA2 passphrase length
MAX_SSID_BYTES = 32
MAX_RETRY_S = 1800  # ceiling for the retry back-off
MAX_CLIENT_SKIPS = 2  # due retries skipped for an attached client before retrying anyway


class WifiError(Exception):
    """An nmcli call failed. The message never contains the command line."""


def run_command(cmd: list[str]) -> str:
    """The real Runner: run argv (never a shell) and return stdout."""
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30).stdout


def split_terse(line: str) -> list[str]:
    """Split one `nmcli -t` line on unescaped ':' and unescape '\\:' and '\\\\'."""
    fields, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    fields.append("".join(cur))
    return fields


def _rows(out: str, width: int) -> list[list[str]]:
    rows = []
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) >= width:
            rows.append(f)
    return rows


class Nmcli:
    def __init__(self, run: Runner, clock: Callable[[], float] = time.monotonic):
        self._run = run
        self._clock = clock
        self._secrets: set[str] = set()  # passwords seen so far; redacted from stderr
        self._hotspot_ensured = False  # the hotspot profile was checked/created by this process
        self._clients_warned = False  # hotspot_clients failure already logged at WARNING

    def _call(self, cmd: list[str], secret: str = "") -> str:
        if secret:
            self._secrets.add(secret)
        try:
            return self._run(cmd)
        except subprocess.CalledProcessError as e:
            # stderr is only surfaced for password-bearing calls (redacted); other commands
            # report just the exit code, so a password can never ride along in their stderr.
            detail = (e.stderr or "").strip().splitlines() if secret else []
            msg = f"nmcli {' '.join(cmd[1:3])} failed (exit {e.returncode})"
            if detail:
                first = detail[0]
                for sec in self._secrets:
                    first = first.replace(sec, "***")
                msg += f": {first}"
            raise WifiError(msg) from None
        except subprocess.TimeoutExpired:
            raise WifiError(f"nmcli {' '.join(cmd[1:3])} timed out") from None
        except OSError as e:
            raise WifiError(f"cannot run nmcli: {e.strerror or type(e).__name__}") from None

    def _active(self) -> list[list[str]]:
        return _rows(self._call(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"]), 2)

    def connected(self) -> bool:
        """A Wi-Fi or ethernet device is fully connected to something other than our hotspot."""
        out = self._call(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"])
        return any(
            dev_type in UPLINK_DEVICE_TYPES and state == "connected" and con != HOTSPOT_CON
            for _dev, dev_type, state, con, *_ in _rows(out, 4)
        )

    def hotspot_active(self) -> bool:
        return any(r[0] == HOTSPOT_CON for r in self._active())

    def _hotspot_profile_exists(self) -> bool:
        out = self._call(["nmcli", "-t", "-f", "NAME", "connection", "show"])
        return any(split_terse(line)[0] == HOTSPOT_CON for line in out.splitlines())

    def ensure_hotspot_profile(self, ssid: str, password: str) -> None:
        """Make sure the hotspot profile exists with this SSID and PSK, with at most one write.

        NetworkManager rewrites profile files on every change, so we only write when needed.
        """
        if not self._hotspot_profile_exists():
            self._call(["nmcli", "connection", "add", "type", "wifi", "ifname", HOTSPOT_IFACE,
                        "con-name", HOTSPOT_CON, "autoconnect", "no", "ssid", ssid,
                        "802-11-wireless.mode", "ap",
                        "ipv4.method", "shared", "ipv6.method", "ignore",
                        "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn",
                        "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp",
                        "wifi-sec.psk", password], secret=password)
            return
        try:
            stored = self._call(["nmcli", "-e", "no", "-s", "-g", "802-11-wireless.ssid,802-11-wireless-security.psk",
                                 "connection", "show", HOTSPOT_CON], secret=password).splitlines()
        except WifiError:
            stored = []
        if len(stored) > 1 and stored[1]:  # never an empty string: replace("") would mangle messages
            self._secrets.add(stored[1])  # the PSK read back is a secret too: redact it
        if stored[:2] == [ssid, password]:
            return
        self._call(["nmcli", "connection", "modify", HOTSPOT_CON, "802-11-wireless.ssid", ssid,
                    "wifi-sec.psk", password], secret=password)

    def start_hotspot(self, ssid: str, password: str) -> None:
        if not self._hotspot_ensured:
            self.ensure_hotspot_profile(ssid, password)
            self._hotspot_ensured = True
        try:
            self._call(["nmcli", "connection", "up", HOTSPOT_CON])
        except WifiError:
            try:
                if not self._hotspot_profile_exists():
                    self._hotspot_ensured = False  # NM lost the profile; re-create it next time
            except WifiError:
                pass  # unknown: keep the flag rather than rewrite the profile every tick
            raise

    def hotspot_clients(self) -> int:
        """Stations associated with the hotspot. Best effort: 0 on any error."""
        try:
            out = self._run(["iw", "dev", HOTSPOT_IFACE, "station", "dump"])
        except Exception as e:
            log.log(logging.DEBUG if self._clients_warned else logging.WARNING,
                    "cannot count hotspot clients: %s", type(e).__name__)
            self._clients_warned = True
            return 0
        return sum(1 for line in out.splitlines() if line.startswith("Station "))

    def stop_hotspot(self) -> None:
        self._call(["nmcli", "connection", "down", HOTSPOT_CON])

    def known_wifi_profiles(self) -> list[str]:
        """Saved Wi-Fi profiles other than our own hotspot."""
        out = self._call(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
        return [r[0] for r in _rows(out, 2) if r[1] == WIFI_TYPE and r[0] != HOTSPOT_CON]

    def try_known_networks(self) -> bool:
        """Bring up saved Wi-Fi profiles one by one; stop after ~30 s. True on first success."""
        profiles = self.known_wifi_profiles()
        start = self._clock()
        for name in profiles:
            if self._clock() - start >= KNOWN_BUDGET_S:
                break
            try:
                self._call(["nmcli", "connection", "up", "id", name])
            except WifiError:
                continue
            return True
        return False

    def scan(self) -> list[str]:
        out = self._call(["nmcli", "-t", "-f", "SSID,SIGNAL", "device", "wifi", "list", "--rescan", "yes"])
        best: dict[str, int] = {}
        for ssid, signal, *_ in _rows(out, 2):
            if not ssid:
                continue
            try:
                level = int(signal)
            except ValueError:
                level = 0
            best[ssid] = max(level, best.get(ssid, -1))
        return sorted(best, key=lambda s: -best[s])  # stable: ties keep first-seen order

    def connect(self, ssid: str, password: str) -> None:
        cmd = ["nmcli", "device", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        self._call(cmd, secret=password)


def decide(now: float, last_connected: float, hotspot_since: float | None, connected: bool,
           after_s: int, retry_s: int) -> Action:
    """Pure policy. `hotspot_since` is None when the hotspot is down."""
    if connected:
        return "stop_hotspot" if hotspot_since is not None else "none"
    if hotspot_since is None:
        return "start_hotspot" if now - last_connected >= after_s else "none"
    return "retry_known" if now - hotspot_since >= retry_s else "none"


# ---- shared files ------------------------------------------------------

def scan_cache_path(config: Config) -> Path:
    return Path(config.state_file).parent / SCAN_CACHE_NAME


def _connecting_marker(scan_cache: Path) -> Path:
    return scan_cache.with_name(CONNECTING_NAME)


def connecting_in_progress(scan_cache: Path, wall: Callable[[], float] = time.time) -> bool:
    """True if the setup page started a connect recently (marker file is fresh)."""
    try:
        return wall() - _connecting_marker(scan_cache).stat().st_mtime < CONNECTING_MAX_AGE_S
    except OSError:
        return False


def write_scan_cache(path: Path, networks: list[str]) -> None:
    """Atomically write {"networks": [...]}. SSIDs only; never credentials."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".wifi-scan-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"networks": networks}, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_scan_cache(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    nets = data.get("networks") if isinstance(data, dict) else None
    if not isinstance(nets, list) or not all(isinstance(n, str) for n in nets):
        return []
    return nets


# ---- watcher -----------------------------------------------------------

def retry_skip_reason(nmcli, client_skips: int) -> tuple[str | None, bool]:
    """Why a due retry should leave the hotspot alone, and whether that was a client skip.

    A client that stays attached forever (a phone that auto-joined, no internet) must not
    hold the Pi off the home network: after MAX_CLIENT_SKIPS skips the retry goes ahead.
    """
    if not nmcli.known_wifi_profiles():
        return "no saved Wi-Fi networks", False
    clients = nmcli.hotspot_clients()
    if clients and client_skips < MAX_CLIENT_SKIPS:
        return f"{clients} client(s) connected", True
    return None, False


def watch(nmcli, config: Config, scan_cache: Path, *, clock: Callable[[], float] = time.monotonic,
          sleep: Callable[[float], object] = time.sleep, busy: Callable[[], bool] = lambda: False,
          iterations: int | None = None) -> None:
    """Apply `decide` every TICK_S seconds. `iterations` bounds the loop (for tests)."""
    last_connected = clock()
    hotspot_since: float | None = None
    client_skips = 0  # consecutive due retries skipped because a client was attached
    retry_wait = config.hotspot_retry_s  # doubles after each failed retry, reset once connected

    def cache_scan() -> None:
        # The single radio cannot scan in AP mode, so scan now and let the setup page read it.
        try:
            write_scan_cache(scan_cache, nmcli.scan())
        except (WifiError, OSError) as e:
            log.warning("scan before hotspot failed: %s", e)

    def raise_hotspot() -> float:
        cache_scan()
        nmcli.start_hotspot(config.hotspot_ssid, config.hotspot_password)
        log.info("hotspot %r up", config.hotspot_ssid)
        return clock()

    n = 0
    while iterations is None or n < iterations:
        n += 1
        try:
            if not busy():
                now = clock()
                connected = nmcli.connected()
                if connected:
                    last_connected = now
                    retry_wait = config.hotspot_retry_s
                    client_skips = 0
                if nmcli.hotspot_active():
                    hotspot_since = now if hotspot_since is None else hotspot_since
                else:
                    hotspot_since = None
                action = decide(now, last_connected, hotspot_since, connected,
                                config.hotspot_after_s, retry_wait)
                if action == "start_hotspot":
                    hotspot_since = raise_hotspot()
                elif action == "stop_hotspot":
                    nmcli.stop_hotspot()
                    hotspot_since = None
                elif action == "retry_known":
                    skip, client_skip = retry_skip_reason(nmcli, client_skips)
                    if skip:
                        client_skips = client_skips + 1 if client_skip else 0
                        log.info("hotspot kept: %s", skip)
                        hotspot_since = now  # nothing to retry, or someone is mid-setup: wait again
                    else:
                        client_skips = 0
                        nmcli.stop_hotspot()
                        hotspot_since = None
                        if nmcli.try_known_networks():
                            last_connected = clock()
                            retry_wait = config.hotspot_retry_s
                            log.info("reconnected to a known network")
                        else:
                            retry_wait = min(retry_wait * 2, max(MAX_RETRY_S, config.hotspot_retry_s))
                            hotspot_since = raise_hotspot()
        except (WifiError, OSError) as e:
            log.warning("wifi watch iteration failed: %s", e)
        sleep(TICK_S)


# ---- setup page routes -------------------------------------------------

def _validate_credentials(body: object) -> tuple[str, str]:
    if not isinstance(body, dict):
        raise BadRequest("expected a JSON object body")
    ssid, password = body.get("ssid"), body.get("password")
    if not isinstance(ssid, str) or not ssid.strip():
        raise BadRequest("ssid must be a non-empty string")
    if not isinstance(password, str):
        raise BadRequest("password must be a string")
    if len(ssid.encode("utf-8", "surrogatepass")) > MAX_SSID_BYTES:
        raise BadRequest("ssid is too long")
    if ssid.startswith("-"):
        raise BadRequest("ssid must not start with '-'")  # never let it parse as an nmcli option
    if any(ord(c) < 32 or ord(c) == 127 for c in ssid + password):
        raise BadRequest("ssid and password must not contain control characters")
    if password and not MIN_PSK <= len(password) <= MAX_PSK:
        raise BadRequest(f"password must be {MIN_PSK}-{MAX_PSK} characters (or empty for an open network)")
    try:
        (ssid + password).encode("utf-8")
    except UnicodeEncodeError:
        raise BadRequest("ssid and password must be valid text") from None
    return ssid, password


def setup_routes(nmcli, scan_cache: Path, settle_s: float = 1.5,
                 sleep: Callable[[float], object] = time.sleep) -> dict[tuple[str, str], Handler]:
    """Handlers for make_server(extra=...). `GET /setup` itself is a static page in web.py."""
    scan_cache = Path(scan_cache)
    marker = _connecting_marker(scan_cache)
    in_flight = threading.Lock()

    def api_networks(_body) -> tuple[int, dict]:
        return 200, {"networks": read_scan_cache(scan_cache)}

    def stop_hotspot_if_up() -> None:
        # Best effort: `connection down` fails when the hotspot is not active (e.g. we are
        # on ethernet, or the watcher just stopped it). That must not abort the connect.
        try:
            if nmcli.hotspot_active():
                nmcli.stop_hotspot()
        except Exception as e:
            log.warning("could not stop hotspot: %s", e if isinstance(e, WifiError) else type(e).__name__)

    def do_connect(ssid: str, password: str) -> None:
        try:
            sleep(settle_s)  # let the 202 reach a phone that is connected over the hotspot
            stop_hotspot_if_up()
            nmcli.connect(ssid, password)
            log.info("connected to %r", ssid)
        except Exception as e:
            # Never log the raw exception: only WifiError is known to be password-free.
            log.warning("connecting to %r failed: %s", ssid, e if isinstance(e, WifiError) else type(e).__name__)
        finally:
            try:
                marker.unlink()
            except OSError:
                pass
            in_flight.release()

    def api_connect(body) -> tuple[int, dict]:
        ssid, password = _validate_credentials(body)
        try:
            hotspot_up = nmcli.hotspot_active()
        except WifiError as e:
            log.warning("cannot check the hotspot: %s", e)
            return 503, {"error": "cannot check the Wi-Fi state; try again"}
        if not hotspot_up:
            # Only a phone on the setup hotspot may change the Wi-Fi.
            return 409, {"error": "the setup hotspot is not active"}
        if not in_flight.acquire(blocking=False):
            return 409, {"error": "a connection attempt is already in progress"}
        try:
            try:
                marker.touch()  # tells wifi-watch not to restart the hotspot mid-connect
            except OSError as e:
                log.warning("cannot write %s: %s", marker, e.strerror)
            threading.Thread(target=do_connect, args=(ssid, password), name="wifi-connect", daemon=True).start()
        except BaseException:
            in_flight.release()
            raise
        return 202, {"status": "connecting"}

    return {("GET", "/api/wifi/networks"): api_networks, ("POST", "/api/wifi/connect"): api_connect}
