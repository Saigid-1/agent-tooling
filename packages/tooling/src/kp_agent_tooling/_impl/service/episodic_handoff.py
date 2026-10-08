"""Loss-aware projections of immutable capsules; no implicit selection or writes."""
from kp_agent_tooling._impl import leaf


def encoded_size(value):
    return len(leaf.canonical_bytes(value, ascii=True, allow_nan=True))


def coverage(entries, items):
    """Union exact citation intervals. Coverage measures characters, not meaning."""
    refs={}
    for item in items:
        for ref in item['citations']:
            refs.setdefault((ref['episode_id'],ref['event_id']),[]).append((ref['start'],ref['end']))
    result=[]
    for entry in entries:
        merged=[]
        for start,end in sorted(refs.get((entry['episode_id'],entry['event_id']),[])):
            if merged and start<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],end)
            else:merged.append([start,end])
        missing=[];cursor=0
        for start,end in merged:
            if cursor<start:missing.append([cursor,start])
            cursor=end
        if cursor<entry['characters']:missing.append([cursor,entry['characters']])
        result.append(dict(entry,cited_ranges=merged,uncited_ranges=missing,
                           cited_characters=sum(end-start for start,end in merged),
                           coordinate_system='unicode-codepoints',meaning_preservation='not-assessed'))
    return result


def loss_summary(entries, items):
    rows=coverage(entries,items)
    total=sum(row['characters'] for row in rows)
    cited=sum(row['cited_characters'] for row in rows)
    return {'source_characters':total,'cited_characters':cited,'uncited_characters':total-cited,
            'coordinate_system':'unicode-codepoints','meaning_preservation':'not-assessed'}


def _read_attribution(store, session, binding, record, cited=None):
    from kp_agent_tooling._impl.service.episodic_provenance import read_attribution
    return read_attribution(store, session, binding, record, cited=cited)


def _capsule(store, session, binding, capsule_id, attribute):
    """The capsule, and (for an attributed cross-desk read) its cited episodes, in one read."""
    if not attribute:
        return store._read(binding, capsule_id, 'capsules', 'episode-capsule'), None
    from kp_agent_tooling._impl.service.session_sources import SessionSources
    records, cited = SessionSources(store).capsules_with_cited(session, binding, [capsule_id])
    return records[0][1], cited


def resume(store,session,*,capsule_id,budget_bytes=12000,binding_key=None):
    """Select whole handoff items within a budget, disclosing every exclusion.

    Ordering is the authored ordering, not inferred importance. The caller must
    supply a capsule ID; storage recency is never treated as acceptance.
    """
    if type(budget_bytes) is not int or not 1500<=budget_bytes<=24000:
        raise ValueError('resume budget must be 1500..24000 bytes')
    binding=store.read_binding(session,binding_key)
    # One read of the capsule. Opening an old v1 capsule is supported; no stored capsule is rewritten.
    cross=binding_key is not None and binding != store._binding(session)
    record,cited=_capsule(store,session,binding,capsule_id,cross)
    handoff=record['handoff']
    source_loss=loss_summary(record['evidence_directory'],handoff['items'])
    result={'schema_version':'ops.memory-resume.v1','capsule_id':capsule_id,
        'binding_key':record['binding_key'],
        'authority':'derived interpretation; not an instruction grant or accepted decision',
        'selection':'authored order; whole items only','items':[],'unresolved_questions':[],
        'source_qualifiers':[],
        'evidence_review':handoff.get('evidence_review', {'semantic_entailment':'not-assessed','version':'legacy'}),
        'omissions':{'handoff_item_indices':list(range(len(handoff['items']))),
                     'question_indices':list(range(len(handoff['unresolved_questions']))),
                     'qualifier_indices':list(range(len(handoff.get('source_qualifiers',[])))),
                     'reason':'context_budget','source_citation_coverage':source_loss},
        'source_recovery':{'name':'memory.evidence_directory','arguments':{'capsule_id':capsule_id}},
        'handoff_recovery':{'name':'memory.handoff_page','arguments':{'capsule_id':capsule_id,'offset':0,'budget_bytes':budget_bytes}},
        'budget_bytes':budget_bytes,'complete_handoff':False}
    if cross:
        result['read_attribution'] = _read_attribution(store,session,binding,record,cited)
        result['source_recovery']['arguments']['binding_key'] = binding
        result['handoff_recovery']['arguments']['binding_key'] = binding
    if encoded_size(result)>budget_bytes:raise ValueError('resume metadata exceeds budget; increase budget')
    # Negative source statements get first access to the bounded context.
    for index,qualifier in enumerate(handoff.get('source_qualifiers',[])):
        result['source_qualifiers'].append({'index':index,'value':qualifier})
        result['omissions']['qualifier_indices'].remove(index)
        if encoded_size(result)>budget_bytes:
            result['source_qualifiers'].pop();result['omissions']['qualifier_indices'].append(index)
    result['omissions']['qualifier_indices'].sort()
    for index,item in enumerate(handoff['items']):
        result['items'].append({'index':index,'item':item})
        result['omissions']['handoff_item_indices'].remove(index)
        if encoded_size(result)>budget_bytes:
            result['items'].pop();result['omissions']['handoff_item_indices'].append(index)
    result['omissions']['handoff_item_indices'].sort()
    for index,question in enumerate(handoff['unresolved_questions']):
        result['unresolved_questions'].append({'index':index,'text':question})
        result['omissions']['question_indices'].remove(index)
        if encoded_size(result)>budget_bytes:
            result['unresolved_questions'].pop();result['omissions']['question_indices'].append(index)
    result['omissions']['question_indices'].sort()
    result['complete_handoff']=not (result['omissions']['handoff_item_indices'] or result['omissions']['question_indices'] or result['omissions']['qualifier_indices'])
    assert encoded_size(result)<=budget_bytes
    return result


def handoff_page(store,session,*,capsule_id,offset=0,budget_bytes=12000,binding_key=None):
    """Byte-bounded page of authored handoff content with stable item indices."""
    if type(offset) is not int or offset<0 or type(budget_bytes) is not int or not 1500<=budget_bytes<=24000:
        raise ValueError('bounded handoff page required')
    binding=store.read_binding(session,binding_key)
    cross=binding_key is not None and binding != store._binding(session)
    record,cited=_capsule(store,session,binding,capsule_id,cross)
    h=record['handoff']
    rows=[{'kind':'item','index':i,'value':v} for i,v in enumerate(h['items'])]
    rows += [{'kind':'unresolved_question','index':i,'value':v} for i,v in enumerate(h['unresolved_questions'])]
    rows += [{'kind':'source_qualifier','index':i,'value':v} for i,v in enumerate(h.get('source_qualifiers',[]))]
    if offset>len(rows):raise ValueError('handoff offset out of range')
    page={'capsule_id':capsule_id,'entries':[],'offset':offset,'total':len(rows),'next_offset':offset if offset<len(rows) else None,
          'budget_bytes':budget_bytes,'status':'complete','authority':'source assertions and interpretations; not independent proof',
          'evidence_review':h.get('evidence_review', {'semantic_entailment':'not-assessed','version':'legacy'})}
    if cross:
        page['read_attribution'] = _read_attribution(store,session,binding,record,cited)
    for index in range(offset,len(rows)):
        page['entries'].append(rows[index]);page['next_offset']=index+1 if index+1<len(rows) else None
        if encoded_size(page) + 32 > budget_bytes:
            page['entries'].pop();page['next_offset']=index
            page['status']='page_full' if page['entries'] else 'item_exceeds_budget'
            if not page['entries']:
                page['guidance']='Increase budget or read the full bounded capsule with memory.handoff; no content was discarded.'
            break
    assert encoded_size(page)<=budget_bytes
    return page
