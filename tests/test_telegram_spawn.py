"""Acceptance tests for issue 024: cold-start spawn from Telegram.

Stage 3 (tests only) — the feature does not exist yet, so every test here is
expected to FAIL until stage 4 implements it. Each test asserts the behaviour
its spec criterion describes, so it fails for the right reason now and can
pass once the feature exists.

Spec: docs/pipeline/runs/20261014T050000Z/spec.md v2, ## Acceptance criteria.

Issue 083 (spec docs/pipeline/runs/20261110T050000Z/spec.md v2) rewrites the alias
fixtures to construct ``config.SpawnAlias`` records and the two issue-065
source-contract tests to pin ``dict[str, SpawnAlias]`` / ``Mapping[str,
SpawnAlias]``. These tests were authored red in e76685a, before the typed-record
propagation landed in e5aac9f; they pass against the implementation on this
branch, and each asserts exactly the behaviour its acceptance criterion
describes.

Contract pinned for the stage-4 implementation:

- New module ``herdr_routines.telegram_spawn`` exposing:
  - ``parse_spawn_command(text: str) -> str | None`` — pure. ``/run <t>`` or
    ``/start <t>`` with exactly one target token matching
    ``[a-z][a-z0-9_-]*`` returns ``<t>``; anything else (zero tokens, extra
    args, non-command text, unknown verb, ill-shaped token) returns None.
  - ``handle_spawn_message(text, chat_id, *, allowed_chat_id, aliases, client,
    reply) -> str`` — event handler. ``aliases`` maps alias ->
    ``config.SpawnAlias`` record (issue 083: config's validated values travel as
    the record itself, never re-flattened to dicts). ``client`` is a
    HerdrClient-like with ``worktree_create_full(*, cwd, branch, base, label=None)``
    (``workspace: worktree``) or ``tab_create(*, cwd, label=None)`` (``workspace:
    root``), and ``agent_start(*, name, kind, pane_id, start_timeout_ms, ...)``. ``reply``
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
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from herdr_routines.config import SpawnAlias, TelegramSpawn
from herdr_routines.herdr import HerdrCliError, WorktreeInfo


def _spawn_module():
    """Import the stage-4 module. Raises ModuleNotFoundError before it exists."""
    return importlib.import_module("herdr_routines.telegram_spawn")


def _aliases(repo: Path | str, *, name: str = "nightly-audit") -> dict[str, SpawnAlias]:
    """One alias as config's own frozen record (issue 083): every default lives in
    ``SpawnAlias``'s fields, and ``repo`` stays a ``Path`` until the handler's
    ``str(spec.repo)`` conversion at the ``SpawnClient`` protocol boundary."""
    return {name: SpawnAlias(alias=name, repo=Path(repo))}


class FakeSpawnClient:
    """HerdrClient-like double recording workspace/agent calls.

    ``worktree_create_full`` (recorded in ``workspace_calls``) returns a worktree whose
    root pane is ``pane_id``; ``tab_create`` returns ``pane_id`` too.
    ``fail_at`` raises on that method ("worktree_create_full" or "agent_start").
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

    def worktree_create_full(
        self, *, cwd: str, branch: str, base: str, label: str | None = None
    ) -> WorktreeInfo:
        self.workspace_calls.append(
            {"cwd": cwd, "branch": branch, "base": base, "label": label}
        )
        if self.fail_at == "worktree_create_full":
            raise self.fail_with or HerdrCliError("herdr down", exit_code=1)
        return WorktreeInfo(
            path=f"{cwd}/.worktrees/{branch}", branch=branch, root_pane_id=self.pane_id
        )

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
    aliases: Mapping[str, SpawnAlias],
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


def test_spawn_parses_cold_start_command(tmp_path: Path) -> None:
    mod = _spawn_module()
    parse = mod.parse_spawn_command

    assert parse("/run nightly-audit").target == "nightly-audit"
    assert parse("/start myrepo").target == "myrepo"
    assert parse("  /run nightly-audit  ").target == "nightly-audit"

    for bad in (
        "/run",  # zero tokens
        "/start",  # zero tokens
        "/run a opus b",  # a model is the last token
        "/start a claude b c",  # extra args
        "/run a -m",  # a model never reads as a flag
        "/run a $(id)",  # nor as shell
        "hello world",  # non-command text
        "",  # empty
        "run nightly-audit",  # missing slash
        "/stop nightly-audit",  # unknown verb
        "/RUN nightly-audit",  # verb is case-sensitive
    ):
        assert parse(bad) is None, f"{bad!r} must not parse"

    # Rejected messages reach no Herdr call.
    client = FakeSpawnClient()
    repo = _git_repo(tmp_path / "x")
    for bad in ("/run", "/run a -m", "hello world", ""):
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


def test_spawn_rejects_unauthorized_chat_id(tmp_path: Path) -> None:
    mod = _spawn_module()
    repo = _git_repo(tmp_path / "y")
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


def test_spawn_fails_closed_without_allowlist(tmp_path: Path) -> None:
    mod = _spawn_module()
    repo = _git_repo(tmp_path / "z")
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


def test_spawn_unknown_target_lists_aliases(tmp_path: Path) -> None:
    mod = _spawn_module()
    repo = _git_repo(tmp_path / "w")
    # Issue 083 (AC 7): the reply path consumes alias_table's new record values —
    # the mapping is alias_table's own output, SpawnAlias values unmodified.
    spawn_cfg = TelegramSpawn(
        allowed_chat_id="12345",
        aliases=(SpawnAlias(alias="nightly-audit", repo=repo),),
    )
    aliases = mod.alias_table(spawn_cfg)
    assert all(isinstance(a, SpawnAlias) for a in aliases.values()), (
        "alias_table must carry config's SpawnAlias records as its values"
    )
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

    # `base` (the branch a worktree spawn starts from) defaults to main and flows into
    # the handler's alias table as the record's own field (issue 083: attribute
    # access, no dict re-flattening); an empty one is rejected. Routed through the
    # module seam so the pre-implementation dict return keeps mypy green while this
    # attribute read is pytest-red.
    mod = _spawn_module()

    assert cfg.telegram_spawn is not None
    assert mod.alias_table(cfg.telegram_spawn)["nightly"].base == "main"
    based_path = tmp_path / "based.yaml"
    based_path.write_text(_spawn_config_text(good_repo) + "      base: develop\n")
    based = load_config(based_path).telegram_spawn
    assert based is not None and mod.alias_table(based)["nightly"].base == "develop"
    empty_base_path = tmp_path / "empty-base.yaml"
    empty_base_path.write_text(_spawn_config_text(good_repo) + "      base: ''\n")
    with pytest.raises(ConfigError, match="base"):
        load_config(empty_base_path)


# 6. Untrusted token: validated shape, shell-less argv only. --------------------


def test_spawn_target_never_shell_interpolated(tmp_path: Path) -> None:
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
    repo = _git_repo(tmp_path / "v")
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

    # workspace: worktree runs on a fresh branch's worktree, never in the checkout.
    assert len(client.workspace_calls) == 1
    assert client.workspace_calls[0]["cwd"] == str(repo)
    branch = client.workspace_calls[0]["branch"]
    assert re.fullmatch(r"auto/spawn-nightly-audit-\d+", branch), branch
    assert client.workspace_calls[0]["base"] == "main"
    assert client.tab_calls == []
    assert len(client.agent_calls) == 1
    started = client.agent_calls[0]
    assert started["kind"] == "claude"
    assert started["pane_id"] == "w7:p1", "agent starts in the worktree's root pane"
    name = started["name"]
    assert re.fullmatch(r"rt-nightly-audit-\d+", name), name
    # Reply carries the handle the phone steers with: pane id, branch and agent name.
    assert "w7:p1" in result
    assert branch in result
    assert name in result


def test_spawn_root_mode_opens_a_tab_in_the_checkout(tmp_path: Path) -> None:
    mod = _spawn_module()
    repo = _git_repo(tmp_path / "repo-root")
    aliases = _aliases(repo)
    # SpawnAlias is frozen: a variant mode is a new record, as config would build it.
    aliases["nightly-audit"] = replace(aliases["nightly-audit"], workspace="root")
    client = FakeSpawnClient()

    result, _ = _handle(
        mod,
        "/run nightly-audit",
        12345,
        allowed_chat_id=12345,
        aliases=aliases,
        client=client,
    )

    assert client.workspace_calls == [], "root mode creates no worktree"
    assert [c["cwd"] for c in client.tab_calls] == [str(repo)]
    assert client.agent_calls[0]["pane_id"] == "w7:p1"
    assert "w7:p1" in result

    # Same dispatch structure (issue 083 AC 5): a worktree-mode alias still reaches
    # worktree_create_full and never a tab — the else-branch stays the structural
    # fallback, with no /workspace == "root" exhaustive-match rewiring.
    worktree_client = FakeSpawnClient()
    _handle(
        mod,
        "/run nightly-audit",
        12345,
        allowed_chat_id=12345,
        aliases=_aliases(repo),
        client=worktree_client,
    )
    assert len(worktree_client.workspace_calls) == 1
    assert worktree_client.workspace_calls[0]["cwd"] == str(repo)
    assert worktree_client.workspace_calls[0]["base"] == "main"
    assert worktree_client.tab_calls == []


def test_spawn_agent_name_fits_the_32_char_cap(tmp_path: Path) -> None:
    """Aliases may be 24 chars; the agent name (and its -r1 retry) must still fit
    herdr's 32-char name cap, or a long alias could never start."""
    mod = _spawn_module()
    repo = _git_repo(tmp_path / "repo-long")
    long_alias = "a-very-long-alias-name-x"  # 24 chars
    client = FakeSpawnClient(collide_once=True)

    _handle(
        mod,
        f"/run {long_alias}",
        12345,
        allowed_chat_id=12345,
        aliases=_aliases(repo, name=long_alias),
        client=client,
    )

    names = [c["name"] for c in client.agent_calls]
    assert len(names) == 2
    assert all(len(n) <= 32 for n in names), names


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

    for fail_at in ("worktree_create_full", "agent_start"):
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


# Harness and model from the message (/run <alias> [harness] [model]). -----------


def test_spawn_parses_optional_harness_and_model() -> None:
    parse = _spawn_module().parse_spawn_command

    def parsed(text: str) -> tuple[str, str | None, str | None]:
        cmd = parse(text)
        assert cmd is not None, f"{text!r} must parse"
        return cmd.target, cmd.agent_kind, cmd.model

    assert parsed("/run x opencode") == ("x", "opencode", None)
    assert parsed("/run x opus") == ("x", None, "opus")
    assert parsed("/run x claude sonnet") == ("x", "claude", "sonnet")
    assert parsed("/run x opencode opencode/big-pickle") == (
        "x",
        "opencode",
        "opencode/big-pickle",
    )


def test_spawn_starts_the_requested_harness_and_model(tmp_path: Path) -> None:
    mod = _spawn_module()
    client = FakeSpawnClient()

    result, _ = _handle(
        mod,
        "/run nightly-audit opencode opencode/big-pickle",
        12345,
        allowed_chat_id=12345,
        aliases=_aliases(_git_repo(tmp_path / "repo")),
        client=client,
    )

    started = client.agent_calls[0]
    assert (started["kind"], started["model"]) == ("opencode", "opencode/big-pickle")
    assert "(opencode opencode/big-pickle)" in result
    # The bot finds the pane to steer by this exact phrase.
    assert ", pane w7:p1. Reply to this message" in result


def test_spawn_alias_model_applies_only_to_its_own_harness(tmp_path: Path) -> None:
    mod = _spawn_module()
    aliases = _aliases(_git_repo(tmp_path / "repo"))
    aliases["nightly-audit"] = replace(aliases["nightly-audit"], model="opus")

    for text, expected in (
        ("/run nightly-audit", ("claude", "opus")),
        ("/run nightly-audit sonnet", ("claude", "sonnet")),
        ("/run nightly-audit codex", ("codex", None)),
    ):
        client = FakeSpawnClient()
        _handle(mod, text, 12345, allowed_chat_id=12345, aliases=aliases, client=client)
        started = client.agent_calls[0]
        assert (started["kind"], started["model"]) == expected, text


def test_spawn_rejects_a_model_for_a_harness_without_a_model_flag(
    tmp_path: Path,
) -> None:
    mod = _spawn_module()
    client = FakeSpawnClient()

    result, _ = _handle(
        mod,
        "/run nightly-audit gemini pro",
        12345,
        allowed_chat_id=12345,
        aliases=_aliases(_git_repo(tmp_path / "repo")),
        client=client,
    )

    assert client.workspace_calls == [] and client.agent_calls == []
    assert "'gemini' takes no model" in result


def test_spawn_malformed_command_gets_usage_only_in_the_allowed_chat(
    tmp_path: Path,
) -> None:
    mod = _spawn_module()
    aliases = _aliases(_git_repo(tmp_path / "repo"))

    usage, _ = _handle(
        mod,
        "/run",
        12345,
        allowed_chat_id=12345,
        aliases=aliases,
        client=FakeSpawnClient(),
    )
    assert usage.startswith("Usage: /run <alias> [harness] [model]")
    assert "nightly-audit" in usage

    refused, _ = _handle(
        mod,
        "/run",
        999,
        allowed_chat_id=12345,
        aliases=aliases,
        client=FakeSpawnClient(),
    )
    assert "nightly-audit" not in refused and "Usage" not in refused


def test_spawn_alias_model_is_validated_and_reaches_the_handler(
    tmp_path: Path,
) -> None:
    """Issue 083 AC 6 (non-blocking): the one None-carrying field round-trips —
    ``SpawnAlias.model`` (``str | None``) reaches the handler as ``None`` when
    omitted and as the configured string per alias otherwise, through the new
    attribute reads (no dict re-flattening in between)."""
    from herdr_routines.config import ConfigError, load_config

    mod = _spawn_module()
    repo = _git_repo(tmp_path / "repo")
    cfg_path = tmp_path / "jobs.yaml"

    cfg_path.write_text(_spawn_config_text(repo) + "      model: opus\n")
    spawn = load_config(cfg_path).telegram_spawn
    assert spawn is not None
    table = mod.alias_table(spawn)
    assert table["nightly"].model == "opus"

    bad = _spawn_config_text(repo).replace("agent_kind: claude", "agent_kind: gemini")
    cfg_path.write_text(bad + "      model: pro\n")
    with pytest.raises(ConfigError, match="'model' is not supported"):
        load_config(cfg_path)

    # The configured model reaches agent_start on the alias's own harness...
    client = FakeSpawnClient()
    _handle(
        mod,
        "/run nightly",
        12345,
        allowed_chat_id=12345,
        aliases=table,
        client=client,
    )
    assert client.agent_calls[0]["model"] == "opus"

    # ...and an omitted model arrives as None, not as a re-coerced default.
    bare_path = tmp_path / "bare.yaml"
    bare_path.write_text(_spawn_config_text(repo))
    bare = load_config(bare_path).telegram_spawn
    assert bare is not None
    bare_table = mod.alias_table(bare)
    assert bare_table["nightly"].model is None
    client = FakeSpawnClient()
    _handle(
        mod,
        "/run nightly",
        12345,
        allowed_chat_id=12345,
        aliases=bare_table,
        client=client,
    )
    assert client.agent_calls[0]["model"] is None


@pytest.mark.parametrize(
    ("chat_id", "allowed", "expected"),
    [
        (12345, 12345, True),
        (12345, "12345", True),
        ("12345", 12345, True),
        ("12345", "12345", True),
        (" 12345 ", "12345", True),
        (12345, " 12345 ", True),
        (999, 12345, False),
        (12345, None, False),
        (12345, "", False),
        (12345, "   ", False),
        ("", "", False),
    ],
)
def test_is_authorized_compares_int_and_str_ids_and_fails_closed(
    chat_id: int | str, allowed: int | str | None, expected: bool
) -> None:
    from herdr_routines.telegram_spawn import is_authorized

    assert is_authorized(chat_id, allowed) is expected


# Issue 178: only a private 1:1 chat may be allowlisted (the id is the only identity the
# bridge passes in, so a group id would authorize every member). ------------------------


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        (123456789, "123456789"),
        ("'123456789'", "123456789"),
        (2**40 - 1, "1099511627775"),
    ],
)
def test_private_chat_id_is_accepted_at_load(
    tmp_path: Path, allowed: Any, expected: str
) -> None:
    from herdr_routines.config import load_config

    repo = _git_repo(tmp_path / "repo")
    cfg_path = tmp_path / "jobs.yaml"
    cfg_path.write_text(_spawn_config_text(repo, allowed=allowed))
    spawn = load_config(cfg_path).telegram_spawn
    assert spawn is not None
    assert spawn.allowed_chat_id == expected


@pytest.mark.parametrize(
    "allowed",
    [-123456789, -1001234567890, "'-1001234567890'", 0, "true", "abc", "'+123'"],
)
def test_non_private_chat_id_is_rejected_at_load(tmp_path: Path, allowed: Any) -> None:
    from herdr_routines.config import ConfigError, load_config

    repo = _git_repo(tmp_path / "repo")
    cfg_path = tmp_path / "jobs.yaml"
    cfg_path.write_text(_spawn_config_text(repo, allowed=allowed))
    with pytest.raises(ConfigError, match="private 1:1 chat"):
        load_config(cfg_path)


@pytest.mark.parametrize("group_id", [-123456789, "-1001234567890"])
def test_is_authorized_refuses_group_chat_even_when_it_matches(
    group_id: int | str,
) -> None:
    from herdr_routines.telegram_spawn import is_authorized

    assert is_authorized(group_id, group_id) is False


def test_spawn_from_group_chat_reaches_no_herdr_call(tmp_path: Path) -> None:
    mod = _spawn_module()
    repo = _git_repo(tmp_path / "g")
    client = FakeSpawnClient()
    sent: list[str] = []
    mod.handle_spawn_message(
        "/run nightly",
        -1001234567890,
        allowed_chat_id=-1001234567890,
        aliases=_aliases(repo),
        client=client,
        reply=sent.append,
    )
    assert client.workspace_calls == []
    assert client.agent_calls == []


# Issue 065 (spec docs/pipeline/runs/20261020T050000Z/spec.md v2): type the remaining
# `Any` boundaries. The four narrowed signatures are enforced as source contracts;
# alias behavior itself is anchored by the existing tests above and in test_auto_fix.py


def test_alias_table_spawn_param_is_telegram_spawn() -> None:
    """AC 1 (blocking) + AC 8 (non-blocking), issue 083 (spec
    docs/pipeline/runs/20261110T050000Z/spec.md v2): ``alias_table`` declares
    ``spawn: TelegramSpawn`` and returns ``dict[str, SpawnAlias]`` — config's own
    frozen record, imported at runtime from ``herdr_routines.config`` (no
    ``TYPE_CHECKING`` guard, no cycle), with the pass-through body
    ``{a.alias: a for a in spawn.aliases}`` so values travel unmodified and the
    ``WorkspaceMode`` Literal stays attached to every entry. The ``cli.py`` call
    site (``aliases=alias_table(spawn)``) is unchanged, and the fixtures (the
    ``_aliases`` helper and every literal alias fixture) construct
    ``SpawnAlias(...)`` records, so ``repo`` arrives as a ``Path`` and leaves the
    handler as ``str(spec.repo)`` — keeping ``SpawnClient.worktree_create_full``/
    ``tab_create``'s ``cwd: str`` clean under strict typing."""
    import ast
    import inspect
    from typing import get_type_hints

    from herdr_routines.config import SpawnAlias, TelegramSpawn

    mod = _spawn_module()

    # Runtime-resolvable hints: SpawnAlias must be imported at runtime (a
    # TYPE_CHECKING-only import would raise NameError here), so no import cycle.
    hints = get_type_hints(mod.alias_table)
    assert hints["spawn"] is TelegramSpawn
    assert hints["return"] == dict[str, SpawnAlias]

    src = inspect.getsource(mod.alias_table)
    assert src.startswith(
        "def alias_table(spawn: TelegramSpawn) -> dict[str, SpawnAlias]:"
    )
    # Pass-through body: values are the records themselves, never re-flattened.
    assert "return {a.alias: a for a in spawn.aliases}" in src

    record = SpawnAlias(alias="nightly", repo=Path("/somewhere/x"))
    table = mod.alias_table(TelegramSpawn(allowed_chat_id="12345", aliases=(record,)))
    assert table == {"nightly": record}
    assert table["nightly"] is record, "values pass through unmodified"
    assert table["nightly"].workspace == "worktree", (
        "the WorkspaceMode value travels with the record"
    )

    module_src = Path(mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(module_src)
    assert not any(
        isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test)
        for node in tree.body
    )
    config_imports = [
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "herdr_routines.config"
        for alias in node.names
    ]
    assert "TelegramSpawn" in config_imports
    assert "SpawnAlias" in config_imports

    # The single in-repo consumer (cli.py) passes alias_table's own output, untouched.
    cli_src = (Path(mod.__file__).parent / "cli.py").read_text(encoding="utf-8")
    assert "aliases=alias_table(spawn)" in cli_src

    # AC 8: the fixtures follow the record — the ``_aliases`` helper constructs
    # SpawnAlias(...) records; no literal-dict alias record remains.
    helper_src = inspect.getsource(_aliases)
    assert "SpawnAlias(" in helper_src
    assert '"repo"' not in helper_src and '"workspace"' not in helper_src


def test_handle_spawn_message_aliases_are_str_or_none_mapping() -> None:
    """AC 2 + AC 3 (blocking), issue 083: ``handle_spawn_message`` declares
    ``aliases: Mapping[str, SpawnAlias]`` and reads the record via direct
    attribute access (``spec.agent_kind``, ``spec.workspace``, ``spec.base``,
    ``spec.model``, ``str(spec.repo)``); the four ``str(spec.get(...) or
    default)`` coercions — and the defaults they mirrored — are gone, leaving the
    ``"worktree"`` default in exactly one place: ``config.SpawnAlias``'s field
    default."""
    import inspect
    from collections.abc import Mapping
    from typing import get_type_hints

    from herdr_routines.config import SpawnAlias

    mod = _spawn_module()

    hints = get_type_hints(mod.handle_spawn_message)
    assert hints["aliases"] == Mapping[str, SpawnAlias]

    src = inspect.getsource(mod.handle_spawn_message)
    assert "aliases: Mapping[str, SpawnAlias]," in src
    assert "spec.agent_kind" in src
    assert "spec.workspace" in src
    assert "spec.base" in src
    assert "spec.model" in src
    assert "str(spec.repo)" in src

    # AC 3: the stale coercions are gone — including every remaining dict read on
    # the record (the outer lookup is aliases.get, never spec.get).
    assert 'str(spec.get("workspace") or "worktree")' not in src
    assert 'str(spec.get("agent_kind") or "claude")' not in src
    assert 'str(spec.get("base") or "main")' not in src
    assert 'str(spec.get("repo") or "")' not in src
    assert "spec.get(" not in src
    assert 'or "worktree"' not in src, (
        'the "worktree" default lives only in config.SpawnAlias'
    )

    # ...which is exactly where config keeps it (config.SpawnAlias's field default).
    config_src = (Path(mod.__file__).parent / "config.py").read_text(encoding="utf-8")
    assert 'workspace: WorkspaceMode = "worktree"' in config_src


def test_typed_boundaries_have_no_any() -> None:
    """AC 5 (blocking): no ``Any`` remains in the four narrowed signatures
    (``alias_table``, ``handle_spawn_message``, ``run_checks``, ``GithubEvent.payload``);
    ``telegram_spawn.py`` drops ``Any`` from its ``typing`` import while keeping
    ``Protocol`` ``SpawnClient``; the change adds no ``cast(`` and no
    ``# type: ignore`` anywhere in src."""
    import ast
    import inspect

    import herdr_routines.auto_fix as af

    mod = _spawn_module()

    for fn in (mod.alias_table, mod.handle_spawn_message):
        assert "Any" not in inspect.getsource(fn)
    assert "Any" not in inspect.getsource(af.run_checks)
    assert "Any" not in inspect.getsource(af.GithubEvent)

    module_src = Path(mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(module_src)
    typing_names = [
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "typing"
        for alias in node.names
    ]
    assert "Any" not in typing_names
    assert "Protocol" in typing_names

    pkg_dir = Path(af.__file__).parent
    suppressions = [
        p.relative_to(pkg_dir).as_posix()
        for p in sorted(pkg_dir.rglob("*.py"))
        if "# type: ignore" in p.read_text(encoding="utf-8")
        or re.search(r"\bcast\(", p.read_text(encoding="utf-8"))
    ]
    assert suppressions == []


# Issue 083 AC 9 (non-blocking, meta-check): ci green with the blocking tests
# present and passing. The assertions below check present state; the TDD ordering
# (tests rewritten red in e76685a, implementation in e5aac9f) is evidenced by git
# history, not by anything this test can inspect.


def test_ci_green_with_blocking_tests_run_first() -> None:
    """The blocking criteria (AC 1-5) are the thing CI greenlights: the rewritten
    contract tests are defined in this file and pass against the typed
    implementation, ``scripts/ci-check.sh`` stays the one definition of green
    wiring mypy and the suite, and ``mypy --strict src`` is green — the
    ``str(spec.repo)`` conversion keeps the ``SpawnClient`` ``cwd: str`` boundary
    clean under strict typing."""
    repo_root = Path(__file__).resolve().parents[1]

    # The blocking contract tests are defined in this file. This is a presence
    # check only — ordering is evidenced by git history (e76685a precedes
    # e5aac9f), not by same-file existence.
    source = Path(__file__).read_text(encoding="utf-8")
    for name in (
        "test_alias_table_spawn_param_is_telegram_spawn",
        "test_handle_spawn_message_aliases_are_str_or_none_mapping",
        "test_typed_boundaries_have_no_any",
        "test_spawn_root_mode_opens_a_tab_in_the_checkout",
    ):
        assert f"def {name}(" in source, f"{name} must be defined in this file"

    # ci-check.sh wires the same two gates this test runs, so its verdict covers
    # every blocking criterion above (its full suite run is CI's business, not a
    # nested pytest's — the -k filter below excludes this meta-test itself).
    script = repo_root / "scripts" / "ci-check.sh"
    assert script.is_file()
    ci_text = script.read_text(encoding="utf-8")
    assert "uv run mypy" in ci_text
    assert "uv run pytest -q" in ci_text

    mypy = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "src"],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
    )
    assert mypy.returncode == 0, mypy.stdout + mypy.stderr

    blocking = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_telegram_spawn.py",
            "-k",
            (
                "alias_table_spawn_param_is_telegram_spawn or "
                "handle_spawn_message_aliases_are_str_or_none_mapping or "
                "typed_boundaries_have_no_any or "
                "spawn_root_mode_opens_a_tab_in_the_checkout"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
    )
    assert blocking.returncode == 0, blocking.stdout + blocking.stderr
