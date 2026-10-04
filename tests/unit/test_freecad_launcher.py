"""The explicit native-Python adapter and documented PATH override."""
from pathlib import Path

import pytest

from tcad.config.loader import resolve_paths
from tcad.config.schema import Config
from tools.freecad_python import parse_args


def test_adapter_parses_worker_arguments_without_a_shell():
    assert parse_args(["--console", "-P", "/a b", "/a b/worker.py", "--pass", "--worker-id=w0"]) == (
        ["/a b"], "/a b/worker.py", ["--worker-id=w0"]
    )


@pytest.mark.parametrize("args", [[], ["--unknown"], ["-P"], ["a.py", "b.py"]])
def test_adapter_rejects_unsupported_arguments(args):
    with pytest.raises(ValueError):
        parse_args(args)


def test_bare_freecad_command_resolves_from_path(tmp_path, monkeypatch):
    binary = tmp_path / "freecadcmd"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    cfg = Config()
    cfg.runtime.freecad_cmd = "freecadcmd"
    resolve_paths(cfg, root=tmp_path / "repo")
    assert cfg.runtime.freecad_cmd == str(binary.resolve())


def test_explicit_relative_command_stays_relative_to_repo(tmp_path):
    cfg = Config()
    cfg.runtime.freecad_cmd = "tools/freecad_python.py"
    resolve_paths(cfg, root=tmp_path)
    assert cfg.runtime.freecad_cmd == str(tmp_path / "tools/freecad_python.py")
