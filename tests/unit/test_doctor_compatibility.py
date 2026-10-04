"""Pre-flight output must distinguish core success from optional API gaps."""

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from tools import doctor
from tcad.core import worker_client


@pytest.mark.parametrize("optional_missing", [[], ["feature:circular_pattern"]])
def test_doctor_surfaces_optional_api_gap(monkeypatch, capsys, optional_missing):
    class Worker:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def close(self):
            pass

        def request_sync(self, method, *args, **kwargs):
            if method == "ping":
                return {"ok": True}
            return {"ok": True, "checks": [{"ok": True, "required": True},
                    {"ok": not optional_missing, "required": False}],
                    "optional_missing": optional_missing, "freecad_version": "1.0.0"}

    monkeypatch.setattr(doctor, "resolve_freecad_cmd", lambda cfg: (Path(sys.executable), "test"))
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout="FreeCAD 1.0.0", stderr=""))
    monkeypatch.setattr(worker_client, "WorkerHandle", Worker)
    assert doctor.check_worker(doctor.Report(), None, fast=False) is True
    output = capsys.readouterr().out
    assert "1 required FreeCAD API probes passed" in output
    if optional_missing:
        assert "optional FreeCAD APIs" in output
        assert "unavailable on FreeCAD 1.0.0: feature:circular_pattern" in output
    else:
        assert "optional FreeCAD APIs" not in output


def test_doctor_does_not_call_a_failed_version_probe_passed(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "resolve_freecad_cmd", lambda cfg: (Path(sys.executable), "test"))
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=2, stdout="", stderr="adapter failed"))
    assert doctor.check_worker(doctor.Report(), None, fast=False) is False
    output = capsys.readouterr().out
    assert "exit 2: adapter failed" in output
    assert not any(line.startswith("PASS") for line in output.splitlines())
