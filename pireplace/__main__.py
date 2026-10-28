"""Command line entry point: python -m pireplace <command>."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from pireplace.config import ConfigError, load_config
from pireplace.controller import Fireplace
from pireplace.mpv import MpvClient
from pireplace.state import State, StateStore
from pireplace.web import make_server
from pireplace.wifi import (
    MAX_PSK,
    MIN_PSK,
    Nmcli,
    connecting_in_progress,
    run_command,
    scan_cache_path,
    setup_routes,
    watch,
)

log = logging.getLogger("pireplace")

RECONCILE_S = 5
LOG_EVERY_S = 60
DEFAULT_CONFIG = "/etc/pireplace/config.toml"

# mpv answers slowly while it opens a multi-GB video; 1 s made /api/status report
# the player unavailable for a few seconds.
PLAYER_TIMEOUT = 3.0


def reconcile_forever(
    fireplace: Fireplace,
    stop: threading.Event,
    lock: threading.Lock,
    interval: float = RECONCILE_S,
    sleep: Callable[[float], bool] | None = None,
) -> None:
    """Call fireplace.reconcile() every `interval` seconds for the life of the server.

    This is what brings a restarted mpv back to a playing fire. `sleep(seconds)` returns
    True when asked to stop; defaults to stop.wait. Each call holds `lock` (shared with
    the web server); the sleep never does. Errors are logged at most once per minute
    and never end the loop.
    """
    sleep = sleep or stop.wait
    last_logged = None
    while True:
        try:
            with lock:
                fireplace.reconcile()
        except Exception:
            now = time.monotonic()
            if last_logged is None or now - last_logged >= LOG_EVERY_S:
                log.exception("reconcile failed, retrying every %ss", interval)
                last_logged = now
        if sleep(interval):
            return


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        config = load_config(Path(args.config))
    except ConfigError as e:
        print(f"pireplace: {e}", file=sys.stderr)
        return 2

    defaults = State(
        mood=config.default_mood,
        music_volume=config.music_volume,
        log_volume=config.log_volume,
        log_muted=False,
        music_playing=config.start_music,
    )
    fireplace = Fireplace(
        config,
        StateStore(config.state_file, defaults),
        MpvClient(config.video_socket, timeout=PLAYER_TIMEOUT),
        MpvClient(config.music_socket, timeout=PLAYER_TIMEOUT),
    )
    try:
        extra = setup_routes(Nmcli(run_command), scan_cache_path(config))
        server = make_server(
            fireplace,
            config.host,
            config.port,
            extra=extra,
            allowed_hosts=config.allowed_hosts,
            hotspot_address=config.hotspot_address,
        )
    except OSError as e:
        print(f"pireplace: cannot listen on {config.host}:{config.port}: {e}", file=sys.stderr)
        return 1

    stop = threading.Event()
    reconciler = threading.Thread(
        target=reconcile_forever, args=(fireplace, stop, server.fireplace_lock), name="reconcile", daemon=True
    )
    reconciler.start()
    log.info("serving on http://%s:%d", config.host, config.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


def cmd_wifi_watch(args: argparse.Namespace) -> int:
    try:
        config = load_config(Path(args.config))
    except ConfigError as e:
        print(f"pireplace: {e}", file=sys.stderr)
        return 2
    if not MIN_PSK <= len(config.hotspot_password) <= MAX_PSK:
        print(
            f"pireplace: [wifi] hotspot_password must be {MIN_PSK}-{MAX_PSK} characters (WPA2)",
            file=sys.stderr,
        )
        return 2
    cache = scan_cache_path(config)
    log.info("watching Wi-Fi; hotspot %r after %ds without a connection", config.hotspot_ssid, config.hotspot_after_s)
    try:
        watch(Nmcli(run_command), config, cache, busy=lambda: connecting_in_progress(cache))
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pireplace")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the control page and drive the players")
    serve.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.toml")
    serve.set_defaults(func=cmd_serve)

    wifi = sub.add_parser("wifi-watch", help="start the setup hotspot when Wi-Fi is down")
    wifi.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.toml")
    wifi.set_defaults(func=cmd_wifi_watch)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
