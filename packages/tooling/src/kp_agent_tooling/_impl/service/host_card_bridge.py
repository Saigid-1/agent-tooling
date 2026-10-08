"""Operator/host card attribution over existing admitted memory; no dispatch.

A private input file expresses the host operator's assertion, not authenticated
board testimony. It can attach provenance but cannot admit any session.
"""
from datetime import datetime
import sqlite3

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.desk_memory_runtime import components, private_json
from kp_agent_tooling._impl.service.episodic_memory import EpisodeUnavailable, _id
from kp_agent_tooling._impl.service.session_sources import SessionSources
from kp_agent_tooling._impl.service.episodic_search import drain_after_seal, index_of


def attach_card(config_path, event_path, *, apply=False):
    event = private_json(event_path)
    fields = {'schema_version', 'provider_instance', 'host_session_id', 'board_id',
              'workspace_id', 'card_id', 'event_id', 'recorded_at', 'asserted_by'}
    if (not isinstance(event, dict) or not fields <= set(event) or
            set(event) - fields not in (set(), {'native_runtime'}) or
            event['schema_version'] != 'ops.host-card-event.v1'):
        raise ValueError('exact ops.host-card-event.v1 fields required')
    if 'native_runtime' in event and event['native_runtime'] not in ('claude', 'codex'):
        raise ValueError('native_runtime must be claude or codex')
    for key in set(event) - {'schema_version'}:
        value = event[key]
        if (not isinstance(value, str) or not value.strip() or len(value.encode()) > 256
                or any(ord(c) < 32 for c in value)):
            raise ValueError('bounded nonblank host event field required: ' + key)
    if datetime.fromisoformat(event['recorded_at'].replace('Z', '+00:00')).tzinfo is None:
        raise ValueError('timezone-aware recorded_at required')
    config, registry, ledger, store = components(config_path)
    if (event['host_session_id'] != config['provider_session_id']
            or event['provider_instance'] != config['provider_instance']):
        raise ValueError('host event does not match the exact configured session')
    admission = ledger.resolve(config['provider_session_id'], registry)
    card = {key: event[key] for key in ('board_id', 'workspace_id', 'card_id')}
    card_key = _id('card', dict(card, tenant_id=admission.tenant_id))
    source_session = _id('source-session', {'schema_version': 'ops.source-session.v1',
        'tenant_id': admission.tenant_id, 'runtime': config['provider_instance'],
        'native_id': config['provider_session_id']})
    sources = SessionSources(store)
    imported_session = None
    if 'native_runtime' in event:
        imported_session = _id('source-session', {'schema_version': 'ops.source-session.v1',
            'tenant_id': admission.tenant_id, 'runtime': event['native_runtime'],
            'native_id': config['provider_session_id']})
        if imported_session == source_session:
            raise ValueError('native_runtime must identify a distinct imported source session')
        # This is an operator assertion linking two existing identities. Refuse
        # missing or other-tenant history before any episode or claim is written.
        sources.metadata(imported_session, admission.tenant_id)
    source_ref = _id('host-card-event', dict(
        {key: event[key] for key in ('provider_instance', 'host_session_id', 'event_id')},
        tenant_id=admission.tenant_id))
    encoded, event_sha256 = leaf.canonical_json_sha256(event, ascii=True, allow_nan=True)
    result = {'schema_version': 'ops.host-card-receipt.v1', 'status': 'planned',
        'source_session_id': source_session, 'card': card, 'card_key': card_key,
        'event_sha256': event_sha256,
        'source_ref': source_ref, 'admission_changed': False, 'dispatch': False,
        'evidence_boundary': 'operator-supplied session association; individual event relevance, board execution and commit linkage not verified',
        'claim_filter': {'predicate': 'session.tag', 'object': {'kind': 'card', 'id': card_key}}}
    if imported_session is not None:
        result.update(imported_source_session_id=imported_session,
                      native_runtime=event['native_runtime'],
                      native_session_mapping='operator_asserted')
    if not apply:
        return result
    # Capture owns source_ref conflict detection. Replay recovers after interruption
    # between capture and claim; altered bytes under the same event key refuse.
    receipt = store.capture(config['provider_session_id'], source_ref=source_ref,
        events=[{'event_id': 'host-card', 'role': 'tool', 'text': encoded}])
    sources.metadata(source_session, admission.tenant_id)
    claim = sources.claim(session_id=source_session, predicate='session.tag',
        object={'kind': 'card', 'id': card_key}, asserted_by=event['asserted_by'],
        recorded_at=event['recorded_at'], evidence=[receipt['episode_id'] + '#host-card'])
    imported_claim = None
    if imported_session is not None:
        imported_claim = sources.claim(session_id=imported_session, predicate='session.tag',
            object={'kind': 'card', 'id': card_key}, asserted_by=event['asserted_by'],
            recorded_at=event['recorded_at'], evidence=[receipt['episode_id'] + '#host-card'])
    attached = dict(result, status='attached', episode_id=receipt['episode_id'], claim_id=claim)
    if imported_claim is not None:
        attached['imported_claim_id'] = imported_claim
    # T12b (meet rulings): a card event requests no reindex. Its seal and claims are committed above,
    # each with its outbox row: a host install drains once now, never waiting; inside Compose the
    # indexer role drains. The card's success never depends on the index: search_index is this card's
    # episode as the index then holds it (one event), read only and on a host install only, so a
    # replay of the same event returns the same receipt; inside Compose it reads 0 and opens nothing.
    # An unreadable index is reported (index_status index_unavailable), never raised, and a drain that
    # did not complete is reported with its result; the outbox keeps the work for the next drain.
    drained = drain_after_seal(store)
    search_index = {'indexed_episodes': 0, 'indexed_events': 0}
    if not leaf.docker_runtime():
        try:
            held = index_of(store).digests([receipt['episode_id']])
        except (EpisodeUnavailable, sqlite3.Error, OSError):
            search_index['index_status'] = 'index_unavailable'
        else:
            search_index = {'indexed_episodes': len(held), 'indexed_events': len(held)}
    if isinstance(drained, dict) and ('index_status' in search_index
                                      or drained.get('status') not in ('drained', 'idle')):
        search_index['drain'] = drained
    attached['search_index'] = search_index
    return attached
