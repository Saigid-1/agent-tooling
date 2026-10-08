"""Closed, operator-authored journey definitions. Registration is not a verdict."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

from kp_agent_tooling_ops._impl.observations import ObservationStore
from kp_agent_tooling._impl.source_citations import source
from kp_agent_tooling._impl import leaf

VERSION = 'ops.journey-registry.v1'
LEGACY_SLICES = ('ats-pool-lifecycle', 'ats-worker-performance', 'ats-pool-failures',
                 'ats-auth-acquisition', 'ats-response-correctness', 'ats-constrained-pool')
LEGACY_QUESTIONS = ('baseline', 'lifecycle', 'performance', 'deployment')
NAME = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
SHA = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError('invalid_journey_registry_shape')


def _name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError('invalid_journey_identifier')


def _text(value):
    if not isinstance(value, str) or not value or len(value) > 300:
        raise ValueError('invalid_journey_text')


def _path(value):
    if (not isinstance(value, str) or len(value) > 300 or value.startswith('/')
            or any(part in ('', '.', '..') for part in value.split('/'))
            or '\\' in value or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('invalid_journey_source_path')


def _unique(values):
    if len(values) != len(set(values)):
        raise ValueError('duplicate_journey_identifier')


def load_registry(config):
    """Read one bounded JSON file selected by operator configuration, never tool input."""
    selected = config.get('journey_registry')
    if selected is None:
        return None
    if not isinstance(selected, str) or not Path(selected).is_absolute():
        raise ValueError('journey_registry_absolute_path_required')
    path = Path(selected)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128_000:
        raise ValueError('journey_registry_file_invalid')
    raw = path.read_bytes()
    if len(raw) > 128_000:
        raise ValueError('journey_registry_exceeds_bound')
    try:
        data = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('journey_registry_not_json') from error
    _keys(data, ('schema_version', 'product', 'sources', 'slices', 'questions'))
    if data['schema_version'] != VERSION:
        raise ValueError('unsupported_journey_registry_version')
    _name(data['product'])
    sources = data['sources']
    if not isinstance(sources, dict) or not 1 <= len(sources) <= 12:
        raise ValueError('invalid_journey_sources')
    for key, revision in sources.items():
        _name(key)
        if not isinstance(revision, str) or not SHA.fullmatch(revision):
            raise ValueError('invalid_journey_revision')
        if config.get('repos', {}).get(key, {}).get('revision') != revision:
            raise ValueError('journey_source_revision_mismatch')
    slices = data['slices']
    if not isinstance(slices, list) or not 1 <= len(slices) <= 32:
        raise ValueError('invalid_journey_slices')
    ids = []
    for item in slices:
        _keys(item, ('id', 'title', 'evidence', 'adjacent', 'unresolved'), ('feature_ids', 'spec_ids'))
        _name(item['id']); ids.append(item['id']); _text(item['title'])
        if item['id'] in LEGACY_SLICES:
            raise ValueError('journey_legacy_slice_collision')
        for field in ('feature_ids', 'spec_ids'):
            values = item.get(field, [])
            if not isinstance(values, list) or len(values) > 20:
                raise ValueError('invalid_journey_associations')
            for value in values: _text(value)
            _unique(values)
        evidence = item['evidence']
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 20:
            raise ValueError('invalid_journey_evidence')
        eids = []
        for entry in evidence:
            _keys(entry, ('id', 'kind'), ('repo_key', 'path', 'blob_sha', 'start_line', 'end_line',
                                           'observation_id', 'subject', 'binding_sha256', 'role'))
            _name(entry['id']); eids.append(entry['id'])
            if entry.get('role', 'supporting_evidence') not in ('target', 'supporting_evidence'):
                raise ValueError('invalid_journey_role')
            if entry['kind'] == 'source':
                if set(entry) - {'id', 'kind', 'repo_key', 'path', 'blob_sha', 'start_line', 'end_line', 'role'}:
                    raise ValueError('invalid_journey_source_entry')
                if entry.get('repo_key') not in sources: raise ValueError('journey_source_not_registered')
                _path(entry.get('path'))
                if not isinstance(entry.get('blob_sha'), str) or not SHA.fullmatch(entry['blob_sha']):
                    raise ValueError('invalid_journey_blob')
                start, end = entry.get('start_line'), entry.get('end_line')
                if (type(start) is not int or type(end) is not int or not 1 <= start <= end
                        or end - start > 300):
                    raise ValueError('invalid_journey_line_range')
            elif entry['kind'] == 'observation':
                if set(entry) - {'id', 'kind', 'observation_id', 'subject', 'binding_sha256', 'role'}:
                    raise ValueError('invalid_journey_observation_entry')
                if any(not isinstance(entry.get(key), str) or not HASH.fullmatch(entry[key])
                       for key in ('observation_id', 'binding_sha256')):
                    raise ValueError('invalid_journey_observation_identity')
                _text(entry.get('subject'))
            else: raise ValueError('unsupported_journey_evidence_kind')
        _unique(eids)
        adjacent, unresolved = item['adjacent'], item['unresolved']
        if not isinstance(adjacent, list) or len(adjacent) > 16 or not isinstance(unresolved, list) or len(unresolved) > 16:
            raise ValueError('invalid_journey_adjacency')
        for link in adjacent:
            _keys(link, ('target_slice', 'question', 'relation'))
            _name(link['target_slice']); _text(link['question']); _name(link['relation'])
        for gap in unresolved:
            _keys(gap, ('question', 'basis'))
            _text(gap['question']); _text(gap['basis'])
    _unique(ids)
    for item in slices:
        if any(link['target_slice'] not in ids for link in item['adjacent']):
            raise ValueError('journey_adjacency_target_missing')
    questions = data['questions']
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 64:
        raise ValueError('invalid_journey_questions')
    by_id = {item['id']: item for item in slices}
    for qid, question in questions.items():
        _name(qid)
        if qid in LEGACY_QUESTIONS: raise ValueError('journey_legacy_question_collision')
        _keys(question, ('title', 'requires'))
        _text(question['title'])
        if not isinstance(question['requires'], list) or not 1 <= len(question['requires']) <= 32:
            raise ValueError('invalid_journey_question_requirements')
        for requirement in question['requires']:
            _keys(requirement, ('slice_id', 'evidence_ids'))
            sid = requirement['slice_id']
            if sid not in by_id: raise ValueError('journey_question_slice_missing')
            wanted = requirement['evidence_ids']
            available = {e['id'] for e in by_id[sid]['evidence']}
            if not isinstance(wanted, list) or not wanted or len(wanted) > 20 or any(e not in available for e in wanted):
                raise ValueError('journey_question_evidence_missing')
            _unique(wanted)
    return {'data': data, 'sha256': leaf.canonical_sha256(data, ascii=True, allow_nan=False),
            'slices': by_id}


def registered_slices(config):
    registry = load_registry(config)
    legacy = (all(key in config.get('repos', {}) for key in ('ats', 'core'))
              and bool(config.get('evidence_repo')) and bool(config.get('evidence_revision')))
    return (list(LEGACY_SLICES) if legacy else []) + (list(registry['slices']) if registry else [])


def registered_questions(config):
    registry = load_registry(config)
    legacy = (all(key in config.get('repos', {}) for key in ('ats', 'core'))
              and bool(config.get('evidence_repo')) and bool(config.get('evidence_revision')))
    return (list(LEGACY_QUESTIONS) if legacy else []) + (list(registry['data']['questions']) if registry else [])


def assemble_registered(config, registry, slice_id):
    specification = registry['slices'][slice_id]
    items = {}
    for entry in specification['evidence']:
        if entry['kind'] == 'source':
            repo = config['repos'][entry['repo_key']]
            try:
                listing = subprocess.check_output(['git', 'ls-tree',
                    registry['data']['sources'][entry['repo_key']], '--', entry['path']],
                    cwd=repo['path'], timeout=15, stderr=subprocess.PIPE).decode().rstrip('\n')
                metadata, actual_path = listing.split('\t', 1)
                mode, kind, actual_blob = metadata.split()
                if actual_path != entry['path'] or mode not in ('100644', '100755') or kind != 'blob':
                    raise ValueError('journey_source_not_regular')
                if actual_blob != entry['blob_sha']:
                    raise ValueError('journey_source_blob_mismatch')
                size = int(subprocess.check_output(['git', 'cat-file', '-s', actual_blob],
                                                   cwd=repo['path'], timeout=15, stderr=subprocess.PIPE))
            except subprocess.CalledProcessError as error:
                raise ValueError('journey_source_unavailable') from error
            except (UnicodeError, ValueError) as error:
                if isinstance(error, ValueError) and str(error) in ('journey_source_not_regular', 'journey_source_blob_mismatch'):
                    raise
                raise ValueError('journey_source_unavailable') from error
            if size > 1_000_000: raise ValueError('journey_source_file_exceeds_bound')
            blob, content = source(repo['path'], registry['data']['sources'][entry['repo_key']], entry['path'])
            if blob != entry['blob_sha']: raise ValueError('journey_source_blob_mismatch')
            lines = content.splitlines()
            if entry['end_line'] > len(lines): raise ValueError('journey_source_range_changed')
            excerpt = '\n'.join(lines[entry['start_line'] - 1:entry['end_line']])
            if len(excerpt.encode()) > 64_000: raise ValueError('journey_source_excerpt_exceeds_bound')
            items[entry['id']] = {'evidence_class': 'source', 'citation': {
                'repo_key': entry['repo_key'], 'revision': registry['data']['sources'][entry['repo_key']],
                'path': entry['path'], 'blob_sha': blob},
                'start_line': entry['start_line'], 'end_line': entry['end_line'], 'text': excerpt,
                'excerpt_sha256': hashlib.sha256(excerpt.encode()).hexdigest()}
        else:
            location = config.get('observation_registry')
            if not location: raise ValueError('journey_observation_registry_unavailable')
            observation = ObservationStore(location).read(entry['observation_id'])
            record = observation['record']
            if (record['subject'] != entry['subject'] or
                    record['scope'] != {'sources': registry['data']['sources'],
                                        'binding_sha256': entry['binding_sha256']}):
                raise ValueError('journey_observation_scope_mismatch')
            if len(json.dumps(observation, ensure_ascii=True).encode()) > 80_000:
                raise ValueError('journey_observation_exceeds_bound')
            items[entry['id']] = {'evidence_class': {
                'static': 'static-source', 'source': 'source',
                'controlled-test': 'controlled-test', 'runtime': 'observed-runtime'}[record['kind']],
                'reference': {'kind': 'observation', 'id': entry['observation_id']},
                'observation': observation}
    return items


def links_registered(registry, slice_id, budget):
    specification = registry['slices'][slice_id]
    rows = [{'target_slice': link['target_slice'], 'question': link['question'],
             'relation': link['relation'], 'role': 'adjacent_obligation',
             'semantic_hops': 1, 'graph_hops': None, 'status': 'registered',
             'next_call': {'tool': 'verification.packet', 'arguments': {
                 'slice_id': link['target_slice'], 'mode': 'plan', 'budget': budget}}}
            for link in specification['adjacent']]
    rows += [{'question': gap['question'], 'basis': gap['basis'],
              'relation': 'requires_observation', 'role': 'unresolved_obligation',
              'semantic_hops': 1, 'graph_hops': None, 'status': 'observation_required'}
             for gap in specification['unresolved']]
    return rows
