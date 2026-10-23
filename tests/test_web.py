import http.client
import json
import threading
import time

import pytest

from pireplace.controller import UnknownMood
from pireplace.mpv import MpvUnavailable
from pireplace.web import make_server

STUB_STATUS = {
    "moods": [{"name": "modern", "label": "Modern", "tracks": 3}],
    "mood": "modern",
    "music": {"available": True, "playing": True, "volume": 50, "now_playing": "Song"},
    "log": {"available": True, "volume": 0, "muted": False, "videos": 2},
}


class StubFireplace:
    """Records mutator calls; detects overlapping entry into any method."""

    def __init__(self):
        self.calls = []
        self.raises = {}
        self.delay = 0.0
        self.overlapped = False
        self._active = 0
        self._guard = threading.Lock()

    def raise_on(self, name, exc):
        self.raises[name] = exc

    def _enter(self, name, record, *args):
        with self._guard:
            self._active += 1
            if self._active > 1:
                self.overlapped = True
        try:
            if self.delay:
                time.sleep(self.delay)
            if record:
                self.calls.append((name, *args))
            if name in self.raises:
                raise self.raises[name]
        finally:
            with self._guard:
                self._active -= 1

    def status(self):
        self._enter("status", False)
        return STUB_STATUS

    def set_mood(self, name):
        self._enter("set_mood", True, name)

    def play(self):
        self._enter("play", True)

    def pause(self):
        self._enter("pause", True)

    def next_track(self):
        self._enter("next_track", True)

    def set_music_volume(self, level):
        self._enter("set_music_volume", True, level)

    def set_log_volume(self, level):
        self._enter("set_log_volume", True, level)

    def set_log_muted(self, muted):
        self._enter("set_log_muted", True, muted)

    def rescan(self):
        self._enter("rescan", True)


class Srv:
    def __init__(self, stub, httpd):
        self.stub = stub
        self.httpd = httpd
        self.port = httpd.server_address[1]


def _start(stub, extra=None):
    httpd = make_server(stub, "127.0.0.1", 0, extra=extra)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    return Srv(stub, httpd), t


def _stop(srv, t):
    srv.httpd.shutdown()
    srv.httpd.server_close()
    t.join(timeout=5)


@pytest.fixture
def srv():
    s, t = _start(StubFireplace())
    yield s
    _stop(s, t)


def request(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    try:
        conn.request(method, path, body=body)
        r = conn.getresponse()
        return r.status, r.read(), r.getheader("Content-Type") or ""
    finally:
        conn.close()


def get_raw(srv, path):
    return request(srv, "GET", path)


def get(srv, path):
    code, body, _ = get_raw(srv, path)
    return code, json.loads(body)


def post_raw(srv, path, body):
    code, data, _ = request(srv, "POST", path, body)
    try:
        return code, json.loads(data)
    except ValueError:
        return code, data


def post(srv, path, obj):
    return post_raw(srv, path, None if obj is None else json.dumps(obj).encode())


def test_status_ok(srv):
    assert get(srv, "/api/status") == (200, STUB_STATUS)


def test_status_is_json_content_type(srv):
    assert "application/json" in get_raw(srv, "/api/status")[2]


def test_index_served(srv):
    code, body, ctype = get_raw(srv, "/")
    assert code == 200 and "text/html" in ctype and b"<html" in body


def test_unknown_path_is_404(srv):
    assert get_raw(srv, "/nope")[0] == 404
    assert post(srv, "/api/nope", {})[0] == 404


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/mood", b"{not json"),
        ("/api/mood", b"{}"),
        ("/api/mood", b""),
        ("/api/mood", b'{"mood": 3}'),
        ("/api/mood", b'{"mood": ""}'),
        ("/api/mood", b'["modern"]'),
        ("/api/music", b'{"action": "dance"}'),
        ("/api/music", b"{}"),
        ("/api/volume", b'{"target": "music", "level": "loud"}'),
        ("/api/volume", b'{"target": "music", "level": true}'),
        ("/api/volume", b'{"target": "music", "level": 5.5}'),
        ("/api/volume", b'{"target": "music"}'),
        ("/api/volume", b'{"target": "tv", "level": 5}'),
        ("/api/log", b'{"muted": "yes"}'),
        ("/api/log", b'{"muted": 1}'),
        ("/api/log", b"{}"),
    ],
)
def test_bad_input_is_400(srv, path, body):
    code, resp = post_raw(srv, path, body)
    assert code == 400 and "error" in resp and srv.stub.calls == []


def test_value_error_from_controller_is_400(srv):
    srv.stub.raise_on("set_music_volume", ValueError("out of range"))
    code, resp = post(srv, "/api/volume", {"target": "music", "level": 150})
    assert code == 400 and resp == {"error": "out of range"}


def test_unknown_mood_is_400(srv):
    srv.stub.raise_on("set_mood", UnknownMood("polka"))
    assert post(srv, "/api/mood", {"mood": "polka"})[0] == 400


def test_player_down_is_503(srv):
    srv.stub.raise_on("pause", MpvUnavailable())
    assert post(srv, "/api/music", {"action": "pause"}) == (503, {"error": "player not running"})


def test_unexpected_error_is_500_and_server_survives(srv, caplog):
    srv.stub.raise_on("rescan", RuntimeError("boom"))
    assert post(srv, "/api/rescan", None) == (500, {"error": "internal error"})
    assert get(srv, "/api/status")[0] == 200
    assert any(r.exc_info for r in caplog.records)


def test_oversize_body_is_413(srv):
    assert post_raw(srv, "/api/mood", b"x" * 5000)[0] == 413
    assert srv.stub.calls == []


def test_happy_paths_dispatch(srv):
    results = [
        post(srv, "/api/mood", {"mood": "modern"}),
        post(srv, "/api/volume", {"target": "log", "level": 0}),
        post(srv, "/api/log", {"muted": True}),
    ]
    assert all(r == (200, STUB_STATUS) for r in results)
    assert srv.stub.calls == [("set_mood", "modern"), ("set_log_volume", 0), ("set_log_muted", True)]


def test_music_actions_music_volume_and_rescan(srv):
    for action in ("play", "pause", "next"):
        assert post(srv, "/api/music", {"action": action}) == (200, STUB_STATUS)
    assert post(srv, "/api/volume", {"target": "music", "level": 70})[0] == 200
    assert post(srv, "/api/rescan", None) == (200, STUB_STATUS)
    assert post(srv, "/api/rescan", {})[0] == 200
    assert srv.stub.calls == [
        ("play",),
        ("pause",),
        ("next_track",),
        ("set_music_volume", 70),
        ("rescan",),
        ("rescan",),
    ]


def test_concurrent_requests_do_not_interleave_fireplace_calls(srv):
    srv.stub.delay = 0.01
    results = []

    def hit(i):
        if i % 2:
            results.append(get(srv, "/api/status")[0])
        else:
            results.append(post(srv, "/api/volume", {"target": "music", "level": i})[0])

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * 12
    assert srv.stub.overlapped is False


def test_extra_routes_receive_body_and_bypass_fireplace():
    seen = []

    def handler(body):
        seen.append(body)
        return 202, {"ok": True}

    stub = StubFireplace()
    s, t = _start(stub, extra={("POST", "/api/wifi"): handler, ("GET", "/api/wifi"): handler})
    try:
        assert post(s, "/api/wifi", {"ssid": "x"}) == (202, {"ok": True})
        assert post(s, "/api/wifi", None)[0] == 202
        assert get(s, "/api/wifi") == (202, {"ok": True})
        assert post_raw(s, "/api/wifi", b"{bad")[0] == 400
        assert seen == [{"ssid": "x"}, None, None]
        assert stub.calls == []
    finally:
        _stop(s, t)


def test_extra_handler_exception_is_500():
    def boom(body):
        raise RuntimeError("x")

    s, t = _start(StubFireplace(), extra={("POST", "/api/x"): boom})
    try:
        assert post(s, "/api/x", {}) == (500, {"error": "internal error"})
        assert get(s, "/api/status")[0] == 200
    finally:
        _stop(s, t)


def test_handler_has_socket_timeout(srv):
    assert srv.httpd.RequestHandlerClass.timeout == 10
