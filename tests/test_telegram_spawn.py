"""Acceptance tests for issue 024: cold-start spawn from Telegram.

Stage 3 (tests only) — the feature does not exist yet, so every test here is
expected to FAIL until stage 4 implements it. Each test asserts the behaviour
its spec criterion describes, so it fails for the right reason now and can
pass once the feature exists.

Spec: docs/pipeline/runs/20261014T050000Z/spec.md v2, ## Acceptance criteria.

Contract pinned for the stage-4 implementation:

- New module ``herdr_routines.telegram_spawn`` exposing:
  - ``parse_spawn_command(text: str) -> str | None`` — pure. ``/run <t>`` or
    ``/start <t>`` with exactly one target token matching
    ``[a-z][a-z0-9_-]*`` returns ``<t>``; anything else (zero tokens, extra
    args, non-command text, unknown verb, ill-shaped token) returns None.
  - ``handle_spawn_message(text, chat_id, *, allowed_chat_id, aliases, client,
    reply) -> str`` — event handler. ``aliases`` maps alias -> ``{"repo":
    <path>, "agent_kind": <kind>, "workspace": <mode>}``. ``client`` is a
    HerdrClient-like with ``workspace_create(*, cwd, label=None, ...)`` and
    ``agent_start(*, name, kind, pane_id, start_timeout_ms, ...)``. ``reply``
    is a ``Callable[[str], None]`` sending the in-chat reply; the handler
    also returns that reply text.
  - ``is_authorized(chat_id, allowed_chat_id) -> bool`` if a pure auth helper
    exists (probed, not required — criteria 2/3 are pinned at handler level).
- Config: ``load_config`` accepts a top-level ``telegram_spawn:`` block with
  ``allowed_chat_id`` plus an ``aliases:`` list of
  ``{alias, repo, agent_kind?, workspace?}`` records, and raises ConfigError
  when ``allowed_chat_id`` is missing/empty, an alias repo does not exist or
  is not a git checkout, or an alias is duplicated.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from herdr_routines.herdr import HerdrCliError


def _spawn_module():
    """Import the stage-4 module. Raises ModuleNotFoundError before it exists."""
    return importlib.import_module("herdr_routines.telegram_spawn")


def _aliases(
    repo: Path | str, *, name: str = "nightly-audit"
) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "repo": str(repo),
            "agent_kind": "claude",
            "workspace": "worktree",
        }
    }


class FakeSpawnClient:
    """HerdrClient-like double recording workspace/agent calls.

    ``workspace_create`` returns a workspace id; ``tab_create`` a pane id.
    ``fail_at`` raises on that method ("workspace_create" or "agent_start").
    ``collide_once`` fails the first ``agent_start`` with ``agent_name_taken``
    (code + ``is already used`` message, matching runner._is_agent_name_taken).
    """

    def __init__(
        self,
        *,
        workspace_id: str = "w7",
        pane_id: str = "w7:p1",
        fail_at: str | None = None,
        fail_with: Exception | None = None,
        collide_once: bool = False,
    ) -> None:
        self.workspace_id = workspace_id
        self.pane_id = pane_id
        self.fail_at = fail_at
        self.fail_with = fail_with
        self.collide_once = collide_once
        self.workspace_calls: list[dict[str, Any]] = []
        self.agent_calls: list[dict[str, Any]] = []
        self.tab_calls: list[dict[str, Any]] = []

    def workspace_create(
        self, *, cwd: str, label: str | None = None, env: dict[str, str] | None = None
    ) -> str:
        self.workspace_calls.append({"cwd": cwd, "label": label, "env": env})
        if self.fail_at == "workspace_create":
            raise self.fail_with or HerdrCliError("herdr down", exit_code=1)
        return self.workspace_id

    def tab_create(self, *, cwd: str, label: str | None = None) -> str:
        self.tab_calls.append({"cwd": cwd, "label": label})
        return self.pane_id

    def agent_start(
        self,
        *,
        name: str,
        kind: str,
        pane_id: str,
        start_timeout_ms: int,
        model: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.agent_calls.append(
            {
                "name": name,
                "kind": kind,
                "pane_id": pane_id,
                "start_timeout_ms": start_timeout_ms,
                "model": model,
                "session_id": session_id,
            }
        )
        if self.fail_at == "agent_start":
            raise self.fail_with or HerdrCliError("herdr down", exit_code=1)
        if self.collide_once and len(self.agent_calls) == 1:
            raise HerdrCliError(
                f"agent name {name!r} is already used",
                exit_code=1,
                error_body={"code": "agent_name_taken"},
            )


def _handle(
    mod: Any,
    text: str,
    chat_id: Any,
    *,
    allowed_chat_id: Any,
    aliases: dict[str, dict[str, Any]],
    client: FakeSpawnClient,
) -> tuple[str, list[str]]:
    sent: list[str] = []
    result = mod.handle_spawn_message(
        text,
        chat_id,
        allowed_chat_id=allowed_chat_id,
        aliases=aliases,
        client=client,
        reply=sent.append,
    )
    assert isinstance(result, str)
    assert sent, "handler must send exactly one in-chat reply"
    assert result == sent[-1]
    return result, sent


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    return path


def _spawn_config_text(
    repo: Path | str, *, allowed: Any = 12345, extra_alias: str = ""
) -> str:
    return (
        "version: 1\n"
        "jobs:\n"
        "  - name: nightly-audit\n"
        "    cron: '0 3 * * *'\n"
        f"    repo: {repo}\n"
        "telegram_spawn:\n"
        f"  allowed_chat_id: {allowed}\n"
        "  aliases:\n"
        "    - alias: nightly\n"
        f"      repo: {repo}\n"
        "      agent_kind: claude\n"
        "      workspace: worktree\n" + extra_alias
    )


# 1. Intake contract: strict one-token parse, no Herdr call on reject. --------


def test_spawn_parses_cold_start_command() -> None:
    mod = _spawn_module()
    parse = mod.parse_spawn_command

    assert parse("/run nightly-audit") == "nightly-audit"
    assert parse("/start myrepo") == "myrepo"
    assert parse("  /run nightly-audit  ") == "nightly-audit"

    for bad in (
        "/run",  # zero tokens
        "/start",  # zero tokens
        "/run a b",  # extra args
        "/start a b c",  # extra args
        "hello world",  # non-command text
        "",  # empty
        "run nightly-audit",  # missing slash
        "/stop nightly-audit",  # unknown verb
        "/RUN nightly-audit",  # verb is case-sensitive
    ):
        assert parse(bad) is None, f"{bad!r} must not parse"

    # Rejected messages reach no Herdr call.
    client = FakeSpawnClient()
    repo = _git_repo(Path("/tmp") / "x")
    for bad in ("/run", "/run a b", "hello world", ""):
        sent: list[str] = []
        mod.handle_spawn_message(
            bad,
            12345,
            allowed_chat_id=12345,
            aliases=_aliases(repo),
            client=client,
            reply=sent.append,
        )
    assert client.workspace_calls == []
    assert client.agent_calls == []


# 2. Auth gate: wrong chat_id drops without Herdr calls or mapping leaks. ------


def test_spawn_rejects_unauthorized_chat_id() -> None:
    mod = _spawn_module()
    repo = _git_repo(Path("/tmp") / "y")
    aliases = _aliases(repo)
    client = FakeSpawnClient()

    result, _ = _handle(
        mod,
        "/run nightly-audit",
        999,
        allowed_chat_id=12345,
        aliases=aliases,
        client=client,
    )

    assert client.workspace_calls == []
    assert client.agent_calls == []
    assert client.tab_calls == []
    # Generic refusal at most: never reveal the mapping or valid aliases.
    for alias in aliases:
        assert alias not in result
    assert "12345" not in result


# 3. Fail-closed allowlist: unset/empty refuses; validate errors. ---------------


def test_spawn_fails_closed_without_allowlist() -> None:
    mod = _spawn_module()
    repo = _git_repo(Path("/tmp") / "z")
    aliases = _aliases(repo)

    for missing in (None, ""):
        client = FakeSpawnClient()
        sent: list[str] = []
        mod.handle_spawn_message(
            "/run nightly-audit",
            12345,
            allowed_chat_id=missing,
            aliases=aliases,
            client=client,
            reply=sent.append,
        )
        assert client.workspace_calls == []
        assert client.agent_calls == []
        assert sent, (
            "fail-closed refusal must still reply (or drop silently with a reply)"
        )

    # Pure helper, when present, matches the same fail-closed truth table.
    if hasattr(mod, "is_authorized"):
        assert mod.is_authorized(12345, 12345) is True
        assert mod.is_authorized(999, 12345) is False
        assert mod.is_authorized(12345, None) is False
        assert mod.is_authorized(12345, "") is False

    # `validate` errors instead of treating unset as allow-all.
    from herdr_routines.config import ConfigError, load_config

    assert mod  # keep module binding live for the pre-feature failure mode
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        cfg_path = Path(d) / "jobs.yaml"
        cfg_path.write_text(_spawn_config_text(repo, allowed=""))
        with pytest.raises(ConfigError, match="allowed_chat_id"):
            load_config(cfg_path)


# 4. Unknown target: helpful reply listing valid aliases. -----------------------


def test_spawn_unknown_target_lists_aliases() -> None:
    mod = _spawn_module()
    repo = _git_repo(Path("/tmp") / "w")
    aliases = _aliases(repo)
    client = FakeSpawnClient()

    result, _ = _handle(
        mod,
        "/run nosuch",
        12345,
        allowed_chat_id=12345,
        aliases=aliases,
        client=client,
    )

    assert client.workspace_calls == []
    assert client.agent_calls == []
    for alias in aliases:
        assert alias in result, "helpful reply must list the valid aliases"


# 5. Alias-map validation at load. ----------------------------------------------


def test_spawn_alias_map_validation(tmp_path: Path) -> None:
    from herdr_routines.config import ConfigError, load_config

    good_repo = _git_repo(tmp_path / "good-repo")

    # Control: a valid map loads.
    ok_path = tmp_path / "ok.yaml"
    ok_path.write_text(_spawn_config_text(good_repo))
    cfg = load_config(ok_path)
    assert cfg.job("nightly-audit") is not None

    # Unknown repo (path does not exist) is rejected.
    bad_path = tmp_path / "unknown.yaml"
    bad_path.write_text(
        _spawn_config_text(good_repo).replace(
            f"    - alias: nightly\n      repo: {good_repo}\n",
            "    - alias: nightly\n      repo: /does/not/exist\n",
        )
    )
    with pytest.raises(ConfigError, match="(?i)repo|unknown|git"):
        load_config(bad_path)

    # Non-git path (exists, no .git) is rejected.
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    nongit_path = tmp_path / "nongit.yaml"
    nongit_path.write_text(
        _spawn_config_text(good_repo).replace(
            f"    - alias: nightly\n      repo: {good_repo}\n",
            f"    - alias: nightly\n      repo: {plain}\n",
        )
    )
    with pytest.raises(ConfigError, match="(?i)repo|git"):
        load_config(nongit_path)

    # Duplicate aliases are rejected.
    dup_path = tmp_path / "dup.yaml"
    dup_path.write_text(
        _spawn_config_text(good_repo) + "    - alias: nightly\n"
        f"      repo: {good_repo}\n"
        "      agent_kind: claude\n"
        "      workspace: worktree\n"
    )
    with pytest.raises(ConfigError, match="(?i)duplicate"):
        load_config(dup_path)


# 6. Untrusted token: validated shape, shell-less argv only. --------------------


def test_spawn_target_never_shell_interpolated() -> None:
    mod = _spawn_module()
    parse = mod.parse_spawn_command

    for evil in (
        "/run foo;rm -rf /",
        "/run $(evil)",
        "/run `evil`",
        "/run a|b",
        "/run ../../etc",
        "/run Foo",
        "/run -h",
        "/run foo_bar$baz",
    ):
        assert parse(evil) is None, f"{evil!r} must not parse as a target"

    # Even past the parser, a malicious token never reaches Herdr or a shell.
    client = FakeSpawnClient()
    repo = _git_repo(Path("/tmp") / "v")
    sent: list[str] = []
    mod.handle_spawn_message(
        "/run foo;rm -rf /",
        12345,
        allowed_chat_id=12345,
        aliases=_aliases(repo),
        client=client,
        reply=sent.append,
    )
    assert client.workspace_calls == []
    assert client.agent_calls == []

    # The success path itself is shell-less: token only as argv elements.
    from herdr_routines.herdr import build_agent_start_args

    argv = build_agent_start_args(
        name="rt-nightly-audit-123",
        kind="claude",
        pane_id="w7:p1",
        start_timeout_ms=120_000,
    )
    assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)

    source = (
        Path(__file__)
        .resolve()
        .parent.parent.joinpath("src/herdr_routines/telegram_spawn.py")
        .read_text()
    )
    assert "shell=True" not in source
    assert "os.system" not in source


# 7. Success: workspace_create + agent start with mapped values, handle reply. -


def test_spawn_success_replies_with_pane_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _spawn_module()
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    repo = _git_repo(tmp_path / "repo")
    client = FakeSpawnClient()

    result, _ = _handle(
        mod,
        "/run nightly-audit",
        12345,
        allowed_chat_id=12345,
        aliases=_aliases(repo),
        client=client,
    )

    assert len(client.workspace_calls) == 1
    assert client.workspace_calls[0]["cwd"] == str(repo)
    assert len(client.agent_calls) == 1
    started = client.agent_calls[0]
    assert started["kind"] == "claude"
    assert started["pane_id"], "agent start needs an existing pane"
    name = started["name"]
    assert re.fullmatch(r"rt-nightly-audit-\d+", name), name
    # Reply carries the handle the phone steers with: workspace/pane id + agent name.
    assert "w7" in result
    assert name in result


# 8. Name collision: retry with a unique suffixed name. -------------------------


def test_spawn_handles_agent_name_collision(tmp_path: Path) -> None:
    mod = _spawn_module()
    repo = _git_repo(tmp_path / "repo2")
    client = FakeSpawnClient(collide_once=True)

    result, _ = _handle(
        mod,
        "/run nightly-audit",
        12345,
        allowed_chat_id=12345,
        aliases=_aliases(repo),
        client=client,
    )

    assert len(client.agent_calls) == 2, "must retry agent start exactly once"
    first, second = client.agent_calls[0]["name"], client.agent_calls[1]["name"]
    assert first != second, "retry must use a unique suffixed name"
    assert re.fullmatch(r"rt-nightly-audit-\S+", second), second
    assert second in result, "reply must carry the retried agent name"


# 9. Failures: short in-chat error, no fabricated terminal history state. -------


def test_spawn_failure_replies_with_short_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _spawn_module()
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    repo = _git_repo(tmp_path / "repo3")

    appended: list[Any] = []

    import herdr_routines.history as history_mod

    monkeypatch.setattr(
        history_mod, "append", lambda path, record: appended.append(record)
    )

    from herdr_routines.history import TERMINAL_STATES

    for fail_at in ("workspace_create", "agent_start"):
        appended.clear()
        client = FakeSpawnClient(fail_at=fail_at)
        result, _ = _handle(
            mod,
            "/run nightly-audit",
            12345,
            allowed_chat_id=12345,
            aliases=_aliases(repo),
            client=client,
        )
        assert result.strip(), "failure must reply in-chat with the short error"
        assert "herdr down" in result
        assert "w7:p1" not in result, "no pane handle exists on failure"

    for record in appended:
        state = getattr(record, "state", None)
        if state is None and isinstance(record, dict):
            state = record.get("state")
        assert state not in TERMINAL_STATES, (
            f"spawn failures must never fabricate a terminal history state, got {state!r}"
        )

    # The default history file itself carries no terminal state either.
    history_path = tmp_path / "state" / "history.jsonl"
    if history_path.exists():
        for line in history_path.read_text().splitlines():
            if line.strip():
                assert json.loads(line).get("state") not in TERMINAL_STATES


# 10. Event-only: no lock, no terminal history logic, no second clock. -----------


def test_spawn_has_no_scheduler_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _spawn_module()
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    repo = _git_repo(tmp_path / "repo4")
    client = FakeSpawnClient()

    _handle(
        mod,
        "/run nightly-audit",
        12345,
        allowed_chat_id=12345,
        aliases=_aliases(repo),
        client=client,
    )

    assert not (tmp_path / "tick.lock").exists()
    assert not (tmp_path / "state" / "tick.lock").exists()
    history_path = tmp_path / "state" / "history.jsonl"
    if history_path.exists():
        from herdr_routines.history import TERMINAL_STATES

        for line in history_path.read_text().splitlines():
            if line.strip():
                assert json.loads(line).get("state") not in TERMINAL_STATES

    source = (
        Path(__file__)
        .resolve()
        .parent.parent.joinpath("src/herdr_routines/telegram_spawn.py")
        .read_text()
    )
    assert "tick.lock" not in source
    assert "from herdr_routines.tick" not in source
    assert "herdr_routines.tick import" not in source
    assert "systemd" not in source
    assert "systemctl" not in source
    assert "default_lock_path" not in source
