import shutil
import socket
import subprocess
import threading
import time

import pytest

from pireplace.mpv import MpvClient, MpvError, MpvUnavailable
from tests.fake_mpv import FakeMpv


def wait_for(predicate, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError("condition not met")


def test_get_and_set(tmp_path):
    with FakeMpv(tmp_path / "s") as fake:
        fake.props["volume"] = 40
        c = MpvClient(tmp_path / "s")
        assert c.get("volume") == 40
        c.set("volume", 55)
        assert fake.props["volume"] == 55


def test_events_are_skipped(tmp_path):
    with FakeMpv(tmp_path / "s", emit_events=True) as fake:
        fake.props["pause"] = False
        assert MpvClient(tmp_path / "s").get("pause") is False


def test_unicode_and_quotes_round_trip(tmp_path):
    name = "/m/Bublé – It's \"Beginning\".flac"
    with FakeMpv(tmp_path / "s") as fake:
        MpvClient(tmp_path / "s").command("loadfile", name, "append")
        assert fake.commands[-1] == ["loadfile", name, "append"]


def test_missing_socket_raises_unavailable(tmp_path):
    with pytest.raises(MpvUnavailable):
        MpvClient(tmp_path / "nope", timeout=0.2).get("volume")


def test_error_reply_raises_mpverror(tmp_path):
    with FakeMpv(tmp_path / "s"):
        with pytest.raises(MpvError):
            MpvClient(tmp_path / "s").get("no-such-prop")


def test_property_traffic_is_not_recorded_as_commands(tmp_path):
    with FakeMpv(tmp_path / "s") as fake:
        c = MpvClient(tmp_path / "s")
        c.set("volume", 10)
        c.get("volume")
        assert fake.commands == []
        assert c.command("playlist-next") is None
        assert fake.commands == [["playlist-next"]]


def test_stop_makes_client_unavailable_and_is_idempotent(tmp_path):
    fake = FakeMpv(tmp_path / "s")
    with fake:
        assert MpvClient(tmp_path / "s").command("playlist-clear") is None
    assert not (tmp_path / "s").exists()
    fake.stop()
    with pytest.raises(MpvUnavailable):
        MpvClient(tmp_path / "s", timeout=0.2).get("volume")


def test_refused_connection_raises_unavailable(tmp_path):
    path = tmp_path / "stale"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))  # file exists but nobody is listening
    stale.close()
    with pytest.raises(MpvUnavailable):
        MpvClient(path, timeout=0.2).get("volume")


def test_silent_server_times_out_as_unavailable(tmp_path):
    path = tmp_path / "silent"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
        srv.bind(str(path))
        srv.listen(1)
        with pytest.raises(MpvUnavailable):
            MpvClient(path, timeout=0.2).get("volume")


def test_connection_closed_without_reply_raises_unavailable(tmp_path):
    path = tmp_path / "hangup"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
        srv.bind(str(path))
        srv.listen(1)
        client_result = []

        def call():
            try:
                MpvClient(path, timeout=2).get("volume")
            except MpvUnavailable as e:
                client_result.append(e)

        t = threading.Thread(target=call)
        t.start()
        srv.settimeout(2)
        conn, _ = srv.accept()
        conn.close()
        t.join(3)
    assert len(client_result) == 1


@pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv not installed")
def test_real_mpv(tmp_path):
    sock = tmp_path / "real.sock"
    proc = subprocess.Popen(["mpv", "--idle=yes", "--vo=null", "--ao=null",
                             f"--input-ipc-server={sock}"])
    try:
        wait_for(sock.exists, 5)
        c = MpvClient(sock)
        c.set("volume", 33)
        assert c.get("volume") == 33
    finally:
        proc.terminate()
        proc.wait(5)
