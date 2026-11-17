"""A malformed config value is a ConfigError, never a bare TypeError or ValueError (issue 087).

A YAML list or mapping is unhashable, so a bare `value not in VALID_...` test raised
TypeError before the ConfigError was reached, and `validate` / `tick` ended in a traceback
that named no job and no key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from herdr_routines.config import ConfigError, load_config

UNHASHABLE = pytest.mark.parametrize("value", [[], {}], ids=["list", "mapping"])


def _load(tmp_path: Path, document: dict[str, object]) -> None:
    path = tmp_path / "jobs.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    load_config(path)


def _job(**fields: object) -> dict[str, object]:
    return {"name": "a", "cron": "0 3 * * *", "repo": "/repo/a", **fields}


@UNHASHABLE
@pytest.mark.parametrize(
    "field", ["kind", "workspace", "agent_kind", "on_missed", "notify_policy"]
)
def test_job_field_rejects_an_unhashable_value(
    tmp_path: Path, field: str, value: object
) -> None:
    document = {"version": 1, "jobs": [_job(**{field: value})]}

    with pytest.raises(
        ConfigError, match=rf"jobs\[0\] \('a'\): '{field}' must be one of"
    ):
        _load(tmp_path, document)


@UNHASHABLE
def test_github_event_check_query_rejects_an_unhashable_value(
    tmp_path: Path, value: object
) -> None:
    job = _job(kind="gated", checks=[{"github_event": {"query": value}}])

    with pytest.raises(ConfigError, match=r"'checks\[0\]\.query' must be one of"):
        _load(tmp_path, {"version": 1, "jobs": [job]})


@UNHASHABLE
def test_event_trigger_query_rejects_an_unhashable_value(
    tmp_path: Path, value: object
) -> None:
    job = _job(trigger={"event": {"github_event": {"query": value}}})
    del job["cron"]

    with pytest.raises(ConfigError, match=r"query' must be one of"):
        _load(tmp_path, {"version": 1, "jobs": [job]})


@UNHASHABLE
@pytest.mark.parametrize("field", ["agent_kind", "workspace"])
def test_spawn_alias_field_rejects_an_unhashable_value(
    tmp_path: Path, field: str, value: object
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    document = {
        "version": 1,
        "jobs": [_job()],
        "telegram_spawn": {
            "allowed_chat_id": 12345,
            "aliases": [{"alias": "nightly", "repo": str(repo), field: value}],
        },
    }

    with pytest.raises(ConfigError, match=rf"\('nightly'\): '{field}' must be one of"):
        _load(tmp_path, document)


@pytest.mark.parametrize("nested", [[], {}], ids=["list", "mapping"])
def test_select_rejects_an_unhashable_entry(tmp_path: Path, nested: object) -> None:
    """The duplicate check built a set of the entries, so an unhashable one raised."""
    job = {"name": "a", "cron": "0 3 * * *", "kind": "review", "select": [nested]}

    with pytest.raises(ConfigError, match=r"'select\[0\]' must be one of"):
        _load(tmp_path, {"version": 1, "jobs": [job]})


@pytest.mark.parametrize("timezone", ["", "/etc/localtime", "../UTC"])
def test_malformed_timezone_key_is_a_config_error(
    tmp_path: Path, timezone: str
) -> None:
    """ZoneInfo raises ValueError, not ZoneInfoNotFoundError, for a malformed key."""
    document = {"version": 1, "jobs": [_job(timezone=timezone)]}

    with pytest.raises(ConfigError, match="'timezone' is not a valid IANA zone"):
        _load(tmp_path, document)
