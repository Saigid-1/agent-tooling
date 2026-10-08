#!/bin/sh
set -eu

# Operator-local layout: the mounted volume that holds the trial workspace, and
# the workspace itself. Neither has a default; set both for your machine.
volume="${OPS_TRIAL_VOLUME:?set OPS_TRIAL_VOLUME to the mounted volume that holds the trial workspace}"
workspace="${OPS_TRIAL_WORKSPACE:?set OPS_TRIAL_WORKSPACE to the trial workspace directory on that volume}"
storage="$workspace/kanban-trial-state"
ops="$workspace/navigation-memory-product"
source_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)

if ! /sbin/mount | /usr/bin/grep -F " on $volume (" >/dev/null; then
	echo 'Trial volume is not mounted; Kanban trial will not start.' >&2
	exit 1
fi
if [ ! -d "$storage" ] || [ "$(realpath "$storage")" != "$storage" ]; then
	echo 'Kanban storage root is absent or symlinked; trial will not start.' >&2
	exit 1
fi
if [ ! -d "$ops/.git" ] && [ ! -f "$ops/.git" ]; then
	echo 'Committed legacy checkout is absent; trial will not start.' >&2
	exit 1
fi
if [ ! -f "$source_root/dist/cli.js" ]; then
	echo 'Kanban source build is absent; run npm run build first.' >&2
	exit 1
fi

export KANBAN_STORAGE_ROOT="$storage"
export KANBAN_NO_AUTO_UPDATE=1
export TMPDIR="$workspace/.tmp/test-tmp"
mkdir -p "$TMPDIR"
unset OTEL_TELEMETRY_ENABLED OTEL_EXPORTER_OTLP_ENDPOINT OTEL_METRICS_EXPORTER OTEL_LOGS_EXPORTER
unset OTEL_EXPORTER_OTLP_PROTOCOL OTEL_METRIC_EXPORT_INTERVAL OTEL_EXPORTER_OTLP_HEADERS
unset POSTHOG_KEY POSTHOG_HOST SENTRY_AUTH_TOKEN SENTRY_DSN
cd "$ops"
exec node "$source_root/dist/cli.js" --no-open --host 127.0.0.1 --port 3484
