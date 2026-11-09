"""Structural pins for the runner seams (issue 081 — spec v2, “Acceptance criteria”).

AC 1: both Protocols (`SubprocessRunner`, `BoundedRunner`) are defined exactly once,
side by side, in one leaf module (`src/herdr_routines/runner_seam.py`), and the five
consumers reach them through `from herdr_routines.runner_seam import ... as Runner`.

AC 7: the structural half of `uv run mypy src --disallow-any-explicit` reporting 0
hits — no ellipsis-form `Callable` runner annotation and no explicit `Any` anywhere
in `herdr_routines`; the mypy command itself stays with the required typecheck CI
job, as in test_ci_check_script.py and test_gates.py.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
from pathlib import Path

import herdr_routines

SRC = Path(herdr_routines.__file__).resolve().parent

# Which shape each consumer injects as `Runner` (spec v2, “Approach”).
CONSUMER_SHAPE = {
    "host_drift": "SubprocessRunner",
    "self_update": "SubprocessRunner",
    "auto_fix": "BoundedRunner",
    "gates": "BoundedRunner",
    "pipeline_run": "BoundedRunner",
}
RUNNER_SEAM_MODULE = "runner_seam"
SHAPES = ("SubprocessRunner", "BoundedRunner")

# The kwargs every src call site passes; the Shape A protocol must spell all five.
SUBPROCESS_KWARGS = ("cwd", "capture_output", "text", "timeout", "check")


def _imports(tree: ast.Module) -> set[str]:
    """Top-level import targets of the module: `import x` and `from x import ...`."""
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


def _top_level_defining_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


def _class_defs(path: Path, wanted: set[str]) -> dict[str, ast.ClassDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name in wanted
    }


def _is_protocol_class(node: ast.ClassDef) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == "Protocol":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "Protocol":
            return True
    return False


def test_runner_seam_is_a_leaf_module_defining_both_protocols() -> None:
    """AC 1 (blocking): `runner_seam.py` defines both Protocols side by side as a leaf
    module — no `herdr_routines.*` imports — each shape is defined exactly once in src,
    and the five consumers all reach the seam through
    `from herdr_routines.runner_seam import ... as Runner`."""
    import herdr_routines.runner_seam as seam

    module_path = Path(seam.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    # Leaf: imports only subprocess, typing and collections.abc (plus the standard
    # `from __future__ import annotations` prologue) — nothing from herdr_routines, so
    # gates (which imports auto_fix) and auto_fix (which cannot import gates) can both
    # consume the one home without a cycle.
    imported = _imports(tree)
    assert imported <= {
        "subprocess",
        "typing",
        "collections.abc",
        "__future__",
    }, imported

    # Both shapes live side by side in the one module, each a typing.Protocol, and the
    # module docstring states the split rationale plus the herdr.CommandRunner cousin
    # note ("Not a unification of CommandRunner — its timeout_s is float | None; the
    # bounded seam needs float required").
    module_doc = ast.get_docstring(tree)
    assert module_doc, "runner_seam.py must document the split rationale"
    defs = _class_defs(module_path, set(SHAPES))
    assert set(defs) == set(SHAPES), sorted(defs)
    shape_docs: list[str] = []
    for shape in SHAPES:
        shape_doc = ast.get_docstring(defs[shape])
        assert shape_doc, f"{shape} must carry its shape's docstring"
        shape_docs.append(shape_doc)
        assert _is_protocol_class(defs[shape]), shape
    seam_docs = " ".join([module_doc, *shape_docs])
    assert "CommandRunner" in seam_docs, (
        "runner_seam.py must carry the herdr.CommandRunner cousin note"
    )

    # Shape A — the subprocess.run-shaped seam: argv is `list[str]` (Decision 1), the
    # five kwargs every src call site passes are spelled with defaults, and the return
    # is CompletedProcess[str].
    sig_a = inspect.signature(seam.SubprocessRunner.__call__, eval_str=True)
    params_a = sig_a.parameters
    argv_a = params_a["argv"]
    assert argv_a.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    assert argv_a.annotation == list[str]
    for kwarg in SUBPROCESS_KWARGS:
        param = params_a[kwarg]
        assert param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ), kwarg
        assert param.default is not inspect.Parameter.empty, kwarg
    assert sig_a.return_annotation == subprocess.CompletedProcess[str]

    # Shape B — the bounded-command seam: `(argv, *, timeout_s) -> (exit_code, stdout,
    # stderr)` with `timeout_s` a required keyword-only float (Decision 3: wider than
    # `float | None` would be, so CommandRunner stays unusable for it).
    sig_b = inspect.signature(seam.BoundedRunner.__call__, eval_str=True)
    params_b = sig_b.parameters
    argv_b = params_b["argv"]
    assert argv_b.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    assert argv_b.annotation == list[str]
    timeout_s = params_b["timeout_s"]
    assert timeout_s.kind is inspect.Parameter.KEYWORD_ONLY
    assert timeout_s.default is inspect.Parameter.empty, "timeout_s must be required"
    assert timeout_s.annotation == float
    assert sig_b.return_annotation == tuple[int, str, str]

    # Exactly one src home per shape, and it is the seam module: no consumer (or any
    # other src module) spells a shape out again.
    homes = {
        shape: [
            path.stem
            for path in sorted(SRC.glob("*.py"))
            if shape in _top_level_defining_names(path)
        ]
        for shape in SHAPES
    }
    assert homes == {shape: [RUNNER_SEAM_MODULE] for shape in SHAPES}, homes

    # The five consumers reach the shapes at the seam's home through
    # `from herdr_routines.runner_seam import <shape> as Runner`.
    for consumer, shape in CONSUMER_SHAPE.items():
        consumer_tree = ast.parse((SRC / f"{consumer}.py").read_text(encoding="utf-8"))
        runner_aliases = [
            alias.name
            for node in consumer_tree.body
            if isinstance(node, ast.ImportFrom)
            and node.module == f"herdr_routines.{RUNNER_SEAM_MODULE}"
            for alias in node.names
            if alias.asname == "Runner"
        ]
        assert runner_aliases == [shape], (consumer, runner_aliases)


def test_src_runner_seams_hold_no_explicit_any() -> None:
    """AC 7 (blocking): no runner seam hides its parameters behind `...` any more —
    no ellipsis-form `Callable` annotation and no explicit `Any` anywhere in
    `herdr_routines` (today's inventory is 10 errors in 5 files, at the sites the
    issue lists; 0 after the swap). The AST halves of
    `uv run mypy src --disallow-any-explicit` = 0 hits and `uv run mypy --strict src`
    green, which the required typecheck CI job re-runs."""
    offenders: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "Callable[..." in text:
            offenders.append(f"{path.name:20s} ellipsis-form Callable annotation")
        for node in ast.walk(ast.parse(text)):
            spill: str | None = None
            if isinstance(node, ast.Name):
                spill = node.id
            elif isinstance(node, ast.Attribute):
                spill = node.attr
            if spill != "Any":
                continue
            line = getattr(node, "lineno", 0)
            offenders.append(f"{path.name:20s} explicit `Any` at line {line}")
    assert offenders == [], "seam escape hatches survive:\n" + "\n".join(offenders)
