"""O2 image instrument: reads the O2 runner's own desk store inside the container (never a live store).

Run with the image's python by apps/kanban/test/runtime/terminal/o2-opencode-image-runner.ts:

    python3 o2_store_probe.py wait-bound <receipt> <timeout_s>
    python3 o2_store_probe.py observe <receipt> <phrase> <timeout_s> <launched_at_epoch_s>
    python3 o2_store_probe.py counts <receipt>

`observe` waits for the launch's first hook to bind the session (its session.json), then polls the episode
store every 0.1 s for the session's first seal (the moment the probe first sees it), then asks the session's
own desk memory server (`kp-agent-memory --config <the launch's memory.json> serve`, as OpenCode starts it)
for `memory.search` every 0.2 s until a result carries the phrase. It prints one JSON object.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from datetime import timedelta
from pathlib import Path


def _ro(path: Path):
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=10)


def _session(receipt: Path):
    path = receipt.parent / 'session.json'
    if not path.exists():
        return None
    return json.loads(path.read_text())['native_session_id']


def wait_bound(receipt: Path, timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        session = _session(receipt)
        if session:
            return {'session': session, 'at': time.time()}
        time.sleep(0.1)
    return {'session': None, 'timed_out': True}


def counts(receipt: Path) -> dict:
    launch = json.loads(receipt.read_text())
    session = _session(receipt)
    state = Path(json.loads((receipt.parent / 'memory.json').read_text())['state_root']) if session else None
    out = {'session': session, 'binding_key': launch['binding_key'], 'desk_id': launch['desk_id'],
           'harness': launch['harness'], 'capture_ledger': Path(launch['capture_ledger']).name}
    if state is None:
        return out
    with _ro(state / 'episodes.sqlite3') as db:
        rows = db.execute('SELECT id, source_ref, payload FROM episodes WHERE session=? ORDER BY rowid',
                          (session,)).fetchall()
    out['episodes'] = [{'episode_id': row[0], 'source_ref': row[1],
                        'events': [{'event_id': e['event_id'], 'role': e['role'], 'text': e['text'][:120]}
                                   for e in json.loads(row[2])['events']]} for row in rows]
    with _ro(state / 'queue.sqlite3') as db:
        out['jobs'] = [{'job_id': r[0], 'episodes': json.loads(r[1]), 'reason': r[2]} for r in db.execute(
            'SELECT id, episodes, reason FROM jobs WHERE binding=? ORDER BY created', (launch['binding_key'],))]
    with _ro(Path(launch['capture_ledger'])) as db:
        out['ledger'] = {
            'published': db.execute('SELECT count(*) FROM opencode_published WHERE session=?', (session,)).fetchone()[0],
            'receipts': [json.loads(r[0]) for r in db.execute(
                'SELECT payload FROM opencode_receipts WHERE session=? ORDER BY page', (session,))],
            'pending': db.execute('SELECT pending FROM opencode_cursor WHERE session=?', (session,)).fetchone(),
        }
    return out


async def _search_until(memory_config: Path, phrase: str, deadline: float) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import os
    params = StdioServerParameters(command='kp-agent-memory', args=['--config', str(memory_config), 'serve'],
                                   env=dict(os.environ))
    attempts = 0
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as client:
            await client.initialize()
            tools = [tool.name for tool in (await client.list_tools()).tools]
            while time.time() < deadline:
                attempts += 1
                reply = await client.call_tool('memory.search', {'query': phrase})
                value = reply.structuredContent or {}
                hits = [hit for hit in value.get('results', []) if phrase in json.dumps(hit)]
                if hits:
                    return {'found_at': time.time(), 'hit': hits[0], 'attempts': attempts, 'tools': tools,
                            'index_status': value.get('index_status')}
                await asyncio.sleep(0.2)
            return {'found_at': None, 'attempts': attempts, 'tools': tools, 'last': value}


def observe(receipt: Path, phrase: str, timeout: float, launched_at: float) -> dict:
    deadline = time.time() + timeout
    bound = wait_bound(receipt, timeout)
    out = {'bound': bound}
    session = bound.get('session')
    if not session:
        return out
    memory_config = receipt.parent / 'memory.json'
    state = Path(json.loads(memory_config.read_text())['state_root'])
    sealed_at = None
    while time.time() < deadline and sealed_at is None:
        try:
            with _ro(state / 'episodes.sqlite3') as db:
                if db.execute('SELECT count(*) FROM episodes WHERE session=?', (session,)).fetchone()[0]:
                    sealed_at = time.time()
        except sqlite3.Error:
            pass
        if sealed_at is None:
            time.sleep(0.1)
    out['sealed_at'] = sealed_at
    if sealed_at is None:
        return out
    found = asyncio.run(_search_until(memory_config, phrase, deadline))
    out['search'] = found
    if found.get('found_at'):
        out['seal_to_search_s'] = round(found['found_at'] - sealed_at, 3)
    out['launch_to_seal_s'] = round(sealed_at - launched_at, 3)
    out['counts'] = counts(receipt)
    return out


def main(argv):
    command, receipt = argv[0], Path(argv[1])
    if command == 'wait-bound':
        result = wait_bound(receipt, float(argv[2]))
    elif command == 'observe':
        result = observe(receipt, argv[2], float(argv[3]), float(argv[4]))
    elif command == 'counts':
        result = counts(receipt)
    else:
        raise SystemExit(f'unknown command {command}')
    print(json.dumps(result))


if __name__ == '__main__':
    main(sys.argv[1:])
