"""Fixtures for the T5 refresh-efficiency contract tests; the rig lives in t5_rig.py."""
import pytest

from t5_rig import Rig


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)
