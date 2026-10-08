"""T11a census instrument: the order's AST patterns (a)-(h) over the product modules.

docs/work/orders/T11a-leaf-consolidation.md, "Instruments", and its appendix
T11a-census-1e74728.md, "Guard-test design". Used by tests/test_t11_leaf_single_home.py
(the guard and its ``--report`` base reading). Standard library only.

Scope: tracked ``.py`` files under packages/tooling/src/kp_agent_tooling, extensions/ops/src
and deploy/image (top level), i.e. the census's 160 modules at base, plus ``_impl/leaf.py``
when it exists (``Census.leaf``). Import aliases are resolved per module
(``import hashlib as _hashlib``, ``from hashlib import sha256``, function-local imports
included). A string constant that parses as Python and contains an import statement is an
embedded script: it is parsed as its own unit (its sites are marked embedded), and an
embedded tail written as ``_A + r'''...'''`` inherits the imports of ``_A``.

Every pattern here is mechanical; where the census's number is a purpose reading, the
report says so and prints what the mechanical pattern finds beside it.
"""
from __future__ import annotations

import ast
import difflib
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T = 'packages/tooling/src/kp_agent_tooling/'
O = 'extensions/ops/src/kp_agent_tooling_ops/'
D = 'deploy/image/'
LEAF = T + '_impl/leaf.py'
LEAF_MODULE = 'kp_agent_tooling._impl.leaf'
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


# ---------------------------------------------------------------- files and units

def tracked(root, *paths):
    out = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--', *paths],
                         capture_output=True, check=True).stdout
    return sorted(p for p in out.decode().split('\0') if p)


def product_files(root=ROOT):
    files = tracked(root, T, 'extensions/ops/src', D)
    found = [f for f in files if f.endswith('.py') and (not f.startswith(D) or f.count('/') == 2)
             and (Path(root) / f).is_file()]
    if (Path(root) / LEAF).is_file() and LEAF not in found:
        found.append(LEAF)  # an untracked leaf is still the leaf
    return sorted(found)


def module_name(rel):
    for prefix, package in ((T, 'kp_agent_tooling.'), (O, 'kp_agent_tooling_ops.')):
        if rel.startswith(prefix):
            dotted = package + rel[len(prefix):-3].replace('/', '.')
            return dotted[:-len('.__init__')] if dotted.endswith('.__init__') else dotted
    return rel[:-3].replace('/', '.')


def strip_doc(body):
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return list(body)


def docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef) + FUNCS):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def import_aliases(tree, package=''):
    """local name -> dotted target, for every import anywhere in ``tree``."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    head = alias.name.split('.')[0]
                    aliases[head] = head
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                parts = package.split('.')
                parts = parts[:len(parts) - node.level + 1] if node.level > 1 else parts
                base = '.'.join(p for p in parts + ([node.module] if node.module else []) if p)
            for alias in node.names:
                aliases[alias.asname or alias.name] = f'{base}.{alias.name}' if base else alias.name
    return aliases


@dataclass
class Unit:
    rel: str
    tree: ast.Module
    offset: int
    embedded: str | None  # binding name of an embedded script; None for module code
    aliases: dict
    docstrings: set
    skip: set = field(default_factory=set)  # constants that are embedded scripts (module units)
    parents: dict = field(default_factory=dict)

    def __post_init__(self):
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parents[id(child)] = node

    def line(self, node):
        return node.lineno + self.offset

    def qual(self, node):
        """Dotted import target of a Name/Attribute chain, or None when the base is not imported."""
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name) and node.id in self.aliases:
            return '.'.join([self.aliases[node.id]] + parts[::-1])
        return None

    def enclosing(self, node):
        """Innermost enclosing def (not lambda), or None at module level."""
        parent = self.parents.get(id(node))
        while parent is not None and not isinstance(parent, FUNCS):
            parent = self.parents.get(id(parent))
        return parent

    def qualname(self, fn):
        if fn is None:
            return '<module>'
        names, node = [fn.name], self.parents.get(id(fn))
        while node is not None:
            if isinstance(node, (ast.ClassDef,) + FUNCS):
                names.append(node.name)
            node = self.parents.get(id(node))
        return '.'.join(reversed(names))

    def functions(self):
        return [n for n in ast.walk(self.tree) if isinstance(n, FUNCS)]

    def own_nodes(self, fn):
        """Nodes of ``fn`` not inside a nested def or lambda."""
        out, stack = [], list(ast.iter_child_nodes(fn))
        while stack:
            node = stack.pop()
            out.append(node)
            if not isinstance(node, SCOPES):
                stack.extend(ast.iter_child_nodes(node))
        return out

    def is_leaf_call(self, call, pattern):
        target = self.qual(call.func) if isinstance(call, ast.Call) else None
        return bool(target and target.startswith(LEAF_MODULE + '.')
                    and re.search(pattern, target.rsplit('.', 1)[1], re.I))


def _binding_name(parents, node):
    parent = parents.get(id(node))
    while parent is not None and not isinstance(parent, (ast.Assign, ast.AnnAssign) + FUNCS):
        parent = parents.get(id(parent))
    if isinstance(parent, ast.Assign) and isinstance(parent.targets[0], ast.Name):
        return parent.targets[0].id
    if isinstance(parent, ast.AnnAssign) and isinstance(parent.target, ast.Name):
        return parent.target.id
    return f'<string@{node.lineno}>'


def load_units(root=ROOT, files=None):
    units = []
    for rel in files or product_files(root):
        source = (Path(root) / rel).read_text(encoding='utf-8')
        tree = ast.parse(source, filename=rel)
        package = module_name(rel).rsplit('.', 1)[0]
        module = Unit(rel, tree, 0, None, import_aliases(tree, package), docstring_ids(tree))
        units.append(module)
        scripts = {}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str) and '\n' in node.value
                    and id(node) not in module.docstrings):
                continue
            try:
                inner = ast.parse(node.value)
            except SyntaxError:
                continue
            if not any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(inner)):
                continue
            name = _binding_name(module.parents, node)
            aliases = import_aliases(inner)
            parent = module.parents.get(id(node))
            if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add) and parent.right is node \
                    and isinstance(parent.left, ast.Name) and parent.left.id in scripts:
                aliases = {**scripts[parent.left.id].aliases, **aliases}
            unit = Unit(rel, inner, node.lineno - 1, name, aliases, docstring_ids(inner))
            scripts[name] = unit
            module.skip.add(id(node))
            units.append(unit)
    return units


# ---------------------------------------------------------------- sites

@dataclass(frozen=True)
class Site:
    category: str
    kind: str
    rel: str
    line: int
    embedded: str | None
    function: str
    detail: str = ''

    def where(self):
        return f"{self.rel}:{self.line}{'e' if self.embedded else ''}"

    def __str__(self):
        extra = f' {self.detail}' if self.detail else ''
        return f'{self.where()} [{self.function}] {self.kind}{extra}'


def _site(unit, category, kind, node, detail=''):
    fn = unit.enclosing(node)
    return Site(category, kind, unit.rel, unit.line(node), unit.embedded, unit.qualname(fn), detail)


def _kw(call, name):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _const(node, default=None):
    return node.value if isinstance(node, ast.Constant) else default


def is_dumps(unit, node):
    return isinstance(node, ast.Call) and unit.qual(node.func) in ('json.dumps', 'json.dump')


def dumps_form(call):
    """(sort_keys, compact, ensure_ascii, allow_nan) of a json.dumps/dump call; None = not literal."""
    separators = _kw(call, 'separators')
    compact = isinstance(separators, ast.Tuple) and [_const(e) for e in separators.elts] == [',', ':']
    return (_const(_kw(call, 'sort_keys'), False) is True, compact,
            _const(_kw(call, 'ensure_ascii'), True), _const(_kw(call, 'allow_nan'), True))


def is_canonical(unit, node):
    if is_dumps(unit, node):
        if any(keyword.arg is None for keyword in node.keywords):
            return True  # **kwargs: the form cannot be read, so it is counted (none at base)
        sort_keys, compact, _, _ = dumps_form(node)
        return sort_keys and compact
    if isinstance(node, ast.Call) and unit.qual(node.func) == 'json.JSONEncoder':
        sort_keys, compact, _, _ = dumps_form(node)
        return sort_keys and compact
    return False


SORTED_JSON_KWARGS = {'sort_keys', 'ensure_ascii', 'allow_nan'}


def serialisation_form(unit, node):
    """'canonical' (sort_keys + compact), 'sorted_json' (sort_keys and no other formatting:
    the fifth variant), 'leaf-serialiser', or the other (display or wire) forms."""
    if not is_dumps(unit, node):
        return 'leaf-serialiser'
    sort_keys, compact, _, _ = dumps_form(node)
    if sort_keys and compact:
        return 'canonical'
    if sort_keys and {k.arg for k in node.keywords} <= SORTED_JSON_KWARGS:
        return 'sorted_json'
    if sort_keys:
        return 'sorted+' + ','.join(sorted({k.arg for k in node.keywords} - SORTED_JSON_KWARGS))
    return 'unsorted'


VARIANTS = {(True, True): 'ascii,nan-allowed (A/B)', (True, False): 'ascii,nan-refused (C/D)',
            (False, False): 'utf8,nan-refused (E)', (False, True): 'utf8,nan-allowed (F)'}


def is_sha256(unit, node):
    return isinstance(node, ast.Call) and unit.qual(node.func) == 'hashlib.sha256'


def _strip_encode(expr):
    while isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) \
            and expr.func.attr in ('encode', 'read_bytes') and not expr.args[1:]:
        if expr.func.attr == 'read_bytes':
            return expr
        expr = expr.func.value
    return expr


def _params(fn):
    a = fn.args
    return {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs} | \
        ({a.vararg.arg} if a.vararg else set()) | ({a.kwarg.arg} if a.kwarg else set())


def _local_values(unit, fn):
    values = defaultdict(list)
    for node in unit.own_nodes(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id].append(node.value)
    return values


def generic_root(unit, fn, expr, *, options=False, seen=None):
    """The parameter the hashed input is, possibly through single-argument serialisers, or None.

    A join (on any separator), a concatenation, an f-string or a literal container is a composed
    preimage, never generic. Keyword arguments of a serialiser must be literals; with
    ``options=True`` (reading the leaf itself) they may also be the function's own parameters."""
    seen = set() if seen is None else seen
    expr = _strip_encode(expr)
    params, values = _params(fn), _local_values(unit, fn)
    if isinstance(expr, ast.Name):
        if expr.id in params:
            return expr.id
        if expr.id in values and expr.id not in seen:
            seen.add(expr.id)
            roots = {generic_root(unit, fn, v, options=options, seen=seen) for v in values[expr.id]}
            return roots.pop() if len(roots) == 1 else None
        return None
    if isinstance(expr, ast.Call):
        if isinstance(expr.func, ast.Attribute) and expr.func.attr == 'join':
            return None  # separator.join(...): a composed preimage
        if isinstance(expr.func, ast.Attribute) and not expr.args and not expr.keywords:
            return generic_root(unit, fn, expr.func.value, options=options, seen=seen)  # Path(p).read_bytes()
        if isinstance(expr.func, ast.Attribute) and isinstance(expr.func.value, ast.Constant):
            return None  # a method of a constant: a composed preimage
        if len(expr.args) == 1 and all(_literal(k.value) or (options and isinstance(k.value, ast.Name)
                                                             and k.value.id in params) for k in expr.keywords):
            return generic_root(unit, fn, expr.args[0], options=options, seen=seen)
    return None


def generic_input(unit, fn, expr, seen=None):
    """The hashed input is one parameter, possibly through single-argument serialisers."""
    return generic_root(unit, fn, expr, seen=seen) is not None


def composed_input(unit, fn, expr, seen=None):
    """The hashed input composes a scheme: concatenation, an f-string, a join on a constant
    separator, or a serialisation of a literal container (the identity-preimage forms).
    Only the expression's spine is read (method receivers and local names), never the
    arguments of other calls: ``stream.readline(n + 1)`` is raw bytes, not a composition."""
    seen = set() if seen is None else seen
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return True
    if isinstance(expr, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in expr.values)
    if isinstance(expr, ast.Call):
        if isinstance(expr.func, ast.Attribute) and expr.func.attr == 'join' \
                and isinstance(expr.func.value, ast.Constant):
            return True
        if is_dumps(unit, expr):
            return bool(expr.args) and isinstance(expr.args[0], (ast.Dict, ast.List, ast.Tuple))
        if isinstance(expr.func, ast.Attribute):
            return composed_input(unit, fn, expr.func.value, seen)
        return False
    if isinstance(expr, ast.Name):
        values = _local_values(unit, fn)
        if expr.id in values and expr.id not in seen:
            seen.add(expr.id)
            return any(composed_input(unit, fn, v, seen) for v in values[expr.id])
    return False


def _literal(node):
    try:
        ast.literal_eval(node)
        return True
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False


def _hexdigest_of(unit, node):
    """The sha256 call under ``<sha256(...)>.hexdigest()``/``.digest()``, else None."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr in ('hexdigest', 'digest') and is_sha256(unit, node.func.value):
        return node.func.value
    return None


def _returns(unit, fn):
    """Every ``return <value>`` of ``fn`` itself (not of nested defs or lambdas)."""
    return [n for n in unit.own_nodes(fn) if isinstance(n, ast.Return) and n.value is not None]


def _identity_parts(expr):
    """The parts an identity is built from: string concatenation, f-string fields, tuple
    members and path joins are unwrapped; containers and call arguments are not."""
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Add, ast.Div)):
        return _identity_parts(expr.left) + _identity_parts(expr.right)
    if isinstance(expr, ast.JoinedStr):
        return [p for v in expr.values if isinstance(v, ast.FormattedValue) for p in _identity_parts(v.value)]
    if isinstance(expr, ast.Tuple):
        return [p for e in expr.elts for p in _identity_parts(e)]
    return [expr]


def _returned_digests(unit, fn):
    """sha256 calls (with input) whose hex digest a Return of ``fn`` yields as (part of) the
    returned identity: ``return <hexdigest>``, ``prefix + <hexdigest>``, an f-string or tuple
    holding it, or a local name bound directly to it."""
    values = _local_values(unit, fn)
    found = []
    for ret in _returns(unit, fn):
        for part in _identity_parts(ret.value):
            candidates = [part] + (values.get(part.id, []) if isinstance(part, ast.Name) else [])
            for candidate in candidates:
                sha = _hexdigest_of(unit, candidate)
                if sha is not None and sha.args:
                    found.append(sha)
    return list({id(s): s for s in found}.values())


def _category_site_inside(unit, node):
    for sub in ast.walk(node):
        if is_sha256(unit, sub) or is_dumps(unit, sub) or \
                (isinstance(sub, ast.Call) and unit.qual(sub.func) == 'sqlite3.connect') or \
                private_primitive(unit, sub) or nofollow_marker(sub):
            return True
    return False


def is_delegation(unit, fn):
    """A one-statement delegating def (P9): ``return f(...)`` or ``f(...)`` with no site inside."""
    body = strip_doc(fn.body)
    return (len(body) == 1 and isinstance(body[0], (ast.Return, ast.Expr))
            and isinstance(body[0].value, ast.Call) and not _category_site_inside(unit, body[0]))


# ---------------------------------------------------------------- (c) private-file primitives

CENSUS_PRIMITIVES = ('os.open+O_CREAT', 'open-x', 'os.chmod', 'Path.chmod', 'os.fchmod', 'os.mkdir(mode)',
                     'os.makedirs(mode)', 'Path.mkdir(mode)', 'tempfile.mkstemp', 'tempfile.NamedTemporaryFile',
                     'tempfile.mkdtemp', 'os.replace', 'os.link', 'os.rename', 'Path.rename', 'os.fsync',
                     'st_mode&0o077')
EXTRA_PRIMITIVES = ('os.fdopen',)  # named by the order's P3 list, not counted by the census's 144


def _mode_arg(call, index):
    mode = _kw(call, 'mode')
    if mode is None and len(call.args) > index:
        mode = call.args[index]
    return mode


def private_primitive(unit, node):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitAnd):
        sides = (node.left, node.right)
        if any(isinstance(s, ast.Attribute) and s.attr == 'st_mode' for s in sides) \
                and any(_const(s) == 0o077 for s in sides):
            return 'st_mode&0o077'
        return None
    if not isinstance(node, ast.Call):
        return None
    func, target = node.func, unit.qual(node.func)
    if target == 'os.open':
        flags = node.args[1] if len(node.args) > 1 else _kw(node, 'flags')
        if flags is not None and any(isinstance(x, ast.Attribute) and x.attr == 'O_CREAT'
                                     for x in ast.walk(flags)):
            return 'os.open+O_CREAT'
        return None
    if (isinstance(func, ast.Name) and func.id == 'open' and 'open' not in unit.aliases) or target == 'io.open':
        mode = _mode_arg(node, 1)
        return 'open-x' if isinstance(_const(mode), str) and 'x' in mode.value else None
    if isinstance(func, ast.Attribute) and func.attr == 'open' and target is None:
        mode = _mode_arg(node, 0)
        return 'open-x' if isinstance(_const(mode), str) and 'x' in mode.value else None
    if target in ('os.chmod', 'os.fchmod', 'tempfile.mkstemp', 'tempfile.NamedTemporaryFile',
                  'tempfile.mkdtemp', 'os.replace', 'os.link', 'os.rename', 'os.fsync', 'os.fdopen'):
        return target
    if target in ('os.mkdir', 'os.makedirs'):
        return target + '(mode)' if _mode_arg(node, 1) is not None else None
    if isinstance(func, ast.Attribute) and target is None:
        if func.attr == 'chmod':
            return 'Path.chmod'
        if func.attr == 'mkdir':
            return 'Path.mkdir(mode)' if _kw(node, 'mode') is not None else None
        if func.attr == 'rename':
            return 'Path.rename'
    return None


def nofollow_marker(node):
    if isinstance(node, ast.Attribute) and node.attr == 'O_NOFOLLOW':
        return True
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'getattr'
            and len(node.args) >= 2 and _const(node.args[1]) == 'O_NOFOLLOW')


# The census's 30 private-file helper definitions (its (c) list plus the re-census's two
# lock helpers). "Helper" there is a purpose reading, not a pattern; the guard keeps the
# census's own enumeration so that, at head, each is gone or a one-statement delegation.
CENSUS_PRIVATE_HELPERS = (
    (T + '_impl/service/launch_binding.py', 'write_private'),
    (T + '_impl/service/launch_binding.py', '_write_once'),
    (T + '_impl/service/host_adapter.py', '_write_new'),
    (T + '_impl/service/host_adapter.py', '_replace'),
    (T + '_impl/service/desk_catalog_setup.py', '_write_private_json'),
    (T + '_impl/service/model_gateway.py', '_write_private'),
    (T + '_impl/runtime_install.py', '_write'),
    (T + '_impl/runtime_install.py', '_placeholder'),
    (T + '_impl/scip_navigation.py', '_atomic'),
    (T + '_impl/semantic_index.py', '_write_once'),
    (T + '_impl/navigation_snapshot.py', '_write_once'),
    (T + 'refresh_cli.py', 'atomic'),
    (O + 'knowledge_publish_cli.py', '_write'),
    (T + '_impl/service/desk_registry.py', 'RoleRoster._write'),
    (T + '_impl/navigation_search_pages.py', '_save'),
    (T + '_impl/navigation_search_pages.py', '_store_manifest'),
    (T + '_impl/service/launch_binding.py', 'private_dir'),
    (T + '_impl/service/launch_binding.py', '_locked'),
    (T + '_impl/service/host_adapter.py', '_private_dir'),
    (T + '_impl/service/desk_catalog_setup.py', '_private_parent'),
    (T + '_impl/service/desk_memory_runtime.py', 'private_json'),
    (T + '_impl/service/desk_memory_runtime.py', '_private_json'),
    (T + '_impl/service/desk_registry.py', '_private_json'),
    (T + '_impl/service/host_adapter.py', '_owned'),
    (T + '_impl/service/host_adapter.py', '_read_json'),
    (T + '_impl/service/model_gateway.py', 'read_api_key'),
    (T + '_impl/navigation_snapshot.py', '_read_regular'),
    (T + '_impl/service/native_history_import.py', '_regular'),
    (T + '_impl/service/desk_registry.py', 'RoleRoster._locked'),
    (T + '_impl/service/spool_ingest.py', 'Cursors.locked'),
)


# ---------------------------------------------------------------- the census

STORE_RULE_NAMES = ('outside_store', 'required_store_path')
STORE_RULE_CONSTANTS = ('STORE_KEYS', 'TEMPLATE_KEY', 'LEGACY_STORES')
STORE_FILE = re.compile(r'\.sqlite3')


class Census:
    def __init__(self, root=ROOT, files=None):
        self.root = Path(root)
        self.units = load_units(self.root, files)
        self.leaf = any(u.rel == LEAF for u in self.units)
        self.sites = defaultdict(list)
        self.functions = {}  # (rel, qualname) -> (unit, fn) for module code
        for unit in self.units:
            if unit.embedded is None:
                for fn in unit.functions():
                    self.functions.setdefault((unit.rel, unit.qualname(fn)), (unit, fn))
        self.generic_names = defaultdict(set)  # rel -> generic sha helper names (for (a5))
        # (module, function) -> 'digest' | 'serialiser': module-level helpers a digest construction
        # can go through (a generic sha helper, a canonical/sorted serialiser helper, or a
        # one-statement delegation to the leaf's digest or serialiser).
        self.roles = {}
        # Leaf functions classified by what they do (not by their names): 'serialiser' returns a
        # json.dumps output (or another leaf serialiser's); 'digest' returns a sha256 hex digest
        # (or another leaf digest's), possibly prefixed or beside other values; readers are neither.
        self.leaf_roles = self._leaf_roles()
        self.leaf_hashed = self._leaf_hashed_params()
        for unit in self.units:
            self._scan_hashing(unit)
        for unit in self.units:
            self._scan_roles(unit)
        for unit in self.units:
            self._scan(unit)

    # -- hashing helpers first: (a5) needs to know which local names are sha helpers
    def _scan_hashing(self, unit):
        for fn in unit.functions():
            if is_delegation(unit, fn):
                continue
            digests = _returned_digests(unit, fn)
            body = strip_doc(fn.body)
            leaf_digest = self._returned_leaf_digests(unit, fn)
            generic = [d for d in digests if generic_input(unit, fn, d.args[0])]
            # The base's generic-input rule, applied to leaf digest calls too (Coordinator ruling):
            # generic only when the digest's input is (a serialisation of) the def's own parameter.
            leaf_generic = [c for c in leaf_digest if self._leaf_call_input(unit, fn, c) == 'generic']
            if len(body) <= 3 and (generic or (leaf_generic and not digests)):
                self.sites['b.generic'].append(_site_def(unit, 'b.generic', fn, 'generic sha256 helper'))
                self.generic_names[unit.rel].add(fn.name)
            elif any(not generic_input(unit, fn, d.args[0]) and composed_input(unit, fn, d.args[0])
                     for d in digests) or any(self._leaf_call_input(unit, fn, c) == 'composed'
                                              for c in leaf_digest):
                self.sites['b.domain'].append(_site_def(unit, 'b.domain', fn, 'domain identity hasher'))

    def _leaf_roles(self):
        unit = next((u for u in self.units if u.rel == LEAF and u.embedded is None), None)
        roles = {}
        if unit is None:
            return roles
        functions = [n for n in unit.tree.body if isinstance(n, FUNCS)]
        changed = True
        while changed:
            changed = False
            for fn in functions:
                if fn.name not in roles:
                    role = self._leaf_role_of(unit, fn, roles)
                    if role:
                        roles[fn.name] = role
                        changed = True
        return roles

    @staticmethod
    def _leaf_role_of(unit, fn, roles):
        values = _local_values(unit, fn)

        def kind(expr, seen):
            while isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) \
                    and expr.func.attr in ('encode', 'decode') and isinstance(expr.func.value, ast.Call):
                expr = expr.func.value
            if _hexdigest_of(unit, expr) is not None:
                return 'digest'
            if is_dumps(unit, expr):
                return 'serialiser'
            if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id in roles:
                return roles[expr.func.id]
            if isinstance(expr, ast.Name) and expr.id in values and expr.id not in seen:
                found = {kind(v, seen | {expr.id}) for v in values[expr.id]} - {None}
                return 'digest' if 'digest' in found else found.pop() if found else None
            return None

        kinds = {kind(part, frozenset()) for ret in _returns(unit, fn) for part in _identity_parts(ret.value)}
        if 'digest' in kinds:
            return 'digest'
        if 'serialiser' in kinds and not any(is_sha256(unit, n) for n in unit.own_nodes(fn)):
            return 'serialiser'
        return None

    def _leaf_hashed_params(self):
        """Leaf digest function -> (its parameter names, the parameter its digest hashes
        generically), or (names, None) when it composes its preimage (a join, a concatenation...)."""
        unit = next((u for u in self.units if u.rel == LEAF and u.embedded is None), None)
        found = {}
        if unit is None:
            return found
        functions = {n.name: n for n in unit.tree.body if isinstance(n, FUNCS)
                     and self.leaf_roles.get(n.name) == 'digest'}
        signature = {name: [a.arg for a in fn.args.posonlyargs + fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
                     for name, fn in functions.items()}

        def argument(call, callee, param):
            names = [a.arg for a in functions[callee].args.posonlyargs + functions[callee].args.args]
            if param in names and names.index(param) < len(call.args):
                return call.args[names.index(param)]
            return next((k.value for k in call.keywords if k.arg == param), None)

        changed = True
        while changed:
            changed = False
            for name, fn in functions.items():
                if name in found:
                    continue
                values = _local_values(unit, fn)
                roots, decided = set(), True
                for ret in _returns(unit, fn):
                    for part in _identity_parts(ret.value):
                        for candidate in [part] + (values.get(part.id, []) if isinstance(part, ast.Name) else []):
                            sha = _hexdigest_of(unit, candidate)
                            if sha is not None and sha.args:
                                roots.add(generic_root(unit, fn, sha.args[0], options=True))
                            elif isinstance(candidate, ast.Call) and isinstance(candidate.func, ast.Name) \
                                    and candidate.func.id in functions:
                                callee = candidate.func.id
                                if callee not in found:
                                    decided = False
                                    continue
                                param = found[callee][1]
                                arg = argument(candidate, callee, param) if param else None
                                roots.add(generic_root(unit, fn, arg, options=True) if arg is not None else None)
                if decided:
                    found[name] = (signature[name], roots.pop() if len(roots) == 1 else None)
                    changed = True
        return found

    def _leaf_call_input(self, unit, fn, call):
        """'generic' when a leaf digest call hashes (a serialisation of) ``fn``'s own parameter,
        'composed' when it hashes a composed preimage, else None (raw bytes of a local)."""
        name = (unit.qual(call.func) or '').rsplit('.', 1)[-1]
        params, hashed = self.leaf_hashed.get(name, ((), None))
        if hashed is None:
            return 'composed' if name in self.leaf_hashed else None
        positional = [p for p in params]
        arg = None
        if hashed in positional and positional.index(hashed) < len(call.args):
            arg = call.args[positional.index(hashed)]
        arg = arg if arg is not None else next((k.value for k in call.keywords if k.arg == hashed), None)
        if arg is None:
            return None
        if generic_input(unit, fn, arg):
            return 'generic'
        return 'composed' if composed_input(unit, fn, _strip_encode(arg)) else None

    def leaf_role(self, unit, call):
        """'serialiser' or 'digest' when ``call`` calls a leaf function of that role, else None."""
        target = unit.qual(call.func) if isinstance(call, ast.Call) else None
        if target and target.startswith(LEAF_MODULE + '.'):
            return self.leaf_roles.get(target.rsplit('.', 1)[1])
        return None

    def _returned_leaf_digests(self, unit, fn):
        """Leaf digest calls that ARE (part of) the returned identity: returned directly,
        prefixed, in an f-string or tuple, or through a local name bound directly to one; never a
        field of a returned container."""
        values = _local_values(unit, fn)
        found = []
        for ret in _returns(unit, fn):
            for part in _identity_parts(ret.value):
                for candidate in [part] + (values.get(part.id, []) if isinstance(part, ast.Name) else []):
                    if self.leaf_role(unit, candidate) == 'digest':
                        found.append(candidate)
        return found

    def _scan_roles(self, unit):
        if unit.embedded is not None:
            return
        module = module_name(unit.rel)
        for fn in unit.tree.body:
            if not isinstance(fn, FUNCS):
                continue
            own = unit.own_nodes(fn)
            body = strip_doc(fn.body)
            if fn.name in self.generic_names[unit.rel]:
                self.roles[(module, fn.name)] = 'digest'
            elif is_delegation(unit, fn):
                role = self.leaf_role(unit, body[0].value)
                if role:
                    self.roles[(module, fn.name)] = role
            elif len(body) <= 3 and not any(is_sha256(unit, n) for n in own) and any(
                    is_dumps(unit, n) and serialisation_form(unit, n) in ('canonical', 'sorted_json')
                    for n in own):
                self.roles[(module, fn.name)] = 'serialiser'

    def role_of(self, unit, call):
        """The helper role of a called module-level function (same module or imported), or None."""
        if not isinstance(call, ast.Call) or unit.embedded is not None:
            return None
        func = call.func
        if isinstance(func, ast.Name) and func.id not in unit.aliases:
            return self.roles.get((module_name(unit.rel), func.id))
        target = unit.qual(func)
        if target and '.' in target:
            module, _, name = target.rpartition('.')
            return self.roles.get((module, name))
        return None

    def _scan(self, unit):
        add = self.sites
        for node in ast.walk(unit.tree):
            if id(node) in unit.skip:
                continue
            # (a) canonical serialisation
            if is_canonical(unit, node):
                fn = unit.enclosing(node)
                _, _, ascii_, nan = dumps_form(node)
                helper = fn is not None and len(strip_doc(fn.body)) <= 3
                add['a.canonical'].append(_site(unit, 'a.canonical', 'helper' if helper else 'inline', node,
                                                VARIANTS[(bool(ascii_), bool(nan))]))
            serialiser_helper = self.role_of(unit, node) == 'serialiser'
            if is_dumps(unit, node) or serialiser_helper or self.leaf_role(unit, node) == 'serialiser':
                fed = self._feeds_digest(unit, node)
                if fed:
                    form = ('serialiser-helper ' + ast.unparse(node.func) if serialiser_helper
                            else serialisation_form(unit, node))
                    shape = ' (its hashed bytes are also written)' if self._bytes_also_written(unit, node) else ''
                    add['a.digest'].append(_site(unit, 'a.digest', form, node, f'feeds {fed}{shape}'))
                    if form == 'sorted_json':
                        add['a.sorted_json'].append(_site(unit, 'a.sorted_json', 'sorted_json', node, f'feeds {fed}'))
            # (b) sha256 calls
            if is_sha256(unit, node):
                add['b.sha256'].append(_site(unit, 'b.sha256', 'streaming' if not node.args else 'call', node))
            # (c) private-file primitives
            kind = private_primitive(unit, node)
            if kind:
                add['c.primitive'].append(_site(unit, 'c.primitive', kind, node))
            if nofollow_marker(node):
                add['c.nofollow'].append(_site(unit, 'c.nofollow', 'O_NOFOLLOW', node))
            # (d) sqlite3.connect
            if isinstance(node, ast.Call) and unit.qual(node.func) == 'sqlite3.connect':
                add['d.connect'].append(_site(unit, 'd.connect', 'sqlite3.connect', node, ast.unparse(node)[:120]))
            # (e) the store-path rule
            if isinstance(node, ast.Constant) and node.value == '/state/memory' and id(node) not in unit.docstrings:
                add['e.rule'].append(_site(unit, 'e.rule', "'/state/memory'", node))
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in STORE_RULE_CONSTANTS
                                                    for t in node.targets) \
                    and isinstance(node.value, (ast.Tuple, ast.List, ast.Set, ast.Constant, ast.Call)) \
                    and any(isinstance(c, ast.Constant) and isinstance(c.value, str) for c in ast.walk(node.value)):
                add['e.rule'].append(_site(unit, 'e.rule', 'literal ' + ast.unparse(node.targets[0]), node))
            if isinstance(node, FUNCS) and node.name in STORE_RULE_NAMES and not is_delegation(unit, node):
                add['e.rule'].append(_site_def(unit, 'e.rule', node, 'def ' + node.name))
            if isinstance(node, ast.Subscript) and _const(node.slice) == 'state_root':
                add['e.state_root'].append(_site(unit, 'e.state_root', "['state_root']", node))
            # (h) store filenames
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in unit.docstrings \
                    and STORE_FILE.search(node.value):
                add['h.store_file'].append(_site(unit, 'h.store_file', repr(node.value)[:60], node))
        # (c) and (d) helper definitions (mechanical readings)
        for fn in unit.functions():
            if is_delegation(unit, fn):
                continue
            own = unit.own_nodes(fn)
            if any(private_primitive(unit, n) or nofollow_marker(n) for n in own):
                add['c.functions'].append(_site_def(unit, 'c.functions', fn, 'contains a private-file primitive'))
            connects = [n for n in own if isinstance(n, ast.Call) and unit.qual(n.func) == 'sqlite3.connect']
            if connects:
                values = _local_values(unit, fn)
                for ret in _returns(unit, fn):
                    parts = _identity_parts(ret.value)
                    if any(p in connects for p in parts) or any(
                            v in connects for p in parts if isinstance(p, ast.Name) for v in values.get(p.id, ())):
                        add['d.helper'].append(_site_def(unit, 'd.helper', fn, 'returns a new connection'))
                        break

    def _feeds_digest(self, unit, node):
        """'sha256' or a helper name when the serialisation's value reaches a digest."""
        def digest_call(call):
            if is_sha256(unit, call) and call.args:
                return 'hashlib.sha256'
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                    and call.func.id in self.generic_names[unit.rel] | {n.rsplit('.', 1)[-1] for n in self._imported_generic(unit)}:
                return call.func.id
            if self.leaf_role(unit, call) == 'digest':
                return unit.qual(call.func)
            if self.role_of(unit, call) == 'digest':
                return ast.unparse(call.func)
            return None
        parent = unit.parents.get(id(node))
        while parent is not None and not isinstance(parent, ast.stmt):
            hit = digest_call(parent)
            if hit:
                return hit
            parent = unit.parents.get(id(parent))
        # local dataflow: the value is bound (assign / append) to a name a digest call reads
        stmt, names = parent, set()
        if isinstance(stmt, ast.Assign):
            names = {t.id for t in stmt.targets if isinstance(t, ast.Name)}
        elif isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
            names = {stmt.target.id}
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) \
                and isinstance(stmt.value.func, ast.Attribute) and stmt.value.func.attr in ('append', 'extend') \
                and isinstance(stmt.value.func.value, ast.Name):
            names = {stmt.value.func.value.id}
        if not names:
            return None
        scope = unit.enclosing(node) or unit.tree
        for sub in (unit.own_nodes(scope) if isinstance(scope, FUNCS) else ast.walk(scope)):
            hit = digest_call(sub)
            if hit and any(isinstance(n, ast.Name) and n.id in names for a in sub.args for n in ast.walk(a)):
                return hit
        return None

    @staticmethod
    def _bytes_also_written(unit, node):
        """The file-content shape (report only): the serialisation is bound to a local name that a
        write call (``write``, ``write_bytes``, ``_atomic``, ``_write_once``...) also receives."""
        stmt = unit.parents.get(id(node))
        while stmt is not None and not isinstance(stmt, ast.stmt):
            stmt = unit.parents.get(id(stmt))
        if not isinstance(stmt, ast.Assign):
            return False
        names = {t.id for t in stmt.targets if isinstance(t, ast.Name)}
        scope = unit.enclosing(node)
        if not names or scope is None:
            return False
        for sub in unit.own_nodes(scope):
            if isinstance(sub, ast.Call) and re.search(r'write|atomic', ast.unparse(sub.func).rsplit('.', 1)[-1]) \
                    and any(isinstance(a, ast.Name) and a.id in names for a in sub.args):
                return True
        return False

    def _imported_generic(self, unit):
        found = set()
        for local, target in unit.aliases.items():
            module, _, name = target.rpartition('.')
            for rel, names in self.generic_names.items():
                if module_name(rel) == module and name in names:
                    found.add(local)
        return found

    # -- readings
    def outside_leaf(self, category):
        return [s for s in self.sites[category] if s.rel != LEAF]

    def split(self, category, sites=None):
        sites = self.sites[category] if sites is None else sites
        module = [s for s in sites if not s.embedded and s.rel != LEAF]
        embedded = [s for s in sites if s.embedded and s.rel != LEAF]
        return module, embedded


def _site_def(unit, category, fn, detail):
    return Site(category, 'def', unit.rel, unit.line(fn), unit.embedded, unit.qualname(fn), detail)


# ---------------------------------------------------------------- (iii) identity schemes

def _return_prefixes(unit, fn):
    prefixes = set()
    for ret in _returns(unit, fn):
        for node in ast.walk(ret.value):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
                prefixes.add(node.value)
    return prefixes


def second_implementations(census):
    """Domain identity hashers implemented more than once outside the leaf (P2 iii).

    Two definitions implement the same scheme when they share a name in different
    modules, or return the same scheme prefix (other than a bare 'sha256:').
    """
    hashers = [s for s in census.sites['b.domain'] if s.rel != LEAF and not s.embedded]
    keys = {}
    for s in hashers:
        unit, fn = census.functions[(s.rel, s.function)]
        keys[s] = ({s.function.rsplit('.', 1)[-1]},
                   {p for p in _return_prefixes(unit, fn) if p.endswith(':') and p != 'sha256:'})
    groups, done = [], set()
    for s in hashers:
        if s in done:
            continue
        group, frontier = {s}, [s]
        while frontier:
            current = frontier.pop()
            for other in hashers:
                if other not in group and other.rel != current.rel and (
                        keys[other][0] & keys[current][0] or keys[other][1] & keys[current][1]):
                    group.add(other)
                    frontier.append(other)
        done |= group
        if len(group) > 1:
            groups.append(sorted(group, key=lambda x: (x.rel, x.line)))
    return groups


# ---------------------------------------------------------------- (f) near-duplicate functions

def _normalised_statements(fn):
    """Per-statement ``ast.dump``s: local aliases renamed, string constants dropped.

    A local alias is ``name = a.b.C`` (or ``name = C``) inside the function; its uses are
    renamed to ``C``. Every statement node of the function is one element (compound
    statements are dumped without their nested statement bodies)."""
    aliases = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, (ast.Attribute, ast.Name)):
            value = node.value
            aliases[node.targets[0].id] = value.attr if isinstance(value, ast.Attribute) else value.id

    class Normalise(ast.NodeTransformer):
        def visit_Name(self, node):
            return ast.copy_location(ast.Name(id=aliases.get(node.id, node.id), ctx=node.ctx), node)

        def visit_Constant(self, node):
            if isinstance(node.value, str):
                return ast.copy_location(ast.Constant(value='<str>'), node)
            return node

    out = []
    for stmt in ast.walk(fn):
        if not isinstance(stmt, ast.stmt) or stmt is fn:
            continue
        shallow = Normalise().visit(_shallow_copy(stmt))
        out.append(ast.dump(shallow, annotate_fields=False))
    return out


def _shallow_copy(stmt):
    import copy
    clone = copy.deepcopy(stmt)
    for name in ('body', 'orelse', 'finalbody', 'handlers', 'cases'):
        if isinstance(getattr(clone, name, None), list):
            setattr(clone, name, [])
    return clone


def near_duplicates(census, min_statements=15, threshold=0.90):
    candidates = []
    for unit in census.units:
        if unit.embedded is not None:
            continue
        for fn in unit.functions():
            if is_delegation(unit, fn):
                continue
            statements = _normalised_statements(fn)
            if len(statements) >= min_statements:
                candidates.append((unit.rel, unit.qualname(fn), unit.line(fn), statements))
    pairs = []
    for i, (rel_a, name_a, line_a, a) in enumerate(candidates):
        for rel_b, name_b, line_b, b in candidates[i + 1:]:
            if min(len(a), len(b)) / max(len(a), len(b)) < threshold:
                continue
            matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
            if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
                continue
            ratio = matcher.ratio()
            if ratio >= threshold:
                pairs.append((round(ratio, 3), f'{rel_a}:{line_a} {name_a}', f'{rel_b}:{line_b} {name_b}'))
    return sorted(pairs, reverse=True)


# ---------------------------------------------------------------- (g) dead code

FRAMEWORK_OVERRIDES = ('get_request', 'log_message', 'do_GET', 'do_POST')
WORD = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


@dataclass(frozen=True)
class Definition:
    rel: str
    name: str
    qualname: str
    start: int  # first decorator line: the span a reference must fall outside of
    end: int
    module_level: bool
    protocol_stub: bool
    line: int  # the def/class line (the census counts lines from here)
    leaf_delegation: bool = False  # a one-statement def whose call resolves into the leaf (P9)


def _definitions(census):
    found, total = [], 0
    for unit in census.units:
        if unit.embedded is not None:
            continue
        total += sum(isinstance(n, FUNCS + (ast.ClassDef,)) for n in ast.walk(unit.tree))
        protocols = set()
        for node in ast.walk(unit.tree):
            if isinstance(node, ast.ClassDef) and any(
                    (isinstance(b, ast.Name) and b.id == 'Protocol') or
                    (isinstance(b, ast.Attribute) and b.attr == 'Protocol') for b in node.bases):
                protocols.add(id(node))
        for node in ast.walk(unit.tree):
            if isinstance(node, FUNCS + (ast.ClassDef,)):
                if node.name.startswith('__') and node.name.endswith('__'):
                    continue
                parent = unit.parents.get(id(node))
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                stub = id(parent) in protocols and isinstance(node, FUNCS) and all(
                    isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) for s in node.body)
                delegation = (isinstance(node, FUNCS) and is_delegation(unit, node)
                              and unit.is_leaf_call(strip_doc(node.body)[0].value, r'.'))
                found.append(Definition(unit.rel, node.name, unit.qualname(node) if isinstance(node, FUNCS)
                                        else _class_qualname(unit, node), start, node.end_lineno,
                                        isinstance(parent, ast.Module), stub, node.lineno, delegation))
    return found, total


def _class_qualname(unit, node):
    names, parent = [node.name], unit.parents.get(id(node))
    while parent is not None:
        if isinstance(parent, (ast.ClassDef,) + FUNCS):
            names.append(parent.name)
        parent = unit.parents.get(id(parent))
    return '.'.join(reversed(names))


# The census's own output and this instrument name every definition they classify; they
# are not references. (The T11a order and its census appendix landed after the census commit, so the
# census never saw them; the T11a test files would otherwise keep every allowlisted name
# alive.)
SELF_REFERENCES = re.compile(r'(^docs/work/orders/T11a-|(^|/)t11a_|(^|/)test_t11_|(^|/)test_t11a_|/t11a/)')


def _reference_index(root):
    """name -> [(file, line)] over every tracked .py file (Name, Attribute, alias, string
    tokens) and word tokens of every tracked non-.py file outside apps/kanban; plus the set
    of files mentioning each word (for the module-name rule)."""
    refs, mentions = defaultdict(list), defaultdict(set)
    for rel in tracked(root):
        path = Path(root) / rel
        if not path.is_file() or SELF_REFERENCES.search(rel):
            continue
        if rel.endswith('.py'):
            try:
                text = path.read_text(encoding='utf-8')
                tree = ast.parse(text)
            except (UnicodeDecodeError, SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                names = ()
                if isinstance(node, ast.Name):
                    names = (node.id,)
                elif isinstance(node, ast.Attribute):
                    names = (node.attr,)
                elif isinstance(node, ast.alias):
                    names = tuple(node.name.split('.')) + ((node.asname,) if node.asname else ())
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = tuple(node.module.split('.'))
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    names = tuple(WORD.findall(node.value))
                line = getattr(node, 'lineno', 0)
                for name in names:
                    refs[name].append((rel, line))
                    mentions[name].add(rel)
        elif not rel.startswith('apps/kanban/'):
            try:
                text = path.read_text(encoding='utf-8')
            except (UnicodeDecodeError, ValueError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                for name in WORD.findall(line):
                    refs[name].append((rel, number))
                    mentions[name].add(rel)
    return refs, mentions


def dead_code(census, refs=None):
    """Definitions with no reference outside their own span, to a fixpoint (census (g)).

    Census step 5: dunders and the stdlib framework overrides of workspace_setup_server
    (FRAMEWORK_OVERRIDES) are not candidates; the framework calls them, so they stay live
    roots and what they reference stays live. Returns (dead, nested-in-dead, total defs)."""
    if refs is None:
        refs = _reference_index(census.root)
    references, mentions = refs
    definitions, total = _definitions(census)
    roots = {d for d in definitions if d.name in FRAMEWORK_OVERRIDES}
    spans = defaultdict(list)
    for d in definitions:
        spans[d.rel].append(d)
    dead = set()
    while True:
        dead_spans = defaultdict(list)
        for d in dead:
            dead_spans[d.rel].append((d.start, d.end))
        changed = False
        for d in definitions:
            if d in dead or d in roots:
                continue
            module = Path(d.rel).stem if Path(d.rel).stem != '__init__' else Path(d.rel).parent.name
            live = False
            for rel, line in references.get(d.name, ()):
                if rel == d.rel and d.start <= line <= d.end:
                    continue
                if any(a <= line <= b for a, b in dead_spans.get(rel, ())):
                    continue
                if d.module_level and rel != d.rel and rel not in mentions.get(module, ()):
                    continue
                live = True
                break
            if not live:
                dead.add(d)
                changed = True
        if not changed:
            break
    nested = {d for d in dead for o in dead if o is not d and o.rel == d.rel and o.start <= d.start
              and d.end <= o.end and (o.start, o.end) != (d.start, d.end)}
    return sorted(dead, key=lambda d: (d.rel, d.start)), nested, total
