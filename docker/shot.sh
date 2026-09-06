#!/usr/bin/env sh
# Look at the window without putting it on your desktop.
#
#   docker/shot.sh home.png --tab 0
#   docker/shot.sh dark.png --tab 0 --dark
#   docker/shot.sh ribbon.png --tab 2 --ribbon-only
#   docker/shot.sh icons.png --icon-sheet
#
# The picture comes from QWidget.grab() rather than from the screen, because
# Xvfb has no compositor - which also means the 3D view comes out black.  This
# is for the chrome: the ribbon, the panels, the theme.
#
# The source tree is mounted, so changing the UI needs no rebuild.  Xvfb is
# started by hand rather than through xvfb-run, which on some hosts exits
# without ever running the command.
set -e
cd "$(dirname "$0")/.."
docker build -q -f docker/Dockerfile -t stamp-tests .
mkdir -p shots
name="$1"; shift
docker run --rm \
  -v "$(pwd)/src:/stamp/src:ro" \
  -v "$(pwd)/tests:/stamp/tests:ro" \
  -v "$(pwd)/docker:/stamp/docker:ro" \
  -v "$(pwd)/shots:/out" \
  --entrypoint sh stamp-tests -c \
  "Xvfb :99 -screen 0 1920x1200x24 -nolisten tcp & sleep 2
   DISPLAY=:99 uv run --no-sync python docker/shot.py '/out/$name' $*"
