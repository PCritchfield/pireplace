"""Thread-backed fake Jellyfin server for tests."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class FakeJellyfin:
    def __init__(self) -> None:
        self.key = "test-api-key-0123456789"
        self.playlists: dict[str, list[dict]] = {}
        self.files: dict[str, bytes] = {}
        self.fail: dict[str, int] = {}  # path (no query) -> status to return
        self.truncate: set[str] = set()  # item Ids: send half the bytes, full Content-Length
        self.html: set[str] = set()  # paths (no query) answering 200 with an HTML page
        self.trickle: dict[str, float] = {}  # item Id -> seconds between 4-byte writes
        self.transcodes: dict[str, bytes] = {}  # item Id -> body of /Audio/<id>/stream.ogg
        self.transcode_type: dict[str, str] = {}  # item Id -> Content-Type override
        self.redirects: dict[str, str] = {}  # path (no query) -> Location of a 302
        self.requests: list[tuple[str, dict]] = []  # (path with query, headers)
        self.url = ""
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "FakeJellyfin":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep test output quiet
                pass

            def _send(self, status: int, body: bytes = b"", ctype: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                fake.requests.append((self.path, dict(self.headers.items())))
                parts = urlsplit(self.path)
                if self.headers.get("Authorization") != f'MediaBrowser Token="{fake.key}"':
                    return self._send(401)
                if parts.path in fake.fail:
                    return self._send(fake.fail[parts.path])
                if parts.path in fake.redirects:
                    self.send_response(302)
                    self.send_header("Location", fake.redirects[parts.path])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if parts.path in fake.html:
                    return self._send(200, b"<html>proxy login</html>", "text/html")
                segs = parts.path.strip("/").split("/")
                if len(segs) == 3 and segs[0] == "Playlists" and segs[2] == "Items":
                    if "userId" not in parse_qs(parts.query):
                        return self._send(400)
                    if segs[1] not in fake.playlists:
                        return self._send(404)
                    body = json.dumps({"Items": fake.playlists[segs[1]]}).encode()
                    return self._send(200, body)
                if len(segs) == 3 and segs[0] == "Items" and segs[2] == "Download":
                    data = fake.files.get(segs[1])
                    if data is None:
                        return self._send(404)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if segs[1] in fake.trickle:
                        try:
                            for i in range(0, len(data), 4):
                                self.wfile.write(data[i:i + 4])
                                self.wfile.flush()
                                time.sleep(fake.trickle[segs[1]])
                        except OSError:  # the client gave up
                            pass
                        return
                    self.wfile.write(data[: len(data) // 2] if segs[1] in fake.truncate else data)
                    return
                if len(segs) == 3 and segs[0] == "Audio" and segs[2] == "stream.ogg":
                    data = fake.transcodes.get(segs[1])
                    if data is None:
                        return self._send(404)
                    self.send_response(200)
                    self.send_header("Content-Type", fake.transcode_type.get(segs[1], "audio/ogg"))
                    if segs[1] in fake.truncate:  # only a declared length reveals a short body
                        self.send_header("Content-Length", str(len(data)))
                    self.end_headers()  # otherwise streamed: no Content-Length, ends on close
                    try:
                        if segs[1] in fake.trickle:
                            for i in range(0, len(data), 4):
                                self.wfile.write(data[i:i + 4])
                                self.wfile.flush()
                                time.sleep(fake.trickle[segs[1]])
                        else:
                            self.wfile.write(data[: len(data) // 2] if segs[1] in fake.truncate else data)
                    except OSError:  # the client gave up
                        pass
                    return
                self._send(404)

        self._server = _Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, name="fake-jellyfin"
        )
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        """Shut down the server. Safe to repeat."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join()
            self._thread = None
