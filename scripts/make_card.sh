#!/usr/bin/env bash
# Build build/card.html: a printable card with two QR codes.
#   HOTSPOT_PASSWORD='the password you gave Ansible' make card
# The password comes from the environment only and ends up in build/ (gitignored).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="$root/build/card.html"

if ! command -v qrencode >/dev/null 2>&1; then
    echo "error: 'qrencode' is not installed (e.g. 'sudo apt install qrencode')" >&2
    exit 1
fi
if [ -z "${HOTSPOT_PASSWORD:-}" ]; then
    echo "error: set HOTSPOT_PASSWORD to the hotspot_password you gave Ansible (e.g. HOTSPOT_PASSWORD=... make card)" >&2
    exit 1
fi

ssid="pireplace-setup"
remote_url="http://pireplace.local"
setup_url="http://10.42.0.1/setup"

esc() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g' -e "s/'/\&#39;/g"; }
# An <img> with a data: URI avoids dealing with the XML header qrencode emits.
qr() { qrencode -t SVG -m 2 -o - "$1" | base64 -w0; }

remote_qr="$(qr "$remote_url")"
setup_qr="$(qr "$setup_url")"

mkdir -p "$root/build"
umask 077
cat > "$out" <<HTML
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Fireplace card</title>
<style>
  body { font-family: sans-serif; margin: 2rem; }
  .panels { display: flex; gap: 2rem; }
  .panel { flex: 1; border: 2px solid #000; padding: 1rem; text-align: center; }
  img { width: 100%; max-width: 16rem; }
  code { font-size: 1.1em; }
  @media print { body { margin: 0; } }
</style>
</head>
<body>
<div class="panels">
  <div class="panel">
    <h1>Fireplace remote</h1>
    <img src="data:image/svg+xml;base64,${remote_qr}" alt="QR code for $(esc "$remote_url")">
    <p>Join the home Wi-Fi, then scan or open<br><code>$(esc "$remote_url")</code></p>
  </div>
  <div class="panel">
    <h1>Wi-Fi setup</h1>
    <img src="data:image/svg+xml;base64,${setup_qr}" alt="QR code for $(esc "$setup_url")">
    <p>Join Wi-Fi <code>$(esc "$ssid")</code><br>Password <code>$(esc "$HOTSPOT_PASSWORD")</code><br>then open <code>$(esc "$setup_url")</code></p>
  </div>
</div>
</body>
</html>
HTML
echo "wrote $out"
