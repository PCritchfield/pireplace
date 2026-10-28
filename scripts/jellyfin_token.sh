#!/usr/bin/env bash
# Print a Jellyfin access token and user Id for the dedicated, non-admin user the
# Pi syncs as (see README, "Music from Jellyfin"):
#   ./scripts/jellyfin_token.sh
# Everything is prompted for. The password is read without echo and travels only
# through stdin (never a command-line argument, so it is not in ps or shell history).
set -euo pipefail

for cmd in curl python3; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "error: '$cmd' is not installed" >&2
        exit 1
    fi
done

read -rp "Jellyfin URL [http://100.88.8.6:8096]: " url
url="${url:-http://100.88.8.6:8096}"
url="${url%/}"
read -rp "Jellyfin user [music-streaming]: " user
user="${user:-music-streaming}"
read -rsp "Password for $user: " password
echo >&2

# printf is a shell builtin, so the password is not on any process's argv.
body="$(printf '%s\0%s' "$user" "$password" | python3 -c '
import json, sys
user, password = sys.stdin.buffer.read().split(b"\0", 1)
print(json.dumps({"Username": user.decode(), "Pw": password.decode()}))
')"
unset password

auth='MediaBrowser Client="pireplace", Device="pireplace", DeviceId="pireplace-sync", Version="1"'
if ! response="$(printf '%s' "$body" | curl -sS --fail --noproxy '*' --max-time 30 \
        -X POST \
        -H "Authorization: $auth" \
        -H 'Content-Type: application/json' \
        --data-binary @- \
        "$url/Users/AuthenticateByName")"; then
    echo "error: Jellyfin refused the login (wrong user or password, or the URL is not reachable)" >&2
    exit 1
fi
unset body

printf '%s' "$response" | python3 -c '
import json, sys
data = json.load(sys.stdin)
print("pireplace_jellyfin_api_key:", data["AccessToken"])
print("pireplace_jellyfin_user_id:", data["User"]["Id"])
'
