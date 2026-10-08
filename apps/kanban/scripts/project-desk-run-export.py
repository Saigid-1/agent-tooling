#!/usr/bin/env python3
"""Read-only trial adapter for raw legacy DeskRun exports; not an admission client."""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path


def digest(value):
    return hashlib.sha256(value).hexdigest()


def stamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone-aware timestamp required')
    return parsed


def project(raw, expected_hash, tenant, role, repo, host):
    if len(raw) > 2_000_000 or digest(raw) != expected_hash:
        raise ValueError('export size or checksum mismatch')
    data = json.loads(raw)
    runs = data['records']['desk_runs']
    note = data['records']['claim_note']
    if not isinstance(runs, list) or not 1 <= len(runs) <= 1000:
        raise ValueError('bounded nonempty run selection required')
    records = [*runs, note]
    ids = [r['id'] for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate record identity')
    expected = data['per_record_export_digest_sha256']
    for record in records:
        actual = digest(json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
        if actual != expected[record['id']]:
            raise ValueError('record export checksum mismatch')
        a = record['attributes']
        if (a['tenant_id'], a['role'], a['repo_key']) != (tenant, role, repo):
            raise ValueError('record desk scope mismatch')
    rows = []
    for record in runs:
        a = record['attributes']
        if record['entity_type'] != 'DeskRun' or a['run_key'] != record['id'] or a.get('void_reason'):
            raise ValueError('invalid or voided run')
        if record['id'] != 'deskrun:' + digest(a['host_session_id'].encode()):
            raise ValueError('run host identity mismatch')
        if digest(a['begin_summary'].encode()) != a['begin_content_digest']:
            raise ValueError('begin content checksum mismatch')
        start = stamp(a['begin_at'])
        end = a.get('end_at')
        if end is not None and stamp(end) < start:
            raise ValueError('reversed run interval')
        if a['status'] not in ('active', 'retired'):
            raise ValueError('unsupported run status')
        if a['status'] == 'retired':
            if end is None or digest(a['end_summary'].encode()) != a['end_content_digest']:
                raise ValueError('retired run needs valid end bookend')
        elif any(a.get(k) is not None for k in ('end_at', 'end_summary', 'end_content_digest')):
            raise ValueError('active run carries end bookend')
        reconstructed = 'reconstructed at retirement' in a['begin_summary']
        rows.append(dict(record_id=record['id'],host_session_id=a['host_session_id'],
            recorded_begin=a['begin_at'],supported_begin=None if reconstructed else a['begin_at'],
            end=end,status_at_export=a['status'],
            begin_qualification='reconstructed_not_observed' if reconstructed else 'recorded_bookend',
            end_qualification='mechanical_marker_in_summary' if 'mechanical SessionEnd hook' in a.get('end_summary','') else 'no_end_record' if end is None else 'unclassified',
            provenance_present='binding_provenance' in a,
            provenance=a.get('binding_provenance'),
            source_locator='records.desk_runs['+str(len(rows))+']'))
    subjects = [r for r in rows if r['host_session_id'] == host]
    if len(subjects) != 1 or data['selection']['subject_host_session_id'] != host:
        raise ValueError('exact subject run required')
    a = note['attributes']
    if note['entity_type'] != 'DeskNote' or a['note_key'] != note['id'] or digest(a['text'].encode()) != a['content_digest']:
        raise ValueError('note identity or content checksum mismatch')
    overlaps = []
    for i, left in enumerate(rows):
        for right in rows[i+1:]:
            if not left['supported_begin'] or not right['supported_begin']:
                continue
            if left['end'] and stamp(left['end']) <= stamp(right['supported_begin']):
                continue
            if right['end'] and stamp(right['end']) <= stamp(left['supported_begin']):
                continue
            overlaps.append([left['record_id'], right['record_id']])
    sessions = {}
    for record, row in zip(runs, rows):
        attrs = record['attributes']
        entry = sessions.setdefault(row['host_session_id'], dict(
            host_session_id=row['host_session_id'], runs=[], transcript_uuids=[],
            registration='present_in_export', reachability='not_checked',
            contact_route=None, contact_reason='No verified harness contact route in export',
            source_as_of=data['exported_at_utc']))
        entry['runs'].append(row)
        if attrs.get('transcript_uuid') and attrs['transcript_uuid'] not in entry['transcript_uuids']:
            entry['transcript_uuids'].append(attrs['transcript_uuid'])
    directory = dict(binding=dict(tenant_id=tenant, role=role, repo_key=repo),
        sessions=list(sessions.values()), concurrency='allowed',
        coverage='selected_export_only', unretired_predecessor_blocks_discovery=False)
    return dict(schema_version='ops.raw-deskrun-temporal.v1.1',export_sha256=expected_hash,
        exported_at=data['exported_at_utc'],db_clock_at_read=data['db_clock_at_read'],
        binding=dict(tenant_id=tenant,role=role,repo_key=repo),rows=rows,
        directory=directory, concurrency_observations=overlaps,subject=subjects[0]['record_id'],
        claim_note=dict(id=note['id'],authored_at=a['authored_at'],trust_class=a['trust_class'],
            subject_host_mentioned=host in a['text'],association='textual co-reference only; no graph edge verified by this adapter'),
        checks=dict(export_hash='matched',record_export_hashes=len(records),bookend_and_note_hashes='matched'),
        authorization='not_assessed',source_authentication='not_established',
        limitations=['Export-limited history; no current database query or liveness check.',
            'Concurrent sessions on one desk are allowed. Intervals are history, not occupancy locks.',
            'Mechanical marker does not exclude a later explicit retirement.',
            'Missing provenance remains missing; desk-authored text is not a verified owner grant.',
            'Export metadata, completeness statements and reader notes are exporter assertions.'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for field in ('source','sha256','tenant','role','repo','host'):
        parser.add_argument('--'+field,required=True)
    args=parser.parse_args()
    try:
        path=Path(args.source)
        if path.is_symlink() or not path.is_file() or path.stat().st_size>2_000_000:
            raise ValueError('bounded regular export required')
        result=project(path.read_bytes(),args.sha256,args.tenant,args.role,args.repo,args.host)
        print(json.dumps(result,indent=2))
        return 0
    except (ValueError,KeyError,TypeError,OSError) as error:
        print('Desk export refused: '+str(error),file=sys.stderr)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
