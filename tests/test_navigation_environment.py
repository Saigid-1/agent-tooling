import pytest
from kp_agent_tooling._impl.navigation_environment import snapshot, changes


def test_ignored_config_contents_and_missing_manifest_identity(tmp_path):
    (tmp_path/'.serena').mkdir()
    file = tmp_path/'.serena/project.yml'
    file.write_text('read_only: true\n')
    before = snapshot(tmp_path)
    file.write_text('read_only: false\n')
    delta = changes(before, snapshot(tmp_path))
    assert [row['path'] for row in delta] == ['.serena/project.yml']
    assert before['requirements.lock'] == {'status':'absent'}


def test_external_config_symlink_refused(tmp_path):
    (tmp_path/'pyproject.toml').symlink_to('/etc/hosts')
    with pytest.raises(ValueError, match='symlink'):
        snapshot(tmp_path)


def test_config_size_bound(tmp_path):
    with (tmp_path/'requirements.lock').open('wb') as file:
        file.truncate(8_000_001)
    with pytest.raises(ValueError, match='budget'):
        snapshot(tmp_path)
