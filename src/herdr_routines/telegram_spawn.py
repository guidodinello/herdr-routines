"""Cold-start spawn from Telegram (issue 024).

An *event handler* for inbound Telegram messages — not a scheduler, not a daemon. It
complements the Telegram bot's reply-to-steer flow (issue 023) with a
cold-start path: a ``/run <alias> [harness] [model]`` or ``/start <alias> ...`` message
creates a workspace and starts an agent against the mapped checkout, then replies with the
pane handle the phone steers with.

The handler deliberately reuses the launcher mechanics rather than inventing a layout,
exactly as ``runner.execute_run`` does per ``workspace`` mode: ``worktree`` opens a fresh
branch's worktree in its own workspace (``HerdrClient.worktree_create_full``), ``root``
opens a tab in the checkout itself (``HerdrClient.tab_create``); either yields the pane
``agent start`` needs, and ``HerdrClient.agent_start`` starts the agent with the
shell-less argv ``build_agent_start_args`` builds. Nothing here interpolates a
phone-supplied token into a shell: the alias passes a strict ``[a-z][a-z0-9_-]*`` parse
and an explicit allowlist lookup, the harness must be a known agent kind, the model must
match ``_MODEL_RE``, and each reaches Herdr only as an argv element.

Event-only by contract (spec criterion 10): no lock file, no clock, no history
terminal-state writes. The timer that already schedules jobs stays the sole scheduler.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Protocol

from logger import get_logger

from herdr_routines.config import (
    AGENT_MODEL_FLAGS,
    NAME_RE,
    VALID_AGENT_KINDS,
    TelegramSpawn,
    is_private_chat_id,
)
from herdr_routines.herdr import HerdrCliError, WorktreeInfo
from herdr_routines.runner import _is_agent_name_taken

log = get_logger(__name__)

__all__ = [
    "DEFAULT_START_TIMEOUT_MS",
    "SpawnClient",
    "SpawnCommand",
    "alias_table",
    "handle_job_message",
    "handle_spawn_message",
    "is_authorized",
    "is_job_verb",
    "parse_spawn_command",
]

# The two recognized cold-start verbs. Case-sensitive on purpose: ``/RUN`` is a typo, not
# a command, and silently accepting it would widen the intake surface the spec pins.
_COMMAND_VERBS = frozenset({"/run", "/start"})

# The token shape an alias must satisfy. Anchored and character-class-based, not a
# denylist: anything with a shell metacharacter, a path separator, an uppercase letter, or
# a leading dash is simply not a target.
_TARGET_RE = re.compile(r"[a-z][a-z0-9_-]*")

# A model id as the harnesses spell them (``opus``, ``claude-sonnet-5-5``,
# ``opencode/big-pickle``, ``openrouter/vendor/model:free``). It must start alphanumeric,
# so it can never read as a flag to the harness it is passed to.
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")

_USAGE = "Usage: /run <alias> [harness] [model]"

# The configured-job verb (issue 075): `/job <name>` runs a job that opted in with
# `trigger: {request: true}`. Case-sensitive for the same reason as the cold-start verbs.
_JOB_VERB = "/job"
_JOB_USAGE = "Usage: /job <name>"

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


@dataclass(frozen=True, slots=True)
class SpawnCommand:
    """A parsed cold-start command. ``agent_kind``/``model`` are None when not given, so
    the alias's own defaults apply."""

    target: str
    agent_kind: str | None = None
    model: str | None = None


def _is_spawn_verb(text: object) -> bool:
    """Whether ``text`` starts with a cold-start verb, well-formed or not."""
    parts = text.split() if isinstance(text, str) else []
    return bool(parts) and parts[0] in _COMMAND_VERBS


def parse_spawn_command(text: str) -> SpawnCommand | None:
    """Parse ``/run <alias> [harness] [model]`` (or ``/start ...``), or return None.

    The alias must have the shape ``[a-z][a-z0-9_-]*``. A second token that names a known
    agent kind is the harness, and a third token is then the model; any other second token
    is the model for the alias's own harness, so ``/run x opus`` works too. Anything else
    (no alias, extra tokens, an unknown verb, an ill-shaped alias or model) returns None:
    a loose parse would spawn the wrong checkout, or hand a flag-like or shell-like token
    to the launcher (spec criterion 1/6)."""
    if not isinstance(text, str):
        return None
    parts = text.split()
    if not 2 <= len(parts) <= 4 or parts[0] not in _COMMAND_VERBS:
        return None
    target, *rest = parts[1:]
    if not _TARGET_RE.fullmatch(target):
        return None
    agent_kind = rest.pop(0) if rest and rest[0] in VALID_AGENT_KINDS else None
    if len(rest) > 1:
        return None
    model = rest[0] if rest else None
    if model is not None and not _MODEL_RE.fullmatch(model):
        return None
    return SpawnCommand(target=target, agent_kind=agent_kind, model=model)


def is_authorized(chat_id: int | str, allowed_chat_id: int | str | None) -> bool:
    """Whether ``chat_id`` matches the single configured allowlist value.

    Fails closed: ``None`` or an empty/whitespace ``allowed_chat_id`` authorizes nobody,
    so an unset allowlist can never degrade to allow-all. A non-private (group,
    supergroup or channel) id is refused even when it equals the allowlist value: config
    load already rejects one, and this keeps a group chat — where every member would be
    authorized — out even if a caller bypasses that check. Comparison is on the string form
    because Telegram chat ids arrive as either an int or a string depending on transport."""
    if allowed_chat_id is None:
        return False
    allowed = str(allowed_chat_id).strip()
    if not allowed or not is_private_chat_id(allowed):
        return False
    return str(chat_id).strip() == allowed


def alias_table(spawn: TelegramSpawn) -> dict[str, dict[str, str | None]]:
    """Turn a ``config.TelegramSpawn`` into the plain ``alias -> record`` mapping
    ``handle_spawn_message`` consumes. Kept here so the bridge glue has one canonical
    adapter and the handler stays free of any config import."""
    return {
        a.alias: {
            "repo": str(a.repo),
            "agent_kind": a.agent_kind,
            "workspace": a.workspace,
            "base": a.base,
            "model": a.model,
        }
        for a in spawn.aliases
    }


def handle_spawn_message(
    text: str,
    chat_id: int | str,
    *,
    allowed_chat_id: int | str | None,
    aliases: Mapping[str, Mapping[str, str | None]],
    client: SpawnClient,
    reply: Callable[[str], None],
) -> str:
    """Handle one inbound Telegram message; return (and send) the in-chat reply text.

    Order is the contract: verb, then auth, then parse, then map — a rejected message
    reaches no Herdr call, and only the allowlisted chat learns the usage or the aliases.
    On success the reply carries the harness, model, pane id, and agent name so the
    existing reply-to-steer flow can take over; every failure is surfaced as a short
    in-chat error and never a fabricated terminal state."""
    if not _is_spawn_verb(text):
        # Not a cold-start command: drop silently (no reply, no Herdr call).
        return ""

    if not is_authorized(chat_id, allowed_chat_id):
        return _reply(reply, _REFUSAL)

    valid = ", ".join(sorted(aliases)) or "(none configured)"
    command = parse_spawn_command(text)
    if command is None:
        return _reply(
            reply,
            f"{_USAGE}. Targets: {valid}. A model needs one of: "
            f"{', '.join(sorted(AGENT_MODEL_FLAGS))}.",
        )
    target = command.target

    spec = aliases.get(target)
    if spec is None:
        return _reply(reply, f"Unknown target {target!r}. Valid targets: {valid}.")

    repo = str(spec.get("repo") or "")
    if not repo:
        return _reply(reply, f"Target {target!r} is misconfigured (no repo).")
    alias_kind = str(spec.get("agent_kind") or "claude")
    kind = command.agent_kind or alias_kind
    # The alias's default model belongs to its own harness; picking another harness
    # without a model starts that harness on its default.
    model = command.model or (spec.get("model") if kind == alias_kind else None)
    if model is not None and kind not in AGENT_MODEL_FLAGS:
        return _reply(
            reply,
            f"Harness {kind!r} takes no model. A model needs one of: "
            f"{', '.join(sorted(AGENT_MODEL_FLAGS))}.",
        )
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
        _start_agent(client, name=name, kind=kind, model=model, pane_id=pane_id)
    except (HerdrCliError, OSError) as e:
        if not _is_agent_name_taken(e):
            _best_effort(client, "pane_close", pane_id)
            return _reply(reply, f"spawn failed: {e}")
        # A live agent still holds the name: retry exactly once with a suffixed name
        # rather than letting a cold spawn wedge until a manual reap (issue 051).
        name = f"{base_name}-r1"
        try:
            _start_agent(client, name=name, kind=kind, model=model, pane_id=pane_id)
        except (HerdrCliError, OSError) as retry_error:
            _best_effort(client, "pane_close", pane_id)
            return _reply(reply, f"spawn failed: {retry_error}")

    return _reply(
        reply,
        f"Spawned {name} ({kind}{f' {model}' if model else ''}) on {where}, "
        f"pane {pane_id}. Reply to this message to steer it.",
    )


def is_job_verb(text: object) -> bool:
    """Whether ``text`` starts with ``/job``, well-formed or not."""
    parts = text.split() if isinstance(text, str) else []
    return bool(parts) and parts[0] == _JOB_VERB


def handle_job_message(
    text: str,
    chat_id: int | str,
    *,
    allowed_chat_id: int | str | None,
    requestable: Collection[str],
    launch: Callable[[str], None],
    reply: Callable[[str], None],
) -> str:
    """Handle one inbound ``/job <name>`` message (issue 075); return (and send) the reply.

    Same contract as ``handle_spawn_message``: verb, then the same allowlist/chat-id rule,
    then parse, then lookup, so a rejected message reaches nothing and only the allowlisted
    chat learns which jobs are requestable. ``requestable`` is the names of the enabled jobs
    with ``request: true``; any other job, or an unknown name, is refused. The run itself is
    ``launch(name)``, which the caller points at the shared run path: this module writes no
    history and takes no lock."""
    if not is_job_verb(text):
        return ""

    if not is_authorized(chat_id, allowed_chat_id):
        return _reply(reply, _REFUSAL)

    valid = ", ".join(sorted(requestable)) or "(none configured)"
    parts = text.split()
    if len(parts) != 2 or not NAME_RE.fullmatch(parts[1]):
        return _reply(reply, f"{_JOB_USAGE}. Requestable jobs: {valid}.")
    name = parts[1]
    if name not in requestable:
        return _reply(
            reply,
            f"Job {name!r} is not requestable. Requestable jobs: {valid}.",
        )

    try:
        launch(name)
    except OSError as e:
        return _reply(reply, f"could not start {name}: {e}")
    return _reply(
        reply, f"Started {name} (request). Results arrive via the job's notification."
    )


def _start_agent(
    client: SpawnClient, *, name: str, kind: str, model: str | None, pane_id: str
) -> None:
    client.agent_start(
        name=name,
        kind=kind,
        pane_id=pane_id,
        start_timeout_ms=DEFAULT_START_TIMEOUT_MS,
        model=model,
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
