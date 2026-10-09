"""Run offline business tests with an in-memory vault; does NOT validate Windows DPAPI."""
import secrets
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import test_workspace

vault = {}

def fake_crypt(blob, decrypt=False):
    if decrypt:
        try:
            return vault[blob]
        except KeyError:
            raise ValueError('Unknown test-only vault token')
    key = secrets.token_bytes(32)
    vault[key] = blob
    return key

if __name__ == '__main__':
    print('OFFLINE BUSINESS TESTS: Windows DPAPI is replaced by an in-memory test vault.')
    with patch.object(test_workspace.s, 'crypt', side_effect=fake_crypt):
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
