"""Thread-backed fake mpv JSON IPC server for tests."""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path


class FakeMpv:
    def __init__(self, socket_path: Path, emit_events: bool = False) -> None:
        self.socket_path = Path(socket_path)
        self.emit_events = emit_events
        self.props: dict = {}
        self.commands: list[list] = []
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "FakeMpv":
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        self._server.listen(8)
        self._server.settimeout(0.05)
        self._thread = threading.Thread(target=self._serve, name="fake-mpv")
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        """Shut down the listener and remove the socket file. Safe to repeat."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        if self._server is not None:
            self._server.close()
            self._server = None
        self.socket_path.unlink(missing_ok=True)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(1.0)
                try:
                    self._handle(conn)
                except OSError:
                    pass

    def _handle(self, conn: socket.socket) -> None:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
        request = json.loads(buf.split(b"\n", 1)[0])
        args = request["command"]
        reply = {"request_id": request.get("request_id"), "error": "success", "data": None}
        if args[0] == "get_property":
            if args[1] in self.props:
                reply["data"] = self.props[args[1]]
            else:
                reply["error"] = "property not found"
        elif args[0] == "set_property":
            self.props[args[1]] = args[2]
        else:
            self.commands.append(args)
        out = b""
        if self.emit_events:
            out += json.dumps({"event": "property-change", "name": "volume"}).encode() + b"\n"
        out += json.dumps(reply).encode() + b"\n"
        conn.sendall(out)
