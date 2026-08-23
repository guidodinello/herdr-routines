# spec: herdr-routines --version / -V

## Problem

`herdr-routines` currently has no way to report its own installed version. Operators and support flows need to confirm which version is on `PATH` (laptop vs Pi, or after `uv sync` / `pipx upgrade`) without inspecting `pyproject.toml` or `importlib` manually. Existing subcommands (`tick`, `status`, `history`, `validate`, `run`) all require config loading and, for most, a running Herdr server or a `jobs.yaml`, so they cannot serve as a version probe. A missing version flag also breaks the conventional expectation that CLI tools answer `--version` / `-V` on stdout with exit 0.

The requirement is for `herdr-routines --version` and `herdr-routines -V` to print the installed distribution version (as declared in `pyproject.toml` / package metadata) to stdout and exit 0, with no config file, no `jobs.yaml`, and no Herdr server required. It must be handled at argument-parse time, before any config validation or logging side-effects beyond what `main()` already does.

## Approach

Add a top-level optional argument to the `argparse.ArgumentParser` in `src/herdr_routines/cli.py:_build_parser()` using `parser.add_argument("--version", "-V", action="version", version=...)`. The version string is obtained at runtime via `importlib.metadata.version("herdr-routines")`, matching `pyproject.toml:3` (`name = "herdr-routines"`, currently `0.1.0`). No hard-coded duplication; the single source of truth remains `pyproject.toml` / installed metadata. If the distribution is not installed (e.g. running from source without `uv sync`), fall back to `importlib.metadata.PackageNotFoundError` handling — either report `unknown` or the fallback `herdr_routines.__version__` (`src/herdr_routines/__init__.py:3`) — so the flag never raises an unhandled exception. `argparse`'s `version` action prints to stdout and exits 0 before subcommand dispatch, so no handler or config loading is reached. Because `subparsers` are `required=True`, the version flag must be registered on the top-level parser, not on a subparser, and must be parsed before `required` is enforced (argparse already does this for `action="version"`). No new dependencies, no config schema changes, no history or Herdr client involvement.

Verification is `herdr-routines --version` and `herdr-routines -V` each print a version string matching `pyproject.toml` / `importlib.metadata` and exit 0 when invoked from a directory with no `jobs.yaml` and with no Herdr server running. Existing subcommands and `herdr-routines --help` remain unchanged except for the new flag appearing in help.

## Files touched

- `src/herdr_routines/cli.py` — only file requiring a functional change: import `importlib.metadata` (and handle `PackageNotFoundError`), add `parser.add_argument("--version", "-V", action="version", version=...)` in `_build_parser()`. Optionally extract a small helper `_get_version()` for testability. No changes to handlers (`_cmd_tick`, `_cmd_status`, etc.) — the version short-circuits before they are reached.
- `src/herdr_routines/__init__.py` — no change required; `__version__ = "0.1.0"` remains as a local fallback / secondary source, but the flag must prefer `importlib.metadata.version("herdr-routines")` per the feature spec so installed metadata and `pyproject.toml` stay authoritative.
- `pyproject.toml` — no change; `version = "0.1.0"` is the source of truth that `importlib.metadata` will return when installed. Noted here only as the version origin.
- Tests/docs — no code changes required in v1, but subsequent stages should add coverage for `--version`/`-V` exiting 0 with no config present.

## Risks

Argparse interaction with `required=True` subparsers is the main subtlety: `action="version"` must be on the top-level parser so it fires before the "required subcommand missing" error (exit 2). A mis-placed definition on a subparser or a custom `required` check would break the "works with no `jobs.yaml` / no server" guarantee. Mitigation is a trivial manual test (`mkdir /tmp/empty && herdr-routines --version` with `HERDR_PLUGIN_CONFIG_DIR` unset) and confirming exit 0.

Second risk is version source skew: `importlib.metadata.version("herdr-routines")` can raise `PackageNotFoundError` when running from a checkout without an install step, or return a stale version if an old wheel is still on `PATH`. Falling back to `herdr_routines.__version__` or `"unknown"` avoids a crash but can mask install issues; the spec requires the `importlib.metadata` path as primary so operators see the actually installed distribution, not a file-local constant.

Scope creep is minimal but worth bounding: do not add `version` subcommand, JSON output, or logging side-effects. The flag should be pure `argparse` with no file I/O, no `init_logging` dependency beyond what `main()` already does, and no Herdr socket touch. Keeping the diff to ~5 lines in one file makes review and rollback trivial.
