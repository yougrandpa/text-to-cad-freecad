"""Tests for the structural tool-tier registry (design §4.2)."""

from __future__ import annotations

from types import SimpleNamespace

from tcad.core.types import ToolResult, ToolTier, TurnKind
from tcad.tools.base import ToolOutcome, ToolRegistry, build_default_registry, execute_tool, _ALLOWED_TIERS


def _fake_services():
    return SimpleNamespace()  # the registry only needs services to close over


def test_inspect_exposes_only_read_tools():
    reg = build_default_registry(_fake_services())
    names = set(reg.names_for(TurnKind.INSPECT))
    read = {"ir_get", "ir_digest", "ir_list_features", "geo_view", "geo_check_motion", "assembly_simulate", "assembly_solve", "assembly_export", "geo_measure", "asset_export", "asset_import"}
    assert names == read | {'ir_help'}
    assert "ir_patch" not in names
    assert "ir_commit" not in names
    # every exposed tool really is read tier
    for spec in reg.tools_for(TurnKind.INSPECT):
        assert spec.tier == ToolTier.READ


def test_create_and_modify_expose_read_and_write():
    reg = build_default_registry(_fake_services())
    for kind in (TurnKind.CREATE, TurnKind.MODIFY):
        names = set(reg.names_for(kind))
        assert "ir_patch" in names and "ir_commit" in names
        assert "ir_get" in names  # read still present


def test_privileged_absent_by_default_but_present_when_enabled():
    reg = build_default_registry(_fake_services())  # default: not enabled
    assert reg.get("raw_python") is None
    assert "raw_python" not in set(reg.names_for(TurnKind.CREATE))

    reg2 = build_default_registry(_fake_services(), enable_privileged=True)
    assert reg2.get("raw_python") is not None
    assert "raw_python" not in set(reg2.names_for(TurnKind.CREATE, include_privileged=False))
    assert "raw_python" in set(reg2.names_for(TurnKind.CREATE, include_privileged=True))
    assert reg2.get("raw_python").tier == ToolTier.PRIVILEGED


def test_as_openai_tools_shape():
    reg = build_default_registry(_fake_services())
    tools = reg.as_openai_tools(TurnKind.CREATE)
    assert all(t["type"] == "function" and "function" in t for t in tools)
    names = {t["function"]["name"] for t in tools}
    assert "ir_patch" in names and "ir_commit" in names


def test_allowed_tiers_map_is_structural():
    assert _ALLOWED_TIERS[TurnKind.INSPECT] == {ToolTier.READ}
    assert _ALLOWED_TIERS[TurnKind.CREATE] == {ToolTier.READ, ToolTier.WRITE}


async def test_execute_tool_normalizes_tool_result_to_outcome():
    async def handler(args, ctx):
        return ToolResult(ok=True, content="hi")

    spec = reg_spec("echo", ToolTier.READ, handler)
    outcome = await execute_tool(spec, {}, _ctx(), allowed_tiers={ToolTier.READ})
    assert isinstance(outcome, ToolOutcome)
    assert outcome.result.ok and outcome.result.content == "hi"
    assert outcome.gate_report is None


async def test_execute_tool_refuses_tool_outside_kind_subset():
    async def handler(args, ctx):
        return ToolResult(ok=True, content="should not run")

    spec = reg_spec("writer", ToolTier.WRITE, handler)
    # INSPECT only allows READ; a WRITE tool must be refused (not just discouraged).
    outcome = await execute_tool(spec, {}, _ctx(), allowed_tiers={ToolTier.READ})
    assert isinstance(outcome, ToolOutcome)
    assert outcome.result.ok is False
    assert outcome.result.error is not None
    assert outcome.result.error.kind.value == "denied"


async def test_execute_tool_converts_exception_to_tool_error():
    async def handler(args, ctx):
        raise ValueError("boom")

    spec = reg_spec("boom", ToolTier.READ, handler)
    outcome = await execute_tool(spec, {}, _ctx(), allowed_tiers={ToolTier.READ})
    assert outcome.result.ok is False
    assert outcome.result.error.kind.value == "runtime"
    assert "boom" in outcome.result.error.message


def reg_spec(name, tier, handler):
    from tcad.core.types import ToolSpec

    return ToolSpec(name=name, tier=tier, description="x", params_schema={}, handler=handler)


def _ctx():
    from tcad.core.types import ToolContext

    return ToolContext(thread_id="t", turn_id="t", model_id="m")


def test_wire_schemas_have_explicit_required_without_mutating_source():
    registry = build_default_registry(_fake_services())
    for spec in registry.tools_for(TurnKind.CREATE):
        before = dict(spec.params_schema)
        wire = spec.as_openai_tool()['function']['parameters']
        assert wire['required'] == before.get('required', [])
        assert spec.params_schema == before
        assert wire is not spec.params_schema
    assert registry.get('ir_patch').as_openai_tool()['function']['parameters']['required'] == ['base_version', 'ops']
