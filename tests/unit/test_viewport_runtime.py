"""Execute the shipped browser camera and async loader code, without npm."""
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_interactive_viewport_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    tests = sorted((ROOT / "tests" / "frontend").glob("*.test.mjs"))
    result = subprocess.run([node, "--test", *map(str, tests)], cwd=ROOT,
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_viewport_module_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    result = subprocess.run([node, "--input-type=module", "--check"],
                            input=(ROOT / "tcad/server/ui/viewport.js").read_text(),
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
