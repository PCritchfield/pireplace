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


def _start(stub, extra=None, **kw):
    httpd = make_server(stub, "127.0.0.1", 0, extra=extra, **kw)
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


JSON = {"Content-Type": "application/json"}


def request(srv, method, path, body=None, headers=None):
    """POSTs send Content-Type: application/json unless `headers` says otherwise."""
    if headers is None:
        headers = JSON if method == "POST" else {}
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers)
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


def test_setup_page_served_on_the_hotspot_address():
    s, t = _start(StubFireplace(), hotspot_address="127.0.0.1")
    try:
        code, body, ctype = get_raw(s, "/setup")
        assert code == 200 and "text/html" in ctype and b"<html" in body
    finally:
        _stop(s, t)


def test_setup_page_is_404_off_the_hotspot(srv):
    assert get_raw(srv, "/setup")[0] == 404


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
    s, t = _start(stub, extra={("POST", "/api/x"): handler, ("GET", "/api/x"): handler})
    try:
        assert post(s, "/api/x", {"ssid": "x"}) == (202, {"ok": True})
        assert post(s, "/api/x", None)[0] == 202
        assert get(s, "/api/x") == (202, {"ok": True})
        assert post_raw(s, "/api/x", b"{bad")[0] == 400
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


def test_server_exposes_and_accepts_the_fireplace_lock():
    own = threading.Lock()
    server = make_server(object(), "127.0.0.1", 0, lock=own)
    try:
        assert server.fireplace_lock is own
    finally:
        server.server_close()
    other = make_server(object(), "127.0.0.1", 0)
    try:
        assert other.fireplace_lock is not own
    finally:
        other.server_close()


# ---- Host / Content-Type / hotspot-only guards ---------------------------

def raw_request(port, method, path, host=None, headers=(), body=b""):
    """Like http.client but with full control of the Host header (None = omit it)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True)
        if host is not None:
            conn.putheader("Host", host)
        for k, v in headers:
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        r = conn.getresponse()
        data = r.read()
        return r.status, json.loads(data) if data.startswith(b"{") else data
    finally:
        conn.close()


@pytest.mark.parametrize("host", ["evil.example", "evil.example:80", "pireplace.local.evil.example", "", None])
def test_foreign_or_missing_host_is_403_on_get_and_post(srv, host):
    assert raw_request(srv.port, "GET", "/api/status", host) == (403, {"error": "forbidden host"})
    assert raw_request(srv.port, "GET", "/", host)[0] == 403
    code, _ = raw_request(srv.port, "POST", "/api/music", host,
                          headers=[("Content-Type", "application/json")], body=b'{"action": "pause"}')
    assert code == 403 and srv.stub.calls == []


@pytest.mark.parametrize("host", ["pireplace.local", "PIREPLACE.LOCAL:80", "pireplace", "localhost:8080",
                                  "pireplace.local."])
def test_allowed_hosts_accepted_case_insensitive_with_port(srv, host):
    assert raw_request(srv.port, "GET", "/api/status", host)[0] == 200


def test_host_equal_to_local_ip_is_accepted_even_if_not_listed():
    s, t = _start(StubFireplace(), allowed_hosts=("pireplace.local",))
    try:
        assert raw_request(s.port, "GET", "/api/status", f"127.0.0.1:{s.port}")[0] == 200
        assert raw_request(s.port, "GET", "/api/status", "localhost")[0] == 403
    finally:
        _stop(s, t)


@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded",
                                   "multipart/form-data; boundary=x", "application/jsonp", None])
def test_post_without_json_content_type_is_415(srv, ctype):
    headers = {} if ctype is None else {"Content-Type": ctype}
    code, data, _ = request(srv, "POST", "/api/music", b'{"action": "pause"}', headers=headers)
    assert code == 415 and json.loads(data) == {"error": "content-type must be application/json"}
    assert srv.stub.calls == []


def test_json_content_type_with_charset_is_accepted(srv):
    code, _, _ = request(srv, "POST", "/api/music", b'{"action": "pause"}',
                         headers={"Content-Type": "Application/JSON; charset=utf-8"})
    assert code == 200 and srv.stub.calls == [("pause",)]


def test_hotspot_only_routes_are_404_unless_on_the_hotspot_address():
    def handler(_body):
        return 200, {"ok": True}

    extra = {("GET", "/api/wifi/networks"): handler, ("POST", "/api/wifi/connect"): handler}
    off, t1 = _start(StubFireplace(), extra=extra, hotspot_address="10.42.0.1")
    on, t2 = _start(StubFireplace(), extra=extra, hotspot_address="127.0.0.1")
    try:
        assert get_raw(off, "/api/wifi/networks")[0] == 404
        assert post(off, "/api/wifi/connect", {})[0] == 404
        assert get_raw(off, "/setup")[0] == 404
        assert get(on, "/api/wifi/networks") == (200, {"ok": True})
        assert post(on, "/api/wifi/connect", {}) == (200, {"ok": True})
        assert get_raw(on, "/setup")[0] == 200
        assert get(on, "/api/status")[0] == 200  # normal routes still work there
    finally:
        _stop(off, t1)
        _stop(on, t2)


def test_index_page_posts_send_json_content_type():
    from pireplace.web import INDEX_HTML

    html = INDEX_HTML.read_text()
    assert 'act("POST", "/api/rescan")' not in html  # every POST carries a JSON body


def _index_css():
    from pireplace.web import INDEX_HTML

    html = INDEX_HTML.read_text()
    return html, html.split("<style>")[1].split("</style>")[0]


def test_index_page_has_a_palette_for_every_mood():
    html, css = _index_css()
    themes = html.split("var THEMES = {")[1].split("var THEME =")[0]
    for mood in ("classic-swing", "modern", "soul-motown", "instrumental"):
        assert f'"{mood}": {{ icon:' in themes  # key inside the THEMES table
        assert f'[data-palette="{mood}"]' in css  # CSS override block


def test_index_page_default_palette_is_in_root():
    _, css = _index_css()
    root = css.split(":root {")[1].split("}")[0]
    for var in ("--bg", "--head", "--panel", "--edge", "--text", "--muted", "--btn",
                "--btn-down", "--accent", "--on-accent", "--accent2", "--warn", "--err"):
        assert f"{var}:" in root
    assert "--bg: #1a0f0a" in root  # today's fire palette stays the default


def test_index_page_loads_nothing_external():
    html, _ = _index_css()
    assert "http://" not in html and "https://" not in html
    assert "<link" not in html and "@import" not in html and "src=" not in html


def test_index_page_css_has_no_hardcoded_colours_outside_variables():
    import re

    _, css = _index_css()
    for line in css.splitlines():
        # Colour values may only appear in custom-property declarations.
        rest = re.sub(r"--[\w-]+:[^;}]*;?", "", line)
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", rest), line


def test_index_page_keeps_theme_color_meta():
    html, _ = _index_css()
    assert '<meta name="theme-color" content="#1a0f0a">' in html


def test_index_page_disables_transitions_for_reduced_motion():
    _, css = _index_css()
    block = css.split("@media (prefers-reduced-motion: reduce)")[1]
    assert "transition: none" in block
