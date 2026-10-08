import hashlib
import json
import subprocess
import pytest

from kp_agent_tooling._impl.server_identity import identity
from kp_agent_tooling._impl.service.agent_tooling import AgentTooling


def test_unversioned_or_dirty_source_reports_unknown(tmp_path):
    config = {'repos': {'ats': {'revision': 'a' * 40}}, 'evidence_revision': 'b' * 40}
    missing = identity(config, 'c' * 64, source_root=tmp_path)
    assert missing['status'] == 'unknown' and missing['server_build_revision'] is None
    assert missing['server_build_reason'] == 'unversioned_source'
    assert missing['configured_navigation_source_revisions'] == {'ats': 'a' * 40}
    assert missing['configured_evidence_revision'] == 'b' * 40
    assert missing['executable_artifact_integrity'] == 'not-attested'

    (tmp_path / 'server.py').write_text('print(1)\n')
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    revision = subprocess.check_output(['git', '-C', str(tmp_path), 'rev-parse', 'HEAD'],
                                       text=True).strip()
    known = identity(config, 'c' * 64, source_root=tmp_path)
    assert known['status'] == 'known' and known['server_build_revision'] == revision
    (tmp_path / 'server.py').write_text('print(2)\n')
    changed = identity(config, 'c' * 64, source_root=tmp_path)
    assert changed['status'] == 'unknown' and changed['server_build_reason'] == 'modified_tracked_source'


def test_agent_config_digest_is_from_bytes_parsed_at_startup(tmp_path):
    config = tmp_path / 'tooling.json'
    content = b'{"schema_version":"ops.agent-tooling.v1", "repos":{}}\n'
    config.write_bytes(content)
    adapter = AgentTooling(config)
    assert adapter.config == json.loads(content)
    assert adapter.config_sha256 == hashlib.sha256(content).hexdigest()


def test_native_identity_is_captured_once_at_server_startup(tmp_path, monkeypatch):
    config = tmp_path / 'tooling.json'
    config.write_text('{"schema_version":"ops.agent-tooling.v1","repos":{}}')
    captured = {'schema_version': 'ops.agent-tooling-identity.v1', 'status': 'known',
                'identity_observed_at': 'server_startup', 'server_build_revision': 'd' * 40}
    monkeypatch.setattr('kp_agent_tooling._impl.service.agent_tooling.server_identity',
                        lambda *args: dict(captured))
    adapter = AgentTooling(config)
    monkeypatch.setattr('kp_agent_tooling._impl.service.agent_tooling.server_identity',
                        lambda *args: pytest.fail('identity recomputed after startup'))
    assert adapter.call('tooling.identity', {})['server_build_revision'] == 'd' * 40
    assert adapter.call('tooling.identity', {})['server_build_revision'] == 'd' * 40


def test_installed_identity_declares_source_without_attestation(tmp_path, monkeypatch):
    import kp_agent_tooling._impl.server_identity as module
    (tmp_path/'build_identity.json').write_text(json.dumps({'source_revision':'a'*40}))
    monkeypatch.setattr(module, '__file__', str(tmp_path/'server_identity.py'))
    monkeypatch.setattr(module, '_git', lambda *a: (_ for _ in ()).throw(OSError()))
    result=module.identity({'repos':{}},'b'*64)
    assert result['server_build_revision']=='a'*40
    assert result['server_build_origin']=='package-build-declaration'
    assert result['executable_artifact_integrity']=='not-attested'
