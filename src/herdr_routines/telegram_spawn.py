"""Cold-start spawn from Telegram (issue 024).

An *event handler* for inbound Telegram messages — not a scheduler, not a daemon. It
complements the Telegram bot's reply-to-steer flow (issue 023) with a
cold-start path: a bare ``/run <job>`` or ``/start <repo>`` message creates a workspace
and starts an agent against the mapped checkout, then replies with the pane handle the
phone steers with.

The handler deliberately reuses the launcher mechanics rather than inventing a layout,
exactly as ``runner.execute_run`` does per ``workspace`` mode: ``worktree`` opens a fresh
branch's worktree in its own workspace (``HerdrClient.worktree_create_full``), ``root``
opens a tab in the checkout itself (``HerdrClient.tab_create``); either yields the pane
``agent start`` needs, and ``HerdrClient.agent_start`` starts the agent with the
shell-less argv ``build_agent_start_args`` builds. Nothing here interpolates the
phone-supplied token into a shell: the token passes a strict ``[a-z][a-z0-9_-]*`` parse
and an explicit allowlist lookup, and reaches Herdr only as an argv element.

Event-only by contract (spec criterion 10): no lock file, no clock, no history
terminal-state writes. The timer that already schedules jobs stays the sole scheduler.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from logger import get_logger

from herdr_routines.herdr import HerdrCliError, WorktreeInfo
from herdr_routines.runner import _is_agent_name_taken

log = get_logger(__name__)

__all__ = [
    "DEFAULT_START_TIMEOUT_MS",
    "SpawnClient",
    "alias_table",
    "handle_spawn_message",
    "is_authorized",
    "parse_spawn_command",
]

# The two recognized cold-start verbs. Case-sensitive on purpose: ``/RUN`` is a typo, not
# a command, and silently accepting it would widen the intake surface the spec pins.
_COMMAND_VERBS = frozenset({"/run", "/start"})

# The token shape an alias must satisfy. Anchored and character-class-based, not a
# denylist: anything with a shell metacharacter, a path separator, an uppercase letter, or
# a leading dash is simply not a target.
_TARGET_RE = re.compile(r"[a-z][a-z0-9_-]*")

# How long ``agent start`` may take. A cold spawn has no per-job config to inherit
# ``start_timeout_ms`` from, so it uses the same default a plain routine job gets.
DEFAULT_START_TIMEOUT_MS = 120_000

# Agent names are capped at 32 chars (herdr's NAME_RE). ``rt-`` + the alias + ``-`` + a
# 10-digit epoch + a possible ``-r1`` retry suffix must fit, so the alias part is cut to
# this many chars (aliases themselves may be up to 24).
_NAME_ALIAS_CHARS = 13

# The generic auth refusal. Deliberately free of the mapping and the allowed chat id so a
# rejected sender cannot learn either (spec criterion 2).
_REFUSAL = "Sorry, this chat is not authorized to spawn sessions."


class SpawnClient(Protocol):
    """The slice of ``HerdrClient`` the spawn orchestration needs.

    Structural on purpose: the real client and the tier-1 ``FakeSpawnClient`` both satisfy
    it without either depending on this module."""

    def worktree_create_full(
        self, *, cwd: str, branch: str, base: str, label: str | None = None
    ) -> WorktreeInfo: ...

    def tab_create(self, *, cwd: str, label: str | None = None) -> str: ...

    def agent_start(
        self,
        *,
        name: str,
        kind: str,
        pane_id: str,
        start_timeout_ms: int,
        model: str | None = None,
        session_id: str | None = None,
    ) -> None: ...


def parse_spawn_command(text: str) -> str | None:
    """Return the single target token of a cold-start command, or None.

    ``/run <t>`` and ``/start <t>`` parse when they carry exactly one token of the shape
    ``[a-z][a-z0-9_-]*``. Zero tokens, extra args, non-command text, an unknown verb, and
    an ill-shaped token all return None — a loose parse would spawn the wrong checkout, or
    hand a shell metacharacter to the launcher (spec criterion 1/6)."""
    if not isinstance(text, str):
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    verb, token = parts
    if verb not in _COMMAND_VERBS:
        return None
    if not _TARGET_RE.fullmatch(token):
        return None
    return token


def is_authorized(chat_id: Any, allowed_chat_id: Any) -> bool:
    """Whether ``chat_id`` matches the single configured allowlist value.

    Fails closed: ``None`` or an empty/whitespace ``allowed_chat_id`` authorizes nobody,
    so an unset allowlist can never degrade to allow-all. Comparison is on the string form
    because Telegram chat ids arrive as either an int or a string depending on transport."""
    if allowed_chat_id is None:
        return False
    allowed = str(allowed_chat_id).strip()
    if not allowed:
        return False
    return str(chat_id).strip() == allowed


def alias_table(spawn: Any) -> dict[str, dict[str, str]]:
    """Turn a ``config.TelegramSpawn`` into the plain ``alias -> record`` mapping
    ``handle_spawn_message`` consumes. Kept here so the bridge glue has one canonical
    adapter and the handler stays free of any config import."""
    return {
        a.alias: {
            "repo": str(a.repo),
            "agent_kind": a.agent_kind,
            "workspace": a.workspace,
            "base": a.base,
        }
        for a in spawn.aliases
    }


def handle_spawn_message(
    text: str,
    chat_id: Any,
    *,
    allowed_chat_id: Any,
    aliases: Mapping[str, Mapping[str, Any]],
    client: SpawnClient,
    reply: Callable[[str], None],
) -> str:
    """Handle one inbound Telegram message; return (and send) the in-chat reply text.

    Order is the contract: parse, then auth, then map — a rejected message reaches no
    Herdr call. On success the reply carries the workspace id, pane id, and agent name so
    the existing reply-to-steer flow can take over; every failure is surfaced as a short
    in-chat error and never a fabricated terminal state."""
    target = parse_spawn_command(text)
    if target is None:
        # Not a cold-start command: drop silently (no reply, no Herdr call).
        return ""

    if not is_authorized(chat_id, allowed_chat_id):
        return _reply(reply, _REFUSAL)

    spec = aliases.get(target)
    if spec is None:
        valid = ", ".join(sorted(aliases))
        return _reply(
            reply,
            f"Unknown target {target!r}. Valid targets: {valid or '(none configured)'}.",
        )

    repo = str(spec.get("repo") or "")
    if not repo:
        return _reply(reply, f"Target {target!r} is misconfigured (no repo).")
    kind = str(spec.get("agent_kind") or "claude")
    mode = str(spec.get("workspace") or "worktree")
    base = str(spec.get("base") or "main")
    stamp = int(time.time())
    base_name = f"rt-{target[:_NAME_ALIAS_CHARS]}-{stamp}"

    # Same per-mode layout as runner.execute_run: a worktree run never edits the
    # checkout the scheduler syncs; a root run works in the checkout itself.
    try:
        if mode == "worktree":
            info = client.worktree_create_full(
                cwd=repo,
                branch=f"auto/spawn-{target}-{stamp}",
                base=base,
                label=base_name,
            )
            pane_id = info.root_pane_id
            where = f"branch {info.branch} (worktree {info.path})"
        else:
            pane_id = client.tab_create(cwd=repo, label=base_name)
            where = f"checkout {repo}"
    except (HerdrCliError, OSError) as e:
        return _reply(reply, f"spawn failed: {e}")

    name = base_name
    try:
        _start_agent(client, name=name, kind=kind, pane_id=pane_id)
    except (HerdrCliError, OSError) as e:
        if not _is_agent_name_taken(e):
            _best_effort(client, "pane_close", pane_id)
            return _reply(reply, f"spawn failed: {e}")
        # A live agent still holds the name: retry exactly once with a suffixed name
        # rather than letting a cold spawn wedge until a manual reap (issue 051).
        name = f"{base_name}-r1"
        try:
            _start_agent(client, name=name, kind=kind, pane_id=pane_id)
        except (HerdrCliError, OSError) as retry_error:
            _best_effort(client, "pane_close", pane_id)
            return _reply(reply, f"spawn failed: {retry_error}")

    return _reply(
        reply,
        f"Spawned {name} on {where}, pane {pane_id}. Reply to this message to steer it.",
    )


def _start_agent(client: SpawnClient, *, name: str, kind: str, pane_id: str) -> None:
    client.agent_start(
        name=name,
        kind=kind,
        pane_id=pane_id,
        start_timeout_ms=DEFAULT_START_TIMEOUT_MS,
    )


def _reply(reply: Callable[[str], None], text: str) -> str:
    reply(text)
    return text


def _best_effort(client: SpawnClient, method: str, arg: str) -> None:
    """Call an optional cleanup method if the client exposes it, swallowing any error.

    The tier-1 fake carries only the three spawning methods, and a leaked pane on a failed
    spawn is a documented manual-GC concern — never a reason to mask the original error."""
    fn = getattr(client, method, None)
    if fn is None:
        return
    try:
        fn(arg)
    except Exception as e:  # noqa: BLE001 — cleanup must never replace the reported failure
        log.warning("spawn cleanup %s(%s) failed: %s", method, arg, e)
