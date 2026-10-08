from test_navigation_search_pages import source
from test_serena_navigation import repo
from kp_agent_tooling._impl.service.repository_coverage import call
from kp_agent_tooling._impl.service.serena_navigation import SerenaNavigationProvider


def test_partial_page_is_prominent_and_continuation_is_callable(source):
    _, config, registry, revision=source
    config['navigation_registry_path']=str(registry)
    args={'repo_key':'repo','target_revision':revision,'query':'needle','path_pattern':'*.txt','limit':2}
    first=call(config,'navigation.search_page',args)
    assert first['status']=='incomplete' and 'incomplete' in first['message'].lower()
    assert first['absence_verdict']=='not-established'
    next_call=first['next_call'];second=call(config,next_call['name'],next_call['arguments'])
    assert second['results'] != first['results']


def test_exact_name_miss_never_claims_absence(repo,monkeypatch):
    root,revision=repo
    provider=SerenaNavigationProvider('/unused')
    monkeypatch.setattr(provider,'_discovery',lambda *a,**k:({'rows':[],'server':{}},[],{}))
    result=provider.find(root,revision,'WrongName')
    assert result['outcome']=='no_exact_match'
    assert result['match_mode']=='exact_name' and result['query']=='WrongName'
    assert result['report']['absence_verdict']=='not-established'
    assert 'differently named' in result['message']


def test_facade_exact_miss_returns_executable_text_search(source,tmp_path,monkeypatch):
    import json
    from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
    _,config,registry,revision=source
    config.update(schema_version='ops.agent-tooling.v1',navigation_registry_path=str(registry),
                  serena={'runtime_home':str(tmp_path/'runtime'),'command':'/unused/bin/uv','python':'/unused/python'},
                  enabled_tools=['serena.find','navigation.search_page'])
    path=tmp_path/'tooling.json';path.write_text(json.dumps(config))
    monkeypatch.setattr(SerenaNavigationProvider,'_discovery',lambda *a,**k:({'rows':[],'server':{}},[],{}))
    adapter=AgentTooling(path)
    result=adapter.call('serena.find',{'repo_key':'repo','symbol':'needle'})
    assert result['outcome']=='no_exact_match' and result['next_call']['name']=='navigation.search_page'
    page=adapter.call(result['next_call']['name'],result['next_call']['arguments'])
    assert page['results'] and page['absence_verdict']=='not-established'


def test_serena_requested_revision_mismatch_is_actionable(source,tmp_path):
    import json
    from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
    _,config,registry,revision=source
    config.update(schema_version='ops.agent-tooling.v1',navigation_registry_path=str(registry),
                  serena={'runtime_home':str(tmp_path/'runtime'),'command':'/unused/bin/uv','python':'/unused/python'},
                  enabled_tools=['serena.find','tooling.identity'])
    path=tmp_path/'tooling.json';path.write_text(json.dumps(config))
    adapter=AgentTooling(path)
    result=adapter.call('serena.find',{'repo_key':'repo','symbol':'Needle','target_revision':'f'*40})
    assert result['reason']=='requested_revision_not_selected'
    assert result['requested_revision']=='f'*40 and result['selected_revision']==revision
    assert result['absence_verdict']=='not-established' and result['recovery']
    assert adapter.call(result['next_call']['name'],result['next_call']['arguments'])
