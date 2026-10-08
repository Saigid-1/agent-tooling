"""T4 host-adapter contract tests (tests/host/).

These tests reuse the T3 launch harness (tests/launch/t3_harness.py) and the S3
installer harness (tests/install/s3_harness.py) as plain modules; this puts
their directories on sys.path before any module here is imported.
"""
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent.parent
for _extra in (_TESTS / 'launch', _TESTS / 'install'):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))
