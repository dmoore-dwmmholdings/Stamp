#!/usr/bin/env sh
# Run Stamp's tests in a container, so nothing reaches the desktop.
#
#   docker/test.sh                      the whole suite
#   docker/test.sh tests/test_ui.py     one file
#   docker/test.sh -k ribbon            anything pytest takes
set -e
cd "$(dirname "$0")/.."
docker build -f docker/Dockerfile -t stamp-tests .
docker run --rm stamp-tests "$@"
