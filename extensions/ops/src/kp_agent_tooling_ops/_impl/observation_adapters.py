"""Provider-specific extraction without increasing the strength of original evidence."""
from kp_agent_tooling_ops._impl.observations import make_record, digest


def lifecycle_applicability(record, original):
    """Extract only known controlled-workload provenance from the lifecycle providers."""
    if record['provider'] not in {'ops.lifecycle', 'ops.lifecycle.retained'}:
        return None
    runtime = original.get('runtime') if isinstance(original, dict) else None
    if not isinstance(runtime, dict):
        return None
    profile = runtime.get('profile')
    targets = {'baseline': 'baseline', 'ordered-pooled-candidate': 'candidate',
               'worker-thread-candidate': 'candidate'}
    if profile not in targets:
        return None
    spans = runtime.get('spans')
    if not isinstance(spans, list):
        return None
    request = next((span for span in spans if isinstance(span, dict)
                    and span.get('name') == 'ats.graph.request'), None)
    if request is None:
        return None
    selector = runtime.get('selector', {})
    resource = request.get('resource', {})
    if not isinstance(selector, dict) or not isinstance(resource, dict):
        return None
    attributes = resource.get('attributes')
    if not isinstance(attributes, dict) or not isinstance(runtime.get('candidate_sha256'), str):
        return None
    return {'target': targets[profile],
            'candidate_sha256': runtime.get('candidate_sha256'),
            'profile': profile, 'scenario': selector.get('scenario') or selector.get('phase'),
            'service': attributes.get('service.name')}


def serena_observation(reply, subject):
    if reply.get('status')!='ok' or reply.get('invocation',{}).get('provider_response_received') is not True:
        raise ValueError('successful Serena invocation receipt required')
    report=reply['report']
    sources={k:v['revision'] for k,v in report['source_snapshot'].items()}
    binding=digest({'provider':report['provider_identity'],'environment':reply.get('environment'),
                    'source_snapshot':report['source_snapshot']})
    return make_record('serena',digest(reply),subject,'static',
        {'sources':sources,'binding_sha256':binding},reply,[],[],
        ['Static symbol/reference observation; not executed behavior or authorization',
         'Invocation identity is a content digest of the retained receipt, not a signed event ID'])


def runtime_observation(packet, sources, references=()):
    if packet.get('status')!='ready' or packet.get('join_status')!='identity_checks_passed':
        raise ValueError('compatible retained packet required')
    runtime=packet['evidence']['runtime']
    if runtime['evidence_class']!='observed-runtime':raise ValueError('runtime evidence required')
    attributes=next(s['attributes'] for s in runtime['spans'] if s['name']=='ats.graph.request')
    if any(attributes.get('source.'+key+'.revision')!=revision for key,revision in sources.items()):
        raise ValueError('runtime source mismatch')
    binding=digest({'candidate':runtime['candidate_sha256'],'profile':runtime['profile'],
        'environment':runtime['environment'],'artifact':runtime['artifact']})
    original={'packet_identity':packet['packet_identity'],'runtime':runtime}
    assertions=[{'predicate':'http.status','value':runtime['row']['status']}]
    return make_record('ops.lifecycle',runtime['row']['trace_id'],'ats::graph_query','runtime',
        {'sources':sources,'binding_sha256':binding},original,assertions,list(references),
        [runtime['scope'],'Retained experiment; no current production observation',
         'Explicit references are navigation links, not proof of equivalent provider bindings'])
