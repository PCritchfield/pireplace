"""Minimal mpv JSON IPC client: one unix-socket connection per call."""
from __future__ import annotations

import itertools
import json
import socket
import time
from pathlib import Path
from typing import Any


class MpvUnavailable(Exception):
    """Socket missing, refused, closed, or timed out."""


class MpvError(Exception):
    """mpv replied with an error status."""


class MpvClient:
    def __init__(self, socket_path: Path, timeout: float = 1.0) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self._ids = itertools.count(1)

    def command(self, *args: Any) -> Any:
        request_id = next(self._ids)
        payload = json.dumps({"command": list(args), "request_id": request_id}) + "\n"
        deadline = time.monotonic() + self.timeout
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(str(self.socket_path))
                sock.sendall(payload.encode("utf-8"))
                reply = self._read_reply(sock, request_id, deadline)
        except OSError as e:  # includes timeout, refused, missing, broken pipe
            raise MpvUnavailable(str(e)) from e
        if reply.get("error") != "success":
            raise MpvError(str(reply.get("error")))
        return reply.get("data")

    def get(self, prop: str) -> Any:
        return self.command("get_property", prop)

    def set(self, prop: str, value: Any) -> None:
        self.command("set_property", prop, value)

    @staticmethod
    def _read_reply(sock: socket.socket, request_id: int, deadline: float) -> dict:
        buf = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for mpv reply")
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("mpv closed the connection")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("request_id") == request_id:
                    return msg
