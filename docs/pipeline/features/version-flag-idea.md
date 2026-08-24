# Feature idea — `--version` flag (trivial pipeline dogfood)

One-paragraph idea to feed stage 1 of the overnight pipeline (first manual run, low blast radius — `docs/pipeline/design.md:257`):

> Add `herdr-routines --version` (and `-V`) that prints the installed package version from `pyproject.toml` via `importlib.metadata.version("herdr-routines")` to stdout and exits 0. The flag should be handled by `argparse` before any config validation, work with no `jobs.yaml` present, and not require a Herdr server.

Rationale: exercises every pipeline stage (spec → acceptance → implement → PR → review → address) without touching scheduler/tick/history/worktree logic.

## Expected spec v2 skeleton (stage 2 will produce this, but this is the shape stage 3 gates on)

```markdown
## Acceptance criteria

1. `--version` prints a semver string matching `pyproject.toml` version — Test: test_cli_version_prints_version
2. `-V` is an alias for `--version` — Test: test_cli_version_short_flag
3. Exit code 0 and no config file required — Test: test_cli_version_no_config_needed
```

Each `Test: <name>` is the exact symbol gate 3 checks via `rg -F -q -- "<name>" tests/` (`docs/pipeline/design.md:207`), then `uv run pytest -q`.

## Notes for the implementer (stage 3)

- `src/herdr_routines/cli.py` — add `parser.add_argument("--version", "-V", action="version", version=importlib.metadata.version("herdr-routines"))` before `load_config` is called.
- Keep `--help` existing behavior; `--version` should not trigger `validate` or `tick` logic.
- Tests in `tests/test_cli.py` (or `tests/test_version.py`) — three tests above, each isolated, no Herdr socket needed.

## Out of scope

- `herdr --version` (that's the Herdr binary, not `herdr-routines`)
- Version bumping logic or `__version__` exposure beyond the CLI flag
