"""D0b amendment 2: R6's F2 local-build set is DERIVED from the tree, and this test is the source.

Order: docs/work/orders/D0b-release-workflow.md, R6 (F2), amendment 2 (Verification). The set is every
image-marked test that takes the `agents` image, plus the two named negatives
(`d0b_seams.NAMED_LOCAL_BUILD_TESTS`). The harness (tests/d0b_r6_harness.py) gives the local builds to
`d0b_seams.LOCAL_BUILD_TESTS` only, so that list must EQUAL the derived set: a test that takes `agents`
unlisted FAILS under pushed digests, and a listed test that does not is a hole in R6's split.

How the tree is read (Python's `ast`, nothing imported or run), over every `test_*.py` under tests/image,
tests/install and tests/host:
- Image-marked: the module's `pytestmark` or the function's decorators name `mark.image`.
- A module that imports `agents_world` from `t7b_harness` takes `agents` in every test (its tests reach it
  through fixtures).
- Otherwise a test (a module-level `def test_*`) takes `agents` when its body names the variable
  AGENT_TOOLING_TEST_IMAGE_AGENTS (the string, or a harness constant bound to it), calls a harness function
  that reads it unconditionally, or reads it through an ACCESSOR with "agents": an accessor is a harness
  function `f(x)` whose body indexes a harness mapping whose "agents" key is that variable
  (`t6a_variants.image_for` over `VARIABLE`, `image_harness.image_for` over `TARGET_VARIABLE`). A literal
  "agents" argument selects every case of the test; a `parametrize` argument passed through selects only the
  cases where it is "agents".
- Test ids follow pytest's: `ids=` when given, else the string, number or boolean value of each argument.
"""
from __future__ import annotations

import ast
import itertools
from dataclasses import dataclass, field
from pathlib import Path

import d0b_seams as seams

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
SUITES = ("tests/image", "tests/install", "tests/host")
AGENTS = seams.AGENTS_VARIABLE


# ----------------------------------------------------------------- the harness index


@dataclass
class Harness:
    names: dict = field(default_factory=dict)       # module -> names bound to the agents variable
    maps: dict = field(default_factory=dict)        # module -> mappings whose "agents" key is it
    accessors: dict = field(default_factory=dict)   # module -> functions f(x) indexing such a mapping with x
    readers: dict = field(default_factory=dict)     # module -> functions reading it unconditionally


def _is_agents(node, names: set) -> bool:
    return (isinstance(node, ast.Constant) and node.value == AGENTS) or (isinstance(node, ast.Name) and node.id in names)


def index_harness() -> Harness:
    harness = Harness()
    for path in TESTS.rglob("*.py"):
        module = path.stem
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        names = {t.id for n in tree.body if isinstance(n, ast.Assign) and _is_agents(n.value, set())
                 for t in n.targets if isinstance(t, ast.Name)}
        maps = {t.id for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict)
                and any(isinstance(k, ast.Constant) and k.value == "agents" and _is_agents(v, names)
                        for k, v in zip(n.value.keys, n.value.values))
                for t in n.targets if isinstance(t, ast.Name)}
        functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        accessors = set()
        for name, fn in functions.items():
            params = [a.arg for a in fn.args.args]
            if params and any(isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name) and sub.value.id in maps
                              and isinstance(sub.slice, ast.Name) and sub.slice.id == params[0] for sub in ast.walk(fn)):
                accessors.add(name)
        readers = {name for name, fn in functions.items()
                   if any(_is_agents(node, names) for node in ast.walk(fn))}
        changed = True
        while changed:  # a function calling a reader of its own module reads it too
            changed = False
            for name, fn in functions.items():
                if name not in readers and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in readers
                                               for c in ast.walk(fn)):
                    readers.add(name)
                    changed = True
        for key, value in (("names", names), ("maps", maps), ("accessors", accessors), ("readers", readers)):
            if value:
                getattr(harness, key).setdefault(module, set()).update(value)
    return harness


# ----------------------------------------------------------------- tests and their cases


def _marked_image(node) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == "image" and isinstance(n.value, ast.Attribute)
               and n.value.attr == "mark" for n in ast.walk(node))


def _literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None


def _cases(fn) -> list[tuple[str, dict]]:
    """(id, {argument: literal value or None}) per parametrized case; [("", {})] when not parametrized."""
    layers = []
    for decorator in reversed(fn.decorator_list):  # the decorator nearest the function applies first
        if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "parametrize" and len(decorator.args) >= 2):
            continue
        argnames = _literal(decorator.args[0])
        argnames = [a.strip() for a in argnames.split(",")] if isinstance(argnames, str) else list(argnames or [])
        values = decorator.args[1].elts if isinstance(decorator.args[1], (ast.List, ast.Tuple)) else []
        ids = next((_literal(k.value) for k in decorator.keywords if k.arg == "ids"), None)
        layer = []
        for index, value in enumerate(values):
            items = value.elts if len(argnames) > 1 and isinstance(value, (ast.Tuple, ast.List)) else [value]
            bound = {name: _literal(item) for name, item in zip(argnames, items)}
            if isinstance(ids, (list, tuple)) and index < len(ids):
                case_id = str(ids[index])
            else:
                case_id = "-".join(str(bound[n]) if isinstance(bound[n], (str, int, float, bool)) else f"{n}{index}"
                                   for n in argnames)
            layer.append((case_id, bound))
        layers.append(layer)
    if not layers:
        return [("", {})]
    cases = []
    for combination in itertools.product(*layers):
        cases.append(("-".join(c[0] for c in combination), {k: v for c in combination for k, v in c[1].items()}))
    return cases


def _imports(tree) -> dict:
    """local name -> (module, name) for `from module import name [as local]`."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                found[alias.asname or alias.name] = (node.module.rsplit(".", 1)[-1], alias.name)
    return found


def _takes_agents(fn, imports: dict, harness: Harness, cases) -> set[str]:
    """The case ids of `fn` that take the agents image."""
    def resolved(name):
        return imports.get(name, (None, None))
    every, by_argument = False, set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and node.value == AGENTS:
            every = True
        elif isinstance(node, ast.Name):
            module, original = resolved(node.id)
            if module and original in harness.names.get(module, ()):
                every = True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            module, original = resolved(node.func.id)
            if module and original in harness.readers.get(module, ()):
                every = True
            if module and original in harness.accessors.get(module, ()) and node.args:
                argument = node.args[0]
                if isinstance(argument, ast.Constant) and argument.value == "agents":
                    every = True
                elif isinstance(argument, ast.Name):
                    by_argument.add(argument.id)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            module, original = resolved(node.value.id)
            if module and original in harness.maps.get(module, ()):
                if isinstance(node.slice, ast.Constant) and node.slice.value == "agents":
                    every = True
                elif isinstance(node.slice, ast.Name):
                    by_argument.add(node.slice.id)
    return {case_id for case_id, bound in cases
            if every or any(bound.get(argument) == "agents" for argument in by_argument)}


def _node(path: Path, name: str, case_id: str) -> str:
    return f"{path.relative_to(ROOT).as_posix()}::{name}" + (f"[{case_id}]" if case_id else "")


def _modules():
    for suite in SUITES:
        for path in sorted((ROOT / suite).rglob("test_*.py")):
            yield path, ast.parse(path.read_text())


def all_tests(path: Path, tree) -> list[str]:
    return [_node(path, fn.name, case_id) for fn in tree.body if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and fn.name.startswith("test") for case_id, _ in _cases(fn)]


def derive() -> set[str]:
    """Every image-marked test that takes the agents image, read from the tree."""
    harness, derived = index_harness(), set()
    for path, tree in _modules():
        module_marked = any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark"
                                                               for t in n.targets) and _marked_image(n.value)
                            for n in tree.body)
        imports = _imports(tree)
        takes_world = any(module == "t7b_harness" and name == "agents_world" for module, name in imports.values())
        for fn in tree.body:
            if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test")):
                continue
            if not (module_marked or any(_marked_image(d) for d in fn.decorator_list)):
                continue
            cases = _cases(fn)
            chosen = {c for c, _ in cases} if takes_world else _takes_agents(fn, imports, harness, cases)
            derived |= {_node(path, fn.name, case_id) for case_id in chosen}
    return derived


def expand(entries) -> set[str]:
    """A list's entries as test ids: a `module::` entry is every test of the module."""
    found = set()
    for entry in entries:
        if entry.endswith("::"):
            path = ROOT / entry[:-2]
            found |= set(all_tests(path, ast.parse(path.read_text()))) if path.is_file() else {entry + "<missing module>"}
        else:
            found.add(entry)
    return found


def test_r6_local_build_list_equals_the_set_derived_from_the_tree():
    """GREEN-IF the set derived from the tree (every image-marked test under tests/image, tests/install and
    tests/host whose module imports `t7b_harness.agents_world`, or whose body reads AGENT_TOOLING_TEST_IMAGE_AGENTS
    directly or through a harness accessor with "agents", plus the named negative
    test_image_identity_and_size's agents case) EQUALS `d0b_seams.LOCAL_BUILD_TESTS` for the agents variable,
    and the named T7a P4 unrevisioned case EQUALS it for AGENT_TOOLING_TEST_IMAGE_UNREVISIONED; and, as the
    derivation's control, every test of each `d0b_seams.DERIVATION_CONTROLS` module (modules that never name the
    variable) is in the derived set."""
    derived = derive() | set(seams.NAMED_LOCAL_BUILD_TESTS[AGENTS])
    controls = expand(seams.DERIVATION_CONTROLS)
    assert controls and controls <= derived, \
        f"control: the derivation misses tests that take agents through agents_world: {sorted(controls - derived)}"
    listed = expand(seams.LOCAL_BUILD_TESTS[AGENTS])
    assert derived == listed, (f"{AGENTS}: LOCAL_BUILD_TESTS differs from the derived set\n  taken but unlisted: "
                               f"{sorted(derived - listed)}\n  listed but not taken: {sorted(listed - derived)}")
    unrevisioned = expand(seams.LOCAL_BUILD_TESTS[seams.UNREVISIONED_VARIABLE])
    named = set(seams.NAMED_LOCAL_BUILD_TESTS[seams.UNREVISIONED_VARIABLE])
    assert unrevisioned == named, f"{seams.UNREVISIONED_VARIABLE}: listed {sorted(unrevisioned)}, named {sorted(named)}"
