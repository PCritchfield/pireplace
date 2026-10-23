#!/usr/bin/env bash
# Run pireplace on a laptop: a windowed video mpv, a headless music mpv, and the controller.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

for tool in mpv ffmpeg; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "error: '$tool' is not installed; dev mode needs mpv and ffmpeg (e.g. 'sudo apt install mpv ffmpeg')" >&2
        exit 1
    fi
done

python="python3"
[ -x .venv/bin/python ] && python=".venv/bin/python"

"$root/scripts/make_sample_media.sh"

mkdir -p .dev/run
rm -f .dev/run/video.sock .dev/run/music.sock

pids=()
cleanup() {
    trap - EXIT
    if [ "${#pids[@]}" -gt 0 ]; then
        kill "${pids[@]}" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mpv --idle=yes --loop-playlist=inf --input-ipc-server=.dev/run/video.sock --force-window=yes &
pids+=($!)
mpv --idle=yes --no-video --loop-playlist=inf --input-ipc-server=.dev/run/music.sock &
pids+=($!)

echo "Open http://127.0.0.1:8080"
"$python" -m pireplace serve --config dev/config.toml
