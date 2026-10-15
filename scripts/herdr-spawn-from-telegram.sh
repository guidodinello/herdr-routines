#!/bin/sh
# Cold-start spawn glue (issue 024): the inbound-message hook the Telegram bridge
# points at. The bridge's poller calls this once per inbound message with the
# sender's chat id and the raw text; the reply the handler prints to stdout is
# what the bridge sends back to the chat. Nothing is spawned unless the message
# is a `/run <alias>` or `/start <alias>` command AND the chat id matches the
# `telegram_spawn.allowed_chat_id` in the jobs config.
#
# Secrets stay out of this script: the bot token lives in the bridge's own
# secrets file, and the allowlisted chat id lives in jobs.yaml / jobs.d (host-
# specific, not committed). This shim only forwards argv.
#
# Resolves the herdr-routines console script the same way the plugin shims do
# (HERDR_ROUTINES_BIN > PATH > usual locations), so the bridge can run it with a
# minimal environment. Input comes from argv (--chat-id/--text) or, when the
# bridge cannot pass argv, the TELEGRAM_CHAT_ID / TELEGRAM_MESSAGE_TEXT env vars.

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
    echo "herdr-routines spawn: cannot find herdr-routines (set HERDR_ROUTINES_BIN)" >&2
    exit 127
fi

if [ "$#" -eq 0 ]; then
    if [ -z "${TELEGRAM_CHAT_ID:-}" ] || [ -z "${TELEGRAM_MESSAGE_TEXT:-}" ]; then
        echo "herdr-routines spawn: pass --chat-id/--text or set" \
            "TELEGRAM_CHAT_ID/TELEGRAM_MESSAGE_TEXT" >&2
        exit 64
    fi
    set -- --chat-id "$TELEGRAM_CHAT_ID" --text "$TELEGRAM_MESSAGE_TEXT"
fi

exec "$bin" spawn-from-telegram "$@"
