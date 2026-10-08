"""Validate declared findings against registered evidence; never certify semantics."""
import json
from pathlib import Path
from importlib.resources import files

from jsonschema import Draft202012Validator
from kp_agent_tooling._impl import leaf
from kp_agent_tooling_ops._impl.verification_packet import packet
from kp_agent_tooling_ops._impl.observations import ObservationStore
from kp_agent_tooling_ops._impl.observation_adapters import lifecycle_applicability
from kp_agent_tooling_ops._impl.evidence_references import resolve_reference, EvidenceReferenceError

def schema():
    if __package__.startswith('kp_agent_tooling_ops.'):
        return json.loads(files('kp_agent_tooling_ops').joinpath('assets/finding.schema.json').read_text())
    return json.loads((Path(__file__).resolve().parents[1] / 'docs/verification/finding.schema.json').read_text())


def finding_digest(finding):
    """Identity of parsed JSON: sorted keys, ASCII escaping, compact separators."""
    return leaf.canonical_sha256(finding, ascii=True, allow_nan=False)


def validate_finding(config, finding, resolver=None, expected_finding_sha256=None):
    resolver = resolver or (lambda config, **args: packet(config, **args, _internal=True))
    errors = []
    def fail(path, code):
        errors.append({'path': path, 'code': code})
    base = {'schema_version': 'ops.finding-validation.v1',
            'finding_sha256': finding_digest(finding),
            'digest_contract': 'SHA256 of sorted-key ASCII JSON with compact separators; array order preserved',
            'semantic_verdict': 'not-assessed', 'memory_promotion': 'not-performed',
            'delivery_verification': 'self-reported; not proof of model receipt or review'}
    if expected_finding_sha256 is not None and expected_finding_sha256!=base['finding_sha256']:
        fail('/', 'finding_identity_changed')
    for error in Draft202012Validator(schema()).iter_errors(finding):
        fail('/' + '/'.join(map(str, error.absolute_path)), 'schema:' + error.validator)
    if errors:
        return dict(base, status='invalid', errors=errors[:50])
    # Recompute from immutable originals; a caller-provided status is not a receipt.
    assessment = finding['observation_assessment']
    if assessment is not None:
        try:
            registry = config.get('observation_registry')
            if not registry:
                raise ValueError('registry unavailable')
            store = ObservationStore(registry)
            checked = store.assess(assessment['ids'], assessment['expected_scope'])
            base['checked_observations'] = dict(checked, ids=assessment['ids'],
                expected_scope=assessment['expected_scope'], use=assessment['use'])
            if checked['status'] != assessment['declared_status']:
                fail('/observation_assessment/declared_status', 'observation_status_mismatch')
            if assessment['use'] == 'joint_support':
                if checked['status'] != 'compatible':
                    fail('/observation_assessment', 'observation_join_not_compatible')
                if assessment['expected_scope']['sources'] != finding['scope']['sources']:
                    fail('/observation_assessment/expected_scope', 'observation_source_mismatch')
                if any(store.read(key)['record']['subject'] != finding['claim']['subject']
                       for key in assessment['ids']):
                    fail('/observation_assessment/ids', 'observation_subject_mismatch')
                applicability_checks = []
                for key in assessment['ids']:
                    observed = store.read(key)
                    record = observed['record']
                    if record['kind'] not in {'runtime', 'controlled-test'}:
                        continue
                    applicability = lifecycle_applicability(record, observed['original'])
                    applicability_checks.append({'id': key, 'observed': applicability,
                        'expected': {'target': finding['claim']['target'],
                                     'scenario': finding['scope']['scenario'],
                                     'execution_evidence': finding['scope']['execution_evidence']}})
                    if applicability is None:
                        # Generic controlled receipts can still be joined by their
                        # source/binding scope. Runtime claims need runtime provenance.
                        if record['kind'] == 'runtime':
                            fail('/observation_assessment/ids', 'observation_applicability_missing')
                        continue
                    if finding['claim']['target'] != applicability['target']:
                        fail('/observation_assessment/ids', 'observation_target_mismatch')
                    execution = finding['scope']['execution_evidence']
                    if (execution is None or applicability['candidate_sha256'] != execution['candidate_sha256']
                            or applicability['profile'] != execution['profile']):
                        fail('/observation_assessment/ids', 'observation_transformation_mismatch')
                    if not applicability['scenario'] or applicability['scenario'] != finding['scope']['scenario']:
                        fail('/observation_assessment/ids', 'observation_scenario_mismatch')
                    if (not applicability['service'] or execution is None
                            or execution.get('runtime_service') != applicability['service']):
                        fail('/observation_assessment/ids', 'observation_runtime_scope_mismatch')
                base['checked_observations']['applicability'] = applicability_checks
        except (OSError, ValueError, KeyError, TypeError):
            fail('/observation_assessment', 'observation_assessment_unavailable')
    scope = finding['scope']
    expected = {key: value['revision'] for key, value in config['repos'].items()}
    if scope['sources'] != expected:
        fail('/scope/sources', 'configured_source_mismatch')
    execution = scope['execution_evidence']
    if execution is not None and execution['execution_revision'] != config.get('evidence_revision'):
        fail('/scope/execution_evidence/execution_revision', 'execution_revision_mismatch')
    packets = {}
    for i, pin in enumerate(finding['packets']):
        sid = pin['slice_id']
        if sid in packets:
            fail(f'/packets/{i}', 'duplicate_slice')
            continue
        try:
            result = resolver(config, slice_id=sid, mode='read', budget=100000,
                              expected_packet_identity=pin['packet_identity'])
        except (ValueError, OSError) as error:
            result = {'status': 'unverified', 'gap': {'reason': type(error).__name__}}
        packets[sid] = result
        if result.get('status') != 'ready':
            fail(f'/packets/{i}', 'packet_unavailable_or_identity_changed')
    def key(ref):
        return finding_digest(ref)
    resolved = {}
    def check(ref, path):
        k = key(ref)
        if k in resolved:
            return
        try:
            if ref['kind'] == 'packet':
                sid, eid = ref['slice_id'], ref['id']
                p = packets.get(sid, {})
                if p.get('packet_identity') != ref['packet_identity'] or eid not in p.get('evidence', {}):
                    raise EvidenceReferenceError('unresolved_evidence_reference')
                value = p['evidence'][eid]
                resolved[k] = {'reference':ref, 'evidence_class':value.get('evidence_class'), 'evidence':value}
            else:
                resolved[k] = resolve_reference(config, ref)
                if ref['kind'] == 'observation':
                    obs_scope=resolved[k]['source_identity']['scope']
                    if obs_scope['sources'] != finding['scope']['sources']:
                        fail(path,'observation_source_mismatch')
                    if assessment is None or ref['id'] not in assessment['ids']:
                        fail(path,'observation_assessment_required')
        except (EvidenceReferenceError, OSError, ValueError, KeyError, TypeError) as error:
            fail(path, getattr(error, 'code', 'unresolved_evidence_reference'))
    all_refs = [(r['reference'], f'/evidence/{i}') for i, r in enumerate(finding['evidence'])]
    all_refs += [(r, f'/delivery/complete/{i}') for i, r in enumerate(finding['delivery']['complete'])]
    all_refs += [(r['reference'], f'/delivery/gaps/{i}') for i, r in enumerate(finding['delivery']['gaps'])]
    for i, obligation in enumerate(finding['unresolved']):
        all_refs += [(r, f'/unresolved/{i}/references') for r in obligation['references']]
    for ref, path in all_refs:
        check(ref, path)
    complete = {key(r) for r in finding['delivery']['complete']}
    cited = {key(r['reference']) for r in finding['evidence']}
    if len(cited) != len(finding['evidence']):
        fail('/evidence', 'duplicate_evidence')
    if not cited <= complete:
        fail('/evidence', 'cited_evidence_not_delivered_complete')
    gap_keys = set()
    for i, gap in enumerate(finding['delivery']['gaps']):
        k = key(gap['reference'])
        if k in gap_keys:
            fail(f'/delivery/gaps/{i}', 'duplicate_delivery_gap')
        gap_keys.add(k)
        if gap['recovered'] != (k in complete):
            fail(f'/delivery/gaps/{i}', 'delivery_recovery_conflict')
    for i, obligation in enumerate(finding['unresolved']):
        keys = {key(r) for r in obligation['references']}
        kind = obligation['classification']
        if kind == 'registered_but_unread' and (not keys or keys & complete):
            fail(f'/unresolved/{i}', 'unread_classification_conflict')
        if kind in {'reviewed_with_limitations', 'observation_required'} and (not keys or not keys <= cited):
            fail(f'/unresolved/{i}', 'reviewed_basis_requires_cited_evidence')
    if execution is None:
        for i, ref in enumerate(finding['evidence']):
            value = resolved.get(key(ref['reference']), {})
            if value.get('evidence_class') not in {'source', 'source-syntax', 'static-source'}:
                fail(f'/evidence/{i}', 'execution_evidence_identity_required')
        if assessment is not None and assessment['use'] == 'joint_support' and 'checked_observations' in base:
            if any(store.read(key)['record']['kind'] not in {'source', 'static'}
                   for key in assessment['ids']):
                fail('/observation_assessment', 'execution_evidence_identity_required')
    # Candidate/profile must occur together in registered evidence, not just look like hashes.
    supported=[]
    def collect(value, sid, eid, pointer=''):
        if isinstance(value, dict):
            if isinstance(value.get('candidate_sha256'),str) and isinstance(value.get('profile'),str):
                supported.append({'candidate_sha256':value['candidate_sha256'],'profile':value['profile'],
                    'reference':{'slice_id':sid,'id':eid,'pointer':pointer}})
            for key,v in value.items():
                collect(v,sid,eid,pointer+'/'+key.replace('~','~0').replace('/','~1'))
        elif isinstance(value,list):
            for index,v in enumerate(value):collect(v,sid,eid,pointer+'/'+str(index))
    # Identity support must come from cited, completely delivered evidence, never
    # an internally expanded packet neighbor or an explicit unread/gap reference.
    for k,value in resolved.items():
        if k not in cited or k not in complete:
            continue
        ref=value['reference']
        collect(value['evidence'],ref.get('slice_id',ref['kind']),
                ref.get('id',ref.get('evidence_id',ref.get('path'))))
    if execution is not None and not any(x['candidate_sha256']==execution['candidate_sha256'] and x['profile']==execution['profile'] for x in supported):
        same_candidate=[x for x in supported if x['candidate_sha256']==execution['candidate_sha256']]
        fail('/scope/execution_evidence/profile' if same_candidate else '/scope/execution_evidence/candidate_sha256', 'candidate_profile_not_resolved')
        choices=same_candidate or supported
        errors[-1].update(supported_pairs=choices[:20],omitted_pairs=max(0,len(choices)-20),
            guidance='Use the exact candidate/profile pair only if it matches your intended evidence scope. Workload labels and prose are not profile identifiers. No automatic correction applied.')
    return dict(base, status='invalid' if errors else 'valid', errors=errors[:50],
                checked_packets=[{'slice_id': sid, 'packet_identity': p.get('packet_identity')}
                                 for sid, p in packets.items()])
