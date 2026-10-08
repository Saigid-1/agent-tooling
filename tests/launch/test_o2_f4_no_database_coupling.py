"""O2 F4: no database coupling (docs/work/orders/O2-opencode-third-harness.md, R3 and Verification A2).

- A source guard: no `opencode.db` and no `OPENCODE_DB` in agent-tooling source (scope O2-S15). Its
  positive control is the source that implements the `opencode` profile's parser: the guard cannot state a
  result without the capture code it guards, so it is RED at base (no profile, no parser).
- An open-file audit of agent-tooling's own processes during a capture (O2-S16): no open of `opencode.db`
  or its `-wal`/`-shm`, no SQLite connection to them, and no child that names them or asks `opencode`
  for anything but export. The export child (the fake `opencode`, not audited) is the only reader.
  Its controls: the turn was captured, and an audited process started the export child.
- A2: the export child runs with external plugins off and the R2 disable flags, whatever the hook's own
  environment carries (the hook here runs WITHOUT them).

Mutant named by the order: a capture path that reads the database (the audit and the guard go RED).
"""
from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

import o2_seams as seams
from o2_test_harness import OpenCodeSession, OpenCodeWorld, count_text, describe, opencode_profile, parser_of

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def oc(tmp_path):
    return OpenCodeWorld(tmp_path)


DB_WORD = re.compile(r'opencode\.db|OPENCODE_DB', re.IGNORECASE)


def _source_files() -> list[Path]:
    listed = subprocess.run(['git', '-C', str(REPO_ROOT), 'ls-files', '-z', '--cached', '--others',
                             '--exclude-standard', '--', *seams.SOURCE_ROOTS],
                            capture_output=True, check=True).stdout.decode().split('\0')
    return [REPO_ROOT / name for name in listed if name and (REPO_ROOT / name).is_file()]


def test_f4_no_agent_tooling_source_names_the_opencode_database():
    """GREEN-IF no file under the source roots names `opencode.db` or `OPENCODE_DB` (any case), AND the scan
    read the Python source that names the `opencode` profile's parser (the capture code it guards)."""
    parser = parser_of(opencode_profile())
    assert isinstance(parser, str) and parser, f'O2-S1: the opencode profile names no parser: {opencode_profile()!r}'
    hits, guarded = [], []
    for path in _source_files():
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if path.suffix == '.py' and (f"'{parser}'" in text or f'"{parser}"' in text):
            guarded.append(str(path.relative_to(REPO_ROOT)))
        for number, line in enumerate(text.splitlines(), 1):
            if DB_WORD.search(line):
                hits.append(f'{path.relative_to(REPO_ROOT)}:{number}: {line.strip()[:160]}')
    assert guarded, f'positive control: no scanned Python source names the parser {parser!r}, so the guard ' \
                    f'did not read the capture code ({len(_source_files())} files scanned)'
    assert not hits, 'agent-tooling source names OpenCode\'s database:\n' + '\n'.join(hits)


# ------------------------------------------------------------------------------------- the audit

_AUDIT = textwrap.dedent(r'''
    """O2 TEST audit hook (O2-S16): records file opens, SQLite connects and child processes of this process."""
    import json, os, sys
    _log = os.environ.get("O2_AUDIT_LOG")
    if _log:
        _fd = os.open(_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        _busy = []
        _names = __NAMES__
        def _hook(event, args):
            if _busy or not (event in _names or event.startswith("os.exec") or event.startswith("os.spawn")):
                return
            _busy.append(1)
            try:
                os.write(_fd, (json.dumps({"pid": os.getpid(), "argv0": sys.argv[0] if sys.argv else "",
                                           "event": event, "args": [repr(a)[:4000] for a in args]}) + "\n").encode())
            except Exception:
                pass
            finally:
                _busy.pop()
        sys.addaudithook(_hook)
''').replace('__NAMES__', repr(tuple(seams.AUDIT_EVENTS)))


def _audited_stop(oc, tmp_path, session, prepared):
    audit = tmp_path / 'audit'
    audit.mkdir()
    (audit / 'sitecustomize.py').write_text(_AUDIT)
    log = tmp_path / 'audit.jsonl'
    env = oc.launch_env(prepared)
    for name in seams.DISABLE_ENV:
        env.pop(name, None)
    env.pop(seams.PURE_ENV, None)
    extra = {'PYTHONPATH': str(audit) + (':' + env['PYTHONPATH'] if env.get('PYTHONPATH') else ''),
             'O2_AUDIT_LOG': str(log)}
    oc.fake.serve(session)
    assert oc.fake.db.exists(), 'fixture: OpenCode\'s own store is missing, so a reader would find nothing'
    runs = []
    for command in oc.hook_commands(prepared, 'Stop'):
        done = subprocess.run(['/bin/sh', '-c', command], input=json.dumps(oc.payload(session)), text=True,
                              capture_output=True, cwd=oc.world.workspace, env={**env, **extra}, timeout=300)
        runs.append({'code': done.returncode, 'stderr': done.stderr[-3000:]})
    assert runs and all(r['code'] == 0 for r in runs), runs
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()] if log.exists() else []
    return records


def _touches_database(record) -> bool:
    text = ' '.join(record['args'])
    return any(re.search(rf'(^|[/\'"\s]){re.escape(name)}($|[\'"\s,)])', text) for name in seams.DB_NAMES) \
        or 'OPENCODE_DB' in text


def _spawned_export(record) -> bool:
    if not (record['event'] == 'subprocess.Popen' or record['event'].startswith(('os.exec', 'os.posix_spawn',
                                                                                  'os.spawn'))):
        return False
    text = ' '.join(record['args'])
    return 'opencode' in text and seams.EXPORT_SUBCOMMAND in text


def test_f4_capture_never_opens_the_opencode_database(oc, tmp_path):
    """GREEN-IF, during a Stop's capture, no audited agent-tooling process opens `opencode.db`, `-wal` or `-shm`
    (open, os.open, sqlite3.connect) or starts a child naming them or OPENCODE_DB; every `opencode` call the
    capture makes is one of EXPORT_SUBCOMMANDS. Controls: the turn is captured, the audit recorded the hook
    process, and an audited process started the export child (so it was the capturing process)."""
    desk = oc.save_desk(name='Audit desk')
    prepared = oc.prepare(desk)
    session = OpenCodeSession(oc.world.workspace)
    session.user('Audit ask: Arrowgrass.')
    session.assistant('Audit answer: Arrowgrass.')

    records = _audited_stop(oc, tmp_path, session, prepared)
    events = oc.events(session.id)
    assert count_text(events, 'Audit answer: Arrowgrass', 'assistant') == 1, describe(events)
    assert records, 'instrument: the audit recorded nothing, so the hook process was not audited'
    assert any(_spawned_export(r) for r in records), \
        'instrument: no audited process started `opencode export`; the capturing process was not audited:\n' + \
        '\n'.join(json.dumps(r)[:300] for r in records if r['event'] != 'open')[-4000:]
    touching = [r for r in records if _touches_database(r)]
    assert not touching, 'agent-tooling opened OpenCode\'s database during a capture:\n' + \
        '\n'.join(json.dumps(r)[:600] for r in touching)
    asked = []
    for call in oc.fake.calls():
        positional = [a for a in call['argv'] if not a.startswith('-')]
        asked.append(positional[0] if positional else (call['argv'][0] if call['argv'] else ''))
    assert asked and set(asked) <= set(seams.EXPORT_SUBCOMMANDS), \
        f'the capture asked opencode for {asked}; only {seams.EXPORT_SUBCOMMANDS} are a capture\'s'


def test_f4_export_child_runs_pure_with_the_disable_flags(oc, tmp_path):
    """A2. GREEN-IF every `opencode export` the capture starts runs with external plugins off (`--pure` in its
    argv or OPENCODE_PURE=1/true) and carries OPENCODE_DISABLE_AUTOUPDATE=1 and OPENCODE_DISABLE_SHARE=1,
    although the hook's own environment carries none of them."""
    desk = oc.save_desk(name='Pure desk')
    prepared = oc.prepare(desk)
    session = OpenCodeSession(oc.world.workspace)
    session.user('Pure ask: Cottongrass.')
    session.assistant('Pure answer: Cottongrass.')
    _audited_stop(oc, tmp_path, session, prepared)
    exports = [c for c in oc.fake.calls() if seams.EXPORT_SUBCOMMAND in c['argv']]
    assert exports, 'the capture never ran `opencode export`'
    for call in exports:
        pure = seams.PURE_FLAG in call['argv'] or call['env'].get(seams.PURE_ENV, '').lower() in ('1', 'true')
        assert pure, f'an export child ran with external plugins on: {call}'
        for name, value in seams.DISABLE_ENV.items():
            assert call['env'].get(name) == value, f'an export child lacks {name}={value}: {call}'
