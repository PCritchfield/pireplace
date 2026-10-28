"""scripts/jellyfin_token.sh against a fake /Users/AuthenticateByName."""

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "jellyfin_token.sh"
AUTH = 'MediaBrowser Client="pireplace", Device="pireplace", DeviceId="pireplace-sync", Version="1"'


class _Auth(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).seen.append((self.path, dict(self.headers.items()), body))
        reply = json.dumps({"AccessToken": "tok-123", "User": {"Id": "f" * 32, "Name": "pireplace"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *a):
        pass


@pytest.fixture
def auth_server():
    handler = type("H", (_Auth,), {"seen": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", handler.seen
    server.shutdown()
    server.server_close()
    t.join()


def test_script_parses_and_reads_secrets_silently():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    text = SCRIPT.read_text()
    assert "read -rs" in text
    assert "--data-binary @-" in text  # the body (with the password) goes through stdin
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(SCRIPT)], check=True)


def test_script_prints_token_and_user_id(auth_server):
    if not shutil.which("curl"):
        pytest.skip("curl not installed")
    url, seen = auth_server
    password = 'p"w \\ secret'
    out = subprocess.run(
        ["bash", str(SCRIPT)], input=f"{url}\n\n{password}\n",  # empty user: the default
        capture_output=True, text=True, check=True, timeout=30,
    )
    path, headers, body = seen[0]
    assert path == "/Users/AuthenticateByName"
    assert headers["Authorization"] == AUTH
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"Username": "music-streaming", "Pw": password}
    assert "tok-123" in out.stdout and "f" * 32 in out.stdout
    assert password not in out.stdout and password not in out.stderr
