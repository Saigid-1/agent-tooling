"""The `repo` fixture from OPS tests/test_operation_navigation.py lines 7-14 at the extraction commit.

Copied verbatim (S5, Coordinator ruling on AMBIGUITY A2). It needs only git and
tmp_path. It lives here because the OPS module that defined it imports the
OPS-only kp_ops.operation_navigation at module level.
"""
import subprocess
import pytest

@pytest.fixture
def repo(tmp_path):
    def git(*args): return subprocess.check_output(['git', *args], cwd=tmp_path).decode().strip()
    git('init','-q');git('config','user.email','fixture@example.test');git('config','user.name','Fixture')
    (tmp_path/'client.js').write_text('function inspect() {\n return remote()\n}\n')
    (tmp_path/'server.py').write_text('def inspect():\n return custody()\n')
    git('add','.');git('commit','-qm','baseline')
    return tmp_path,git('rev-parse','HEAD')
