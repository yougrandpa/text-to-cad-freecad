"""The public black-box driver cannot hide a failed geometry verdict."""

from types import SimpleNamespace

import pytest

from tcad.core.types import GateReport, HookDecision, ToolResult, TurnKind
from tcad.config.schema import Config
from tcad.ir.schema import IrDocument
from tcad.tools.base import ToolOutcome
from tools import agent_driver


@pytest.mark.parametrize("passed,exit_code", [(True, 0), (False, 1)])
async def test_successful_tool_execution_with_failed_gate_exits_nonzero(monkeypatch, passed, exit_code):
    svc = SimpleNamespace(config=Config(),
        store=SimpleNamespace(data_dir="unused", load=lambda _: IrDocument(model_id="part")),
        worker=None, hooks=SimpleNamespace(dispatch=lambda *_: SimpleNamespace(decision=HookDecision.ALLOW)))
    async def execute(*args, **kwargs):
        return ToolOutcome(ToolResult(ok=True, content="build finished"),
                           GateReport(model_id="part", ir_version=0, passed=passed))
    monkeypatch.setattr(agent_driver, "execute_tool", execute)
    assert await agent_driver.run_calls(svc, "part", [{"name": "ir_commit", "args": {}}],
                                       kind=TurnKind.CREATE) == exit_code


def test_tool_discovery_honors_kind_and_explains_nested_schema(monkeypatch, capsys):
    monkeypatch.setattr(agent_driver, "build_services", lambda *_args, **_kwargs: SimpleNamespace())
    assert agent_driver.main(["--list-tools", "--kind", "inspect"]) == 0
    output = capsys.readouterr().out
    assert "inspect:" in output and "create:" not in output
    assert "ir_commit(" not in output
    assert "--full --only TOOL" in output
    assert agent_driver.main(["--list-tools", "--all-kinds"]) == 0
    output = capsys.readouterr().out
    assert all(kind + ":" in output for kind in ("create", "modify", "inspect"))
