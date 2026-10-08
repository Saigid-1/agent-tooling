import json
from kp_agent_tooling_ops._impl.verification_handoff import check_handoff
from kp_agent_tooling_ops._impl.verification_finding import finding_digest


def test_exact_handoff_missing_changed_invalid_and_duplicate():
    f={'schema_version':'ops.verification-finding.v5','claim':'fixture'}
    digest=finding_digest(f);receipts={digest:{'status':'valid'}}
    text='Review\n```json\n'+json.dumps([f])+'\n```'
    assert check_handoff(text,[digest],receipts)['status']=='valid'
    assert check_handoff('All passed',[digest],receipts)['status']=='invalid'
    assert check_handoff(text.replace('fixture','changed'),[digest],receipts)['status']=='invalid'
    assert check_handoff(text,[digest],{})['status']=='invalid'
    assert check_handoff(text,[digest],{digest:{'status':'invalid'}})['status']=='invalid'
    assert check_handoff(json.dumps([f,f]),[digest],receipts)['status']=='invalid'


def test_malformed_extra_json_cannot_hide_behind_valid_artifact():
    f={'schema_version':'ops.verification-finding.v5','claim':'fixture'}
    d=finding_digest(f);valid='```json\n'+json.dumps(f)+'\n```'
    for extra in ['\n```json\n{"broken":\n```','\n```json\n{"broken":']:
        assert check_handoff(valid+extra,[d],{d:{'status':'valid'}})['status']=='invalid'
