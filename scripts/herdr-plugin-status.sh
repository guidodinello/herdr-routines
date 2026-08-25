#!/bin/sh
# Thin forwarder for the `status` action in herdr-plugin.toml (spec.md).
#
# Herdr may launch plugin commands with a minimal environment, so resolving
# the herdr-routines console script cannot assume a full login PATH: try
# HERDR_ROUTINES_BIN, then PATH, then the usual console-script locations.
# Exec preserves the CLI's output and exit codes; a total resolution failure
# exits 127 loudly instead of silently no-oping.

set -eu

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

exec "$bin" status
