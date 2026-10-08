"""P1 — the core is closed (docs/work/orders/T1-extension-seam.md).

"No module under packages/tooling/src imports kp_agent_tooling_ops, a MOVE module, or
kp_core/kp_ops. This covers top-level, function-local and string (-m, importlib)
forms. Every MOVE and REMOVE path is absent from the core."
Falsifier: any such import, or a MOVE module still present in the core.

The MOVE/REMOVE sets come from the frozen docs/work/inventory/T1-module-dispositions.csv
(sha256-checked). The scan reads every .py file under packages/tooling/src from disk,
so an untracked leftover counts as present.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from t1_contract import (CORE_SRC, REPO_ROOT, is_forbidden_module, modules_with, paths_with)

IMPORT_CALLS = {"import_module", "__import__", "find_spec", "run_module", "resolve_name",
                "find_loader", "spec_from_loader"}
DOTTED = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*"
EMBEDDED = (re.compile(r"(?<![\w-])-m\s+(" + DOTTED + r")"),
            re.compile(r"(?<![\w.])import\s+(" + DOTTED + r")"),
            re.compile(r"(?<![\w.])from\s+(" + DOTTED + r")\s+import\b"))
ENTRY_POINT_FORM = re.compile(r"(" + DOTTED + r")\s*:\s*[A-Za-z_][\w.]*")


def core_modules():
    """(module name, path, is_package) for every .py file under packages/tooling/src."""
    found = []
    for path in sorted(CORE_SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(CORE_SRC).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts.pop()
        found.append((".".join(parts), path, is_package))
    assert found, f"no Python modules found under {CORE_SRC}"
    return found


def _package(module: str, is_package: bool) -> str:
    return module if is_package else module.rpartition(".")[0]


def _resolve(relative: str, package: str) -> str | None:
    level = len(relative) - len(relative.lstrip("."))
    parts = package.split(".") if package else []
    if level - 1 > len(parts):
        return None
    base = parts[:len(parts) - (level - 1)]
    rest = relative[level:]
    return ".".join(base + ([rest] if rest else []))


def _where(path: Path, node) -> str:
    return f"{path.relative_to(REPO_ROOT)}:{getattr(node, 'lineno', '?')}"


def test_p1_move_and_remove_paths_are_absent_from_the_core():
    listed = paths_with("MOVE", "REMOVE")
    assert len(listed) == 74, f"the frozen CSV lists 71 MOVE + 3 REMOVE paths; read {len(listed)}"
    present_paths = sorted(path for path in listed if (REPO_ROOT / path).exists())
    gone = modules_with("MOVE") | modules_with("REMOVE")
    present_modules = sorted(f"{name} ({path.relative_to(REPO_ROOT)})"
                             for name, path, _ in core_modules() if name in gone)
    assert not present_paths and not present_modules, (
        f"{len(present_paths)} MOVE/REMOVE paths from the frozen CSV still exist:\n  "
        + "\n  ".join(present_paths)
        + f"\n{len(present_modules)} core modules still carry a MOVE/REMOVE module name:\n  "
        + "\n  ".join(present_modules))


def statement_violations():
    violations = []
    for module, path, is_package in core_modules():
        tree = ast.parse(path.read_bytes(), filename=str(path))
        package = _package(module, is_package)
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = (_resolve("." * node.level + (node.module or ""), package)
                        if node.level else node.module)
                if base is None:
                    continue
                targets = [base] + [f"{base}.{alias.name}" for alias in node.names if alias.name != "*"]
            bad = [target for target in targets if is_forbidden_module(target)]
            if bad and is_forbidden_module(targets[0]):
                bad = targets[:1]
            if bad:
                violations.append(f"{_where(path, node)} {ast.unparse(node)}  ->  {', '.join(bad)}")
    return violations


def test_p1_no_core_module_has_an_import_statement_of_the_extension_a_move_module_or_kp_core_kp_ops():
    violations = statement_violations()
    assert not violations, (f"{len(violations)} import statements (top-level or function-local) in "
                            "packages/tooling/src reach a forbidden module:\n  " + "\n  ".join(violations))


def _statement_strings(tree) -> set[int]:
    """ids of string constants that are bare expression statements (docstrings)."""
    return {id(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)}


def _string_constants(tree) -> dict[str, str]:
    """Simple NAME = 'literal' bindings anywhere in the module (for import_module(NAME))."""
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(getattr(node, "value", None), ast.Constant):
            if isinstance(node.value.value, str):
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    if isinstance(target, ast.Name):
                        bound[target.id] = node.value.value
    return bound


def _literal(node, bound) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bound.get(node.id)
    return None


def string_violations():
    violations = []
    for module, path, is_package in core_modules():
        tree = ast.parse(path.read_bytes(), filename=str(path))
        package = _package(module, is_package)
        docstrings = _statement_strings(tree)
        bound = _string_constants(tree)

        def flag(node, text, target, form):
            if target and is_forbidden_module(target):
                violations.append(f"{_where(path, node)} {form}: {text!r}  ->  {target}")

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                if name in IMPORT_CALLS and node.args:
                    text = _literal(node.args[0], bound)
                    if text:
                        anchor = package
                        if len(node.args) > 1 and _literal(node.args[1], bound):
                            anchor = _literal(node.args[1], bound)
                        for keyword in node.keywords:
                            if keyword.arg == "package" and _literal(keyword.value, bound):
                                anchor = _literal(keyword.value, bound)
                        target = _resolve(text, anchor) if text.startswith(".") else text.split(":")[0].strip()
                        flag(node, text, target, f"{name}()")
            if isinstance(node, (ast.List, ast.Tuple)):
                items = [_literal(element, bound) for element in node.elts]
                for flag_text, value in zip(items, items[1:]):
                    if flag_text == "-m" and value:
                        flag(node, value, value.strip(), "'-m' argument")
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                text = node.value
                for pattern in EMBEDDED:
                    for match in pattern.finditer(text):
                        flag(node, text[:160], match.group(1), "embedded -m/import")
                whole = ENTRY_POINT_FORM.fullmatch(text.strip())
                if whole:
                    flag(node, text, whole.group(1), "module:attribute reference")
    return violations


def test_p1_no_core_module_has_a_string_form_import_of_the_extension_a_move_module_or_kp_core_kp_ops():
    violations = string_violations()
    assert not violations, (f"{len(violations)} string-form imports (-m, importlib/__import__/find_spec, "
                            "module:attribute, embedded import code) in packages/tooling/src reach a "
                            "forbidden module:\n  " + "\n  ".join(violations))
