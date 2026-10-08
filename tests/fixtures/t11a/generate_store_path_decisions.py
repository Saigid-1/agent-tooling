"""P5 golden: the store-path decisions of BOTH consumers of the rule (T11a order, P5; U3).

Generated at base by:

    python tests/fixtures/t11a/generate_store_path_decisions.py

which rewrites tests/fixtures/t11a/store_path_decisions.json; tests/test_t11a_p5_store_path_decisions.py
recomputes it and compares byte for byte.

The consumers are driven through their own code paths, one operator file per case (FEATURE's
p5_check2.py was the starting point):
- deploy/image/container.py ``operator_store_paths`` (the role preflight's refusal list),
  imported by path from this tree's deploy/image/container.py;
- kp_agent_tooling._impl.runtime_install ``operator_store_paths`` (the installer's plan).

Each case is one key (state_root, roster_path, config_template, or another key) with one value:
paths inside and outside /state/memory, lexical tricks, non-paths, and control characters, where
the two consumers differ at base (runtime_install rejects them; container.py does not).
Normalised: the scratch root (``<root>``).
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
TREE = HERE.parents[3]
sys.path.insert(0, str(HERE.parents[2]))  # tests/

from t11a_golden import golden, jsonable, write_golden  # noqa: E402

GOLDEN = 'store_path_decisions.json'
KEYS = ('state_root', 'roster_path', 'config_template', 'other')
VALUES = (
    '', 'relative/x', '/', '/state', '/state/', '/state/memory', '/state/memory/', '/state/memory/x',
    '/state/memoryx', '/state/registry', '/state/registry/roster.json', '/state/../state/x',
    '/state/./memory/../registry', '/config/x.json', '/statefoo/x', '//state/x', '/state//registry',
    '/stéte/x', '/state/a/../../etc', '/state/memory/../..', 3, None, ['/state/x'], {'path': '/state/x'},
    # control characters: rejected by runtime_install, not by container.py, at base
    '/state/x\n', '/state/x\x01y', '/state/x\ty', '/state/x\x7f', '/state/memory/x\x00',
    '/state/memory/x\x01y', '/state/registry\x1f', '\x01', '/config/t\n.json', '/state/x\x85y',
    '/state/x y',
)


def container_module():
    spec = importlib.util.spec_from_file_location('t11a_container', TREE / 'deploy' / 'image' / 'container.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build():
    container = container_module()
    runtime_install = importlib.import_module('kp_agent_tooling._impl.runtime_install')
    out = {}
    for key in KEYS:
        for value in VALUES:
            with tempfile.TemporaryDirectory(prefix='t11a-p5-') as name:
                root = os.path.realpath(name)
                (Path(root) / 'config').mkdir()
                (Path(root) / 'config' / 'op.json').write_text(json.dumps({key: value}))
                found = container.operator_store_paths(Path(root) / 'config')
                rows = runtime_install.operator_store_paths(Path(root))
                case = f'{key}|{value!r}'
                out.setdefault('container.operator_store_paths', {})[case] = {
                    'refuse': bool(found), 'text': [line.replace(root, '<root>') for line in found]}
                out.setdefault('runtime_install.operator_store_paths', {})[case] = {
                    'refuse': bool(rows),
                    'rows': json.loads(json.dumps(jsonable(rows)).replace(json.dumps(root)[1:-1], '<root>'))}
    return golden(out)


def main():
    text = build()
    write_golden(text, GOLDEN)
    print(f'wrote tests/fixtures/t11a/{GOLDEN} ({len(text)} bytes)')


if __name__ == '__main__':
    main()
