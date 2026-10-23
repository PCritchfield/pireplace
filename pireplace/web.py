"""Stdlib HTTP JSON API and the phone control page."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from pireplace.controller import Fireplace, UnknownMood
from pireplace.mpv import MpvUnavailable

log = logging.getLogger(__name__)

Handler = Callable[[dict | None], tuple[int, dict]]

MAX_BODY = 4096
_DRAIN_LIMIT = 1 << 20  # read and discard at most this much of an oversize body
INDEX_HTML = Path(__file__).parent / "static" / "index.html"

MUSIC_ACTIONS = ("play", "pause", "next")
VOLUME_TARGETS = ("music", "log")


class BadRequest(Exception):
    """Client sent something invalid; the message is shown to the client."""


class _BodyTooLarge(Exception):
    pass


def _require_object(body: dict | None) -> dict:
    if not isinstance(body, dict):
        raise BadRequest("expected a JSON object body")
    return body


def _int_level(value: object) -> int:
    if type(value) is not int:
        raise BadRequest("level must be an integer")
    return value


def make_server(
    fireplace: Fireplace,
    host: str,
    port: int,
    extra: dict[tuple[str, str], Handler] | None = None,
    lock: threading.Lock | None = None,
) -> ThreadingHTTPServer:
    """Build (but do not start) the HTTP server. Use port 0 for an ephemeral port.

    Fireplace is not thread-safe: every call must hold `lock`. It is also exposed
    as `server.fireplace_lock` for other threads that call into the Fireplace.
    """
    lock = lock or threading.Lock()
    extra = dict(extra or {})

    def with_status(fn: Callable, *args) -> tuple[int, dict]:
        with lock:
            fn(*args)
            return 200, fireplace.status()

    def api_status(_body) -> tuple[int, dict]:
        with lock:
            return 200, fireplace.status()

    def api_mood(body) -> tuple[int, dict]:
        mood = _require_object(body).get("mood")
        if not isinstance(mood, str) or not mood:
            raise BadRequest("mood must be a non-empty string")
        return with_status(fireplace.set_mood, mood)

    def api_music(body) -> tuple[int, dict]:
        action = _require_object(body).get("action")
        actions = {"play": fireplace.play, "pause": fireplace.pause, "next": fireplace.next_track}
        if not isinstance(action, str) or action not in actions:
            raise BadRequest(f"action must be one of {', '.join(MUSIC_ACTIONS)}")
        return with_status(actions[action])

    def api_volume(body) -> tuple[int, dict]:
        body = _require_object(body)
        target = body.get("target")
        if not isinstance(target, str) or target not in VOLUME_TARGETS:
            raise BadRequest(f"target must be one of {', '.join(VOLUME_TARGETS)}")
        level = _int_level(body.get("level"))
        setter = fireplace.set_music_volume if target == "music" else fireplace.set_log_volume
        return with_status(setter, level)

    def api_log(body) -> tuple[int, dict]:
        muted = _require_object(body).get("muted")
        if not isinstance(muted, bool):
            raise BadRequest("muted must be a boolean")
        return with_status(fireplace.set_log_muted, muted)

    def api_rescan(_body) -> tuple[int, dict]:
        return with_status(fireplace.rescan)

    routes: dict[tuple[str, str], Handler] = {
        ("GET", "/api/status"): api_status,
        ("POST", "/api/mood"): api_mood,
        ("POST", "/api/music"): api_music,
        ("POST", "/api/volume"): api_volume,
        ("POST", "/api/log"): api_log,
        ("POST", "/api/rescan"): api_rescan,
    }
    routes.update(extra)
    no_body = {("POST", "/api/rescan")}  # body ignored, may be empty

    class RequestHandler(BaseHTTPRequestHandler):
        timeout = 10  # a client that stalls mid-request must not pin a thread forever

        def log_message(self, format, *args):  # silence per-request stderr logging
            log.debug("%s - %s", self.address_string(), format % args)

        def _send(self, code: int, payload: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _read_body(self) -> bytes:
            raw = self.headers.get("Content-Length")
            if raw is None:
                return b""
            try:
                length = int(raw)
            except ValueError:
                raise BadRequest("invalid Content-Length") from None
            if length < 0:
                raise BadRequest("invalid Content-Length")
            if length > MAX_BODY:
                # Drain so the client reads our response instead of a connection reset.
                self.rfile.read(min(length, _DRAIN_LIMIT))
                raise _BodyTooLarge
            return self.rfile.read(length)

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            try:
                if method == "GET" and path in ("/", "/index.html"):
                    self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
                    return
                handler = routes.get((method, path))
                if handler is None:
                    self._send_json(404, {"error": "not found"})
                    return
                raw = self._read_body() if method == "POST" else b""
                body = None
                if raw.strip() and (method, path) not in no_body:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        raise BadRequest("malformed JSON") from None
                code, payload = handler(body)
                self._send_json(code, payload)
            except _BodyTooLarge:
                self._send_json(413, {"error": "request body too large"})
            except UnknownMood as e:
                self._send_json(400, {"error": f"unknown mood: {e}"})
            except (BadRequest, ValueError) as e:
                self._send_json(400, {"error": str(e)})
            except MpvUnavailable:
                self._send_json(503, {"error": "player not running"})
            except Exception:
                log.exception("unhandled error serving %s %s", method, path)
                self._send_json(500, {"error": "internal error"})

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.daemon_threads = True
    server.fireplace_lock = lock
    return server
