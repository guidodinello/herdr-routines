#!/bin/sh
# Thin forwarder for the `run` action in herdr-plugin.toml (spec.md).
#
# Plugin v1 action commands are fixed argv arrays with no parameter
# interpolation, so the job name arrives via HERDR_PLUGIN_RUN_JOB (or argv[1]).
# Like herdr-plugin-status.sh, the herdr-routines console script is resolved
# without assuming a full login PATH (HERDR_ROUTINES_BIN > PATH > usual
# locations). Exec preserves output and exit codes; a missing job fails
# loudly instead of silently no-oping, and malformed job names reach the CLI
# untouched and surface its normal ConfigError/no-such-job exit.

set -eu

job=${1:-${HERDR_PLUGIN_RUN_JOB:-}}
if [ -z "$job" ]; then
    echo "herdr-routines plugin: no job given; set HERDR_PLUGIN_RUN_JOB=<job>" >&2
    exit 64
fi

resolve_herdr_routines() {
    if [ -n "${HERDR_ROUTINES_BIN:-}" ]; then
        printf '%s\n' "$HERDR_ROUTINES_BIN"
        return 0
    fi
    if command -v herdr-routines >/dev/null 2>&1; then
        command -v herdr-routines
        return 0
    fi
    for dir in "${HOME:+$HOME/.local/bin}" /usr/local/bin /usr/bin; do
        if [ -x "$dir/herdr-routines" ]; then
            printf '%s\n' "$dir/herdr-routines"
            return 0
        fi
    done
    return 1
}

if ! bin=$(resolve_herdr_routines); then
    echo "herdr-routines plugin: cannot find herdr-routines (set HERDR_ROUTINES_BIN)" >&2
    exit 127
fi

exec "$bin" run "$job"
