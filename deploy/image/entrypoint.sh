#!/bin/sh
set -eu
mkdir -p "$TMPDIR" /state/snapshots /state/search
exec kp-agent-tooling "$@"
