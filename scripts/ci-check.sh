#!/usr/bin/env bash
# The one definition of "what CI runs". `.github/workflows/ci.yml` calls this per job
# (lint | typecheck | test) and the pipeline calls it with no argument before pushing,
# so the two cannot drift apart: a check added here is a check in both.
set -euo pipefail
cd "$(dirname "$0")/.."

lint() {
  uv run ruff format --check .
  uv run ruff check .
  uv run vulture
}
typecheck() {
  uv run mypy
}
tests() {
  uv run pytest -q
}

case "${1:-all}" in
  lint) lint ;;
  typecheck) typecheck ;;
  test) tests ;;
  all) lint && typecheck && tests ;;
  *)
    echo "usage: ci-check.sh [lint|typecheck|test]" >&2
    exit 2
    ;;
esac
