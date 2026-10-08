"""One drain in its own process, for the T12b B2 lease and restart tests (tests/test_t12b_b2_indexer.py).

    python tests/t12b_drain_process.py --config <desk-memory config> --batch N --out <result.json>
        [--hold <directory>]   at the indexer's first write to the index, create <directory>/held and wait
                               (at most 120 s) until <directory>/release exists, then go on
        [--crash-at-first-write] end the process (os._exit(17)) at the indexer's first write to the index, before
                               it runs (a drainer killed before it applied anything)
        [--crash-after-commit] end the process (os._exit(17)) at the first non-internal statement, on any
                               connection, issued after the indexer's first COMMIT on the index returned (a crash
                               between two batches). Statements SQLite runs on its own behalf while that COMMIT
                               completes (FTS5 shadow-table writes, traced with a leading `--` or as
                               'schema'.'table') are internal and never the crash point (meet, 2026-10-04).

The drain is the product's (tests/t12b_seams.py, one call, `drain_once`). The result file holds the exit
`status`, the elapsed seconds, the statements this process ran on the index (or a build file beside it) and on
the store, each with whether the indexer was on the stack, and the call's return value (repr).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import t12b_seams as seams  # noqa: E402

# SQLite's own statements (FTS5 shadow tables), as tests/t10_instruments.py tells them apart.
INTERNAL = re.compile(r"\s*--|\s*(?:SELECT|INSERT|REPLACE|UPDATE|DELETE)\b.*?'[A-Za-z_]\w*'\.'[A-Za-z_]\w*'", re.I | re.S)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--batch', type=int, default=seams.DEFAULT_BATCH)
    parser.add_argument('--out', required=True)
    parser.add_argument('--hold')
    parser.add_argument('--crash-after-commit', action='store_true')
    parser.add_argument('--crash-at-first-write', action='store_true')
    args = parser.parse_args()
    from kp_agent_tooling._impl.service.desk_memory_runtime import components
    store = components(args.config)[3]
    store_path = os.path.realpath(os.fspath(store.path))
    state = {'held': False, 'committed': False}
    result = {'status': 'started', 'pid': os.getpid()}
    started = time.monotonic()

    def write_result():
        result['elapsed'] = time.monotonic() - started
        result['statements'] = [{'db': 'index' if seams.index_family(e.db) else 'store', 'name': Path(e.db).name,
                                 'kind': e.kind, 'sql': e.sql[:2000], 'drain': e.drain, 'write': e.write}
                                for e in trace.events if e.db == store_path or seams.index_family(e.db)]
        Path(args.out).write_text(json.dumps(result))

    def observe(current):
        last = current.events[-1] if current.events else None
        if last is None:
            return
        internal = last.kind == 'sql' and bool(INTERNAL.match(last.sql))
        if args.crash_after_commit and state['committed'] and not internal:
            result['status'] = 'crashed after the first index commit'
            write_result()
            os._exit(17)
        if last.kind == 'sql' and seams.index_family(last.db) and last.drain:
            if args.crash_at_first_write and last.write:
                result['status'] = 'crashed at the first index write'
                write_result()
                os._exit(17)
            if args.hold and not state['held'] and last.write:
                state['held'] = True
                hold = Path(args.hold)
                (hold / 'held').write_text(str(os.getpid()))
                deadline = time.monotonic() + 120
                while not (hold / 'release').exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
            if not internal and last.sql.strip().upper().startswith(('COMMIT', 'END')):
                state['committed'] = True

    with seams.traced(observe) as trace:
        try:
            value = seams.drain_once(store, batch=args.batch)
            result['status'] = 'returned'
            result['value'] = repr(value)[:2000]
        except BaseException as error:  # noqa: BLE001 - the outcome is the observation
            result['status'] = 'raised'
            result['error'] = ''.join(traceback.format_exception(error))[-4000:]
    write_result()
    return 0 if result['status'] == 'returned' else 1


if __name__ == '__main__':
    raise SystemExit(main())
