"""P3 of order S1: every absolute ``kp_agent_tooling`` import in the package source resolves.

Order: docs/work/orders/S1-knowledge-platform-coverage.md, frozen at its merge
commit.

The guard is static. It parses every module under ``packages/tooling/src/kp_agent_tooling``
and walks every import statement: module level, function-local and conditional; ``import x``
and ``from x import y``; and, for ``from x import y``, whether ``y`` is a present submodule or
a name that ``x`` binds. It imports nothing from the package, so an import that never runs in
the suite cannot hide. Resolution is case-exact, as the import system is on a case-insensitive
filesystem. Relative imports are outside the property.
"""
from __future__ import annotations

import ast
import os
from functools import lru_cache
from pathlib import Path

PACKAGE = 'kp_agent_tooling'
REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / 'packages' / 'tooling' / 'src'
PACKAGE_ROOT = SOURCE_ROOT / PACKAGE


@lru_cache(maxsize=None)
def _entries(directory: Path) -> frozenset:
    try:
        return frozenset(os.listdir(directory))
    except OSError:
        return frozenset()


def _module_path(dotted: str) -> Path | None:
    """The source a package module resolves to (a file, or a namespace directory), else None."""
    parts = dotted.split('.')
    if parts[0] != PACKAGE:
        return None
    directory, found = SOURCE_ROOT, None
    for position, part in enumerate(parts):
        names, candidate = _entries(directory), directory / part
        if part in names and candidate.is_dir() and '__init__.py' in _entries(candidate):
            found = candidate / '__init__.py'
        elif f'{part}.py' in names:
            found = directory / f'{part}.py'
        elif part in names and candidate.is_dir():
            found = candidate  # PEP 420 namespace portion
        else:
            return None
        if position < len(parts) - 1:
            if found.suffix == '.py' and found.name != '__init__.py':
                return None  # a plain module has no submodules
            directory = candidate
    return found


@lru_cache(maxsize=None)
def _bound_names(path: Path) -> frozenset:
    """Names a module binds when imported, including under module-level if/try/with/for."""
    if path.is_dir():
        return frozenset()
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    names, pending = set(), list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue  # locals are not module attributes
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split('.')[0] for alias in node.names)
            continue
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        if isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        pending.extend(ast.iter_child_nodes(node))
    return frozenset(names)


def _ours(name: str | None) -> bool:
    return bool(name) and (name == PACKAGE or name.startswith(PACKAGE + '.'))


def _function_local(tree: ast.AST) -> set:
    return {id(node) for function in ast.walk(tree)
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(function) if isinstance(node, (ast.Import, ast.ImportFrom))}


def scan(source: str, filename: str):
    """Return (unresolved findings, package imports seen, of which function-local)."""
    tree = ast.parse(source, filename=filename)
    local = _function_local(tree)
    findings, seen, seen_local = [], 0, 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _ours(alias.name):
                    seen += 1
                    seen_local += id(node) in local
                    if _module_path(alias.name) is None:
                        findings.append((filename, node.lineno, f'import {alias.name}'))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and _ours(node.module):
            seen += 1
            seen_local += id(node) in local
            target = _module_path(node.module)
            if target is None:
                findings.append((filename, node.lineno, f'from {node.module} import ...'))
                continue
            for alias in node.names:
                if alias.name == '*' or _module_path(f'{node.module}.{alias.name}') is not None:
                    continue
                bound = _bound_names(target)
                if alias.name in bound or '__getattr__' in bound or '*' in bound:
                    continue
                findings.append((filename, node.lineno, f'from {node.module} import {alias.name}'))
    return findings, seen, seen_local


CALIBRATION = '''\
import kp_agent_tooling._impl.s1_absent_plain
def local():
    from kp_agent_tooling._impl.s1_absent_local import thing
if TYPE_CHECKING:
    import kp_agent_tooling._impl.s1_absent_conditional as conditional
from kp_agent_tooling._impl import s1_absent_submodule
from kp_agent_tooling._impl.service import serena_navigation
from kp_agent_tooling._impl.service.serena_navigation import SerenaNavigationProvider
'''


def test_p3_every_package_import_resolves():
    # Calibration: each form the property names is caught; resolvable imports are not.
    probe, _, _ = scan(CALIBRATION, 'calibration.py')
    assert sorted(line for _, line, _ in probe) == [1, 3, 5, 6], probe

    files = sorted(PACKAGE_ROOT.rglob('*.py'))
    findings, seen, seen_local = [], 0, 0
    for path in files:
        found, count, count_local = scan(path.read_text(encoding='utf-8'),
                                         str(path.relative_to(REPO_ROOT)))
        findings += found
        seen += count
        seen_local += count_local
    # Zero is not a result: the walk reached package imports, including function-local ones.
    assert files and seen and seen_local, (PACKAGE_ROOT, len(files), seen, seen_local)
    assert findings == [], 'unresolved kp_agent_tooling imports:\n' + '\n'.join(
        f'{filename}:{line}: {statement}' for filename, line, statement in findings)
