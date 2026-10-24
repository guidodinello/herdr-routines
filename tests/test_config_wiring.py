"""Every config field is read by the code it configures, not only parsed and validated.

The audit of 2026-10-23 found `tmp_hygiene:` loaded, validated and tested, with nothing
outside config.py ever reading it (fixed in #218). A text grep for `.tmp_hygiene` did not
catch it: the module import `herdr_routines.tmp_hygiene` matched. This walks src/ with
`ast` and counts only real attribute reads.

Reads are matched by attribute name, not by owner type, so a field sharing its name with
a field read elsewhere (`enabled`, `repo`, `model`, ...) passes on that other read. The
check is a floor: it catches a field nobody reads under any owner, which is how an
unwired block looks.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

from herdr_routines import config

SRC = Path(config.__file__).resolve().parent
PACKAGE = SRC.name

# Fields read through an accessor defined in config.py: the accessor must be called
# outside config.py instead. These raise JobInvariantError when the kind's required field
# is missing, so production code calls them rather than reading the field directly.
ACCESSORS: dict[tuple[str, str], str] = {
    ("Job", "audit"): "require_audit",
    ("Job", "checks"): "require_checks",
    ("Job", "target"): "require_target",
    ("Job", "deadline_ms"): "require_deadline_ms",
    ("Job", "prompt_file"): "require_prompt_file",
}

# Fields with no production read on purpose. Every entry needs a reason; a field read
# nowhere for no reason is the bug this test exists to catch, so wire it or delete it.
NOT_READ: dict[tuple[str, str], str] = {}


def _is_package_path(node: ast.expr) -> bool:
    """True for `herdr_routines.x.y`: a module path, not an attribute read."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name) and node.id == PACKAGE


def _reads_in(tree: ast.AST) -> set[str]:
    """Attribute names read in `tree`: `x.name` loads and `getattr(x, "name")`."""
    reads: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and not _is_package_path(node.value)
        ):
            reads.add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            reads.add(node.args[1].value)
    return reads


def _attribute_reads() -> set[str]:
    reads: set[str] = set()
    for path in SRC.rglob("*.py"):
        if path.name != "config.py":
            reads |= _reads_in(ast.parse(path.read_text(), filename=str(path)))
    return reads


def _config_fields() -> list[tuple[str, str]]:
    """Every field of every dataclass defined in config.py, so a new nested config block
    is covered without editing this test."""
    return [
        (obj.__name__, f.name)
        for obj in vars(config).values()
        if isinstance(obj, type)
        and dataclasses.is_dataclass(obj)
        and obj.__module__ == config.__name__
        for f in dataclasses.fields(obj)
    ]


def test_every_config_field_is_read_outside_config_py() -> None:
    reads = _attribute_reads()
    fields = _config_fields()
    assert ("RoutinesConfig", "tmp_hygiene") in fields  # discovery found the classes

    unread = [
        f"{owner}.{name}"
        for owner, name in fields
        if (owner, name) not in NOT_READ
        and ACCESSORS.get((owner, name), name) not in reads
    ]
    assert not unread, (
        f"config fields parsed but never read outside config.py: {unread}"
    )


def test_allowlists_name_real_fields() -> None:
    """A renamed or deleted field must not leave a stale entry exempting nothing."""
    fields = set(_config_fields())
    assert set(ACCESSORS) <= fields
    assert set(NOT_READ) <= fields


def test_a_module_path_or_a_write_is_not_a_read() -> None:
    """The trap that hid the tmp_hygiene bug: the module path `herdr_routines.tmp_hygiene`
    names the field, and so does assigning it. Neither consumes the config value."""
    tree = ast.parse(
        "herdr_routines.tmp_hygiene.reap_tmp()\n"
        "obj.tmp_hygiene = None\n"
        "cfg.retention.history_max_bytes\n"
        "getattr(cfg, 'telegram_spawn')\n"
    )
    assert _reads_in(tree) == {"retention", "history_max_bytes", "telegram_spawn"}
