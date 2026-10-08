"""Source attribution remains separate from an operator's import operation."""

def origin(record, event_id=None):
    source = record.get('source_provenance')
    if source is None:
        return {'source_session':record['session']}
    authored_at = source.get('authored_at', 'unknown')
    if event_id is not None:
        index = next((index for index, event in enumerate(record['events'])
                      if event['event_id'] == event_id), None)
        authored_at = 'unknown'
        if index is not None:
            for section in source.get('evidence_event_map', []):
                if section['event_start'] <= index < section['event_start'] + section['event_count']:
                    authored_at = section.get('authored_at', 'unknown')
                    break
    legacy_session = source.get('legacy_host_session_id')
    if not isinstance(legacy_session, str) or not legacy_session.strip() or legacy_session == 'legacy-import':
        legacy_session = 'unknown'
    return {'source_session':legacy_session,
            'source_actor':source.get('source_actor','unknown'),
            'source_authored_at':authored_at,
            'source_observed_at':source.get('source_observed_at','unknown'),
            'source_record_id':source['row_id'],'source_record_digest':source['row_digest'],
            'source_binding_provenance':source.get('legacy_binding_provenance'),
            'capture_session':record['session'],'capture_provider':record['provider_instance'],
            'import_recorded_at':record.get('import_recorded_at')}


def cited_records(store, session, capsules):
    """Every source episode the capsules cite, reopened in topic scope in one batched read."""
    from kp_agent_tooling._impl.service.session_sources import SessionSources
    identities = list(dict.fromkeys(identity for capsule in capsules
                                    for identity in capsule['handoff']['source_episode_ids']))
    if not identities:
        return {}
    found = SessionSources(store).scoped_records(session, identities, scope='topic')
    return {identity: record for identity, (record, _) in found.items()}


def read_attribution(store, session, binding, record=None, *, cited=None):
    records = []
    if record is not None:
        if 'events' in record: records = [record]
        elif 'handoff' in record:
            if cited is None:
                cited = cited_records(store, session, [record])
            records = [cited[identity] for identity in record['handoff']['source_episode_ids']]
    sources = [origin(row) for row in records]
    sessions = list(dict.fromkeys(row['source_session'] for row in sources if row['source_session'] != 'unknown'))
    cross = binding != store._binding(session)
    result = {'binding_key':binding,'read_scope':'other_desk' if cross else 'own',
              'source_sessions':sessions,'source_actor':'unknown','source_observed_at':'unknown',
              'authority':"another desk's claim as of an unknown observation time" if cross else
                          'desk evidence; observation time unknown'}
    if any('source_provenance' in row for row in records): result['sources'] = sources
    return result
