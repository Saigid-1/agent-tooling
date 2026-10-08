"""Operator-only, additive import of a private legacy desk graph export.

No graph, Core, model, session admission, or network dependency is used here.
Each imported episode retains the raw row and its selected authored text.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.desk_identity import binding_key
from kp_agent_tooling._impl.service.desk_binding import DeskLaunchUnavailable
from kp_agent_tooling._impl.service.episodic_memory import EpisodeConflict, EpisodeStore, _bytes

SOURCE = 'postgres:org_ops.kp_nodes'
SCHEMA = 'ops.desk-history-export.v1'


def _required(value, field, maximum=1024):
    if not isinstance(value, str) or not value or len(value.encode()) > maximum:
        raise ValueError(f'{field} is missing or exceeds its bound')
    return value


def _chunks(text):
    # 16K Unicode characters are at most 64K UTF-8 bytes, below event limit.
    return [text[pos:pos + 16000] for pos in range(0, len(text), 16000)]


def _row(row):
    if not isinstance(row, dict) or not {'id', 'entity_type', 'state',
            'attributes', 'created_at', 'updated_at'} <= set(row):
        raise ValueError('raw row columns incomplete')
    _required(row['id'], 'row id', 128)
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9:._-]{0,127}', row['id']) is None:
        raise ValueError('row id contains unsupported characters')
    _required(row['entity_type'], 'entity type', 64)
    if not isinstance(row['attributes'], dict):
        raise ValueError('row attributes are not an object')
    return row


def _binding_record(row):
    attr = _row(row)['attributes']
    if row['entity_type'] != 'Binding':
        raise ValueError('binding export contains a non-Binding row')
    coordinates = {name: _required(attr.get(name), name, 512)
                   for name in ('tenant_id', 'role', 'repo_key')}
    expected = binding_key(**coordinates)
    if row['id'] != expected or attr.get('binding_key') != expected:
        raise ValueError('binding identity does not match coordinates')
    return expected, coordinates


def _selected_text(row):
    attr = row['attributes']
    if row['entity_type'] == 'DeskNote':
        text = _required(attr.get('text'), 'note text', 2_000_000)
        if attr.get('content_digest') != hashlib.sha256(text.encode()).hexdigest():
            raise ValueError('note text digest mismatch')
        authored_at = attr.get('authored_at')
        return [('note-text', text, authored_at)], authored_at, None
    if row['entity_type'] == 'DeskRun':
        fields = [('begin-summary', 'begin_summary', 'begin_content_digest', 'begin_at')]
        if attr.get('end_summary') is not None:
            fields.append(('end-summary', 'end_summary', 'end_content_digest', 'end_at'))
        result = []
        for label, key, digest_key, time_key in fields:
            text = _required(attr.get(key), key, 2_000_000)
            if attr.get(digest_key) != hashlib.sha256(text.encode()).hexdigest():
                raise ValueError(f'{label} digest mismatch')
            result.append((label, text, attr.get(time_key)))
        # A retired run has two distinct authoring times. The episode itself
        # cannot honestly assign one timestamp to both summaries.
        authored_at = 'multiple' if len(result) > 1 else attr.get('begin_at')
        return result, authored_at, attr.get('host_session_id')
    raise ValueError('unsupported legacy entity type')


def import_export(path, store: EpisodeStore, *, dry_run=False):
    """Import or preflight one immutable private JSON export, reporting every row.

    Export shape: schema_version, exported_at, source, records, bindings.
    `bindings` contains raw Binding rows. Only DeskNote and DeskRun records
    enter episodic storage; document chunks remain in maintained RAG scope.
    """
    source_path = Path(path)
    if (not source_path.is_absolute() or source_path.is_symlink() or
            not source_path.is_file() or source_path.stat().st_uid != os.getuid() or
            leaf.shared_bits(source_path.stat().st_mode)):
        raise ValueError('existing owner-private absolute export file required')
    if source_path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('export exceeds 64 MiB bound')
    raw = source_path.read_bytes()
    artifact = json.loads(raw)
    if (not isinstance(artifact, dict) or set(artifact) != {
            'schema_version', 'exported_at', 'source', 'records', 'bindings'} or
            artifact['schema_version'] != SCHEMA or artifact['source'] != SOURCE or
            not isinstance(artifact['records'], list) or
            not isinstance(artifact['bindings'], list) or
            len(artifact['records']) > 10000 or len(artifact['bindings']) > 1000):
        raise ValueError('invalid bounded desk-history export envelope')
    _required(artifact['exported_at'], 'exported_at', 64)
    receipt = {'schema_version': 'ops.desk-history-import.v1',
               'export_sha256': hashlib.sha256(raw).hexdigest(),
               'dry_run': bool(dry_run), 'imported': 0, 'already_present': 0,
               'ready': 0, 'quarantined': [], 'excluded': {}, 'bindings': 0}
    registered = {}
    for item in artifact['bindings']:
        try:
            key, coordinates = _binding_record(item)
            prior = registered.setdefault(key, coordinates)
            if prior != coordinates:
                raise ValueError('duplicate binding has conflicting coordinates')
        except (TypeError, KeyError, ValueError) as error:
            receipt['quarantined'].append({'source_id': item.get('id') if isinstance(item, dict) else None,
                                           'entity_type': 'Binding', 'reason': str(error)})
    receipt['bindings'] = len(registered)
    for item in artifact['records']:
        identity = item.get('id') if isinstance(item, dict) else None
        kind = item.get('entity_type') if isinstance(item, dict) else None
        if kind not in {'DeskNote', 'DeskRun'} and isinstance(kind, str):
            receipt['excluded'][kind] = receipt['excluded'].get(kind, 0) + 1
            continue
        try:
            row = _row(item)
            attr = row['attributes']
            coordinates = {name: _required(attr.get(name), name, 512)
                           for name in ('tenant_id', 'role', 'repo_key')}
            key = binding_key(**coordinates)
            if registered.get(key) != coordinates:
                raise ValueError('row binding absent from exported Binding rows')
            current = store.registry.resolve(**coordinates)
            if current.binding_key != key:
                raise ValueError('current binding identity mismatch')
            selected, authored_at, host_session = _selected_text(row)
            row_digest = leaf.canonical_sha256(row, ascii=True, allow_nan=True)
            source_ref = f'legacy:org_ops.kp_nodes:{row["id"]}:{row_digest}'
            if len(source_ref.encode()) > 1024:
                raise ValueError('source reference exceeds bound')
            sections = [('raw-row-json', _bytes(row).decode(), 'unknown'), *selected]
            chunks = []
            event_map = []
            for label, text, section_time in sections:
                pieces = _chunks(text)
                event_map.append({'label': label, 'event_start': len(chunks),
                                  'event_count': len(pieces),
                                  'authored_at': section_time if isinstance(section_time, str) else 'unknown'})
                chunks.extend(pieces)
            events = [{'event_id': f'import-{index:03d}', 'role': 'tool', 'text': chunk}
                      for index, chunk in enumerate(chunks)]
            if not 1 <= len(events) <= 125:
                raise ValueError('evidence chunk count exceeds bound')
            provenance = {'source_system': SOURCE, 'row_id': row['id'],
                          'row_digest': row_digest, 'entity_type': kind,
                          'source_actor': 'unknown', 'source_observed_at': 'unknown',
                          'authored_at': authored_at if isinstance(authored_at, str) else 'unknown',
                          'legacy_host_session_id': host_session if isinstance(host_session, str) else None,
                          'legacy_binding_provenance': attr.get('binding_provenance'),
                          'evidence_event_map': event_map}
            # A preflight must prove the same size and event bounds as a write.
            if len(_bytes({'events': events, 'source_provenance': provenance})) > 1_900_000:
                raise ValueError('imported episode exceeds two-megabyte bound')
            if dry_run:
                receipt['ready'] += 1
                continue
            result = store.import_operator_episode(**coordinates, source_ref=source_ref,
                events=events, source_provenance=provenance)
            receipt[result['status']] += 1
        except (DeskLaunchUnavailable, EpisodeConflict, TypeError, KeyError, ValueError) as error:
            receipt['quarantined'].append({'source_id': identity, 'entity_type': kind,
                                           'reason': str(error)})
    receipt['quarantined_count'] = len(receipt['quarantined'])
    receipt['excluded_total'] = sum(receipt['excluded'].values())
    return receipt
