"""Authenticate the only economic oracle when it is available on this host."""

from hashlib import sha256
from pathlib import Path

import pytest

ORACLE = Path("/Users/kevin/py000-nt/tests/fixtures/legacy_active/arbi_trader.py")
EXPECTED = "741e264c28bde67d59583b9fa49c626436e04066125e3015737add77e122d3bd"


def test_active_oracle_sha256() -> None:
    if not ORACLE.exists():
        pytest.skip("active oracle is not mounted")
    assert sha256(ORACLE.read_bytes()).hexdigest() == EXPECTED
