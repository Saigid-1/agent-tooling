import json
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import pytest
from kp_agent_tooling._impl.service.memory_budget import openrouter_profile,memory_budget
from kp_agent_tooling._impl.service.episodic_summarizer import OpenRouterEpisodeSummarizer,SummaryUnavailable
from kp_agent_tooling._impl.service.summary_contract import full_request


def packet(text='hi'):
    return full_request(binding='desk',records=[{'episode_id':'ep',
        'source_ref':'visible:test','events':[{'event_id':'e','role':'user','text':text}]}],
        input_bytes=60000,handoff_bytes=12000)


def configured(transport):
    # Keep fixture metadata inside the freshness window, away from its zero-age boundary.
    profile=openrouter_profile({'id':'synthetic/model','context_length':24000,
        'top_provider':{'max_completion_tokens':4000}},provider_id='synthetic',observed_at=datetime.now(timezone.utc)-timedelta(minutes=1))
    budget=memory_budget(profile,system_tokens=2048,tool_tokens=1024,reasoning_tokens=1024,output_tokens=4000,safety_tokens=1024)
    return OpenRouterEpisodeSummarizer(api_key='test-only',budget=budget,transport=transport,
        provider_order=['synthetic'],require_zdr=True)


def response(**kwargs):
    return dict(choices=[{'finish_reason':'stop','message':{'content':json.dumps({'items':[], 'unresolved_questions':['No evidence.']})}}],**kwargs)


def test_proposal_route_budget_and_receipt():
    calls=[]
    def transport(key,body,timeout):
        calls.append(json.loads(body));return response(usage={'prompt_tokens':12,'completion_tokens':9,'cost':0.01},model='synthetic/model')
    summarizer=configured(transport)
    assert summarizer(packet())['items']==[]
    assert calls[0]['provider']=={'allow_fallbacks':True,'require_parameters':True,'order':['synthetic'],'zdr':True,'data_collection':'deny'}
    assert summarizer.last_receipt['usage']['prompt_tokens']==12
    assert summarizer.last_receipt['citation_validation']=='pending'
    assert 'test-only' not in json.dumps(summarizer.last_receipt)


def test_failure_not_retried_or_exposed():
    calls=[]
    def transport(*args):
        calls.append(1);raise RuntimeError('secret provider detail')
    summarizer=configured(transport)
    with pytest.raises(SummaryUnavailable) as e:summarizer(packet())
    assert 'secret' not in str(e.value) and len(calls)==1
    assert summarizer.last_receipt['status']=='failed_or_uncertain'


@pytest.mark.parametrize('result',[{'choices':[{'finish_reason':'length','message':{'content':'{}'}}]},
    {'choices':[{'finish_reason':'stop','message':{'content':'not JSON'}}]},
    {'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}])
def test_incomplete_or_invalid_proposals_fail(result):
    with pytest.raises(SummaryUnavailable):configured(lambda *a:result)(packet())


def test_oversize_and_expired_block_before_transport():
    calls=[];summary=configured(lambda *a:calls.append(a))
    with pytest.raises(ValueError):summary(packet('é'*20000))
    summary.budget=replace(summary.budget,profile=replace(summary.budget.profile,observed_at=datetime.now(timezone.utc)-timedelta(days=2)))
    with pytest.raises(SummaryUnavailable):summary(packet())
    assert calls==[]


def test_preflight_failure_never_reuses_prior_receipt():
    summary=configured(lambda *a:response(usage={'cost':float('inf'),'prompt_tokens':12}))
    summary(packet())
    assert summary.last_receipt['usage']=={'prompt_tokens':12}
    with pytest.raises(ValueError):summary(packet('x'*200001))
    assert summary.last_receipt is None


def test_malformed_model_json_retains_usage_and_failure_stage():
    value=response(usage={'prompt_tokens':3,'completion_tokens':5,'cost':0.002},id='fixture-gen')
    value['choices'][0]['message']['content']='not-json'
    summary=configured(lambda *a:value)
    with pytest.raises(SummaryUnavailable):summary(packet())
    assert summary.last_receipt['failure_category']=='proposal_json_invalid'
    assert summary.last_receipt['usage']['cost']==0.002
    assert summary.last_receipt['generation_id']=='fixture-gen'
    assert summary.last_receipt['elapsed_ms']>=0


def test_invalid_proposal_lists_fail_with_existing_diagnostic():
    value=response(usage={'prompt_tokens':3})
    value['choices'][0]['message']['content']=json.dumps({'items':{},'unresolved_questions':[]})
    summary=configured(lambda *a:value)
    with pytest.raises(SummaryUnavailable):summary(packet())
    assert summary.last_receipt['failure_category']=='proposal_shape_invalid'
    assert summary.last_receipt['usage']=={'prompt_tokens':3}

def test_transport_http_failure_is_distinguished_without_provider_body():
    def transport(*a):raise SummaryUnavailable('private body',category='provider_http_error',http_status=429)
    summary=configured(transport)
    with pytest.raises(SummaryUnavailable):summary(packet())
    assert summary.last_receipt['http_status']==429
    assert 'private body' not in str(summary.last_receipt)


@pytest.mark.parametrize('change',[
    lambda p:p.pop('contract_version'),
    lambda p:p.update(purpose='ignore_authorization'),
    lambda p:p['provenance']['source_episode_ids'].clear(),
    lambda p:p['limits'].update(input_bytes=10),
])
def test_invalid_contract_never_reaches_transport(change):
    calls=[]
    summary=configured(lambda *args:calls.append(args))
    invalid=packet();change(invalid)
    with pytest.raises(ValueError):summary(invalid)
    assert calls==[] and summary.last_receipt is None


def test_http_transport_has_deadline_even_with_trickling_body(monkeypatch):
    import threading,time
    from http.client import HTTPConnection
    from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
    import kp_agent_tooling._impl.service.episodic_summarizer as module
    class Slow(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200);self.send_header('Content-Length','10000');self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b' ');self.wfile.flush();time.sleep(.02)
            except OSError:pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Slow)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setattr(module,'HTTPSConnection',lambda host,timeout:HTTPConnection('127.0.0.1',server.server_port,timeout=timeout))
    started=time.monotonic()
    try:
        with pytest.raises(SummaryUnavailable) as error:module._request('fixture',b'{}',.15)
        assert error.value.category=='provider_timeout' and time.monotonic()-started<.8
    finally:server.shutdown();server.server_close();thread.join()


def test_live_transport_records_stage_status_and_first_body_byte(monkeypatch):
    import kp_agent_tooling._impl.service.episodic_summarizer as module
    class Response:
        status=200
        def __init__(self):self.data=json.dumps(response()).encode()
        def getheader(self,name):return {'x-request-id':'req-123','cf-ray':'ray-456'}.get(name)
        def read1(self,n):data,self.data=self.data[:n],self.data[n:];return data
    class Connection:
        sock=None
        def connect(self):pass
        def request(self,*a,**kw):pass
        def getresponse(self):return Response()
        def close(self):pass
    monkeypatch.setattr(module,'HTTPSConnection',lambda *a,**kw:Connection())
    summary=configured(module._request);summary(packet())
    diag=summary.last_receipt['transport']
    assert diag['stage']=='completed' and diag['http_status']==200
    assert diag['response_ids']=={'x-request-id':'req-123','cf-ray':'ray-456'}
    assert 0<=diag['connected_ms']<=diag['request_sent_ms']<=diag['headers_received_ms']<=diag['first_body_byte_ms']<=diag['body_complete_ms']
    assert diag['response_bytes']>0 and 'test-only' not in json.dumps(diag)


@pytest.mark.parametrize('failure_stage',['connecting','sending_request','waiting_headers','reading_body'])
def test_timeout_retains_exact_phase_and_safe_partial_receipt(monkeypatch,failure_stage):
    import kp_agent_tooling._impl.service.episodic_summarizer as module
    class Response:
        status=200
        def getheader(self,name):return 'private\nnot-an-id'
        def read1(self,n):raise TimeoutError('secret transport detail')
    class Connection:
        sock=None
        def connect(self):
            if failure_stage=='connecting':raise TimeoutError('secret')
        def request(self,*a,**kw):
            if failure_stage=='sending_request':raise TimeoutError('secret')
        def getresponse(self):
            if failure_stage=='waiting_headers':raise TimeoutError('secret')
            return Response()
        def close(self):pass
    monkeypatch.setattr(module,'HTTPSConnection',lambda *a,**kw:Connection())
    summary=configured(module._request)
    with pytest.raises(SummaryUnavailable):summary(packet())
    diag=summary.last_receipt['transport']
    assert diag['stage']==failure_stage
    assert summary.last_receipt['failure_category']=='provider_timeout'
    assert 'secret' not in json.dumps(summary.last_receipt)
    if failure_stage=='reading_body':assert diag['http_status']==200 and diag['response_ids']=={}
    else:assert 'http_status' not in diag


@pytest.mark.parametrize('status',[400,429,503])
def test_http_rejection_keeps_status_when_error_body_is_unreadable(monkeypatch,status):
    import kp_agent_tooling._impl.service.episodic_summarizer as module
    class Response:
        def getheader(self,name):return 'req-rejected' if name=='x-request-id' else None
        def read1(self,n):raise TimeoutError('private body read failed')
    Response.status=status
    class Connection:
        sock=None
        def connect(self):pass
        def request(self,*a,**kw):pass
        def getresponse(self):return Response()
        def close(self):pass
    monkeypatch.setattr(module,'HTTPSConnection',lambda *a,**kw:Connection())
    summary=configured(module._request)
    with pytest.raises(SummaryUnavailable):summary(packet())
    assert summary.last_receipt['failure_category']=='provider_http_error'
    assert summary.last_receipt['transport']['http_status']==status
    assert summary.last_receipt['transport']['response_ids']=={'x-request-id':'req-rejected'}
    assert summary.last_receipt['transport']['response_bytes']==0


def test_explicit_reasoning_policy_and_numeric_usage_only():
    calls=[]
    def transport(key,body,timeout):
        calls.append(json.loads(body))
        result=response(usage={'completion_tokens':100,'completion_tokens_details':{'reasoning_tokens':90,'private':'secret'}})
        result['choices'][0]['message']['reasoning']='private reasoning not retained'
        return result
    summary=configured(transport)
    summary.disable_reasoning=True
    summary(packet())
    assert calls[0]['reasoning']=={'enabled':False,'exclude':True}
    assert summary.last_receipt['reasoning_policy']=='disabled'
    assert summary.last_receipt['usage']['reasoning_tokens']==90
    assert 'private' not in json.dumps(summary.last_receipt)


def test_json_output_and_truncated_visible_byte_diagnostic():
    calls=[]
    def transport(key,body,timeout):
        calls.append(json.loads(body))
        return {'choices':[{'finish_reason':'length','message':{'content':'{"items":'}}]}
    summary=configured(transport);summary.json_output=True
    with pytest.raises(SummaryUnavailable):summary(packet())
    assert calls[0]['response_format']=={'type':'json_object'}
    assert summary.last_receipt['failure_category']=='output_limit_reached'
    assert summary.last_receipt['message_content_bytes']==9
    assert summary.last_receipt['response_format']=='json_object'


def test_source_ids_reconstruct_unicode_citations_without_model_offsets():
    from kp_agent_tooling._impl.service.episodic_summarizer import source_span_request, resolve_source_spans
    wire, spans = source_span_request(packet('é😀 correction'))
    sid = wire['sources'][0]['source_id']
    assert wire['sources'][0]['text'] == 'é😀 correction'
    result = resolve_source_spans({'items':[{'kind':'decision','text':'Correction','source_ids':[sid]}],
                                  'unresolved_questions':[]}, spans)
    assert result['items'][0]['citations'] == [{'episode_id':'ep','event_id':'e',
        'start':0,'end':13,'quote':'é😀 correction'}]
    assert source_span_request(packet('changed'))[0]['sources'][0]['source_id'] != sid


def test_span_slices_preserve_absolute_offsets_and_boundaries():
    from kp_agent_tooling._impl.service.episodic_summarizer import source_span_request, resolve_source_spans
    value = packet()
    value.pop('episodes')
    value.update(schema_version='ops.episode-consolidation-slice.v1', token_counter='json-escaped-byte-estimate',
        sources=[{'episode_id':'ep','event_id':'e','role':'user','start':71,'end':1372,
                  'text':'é'*1301,'source_tokens':2602}])
    wire, refs = source_span_request(value)
    assert [(r['start'],r['end']) for r in refs.values()] == [(71,671),(671,1271),(1271,1372)]
    assert ''.join(s['text'] for s in wire['sources']) == 'é'*1301
    sid = wire['sources'][1]['source_id']
    result = resolve_source_spans({'items':[{'kind':'observation','text':'Observed','source_ids':[sid]}],
                                  'unresolved_questions':[]}, refs)
    assert result['items'][0]['citations'][0]['start'] == 671


@pytest.mark.parametrize('ids', [['missing'], [], ['valid','valid'], [17]])
def test_invalid_span_selection_fails_closed(ids):
    from kp_agent_tooling._impl.service.episodic_summarizer import resolve_source_spans
    with pytest.raises(ValueError):
        resolve_source_spans({'items':[{'kind':'decision','text':'x','source_ids':ids}],
                             'unresolved_questions':[]}, {'valid':{}})


def test_adapter_resolves_ids_and_classifies_unknown_ids():
    def transport(key, raw, timeout):
        wire=json.loads(json.loads(raw)['messages'][1]['content'])
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
            'items':[{'kind':'decision','text':'hi','source_ids':[wire['sources'][0]['source_id']]}],
            'unresolved_questions':[]})}}]}
    summary=configured(transport)
    assert summary(packet())['items'][0]['citations'][0]['quote']=='hi'
    value=response()
    value['choices'][0]['message']['content']=json.dumps({'items':[{'kind':'decision','text':'x','source_ids':['bad']}], 'unresolved_questions':[]})
    summary=configured(lambda *args:value)
    with pytest.raises(SummaryUnavailable): summary(packet())
    assert summary.last_receipt['failure_category']=='proposal_source_ids_invalid'


def test_explicit_low_reasoning_and_conflicting_policies():
    seen=[]
    base=configured(lambda *args:response())
    adapter=OpenRouterEpisodeSummarizer(api_key='test', budget=base.budget,
        reasoning_effort='low', transport=lambda key,raw,timeout:(seen.append(json.loads(raw)) or response()))
    adapter(packet())
    assert seen[0]['reasoning']=={'effort':'low','exclude':True}
    assert adapter.last_receipt['reasoning_policy']=='low'
    with pytest.raises(ValueError):
        OpenRouterEpisodeSummarizer(api_key='test',budget=base.budget,reasoning_effort='low',disable_reasoning=True)


def test_early_request_shape_precedes_transport_without_source_or_key():
    events=[]
    def transport(*args):
        assert events[0]['event']=='request_prepared'
        assert events[0]['request_shape']['stream'] is False
        assert events[0]['request_shape']['message_roles']==['system','user']
        raise TimeoutError('secret')
    base=configured(transport)
    summary=OpenRouterEpisodeSummarizer(api_key='private-key',budget=base.budget,
        transport=transport,diagnostic_sink=events.append)
    with pytest.raises(SummaryUnavailable):summary(packet('private-source'))
    assert 'private-key' not in json.dumps(events) and 'private-source' not in json.dumps(events)


def test_429_error_metadata_is_bounded_and_keeps_status(monkeypatch):
    import kp_agent_tooling._impl.service.episodic_summarizer as module
    class Response:
        status=429
        def getheader(self,name):return {'Retry-After':'60','Content-Type':'application/json'}.get(name)
        chunks=[json.dumps({'error':{'code':429,'message':'private echo',
            'metadata':{'error_type':'rate_limit_exceeded','limit_source':'openrouter_in_flight_budget','raw':'private secret'}}}).encode(),b'']
        def read1(self,n):return self.chunks.pop(0)
    class Connection:
        sock=None
        def connect(self):pass
        def request(self,*a,**kw):pass
        def getresponse(self):return Response()
        def close(self):pass
    monkeypatch.setattr(module,'HTTPSConnection',lambda *a,**kw:Connection())
    summary=configured(module._request)
    with pytest.raises(SummaryUnavailable):summary(packet())
    diag=summary.last_receipt['transport']
    assert diag['retry_after_seconds']==60
    assert diag['error']=={'code':429,'error_type':'rate_limit_exceeded','limit_source':'openrouter_in_flight_budget'}
    assert summary.last_receipt['failure_category']=='provider_http_error'
    assert 'private' not in json.dumps(diag)


def test_whitespace_timeout_is_observed_not_assumed(monkeypatch):
    import kp_agent_tooling._impl.service.episodic_summarizer as module
    class Response:
        status=200
        def getheader(self,name):return 'application/json' if name=='Content-Type' else None
        calls=0
        def read1(self,n):
            self.calls+=1
            if self.calls==1:return b' \n\t  '
            raise TimeoutError()
    class Connection:
        sock=None
        def connect(self):pass
        def request(self,*a,**kw):pass
        def getresponse(self):return Response()
        def close(self):pass
    monkeypatch.setattr(module,'HTTPSConnection',lambda *a,**kw:Connection())
    summary=configured(module._request)
    with pytest.raises(SummaryUnavailable):summary(packet())
    diag=summary.last_receipt['transport']
    assert diag['response_bytes']==5 and diag['non_whitespace_bytes']==0
    assert 'first_non_whitespace_ms' not in diag


def test_nested_error_sanitizer_drops_unknown_or_content_fields():
    from kp_agent_tooling._impl.service.episodic_summarizer import safe_provider_error,safe_transport_diagnostics
    assert safe_provider_error({'code':429,'metadata':{'error_type':['secret'],'raw':'secret'}})=={'code':429}
    assert safe_transport_diagnostics({'error':{'code':429,'error_type':'rate_limit_exceeded','raw':'secret'},
        'retry_after_seconds':60,'content_type':'application/json; secret'})=={
        'retry_after_seconds':60,'error':{'code':429,'error_type':'rate_limit_exceeded'}}


def test_native_upstream_rate_limit_is_classified_without_raw_text():
    from kp_agent_tooling._impl.service.episodic_summarizer import safe_provider_error,safe_transport_diagnostics
    raw='z-ai/glm-5.3-flash is temporarily rate-limited upstream. Please retry shortly, private tail'
    error=safe_provider_error({'code':429,'metadata':{'raw':raw}})
    assert error=={'code':429,'native_error_class':'upstream_rate_limited'}
    assert safe_transport_diagnostics({'error':error})['error']==error
    assert safe_provider_error({'metadata':{'raw':'private generic message'}})=={}


def test_provider_fallback_is_explicit_and_does_not_retry_client():
    calls=[]
    base=configured(lambda *args:response())
    def transport(key,raw,timeout):
        calls.append(json.loads(raw))
        raise TimeoutError()
    summary=OpenRouterEpisodeSummarizer(api_key='test',budget=base.budget,
        transport=transport,allow_fallbacks=True)
    with pytest.raises(SummaryUnavailable):summary(packet())
    assert len(calls)==1
    assert calls[0]['provider']=={'allow_fallbacks':True,'require_parameters':True,'zdr':True,'data_collection':'deny'}
    assert summary.last_receipt['allow_fallbacks'] is True
    with pytest.raises(ValueError):
        OpenRouterEpisodeSummarizer(api_key='test',budget=base.budget,allow_fallbacks='true')


def test_default_fallback_cannot_remove_privacy_filters_on_error():
    calls=[]
    def rejected(key, raw, timeout):
        calls.append(json.loads(raw))
        raise SummaryUnavailable('no privacy-compatible route',category='provider_http_error',http_status=404)
    s=OpenRouterEpisodeSummarizer(api_key='fixture',budget=configured(None).budget,transport=rejected)
    with pytest.raises(SummaryUnavailable): s(packet())
    assert len(calls)==1
    assert calls[0]['provider']=={'allow_fallbacks':True,'require_parameters':True,'zdr':True,'data_collection':'deny'}
    assert s.last_receipt['request_shape']['zdr'] is True
    assert s.last_receipt['data_collection']=='deny'


def test_disable_fallback_keeps_privacy():
    calls=[]
    s=OpenRouterEpisodeSummarizer(api_key='fixture',budget=configured(None).budget,
        allow_fallbacks=False,transport=lambda k,b,t:(calls.append(json.loads(b)) or response()))
    s(packet())
    assert calls[0]['provider']=={'allow_fallbacks':False,'require_parameters':True,'zdr':True,'data_collection':'deny'}
