#!/usr/bin/env bash
# Generate small synthetic sample media under .dev/media. Idempotent: skips existing files.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
media="$root/.dev/media"

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "error: ffmpeg is required to generate sample media (e.g. 'sudo apt install ffmpeg')" >&2
    exit 1
fi

ff() { ffmpeg -nostdin -hide_banner -loglevel error -y "$@"; }

video="$media/videos/fire-test.mp4"
if [ ! -e "$video" ]; then
    mkdir -p "$(dirname "$video")"
    echo "creating $video"
    ff -f lavfi -i "testsrc2=size=1280x720:rate=30:duration=20" \
       -f lavfi -i "anoisesrc=color=brown:amplitude=0.1:duration=20" \
       -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$video.tmp.mp4"
    mv "$video.tmp.mp4" "$video"
fi

# tone <file> <frequency-hz>
tone() {
    local file="$1" freq="$2"
    [ -e "$file" ] && return 0
    mkdir -p "$(dirname "$file")"
    echo "creating $file"
    ff -f lavfi -i "sine=frequency=$freq:duration=30" -c:a libmp3lame -q:a 6 "$file.tmp.mp3"
    mv "$file.tmp.mp3" "$file"
}

modern="$media/music/modern"
tone "$modern/tone-220.mp3" 220
tone "$modern/tone-330.mp3" 330
tone "$modern/Don't Stop Believin’ – café.mp3" 440

swing="$media/music/classic-swing/Some Artist/Some Album"
tone "$swing/01 low.mp3" 262
tone "$swing/02 mid.mp3" 392
tone "$swing/03 Sérénade d'été.mp3" 523
