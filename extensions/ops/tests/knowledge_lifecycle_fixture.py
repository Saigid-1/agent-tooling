"""Public CLI tests must never touch the operator's real lifecycle state."""
import pytest
from kp_agent_tooling_ops._impl.service.knowledge_lifecycle import LifecycleLedger

@pytest.fixture(autouse=True)
def isolated_lifecycle(tmp_path, monkeypatch):
    ledger=LifecycleLedger.initialize(tmp_path/'cli-lifecycle.sqlite3')
    monkeypatch.setenv('KP_KNOWLEDGE_LIFECYCLE_DB',str(ledger.path))
    return ledger
