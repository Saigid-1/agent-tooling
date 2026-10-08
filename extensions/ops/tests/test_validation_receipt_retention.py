"""Identity probes must not replace evidence validation of the actual artifact."""
import json
from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
from test_verification_finding import CONFIG, finding, resolver


def adapter(tmp_path, monkeypatch):
    config = tmp_path / 'tooling.json'
    config.write_text(json.dumps(dict(CONFIG, schema_version='ops.agent-tooling.v1',
        enabled_tools=['verification.finding', 'verification.handoff'])))
    monkeypatch.setattr('kp_agent_tooling_ops._impl.verification_finding.packet', resolver)
    return AgentTooling(config)


def handoff(a, artifact, digest):
    return a.call('verification.handoff', {'final_text': json.dumps(artifact),
                                           'expected_digests': [digest]})


def test_negative_identity_probe_preserves_valid_receipt(tmp_path, monkeypatch):
    a = adapter(tmp_path, monkeypatch); f = finding()
    good = a.call('verification.finding', {'mode': 'validate', 'finding': f})
    digest = good['finding_sha256']
    assert good['status'] == 'valid'
    bad = a.call('verification.finding', {'mode': 'validate', 'finding': f,
                                         'expected_finding_sha256': '0' * 64})
    assert bad['status'] == 'invalid'
    assert bad['receipt_retention'] == 'not_retained_identity_mismatch'
    assert a.validation_receipts[digest] == good
    assert handoff(a, f, digest)['status'] == 'valid'


def test_identity_probe_cannot_create_receipt(tmp_path, monkeypatch):
    a = adapter(tmp_path, monkeypatch); f = finding()
    bad = a.call('verification.finding', {'mode': 'validate', 'finding': f,
                                         'expected_finding_sha256': '0' * 64})
    assert not a.validation_receipts
    assert handoff(a, f, bad['finding_sha256'])['status'] == 'invalid'


def test_actual_failed_revalidation_invalidates_prior_receipt(tmp_path, monkeypatch):
    a = adapter(tmp_path, monkeypatch); f = finding()
    good = a.call('verification.finding', {'mode': 'validate', 'finding': f})
    digest = good['finding_sha256']
    a.config['repos']['ats']['revision'] = 'e' * 40
    failed = a.call('verification.finding', {'mode': 'validate', 'finding': f,
                                            'expected_finding_sha256': digest})
    assert failed['status'] == 'invalid'
    assert failed['receipt_retention'] == 'retained'
    assert handoff(a, f, digest)['status'] == 'invalid'
