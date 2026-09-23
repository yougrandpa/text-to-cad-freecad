"""The system prompt is a claim about the tool set, so it is checked like one.

A model reads the system prompt before it sees any tool schema, and it acts on
what it reads: it is told which tools exist and which of them are safe to call
freely. Both statements are checkable against the registry — and neither was
checked, so a renamed tool or a tool promoted from READ to WRITE would leave the
prompt describing a system that no longer exists ("call geo_measure" pointing at
nothing, or "read tools never change the design" said about a tool that writes).

These tests pin the prompt to the registry and to the report type it quotes, so
the runtime prompt and the working system cannot drift apart silently.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from tcad.core.types import GateReport
from tcad.loop.engine import LoopConfig
from tcad.tools.base import ToolTier, build_default_registry

#: Names the prompt mentions without spelling every member out (``geo_*``).
_WILDCARD = "geo_*"

#: Prefixes that make a word look like a tool name to this test.
_PREFIXES = ("ir_", "geo_", "asset_", "raw_")

#: Words that start with a tool prefix but are not tools. Kept explicit: adding
#: vocabulary to the prompt should be a conscious act, not a silent blind spot
#: for the check above — a renamed tool must never become invisible here. (An
#: earlier version of this test only matched ``[a-z_]+``, so ``ir_get_v2`` slipped
#: straight through: the mutation check caught it.)
_NOT_TOOLS = {"ir_version"}


def _registry():
    """The production tool set. The builders only close over `services`, so a
    stub bundle is enough — nothing here calls a handler."""
    svc = SimpleNamespace(store=None, worker=None, gate=None, config=None)
    return build_default_registry(svc)


def _prompt() -> str:
    return LoopConfig().system_prompt


def _named_tools(text: str) -> set[str]:
    """Tool names written literally in `text`, e.g. ``ir_patch``.

    ``geo_*`` is deliberately excluded: it is a family, handled separately.
    """
    tokens = re.findall(r"\b[a-z][a-z0-9_]*\b", text)
    return {
        t for t in tokens
        if t.startswith(_PREFIXES) and t not in _NOT_TOOLS and not t.endswith("_")
    }


def test_every_tool_the_prompt_names_is_registered():
    """A rename must not leave the prompt telling the model to call a tool that
    does not exist — that is a turn spent on an unknown-tool error."""
    reg = _registry()
    missing = sorted(n for n in _named_tools(_prompt()) if reg.get(n) is None)
    assert not missing, f"system prompt 提到的工具不存在：{missing}"


def test_the_read_only_claim_is_true_for_every_tool_it_covers():
    """The prompt tells the model it may call these freely because they never
    change the design. If one of them is a WRITE tool, that sentence is a trap."""
    reg = _registry()
    prompt = _prompt()
    assert "never change the design" in prompt, "提示词不再声明「只读」这条保证"

    read_list = re.search(r"Read tools \(([^)]*)\)", prompt)
    assert read_list, "找不到只读工具名单 —— 提示词结构变了"

    covered: set[str] = set()
    for token in (t.strip() for t in read_list.group(1).split("/")):
        if not token:
            continue
        if token.endswith("*"):
            prefix = token[:-1]
            matches = [s for s in reg._tools if s.startswith(prefix)]
            assert matches, f"提示词写了通配 {token}，但没有任何工具匹配"
            covered.update(matches)
        else:
            covered.add(token)

    assert covered, "只读名单是空的"
    not_read = sorted(
        n for n in covered
        if reg.get(n) is not None and reg.get(n).tier is not ToolTier.READ
    )
    assert not not_read, (
        f"提示词说这些工具「never change the design」，但它们会写：{not_read}"
    )


def test_the_wildcard_family_the_prompt_uses_is_covered_by_the_test():
    """Guards this file itself: if the prompt drops ``geo_*`` the assertion above
    would silently stop covering the geometry tools."""
    reg = _registry()
    geo = [n for n in reg._tools if n.startswith("geo_")]
    assert geo, "注册表里没有 geo_* 工具 —— 提示词的通配写法已失效"
    assert _WILDCARD in _prompt(), "提示词不再用 geo_* 通配写法，请更新本测试"


@pytest.mark.parametrize("word", ["passed"])
def test_the_success_rule_quotes_a_field_that_exists(word: str):
    """The prompt's rule — "never claim success unless ir_commit returns
    passed=true" — is only actionable if the report actually has that field."""
    assert word in _prompt()
    assert word in GateReport.model_fields, (
        "提示词让模型看 passed=true，但 GateReport 没有这个字段"
    )
