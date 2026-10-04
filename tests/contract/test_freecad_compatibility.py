"""Self-test reports optional gaps honestly and still fails on core API losses."""

import json
import os
from pathlib import Path
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[2]
FREECAD_CMD = os.environ.get(
    "TCAD_FREECAD_CMD", str(ROOT / "free-cad/FreeCAD/build/debug/bin/FreeCADCmd"))
pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(not Path(FREECAD_CMD).exists(), reason="FreeCADCmd build not found"),
]


@pytest.fixture(scope="module")
def selftests(tmp_path_factory):
    script = tmp_path_factory.mktemp("compat_selftest") / "probe.py"
    script.write_text(textwrap.dedent('''
        import json
        from tcad.worker.compiler import FEATURE_TYPE_MAP
        from tcad.worker.selftest import api_selftest
        actual = api_selftest()
        saved = FEATURE_TYPE_MAP["pad"]
        try:
            FEATURE_TYPE_MAP["pad"] = "PartDesign::DeliberatelyMissingCoreType"
            missing_core = api_selftest()
        finally:
            FEATURE_TYPE_MAP["pad"] = saved
        print("###SELFTEST###" + json.dumps({"actual": actual, "missing_core": missing_core}))
    '''), encoding="utf-8")
    proc = subprocess.run(
        [FREECAD_CMD, "--console", "-P", str(ROOT), str(script)],
        capture_output=True, text=True, timeout=180, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-2000:]
    lines = [line for line in proc.stdout.splitlines() if line.startswith("###SELFTEST###")]
    assert len(lines) == 1, proc.stdout[-2000:]
    return json.loads(lines[0].removeprefix("###SELFTEST###"))


def test_optional_absence_is_reported_without_marking_the_probe_passed(selftests):
    result = selftests["actual"]
    assert result["ok"] is True and result["missing"] == []
    assert result["errors"] == []
    assert result["fully_supported"] is (not result["optional_missing"])
    assert all(check["ok"] for check in result["checks"] if check["required"])
    unavailable = [check for check in result["checks"] if not check["ok"]]
    assert result["optional_missing"] == [check["name"] for check in unavailable]
    assert all(check["name"] == "feature:circular_pattern" and check["detail"]
               and not check["required"] for check in unavailable)
    if result["freecad_version"].startswith("1.0."):
        assert result["fully_supported"] is False
        assert result["optional_missing"] == ["feature:circular_pattern"]


def test_a_missing_required_type_still_fails_with_specific_error(selftests):
    result = selftests["missing_core"]
    assert result["ok"] is False and result["fully_supported"] is False
    assert result["missing"] == ["feature:pad"]
    assert "feature:pad" not in result["optional_missing"]
    assert result["errors"][0]["kind"] == "compile"
    assert "feature:pad" in result["errors"][0]["message"]
    assert "DeliberatelyMissingCoreType" in result["errors"][0]["message"]
