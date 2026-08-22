from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_config_path(tmp_path: Path) -> Path:
    return tmp_path / "jobs.yaml"


@pytest.fixture
def tmp_history_path(tmp_path: Path) -> Path:
    return tmp_path / "history.jsonl"
