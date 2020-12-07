#! /bin/bash

# Absolute path to this script, e.g. /home/user/bin/foo.sh
SCRIPT=$(readlink -f "$0")
# Absolute path this script is in, thus /home/user/bin
SCRIPTPATH=$(dirname "$SCRIPT")
# set here the path to the directory containing your videos
VIDEOPATH="$SCRIPTPATH/video"

# get rid of the cursor so we don’t see it when videos are running
setterm -cursor on

# set video player based on hardware. Currently AMD or ARM(pi)
# TODO set to a map and use a single if block
if [ $(uname -m) = "x86_64" ]; then
  SERVICE="mpv"
  OPTIONS="--fs"
fi

if [ $(uname -m) = "armv7l" ]; then
  SERVICE="omxplayer"
  OPTIONS="-r"
fi

./pbstart

# Until interupted continue to play the available videos on a loop
while true; do
  if ps ax | grep -v grep | grep $SERVICE >/dev/null; then
    sleep 1
  else
    for entry in $VIDEOPATH/*; do
      clear
      $SERVICE $OPTIONS $entry >/dev/null
      if [ $? = "4" ]; then
        ./pbstop
        kill $(ps aux | fgrep -v grep | fgrep 'fireplace' | awk '{print $2}')
      fi
    done
  fi
done
