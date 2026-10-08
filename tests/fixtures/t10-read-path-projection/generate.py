"""Generate the T10 P5 golden outputs FROM BASE, with a frozen clock.

The product code must be the order's base: ``packages/tooling/src`` committed at HEAD and unchanged,
with the content hash ``BASE_PRODUCT_SHA256`` (the sha256 of its ``git ls-tree -r`` listing).
The generator refuses otherwise. That base is in the private history only; the public history does
not contain it, so these goldens cannot be regenerated from the public history as it stands. Command, from the repository root, with the
interpreter that has the package installed editable from this checkout:

    TMPDIR=<scratch dir> python tests/fixtures/t10-read-path-projection/generate.py

It replays ``t10_corpus.STEPS`` (public writes only), runs ``t10_corpus.battery``
after every step, checks every step against the base ``select`` oracle
(``t10_oracle``), and writes ``goldens.json`` beside this file:

* ``meta``: this generator's own sha256 (``generator_sha256``), the base product's content hash
  (``product_sha256``), the clock, the command;
* ``base_schema``: tables, columns and indexes of the episode store written by
  the base code for this corpus (the P6 older-writer emulation copies exactly
  these);
* ``calls``: per read key, the session, tool and arguments (stable across steps);
* ``steps``: per write step, the write receipt and, per read key, the canonical
  digest (``t10_corpus.digest``: the first 32 hex digits of the sha256 of the
  sorted, compact, ASCII JSON) of the output;
* ``outputs``: per read key, the full output at its first and final step (the
  digests pin every intermediate output; rerun this generator at base to see
  any of them in full).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = HERE.parents[1]
ROOT = TESTS.parent
BASE_PRODUCT_SHA256 = '01667da11242ee4128eaf9b340d690f02753d6ce8caaedffb5a029246810a289'
COMMAND = 'TMPDIR=<scratch dir> python tests/fixtures/t10-read-path-projection/generate.py'


def _git(*args):
    return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True, check=False)


def product_at_base():
    listing = subprocess.run(['git', '-C', str(ROOT), 'ls-tree', '-r', 'HEAD', '--', 'packages/tooling/src'],
                             capture_output=True, check=True).stdout
    digest = hashlib.sha256(listing).hexdigest()
    changed = _git('diff', '--quiet', 'HEAD', '--', 'packages/tooling/src').returncode
    untracked = _git('ls-files', '--others', '--exclude-standard', '--', 'packages/tooling/src').stdout.strip()
    return digest, digest == BASE_PRODUCT_SHA256 and changed == 0 and not untracked


def main():
    sys.path.insert(0, str(TESTS))
    import kp_agent_tooling
    import t10_corpus
    import t10_world
    source = Path(kp_agent_tooling.__file__).resolve()
    if not source.is_relative_to((ROOT / 'packages/tooling/src').resolve()):
        raise SystemExit(f'kp_agent_tooling is imported from {source}, not from this checkout')
    product, clean = product_at_base()
    if not clean:
        raise SystemExit('packages/tooling/src differs from the base product tree; goldens are generated FROM BASE only')
    with tempfile.TemporaryDirectory(prefix='t10-goldens-') as scratch:
        problems = []

        def check(ctx, name):
            mismatches = t10_corpus.oracle_mismatches(ctx)
            problems.extend(f'{name}: {m}' for m in mismatches)

        ctx, record = t10_corpus.run(Path(scratch) / 'corpus', on_step=check)
        if problems:
            raise SystemExit('base disagrees with its own select oracle:\n' + '\n'.join(problems[:50]))
        schema = t10_world.base_schema(ctx.world.store_path)
    calls, outputs, steps = {}, {}, []
    for number, entry in enumerate(record):
        digests = {}
        for key, read in entry['reads'].items():
            spec = {'session': read['session'], 'tool': read['tool'], 'arguments': read['arguments']}
            if calls.setdefault(key, spec) != spec:
                raise SystemExit(f'read {key} changed its call between steps')
            digests[key] = t10_corpus.digest(read['output'])
            if key not in outputs:
                outputs[key] = {'first_step': number, 'first': read['output']}
            outputs[key]['final'] = read['output']
            outputs[key]['final_step'] = number
        steps.append({'step': entry['step'], 'write': entry['write'], 'digests': digests})
    generator = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    goldens = {'meta': {'generator_sha256': generator, 'product_sha256': product,
                        'frozen_clock': t10_world.FROZEN_AT.isoformat(),
                        'command': COMMAND, 'reads': sum(len(s['digests']) for s in steps),
                        'steps': len(steps)},
               'base_schema': schema, 'calls': calls, 'outputs': outputs, 'steps': steps}
    target = HERE / 'goldens.json'
    target.write_text(json.dumps(goldens, sort_keys=True, indent=None, separators=(',', ':')) + '\n')
    print(json.dumps({'written': str(target.relative_to(ROOT)), 'bytes': target.stat().st_size,
                      **goldens['meta']}))


if __name__ == '__main__':
    main()
