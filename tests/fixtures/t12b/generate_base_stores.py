"""T12b B1 fixtures: stores CREATED AT BASE, for the existing-store outbox falsifier.

Order: docs/work/orders/T12b-one-indexer-outbox.md, B1, "existing stores (r5): a store created at BASE
(a T10 corpus store, and one shaped like the live T10h-upgraded store), sealed through each of the eight
statements by the T12b code, not having exactly one outbox row per insert".

Generated ONCE, at T12b's base, by:

    python tests/fixtures/t12b/generate_base_stores.py

It refuses to run on a tree whose product already has an outbox (a store it creates holds `index_outbox`),
so the committed files can only have been written by base code. Each store directory holds the state
root's SQLite files as SQLite's backup API copied them (`World.snapshot`), gzipped byte for byte
(`<name>.sqlite3.gz`: the repository ignores `*.sqlite3`), and `ids.json` (the names the tests use).
`manifest.json` records whether the product tree was clean and the sha256 of every SQLite file (uncompressed) and of `ids.json`.

- `t10_corpus/`: the T10 P5 corpus (tests/t10_corpus.py), every write step, at base (inline indexing,
  the reindex step included), frozen clock.
- `t10h_upgraded/`: shaped like the live T10h-upgraded store: written by public writes with an index
  (tests/test_t10_p6_upgrade.py `_populate`, then an operator rebuild), its `episode_desks` given T10's
  declaration (tenant between key columns, tests/test_t10h_p1_projection_key_order.py `give_old_order`), then
  upgraded by base `kp-agent-desk ... upgrade-sources` (the T10h key-first rebuild, quick_check, coverage),
  then written again by base code (a capture, a claim, an import and a link, each indexed inline).
"""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = HERE.parents[1]
sys.path.insert(0, str(TESTS))

FILES = ('episodes.sqlite3', 'sessions.sqlite3', 'episode-search.sqlite3')


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _save(world, name: str, ids: dict) -> dict:
    target = HERE / name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    world.snapshot(target)
    (target / 'catalog.json').unlink()
    digests = {}
    for file in FILES:
        assert (target / file).is_file(), f'{name}: {file} was not written'
        data = (target / file).read_bytes()
        digests[file] = hashlib.sha256(data).hexdigest()
        with gzip.GzipFile(target / (file + '.gz'), 'wb', mtime=0) as out:
            out.write(data)
        (target / file).unlink()
    (target / 'ids.json').write_text(json.dumps(ids, indent=1, sort_keys=True) + '\n')
    digests['ids.json'] = _sha(target / 'ids.json')
    return digests


def _refuse_outbox(path: Path) -> None:
    import sqlite3
    with contextlib.closing(sqlite3.connect(path)) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    if 'index_outbox' in names:
        raise SystemExit('refused: this tree already writes an outbox; the fixtures are created at BASE only')


def corpus_store(root: Path) -> dict:
    import t10_corpus as corpus
    import t10_world as w
    ctx = corpus.Context(root / 'corpus')
    with w.frozen_clock():
        ctx.world.initialize()
        for _name, write in corpus.STEPS:
            try:
                write(ctx)
            except Exception:  # noqa: BLE001 - refusals are corpus steps too (as t10_corpus.run records them)
                pass
    _refuse_outbox(ctx.world.store_path)
    return _save(ctx.world, 't10_corpus', ctx.ids)


def t10h_store(root: Path) -> dict:
    import t10_corpus as corpus
    import t10_world as w
    from test_t10_p6_upgrade import _populate, _world
    from test_t10h_p1_projection_key_order import give_old_order
    from kp_agent_tooling import desk_cli
    world = _world(root / 't10h')
    ids = _populate(world)
    world.index().rebuild()
    give_old_order(world.store_path)
    output = io.StringIO()
    with w.frozen_clock(), contextlib.redirect_stdout(output):
        assert desk_cli.main(['--config', str(world.configs[corpus.A]), 'upgrade-sources']) == 0, output.getvalue()
    report = json.loads(output.getvalue())
    assert report['rebuilt'].get('episode_desks'), f'the T10h rebuild did not run: {report}'
    store, sources = world.store(), world.sources()
    with w.frozen_clock():
        ids['L9'] = store.capture(corpus.A, source_ref='t12b:base-capture',
                                  events=w.events('Base cedar capture after the T10h upgrade.'))['episode_id']
        ids['S9'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='t12b-base-s9')
        ids['E9'] = sources.import_episode(session_id=ids['S9'], source_ref='t12b:base-import',
                                           events=w.events('Base cedar import after the upgrade.'),
                                           provenance=w.provenance('t12b-base-e9'))['episode_id']
        ids['c9'] = sources.claim(**w.claim_args(ids['S9'], corpus.BETA))
        world.index().upsert_episodes([ids['L9'], ids['E9']])
    _refuse_outbox(world.store_path)
    return _save(world, 't10h_upgraded', ids)


def main():
    dirty = subprocess.run(['git', '-C', str(TESTS), 'status', '--porcelain', '--', '../packages', '../extensions'],
                           capture_output=True, text=True, check=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix='t12b-base-stores-') as scratch:
        root = Path(scratch).resolve()
        files = {'t10_corpus': corpus_store(root), 't10h_upgraded': t10h_store(root)}
    import sqlite3
    manifest = {'product_tree_clean': not dirty,
                'sqlite_version': sqlite3.sqlite_version, 'python': sys.version.split()[0], 'files': files}
    (HERE / 'manifest.json').write_text(json.dumps(manifest, indent=1, sort_keys=True) + '\n')
    print(json.dumps(manifest, indent=1))


if __name__ == '__main__':
    os.umask(0o077)
    main()
