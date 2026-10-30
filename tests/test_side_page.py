import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = sorted((Path(__file__).parent / "side_page").glob("*.test.js"))


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is not installed")
def test_side_page_node_suite() -> None:
    """The side page is plain browser JavaScript; its logic runs under node:test."""
    assert TESTS
    result = subprocess.run(
        ("node", "--test", *map(str, TESTS)), capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
