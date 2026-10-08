import json
import pytest
from kp_agent_tooling_ops._impl.service import gateway_transport as g
from kp_agent_tooling._impl.service.agent_tooling import AgentTooling

@pytest.mark.parametrize('url',['http://gateway.example/api/mcp','http://host.docker.internal:8400/api/mcp','file:///etc/passwd','http://user:secret@host/api/mcp','https://host/api?token=x','http://host/api#x','http://host:bad/api'])
def test_invalid_endpoints_rejected(url):
    with pytest.raises(ValueError):g.endpoint_parts(url)

def test_operator_endpoint_is_used_without_harness_config(tmp_path, monkeypatch):
    headers=tmp_path/'headers.json';headers.write_text(json.dumps({'Authorization':'Bearer fixture'}))
    config=tmp_path/'tooling.json';config.write_text(json.dumps({'schema_version':'ops.agent-tooling.v1','repos':{},'gateway_endpoint':'https://gateway.example/tools','gateway_headers_file':str(headers)}))
    a=AgentTooling(config); seen={}
    class Connection:
        def __init__(self,host,port,**kw):seen.update(host=host,port=port)
        def request(self,method,path,body,headers):seen.update(path=path,headers=headers)
        def getresponse(self):return self
        status=200
        def read(self,n):return b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'
        def close(self):pass
    monkeypatch.setattr(g.http.client,'HTTPSConnection',Connection)
    assert a.rpc('tools/list',{})=={'ok':True}
    assert seen['host']=='gateway.example' and seen['port']==443 and seen['path']=='/tools'
    assert seen['headers']['Authorization']=='Bearer fixture'
