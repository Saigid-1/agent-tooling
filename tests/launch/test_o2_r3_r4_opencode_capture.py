"""O2 R3 and R4, F3 and F4: OpenCode turns captured through OpenCode's own export, idempotently.

Order: docs/work/orders/O2-opencode-third-harness.md. The hook runs as the board's plugin runs it
(`kp-agent-launch --receipt <launch.json> hook`, the payload on stdin); the export child is a fake
`opencode` (tests/launch/o2_harness.py) serving the snapshot shape the pinned binary prints. The real
binary is exercised by tests/image/test_o2_opencode_harness_image.py.

GREEN-IF:
- R3: the first Stop binds the session and captures exactly the visible events (user text before a
  completed assistant message; assistant text; completed and errored tools as role `tool`; reasoning
  excluded and counted; synthetic and ignored text omitted; a turn still running left out), each
  under its OpenCode part id, and enqueues each published page; the export child runs `--pure` with
  the launch's disable flags, no desk memory server and the workspace as its directory; a session of
  another workspace, a child session or a failed export is refused before anything is bound.
- F3: the same snapshot captured twice publishes nothing new; a dropped Stop is recovered by the next
  Stop (both turns once each), or by SessionEnd; a turn after an OpenCode revert is captured.
- F4: no product source names OpenCode's database; the hook process opens no file of it (an audit hook
  in that process), while the export child does.
- Bounds and crash replay: text over 128 000 bytes is split, pages hold at most 100 events, a capture
  publishes at most 16 pages and the next one continues; a page that failed after it was recorded as
  pending is replayed under the same source reference, as one episode and one job.
RED at base: no `opencode` profile (prepare exits 3).
Mutants (each RED here): the parser drops `text` parts; event ids from positions (the revert case);
the enqueue removed; the export child without --pure; a capture that opens the database.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from o2_harness import DATABASE_NAME, Session, fake_opencode, hook_payload, text, tool  # noqa: E402
from t3_harness import BIN, World, memory  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
PRODUCT = ('packages/tooling/src', 'extensions/ops/src', 'apps/kanban/src', 'deploy')


class OpenCodeLaunch:
    """One prepared OpenCode board launch with a fake `opencode` on its PATH."""

    def __init__(self, root: Path, *, world: World | None = None, desk: str | None = None):
        self.world = world or World.create(root / 'w')
        self.desk = desk or self.world.save_desk()
        self.prepared = self.world.prepared(self.world.request(harness='opencode', desk_id=self.desk))
        self.receipt = self.prepared['receipt_path']
        self.bin = root / 'bin'
        fake_opencode(self.bin)
        self.snapshot_path = root / 'snapshot.json'
        self.log = root / 'export-runs.jsonl'
        self.data = root / 'opencode-data'
        self.session = Session('ses_' + os.urandom(6).hex() + 'AbCdEfGhIjKlMn', self.world.workspace)

    def env(self, **extra) -> dict:
        env = self.world.env()
        env.update(self.prepared['env_additions'])
        env['PATH'] = f"{self.bin}{os.pathsep}{env['PATH']}"
        env.update(O2_FAKE_SNAPSHOT=str(self.snapshot_path), O2_FAKE_LOG=str(self.log), O2_FAKE_DATA=str(self.data))
        env.update(extra)
        return env

    def hook(self, event='Stop', *, session=None, cwd=None, **extra):
        self.session.write(self.snapshot_path)
        payload = hook_payload(event, session or self.session.info['id'], cwd or self.world.workspace)
        run = self.world.hook(self.receipt, payload, env=self.env(**extra))
        result = None
        if run.code == 0:
            result = json.loads(run.stderr.strip().splitlines()[-1])
        return run, result

    def captured(self, event='Stop', **extra) -> dict:
        run, result = self.hook(event, **extra)
        assert run.code == 0, run.describe()
        return result['capture']

    def call(self, *calls):
        _, replies = memory(self.world, self.session.info['id'], calls=list(calls))
        for reply in replies:
            assert not reply.is_error, reply.value
        return [reply.value for reply in replies]

    def episodes(self) -> list:
        return self.call(('memory.list', {'kind': 'episodes'}))[0]['entries']

    def read(self, episode_id, event_id) -> str:
        value = self.call(('memory.read_event', {'episode_id': episode_id, 'event_id': event_id, 'length': 8000}))[0]
        return json.dumps(value)

    def search(self, phrase) -> list:
        return [hit for hit in self.call(('memory.search', {'query': phrase}))[0]['results'] if phrase in json.dumps(hit)]

    def jobs(self) -> list:
        with sqlite3.connect(f"file:{self.world.state / 'queue.sqlite3'}?mode=ro", uri=True) as db:
            return db.execute('SELECT id, episodes, reason FROM jobs ORDER BY created').fetchall()

    def runs(self) -> list:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


@pytest.fixture
def launch(tmp_path):
    return OpenCodeLaunch(tmp_path)


def test_first_stop_binds_and_captures_exactly_the_visible_events(launch):
    s = launch.session
    [question] = s.user('question NONCE-Q1')
    _, reasoning, answer, ran, failed, synthetic, ignored, _ = s.assistant(
        {'type': 'step-start'}, text('reasoning', 'REASON-1'), text('text', 'answer NONCE-A1'),
        tool('bash', 'completed', 'TOOL-OUT-1'), tool('webfetch', 'error', 'TOOL-ERR-1'),
        text('text', 'SYNTH-1', synthetic=True), text('text', 'IGNORED-1', ignored=True),
        {'type': 'step-finish', 'reason': 'stop'})
    s.user('pending NONCE-Q2')
    s.assistant(text('text', 'partial NONCE-PARTIAL'), completed=False)

    capture = launch.captured()
    assert capture['status'] == 'captured' and len(capture['pages']) == 1, capture
    assert capture['reasoning_excluded'] == 1
    assert capture['omitted'] == {'part_step-finish': 1, 'part_step-start': 1, 'reasoning': 1,
                                  'synthetic_or_ignored_text': 2}, capture['omitted']
    [binding] = launch.world.binding_for(s.info['id'])
    assert binding['desk_id'] == launch.desk and binding['harness'] == 'opencode' and binding['source'] == 'board'

    [episode] = launch.episodes()
    assert episode['event_count'] == 4
    expected = {question: ('user', 'question NONCE-Q1'), answer: ('assistant', 'answer NONCE-A1'),
                ran: ('tool', 'TOOL-OUT-1'), failed: ('tool', 'TOOL-ERR-1')}
    for event_id, (role, phrase) in expected.items():
        body = launch.read(episode['episode_id'], event_id)
        assert phrase in body and f'"{role}"' in body, (event_id, body)
    for absent in ('NONCE-Q2', 'NONCE-PARTIAL', 'SYNTH-1', 'IGNORED-1', 'REASON-1'):
        assert launch.search(absent) == [], absent
    [hit] = launch.search('NONCE-A1')
    assert hit['event_id'] == answer
    [job] = launch.jobs()
    assert json.loads(job[1]) == [episode['episode_id']] and job[2] == 'batch'
    assert reasoning and synthetic and ignored


def test_the_export_child_runs_pure_with_the_disable_flags_in_the_workspace(launch):
    launch.session.turn('q', 'a')
    launch.captured()
    [run] = launch.runs()
    assert run['argv'] == ['--pure', 'export', launch.session.info['id']]
    assert Path(run['cwd']).resolve() == launch.world.workspace.resolve()
    env = run['env']
    for name in ('OPENCODE_PURE', 'OPENCODE_DISABLE_AUTOUPDATE', 'OPENCODE_DISABLE_SHARE',
                 'OPENCODE_DISABLE_MODELS_FETCH', 'OPENCODE_DISABLE_CLAUDE_CODE'):
        assert env.get(name) == '1', (name, env)
    assert env.get('OPENCODE_CONFIG_CONTENT') == '', 'the export child must not start a desk memory server'


def test_the_same_snapshot_twice_publishes_nothing_new(launch):
    launch.session.turn('question NONCE-Q1', 'answer NONCE-A1')
    first = launch.captured()
    again = launch.captured()
    assert first['status'] == 'captured' and again['status'] == 'not_due' and again['pages'] == []
    assert len(launch.episodes()) == 1 and len(launch.jobs()) == 1


def test_a_dropped_stop_is_recovered_by_the_next_stop_once_each(launch):
    launch.session.turn('question NONCE-Q1', 'answer NONCE-A1')
    # The first Stop is lost: the next turn ends before any capture ran.
    launch.session.turn('question NONCE-Q2', 'answer NONCE-A2')
    capture = launch.captured()
    assert capture['status'] == 'captured'
    for phrase in ('NONCE-Q1', 'NONCE-A1', 'NONCE-Q2', 'NONCE-A2'):
        assert len(launch.search(phrase)) == 1, phrase
    [episode] = launch.episodes()
    assert episode['event_count'] == 4
    assert launch.captured()['status'] == 'not_due'


def test_session_end_recovers_a_missed_stop(launch):
    launch.session.turn('question NONCE-Q1', 'answer NONCE-A1')
    launch.captured()
    launch.session.turn('question NONCE-Q2', 'answer NONCE-A2')  # its Stop failed
    capture = launch.captured('SessionEnd')
    assert capture['status'] == 'captured'
    assert [len(launch.search(p)) for p in ('NONCE-A1', 'NONCE-A2')] == [1, 1]
    assert [job[2] for job in launch.jobs()] == ['batch', 'session_end']


def test_a_turn_after_an_opencode_revert_is_captured_under_its_own_part_ids(launch):
    launch.session.turn('question NONCE-Q1', 'answer NONCE-A1')
    launch.session.turn('question NONCE-Q2', 'answer NONCE-A2')
    launch.captured()
    # OpenCode's revert removes the last turn; the next turn has new message and part ids.
    del launch.session.messages[2:]
    launch.session.turn('question NONCE-Q3', 'answer NONCE-A3')
    capture = launch.captured()
    assert capture['status'] == 'captured', capture
    assert len(launch.search('NONCE-A3')) == 1 and len(launch.search('NONCE-A1')) == 1


@pytest.mark.parametrize('case', ['another workspace', 'child session', 'export fails', 'export is not JSON'])
def test_a_session_that_is_not_this_launch_root_session_is_refused_before_binding(tmp_path, case):
    launch = OpenCodeLaunch(tmp_path)
    launch.session.turn('q', 'a')
    extra = {}
    if case == 'another workspace':
        other = tmp_path / 'elsewhere'
        other.mkdir()
        launch.session.info['directory'] = str(other)
    elif case == 'child session':
        launch.session.info['parentID'] = 'ses_parent0000AbCdEfGhIjKlMn'
    else:
        extra['O2_FAKE_MODE'] = 'fail' if case == 'export fails' else 'garbage'
    run, _ = launch.hook(**extra)
    assert run.code == 1, run.describe()
    assert launch.world.binding_for(launch.session.info['id']) == []
    assert not (Path(launch.receipt).parent / 'session.json').exists()


def test_a_failed_export_after_binding_publishes_nothing_and_the_next_stop_recovers(launch):
    launch.session.turn('question NONCE-Q1', 'answer NONCE-A1')
    launch.captured()
    launch.session.turn('question NONCE-Q2', 'answer NONCE-A2')
    run, _ = launch.hook(O2_FAKE_MODE='garbage')
    assert run.code == 1 and len(launch.episodes()) == 1
    assert launch.captured()['status'] == 'captured' and len(launch.search('NONCE-A2')) == 1


def test_an_export_that_never_exits_is_killed_at_its_timeout(tmp_path, monkeypatch):
    from kp_agent_tooling._impl.service import opencode_export_capture as capture
    launch = OpenCodeLaunch(tmp_path)
    launch.session.turn('q', 'a')
    launch.session.write(launch.snapshot_path)
    monkeypatch.setattr(capture, 'EXPORT_TIMEOUT_SECONDS', 1)
    with pytest.raises(capture.OpenCodeExportUnavailable, match='timed out'):
        capture.export_session('opencode', launch.session.info['id'], launch.world.workspace,
                               environ=launch.env(O2_FAKE_MODE='hang'))


def test_bounds_split_long_text_and_page_like_rollout_capture(launch):
    launch.session.user('long NONCE-LONG ' + 'x' * 300_000)
    launch.session.assistant(*[text('text', f'reply NONCE-R{i:04d}') for i in range(1700)])
    first = launch.captured()
    assert len(first['pages']) == 16 and first['has_more'] is True
    assert all(page['event_count'] <= 100 for page in first['pages'])
    long_events = [hit['event_id'] for hit in launch.search('NONCE-LONG')]
    assert long_events and all('-chars-0-32000' in event_id for event_id in long_events), long_events
    rest = launch.captured()
    assert rest['status'] == 'captured' and rest['has_more'] is False
    total = sum(entry['event_count'] for entry in launch.episodes())
    assert total == 10 + 1700, total  # 300 016 characters in 32 000-character pieces, and every reply
    assert launch.captured()['status'] == 'not_due'


def test_a_page_recorded_as_pending_is_replayed_as_the_same_episode_and_job(launch, monkeypatch):
    from kp_agent_tooling._impl.service import episodic_queue
    from kp_agent_tooling._impl.service.launch_binding import hook
    launch.session.turn('question NONCE-Q1', 'answer NONCE-A1')
    launch.session.write(launch.snapshot_path)
    payload = hook_payload('Stop', launch.session.info['id'], launch.world.workspace)
    for name, value in launch.env().items():
        monkeypatch.setenv(name, value)
    real = episodic_queue.ConsolidationQueue.enqueue
    monkeypatch.setattr(episodic_queue.ConsolidationQueue, 'enqueue',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('crash after the seal')))
    with pytest.raises(RuntimeError, match='crash after the seal'):
        hook(launch.receipt, payload)
    sealed = launch.episodes()
    assert len(sealed) == 1 and launch.jobs() == []
    monkeypatch.setattr(episodic_queue.ConsolidationQueue, 'enqueue', real)
    capture = hook(launch.receipt, payload)['capture']
    assert [page['replayed'] for page in capture['pages']] == [True]
    assert [e['episode_id'] for e in launch.episodes()] == [e['episode_id'] for e in sealed]
    assert len(launch.jobs()) == 1
    assert hook(launch.receipt, payload)['capture']['status'] == 'not_due'


def test_no_product_source_names_the_opencode_database():
    stem, variable = DATABASE_NAME, 'OPENCODE' + '_DB'
    found = []
    for base in PRODUCT:
        for path in (REPO / base).rglob('*'):
            if path.is_file() and 'node_modules' not in path.parts and path.suffix in (
                    '.py', '.ts', '.tsx', '.js', '.mjs', '.json', '.yaml', '.yml', '.sh', '.md', ''):
                body = path.read_text(errors='replace')
                if stem in body or variable in body:
                    found.append(str(path.relative_to(REPO)))
    assert found == []


_AUDIT = r'''
import json, sys
seen = []
def audit(event, args):
    if event == 'open' and args and isinstance(args[0], (str, bytes)):
        seen.append(('open', str(args[0])))
    elif event == 'sqlite3.connect' and args:
        seen.append(('sqlite3.connect', str(args[0])))
sys.addaudithook(audit)
from kp_agent_tooling.launch_cli import main
try:
    code = main(sys.argv[2:])
finally:
    with open(sys.argv[1], 'w') as out:
        json.dump(seen, out)
sys.exit(code)
'''


def test_the_hook_process_never_opens_the_database_the_export_child_reads(launch, tmp_path):
    launch.session.turn('question NONCE-Q1', 'answer NONCE-A1')
    launch.session.write(launch.snapshot_path)
    audit = tmp_path / 'audit.json'
    done = subprocess.run([sys.executable, '-c', _AUDIT, str(audit), '--receipt', launch.receipt, 'hook'],
                          input=json.dumps(hook_payload('Stop', launch.session.info['id'], launch.world.workspace)),
                          capture_output=True, text=True, env=launch.env(), timeout=300)
    assert done.returncode == 0, done.stderr[-3000:]
    assert 'captured' in done.stderr
    opened = json.loads(audit.read_text())
    assert any(kind == 'sqlite3.connect' for kind, _ in opened), 'instrument: no store connection was seen'
    assert [path for _, path in opened if Path(path).name.startswith(DATABASE_NAME)] == []
    assert [run.get('opened_database') for run in launch.runs()] == [True], 'the export child is the reader'
    assert (launch.data / DATABASE_NAME).exists()
    assert BIN.exists()
