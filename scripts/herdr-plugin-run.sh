#!/bin/sh
# Thin forwarder for the `run` action in herdr-plugin.toml (spec.md).
#
# Herdr action commands are fixed argv arrays with no parameter interpolation,
# so the job name arrives via HERDR_PLUGIN_RUN_JOB (or argv[1]). The shim execs
# `herdr-routines run <job>` so output and exit codes match the CLI exactly; a
# missing job fails loudly instead of silently no-oping. Malformed job names
# reach the CLI untouched and surface its normal ConfigError/no-such-job exit.

set -eu

job=${1:-${HERDR_PLUGIN_RUN_JOB:-}}
if [ -z "$job" ]; then
    echo "herdr-routines plugin: no job given; set HERDR_PLUGIN_RUN_JOB=<job>" >&2
    exit 64
fi

exec herdr-routines run "$job"
