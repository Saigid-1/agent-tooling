"""Resumable operator JSONL import; no claims/admissions are inferred from text."""
import json
from collections import Counter
from pathlib import Path
from kp_agent_tooling._impl.service.session_sources import SessionSources


def import_catalog(path, store):
    catalog = SessionSources(store)
    catalog.upgrade()
    counts = Counter()
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise ValueError('regular operator import file required')
    with source.open('rb') as stream:
        while True:
            line = stream.readline(2_100_001)
            if not line: break
            if len(line) > 2_100_000 or not line.endswith(b'\n'):
                raise ValueError('complete bounded JSONL record required')
            record = json.loads(line)
            operation, arguments = record['operation'], record['arguments']
            if set(record) != {'operation', 'arguments'} or not isinstance(arguments, dict):
                raise ValueError('operator import record invalid')
            if operation == 'session':
                catalog.register(**arguments); counts['sessions_processed'] += 1
            elif operation == 'claim':
                catalog.claim(**arguments); counts['claims_processed'] += 1
            elif operation == 'link':
                catalog.link(**arguments); counts['links_processed'] += 1
            elif operation == 'episode':
                result = catalog.import_episode(**arguments); counts[result['status']] += 1
            else:
                raise ValueError('unknown operator import operation')
    return dict(counts, status='complete', authority='operator history import; admissions unchanged')
