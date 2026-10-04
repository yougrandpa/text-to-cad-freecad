"""Tool registry + the collaborator Protocol bundle.

This module is the *structural* tier filter (design §4.2). It deliberately
imports **no teammate module** at top level — every collaborator (store,
worker, gate, renderer, hooks, context, llm) is described only as a ``Protocol``
so the engine/tools are testable with fakes while four other people build the
concrete implementations in parallel.

The model never reaches FreeCAD directly: the only write path is ``ir_patch`` /
``ir_commit`` (which mutate the IR and trigger the compile pipeline). Read tools
talk to a worker that returns *copies* of geometry, never the live document.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from tcad.core.types import (
    GateReport,
    HookEvent,
    HookResult,
    ImageRef,
    IrDocument,
    IrEvent,
    IrPatch,
    Mesh,
    ToolContext,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolSpec,
    ToolTier,
    TurnKind,
)
from tcad.tools.schema_check import validate_tool_args
from tcad.core.access import AccessMode, READ_ONLY_TOOLS

# ─── collaborator Protocols ────────────────────────────────────────────────
# These are the exact method signatures the engine/tools depend on. Teammates
# implement them concretely inside LoopEngine.build_default_services() with lazy
# imports, so an import error there can never break the engine's unit tests.


@runtime_checkable
class Store(Protocol):
    """Versioned IR + event store (design §4.4)."""

    def load(self, model_id: str, version: int | None = None) -> IrDocument: ...
    def current_version(self, model_id: str) -> int: ...
    def apply_patch(self, model_id: str, patch: IrPatch) -> tuple[IrDocument, IrEvent]: ...
    def validate_patch(self, ir: IrDocument, patch: IrPatch) -> list[ToolError]: ...
    def validate_document(self, ir: IrDocument) -> list[ToolError]: ...
    def persist_digest(self, model_id: str, ir_version: int, digest: Any) -> None: ...


@runtime_checkable
class WorkerHandle(Protocol):
    """FreeCAD worker RPC handle (design §6.1). Returns a plain dict envelope:

    ``{"ok": True, "result": {...}}`` or
    ``{"ok": False, "error": {"kind": ..., "message": ..., "feature_id": ...}}``.
    """

    def request(self, method: str, params: dict, *, timeout_s: float = 30.0) -> dict: ...


@runtime_checkable
class Gate(Protocol):
    """Geometry/requirement gate (design §4.6). Builds CheckContext *from disk*."""

    def evaluate(self, model_id: str, ir_version: int) -> GateReport: ...


@runtime_checkable
class Renderer(Protocol):
    """Headless software rasteriser (design §6.5)."""

    def render(
        self,
        mesh: Mesh,
        *,
        out_dir: str,
        views: list[str],
        style: str,
        width: int,
        height: int,
    ) -> list[ImageRef]: ...


@runtime_checkable
class HookDispatcher(Protocol):
    """Lifecycle hook dispatcher (design §4.5). Fail-closed."""

    def dispatch(self, event: HookEvent, payload: dict) -> HookResult: ...


@runtime_checkable
class ContextService(Protocol):
    """Geometry-digest / context assembler (design §4.3)."""

    def digest(self, model_id: str, ir_version: int) -> Any: ...


@runtime_checkable
class LlmClient(Protocol):
    """Async OpenAI-compatible chat client (design §3, §6)."""

    async def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
    ) -> "Any": ...


@runtime_checkable
class Services(Protocol):
    """The injected collaborator bundle.

    The engine takes a ``Services`` by injection; it never constructs concrete
    collaborators itself (except inside ``build_default_services``).
    """

    store: Store
    worker: WorkerHandle
    gate: Gate
    renderer: Renderer
    hooks: HookDispatcher
    context: ContextService
    llm: LlmClient


# ─── tool execution result wrapper ─────────────────────────────────────────
# ToolResult is the *model-facing* payload (frozen contract, cannot be extended).
# To carry engine-internal metadata — specifically the GateReport produced by
# ir_commit — without modifying the frozen ToolResult, the engine/handler speak
# in ToolOutcome. pydantic v2 forbids attaching unknown attributes to a model
# instance, so a plain dataclass is the honest channel.


@dataclass
class ToolOutcome:
    result: ToolResult
    gate_report: GateReport | None = None


# ─── structural tier filtering ─────────────────────────────────────────────

# inspect  -> read only (write tools physically absent, not just discouraged)
# create   -> read + write
# modify   -> read + write
# privileged is appended only when explicitly requested AND the policy allows it.
_ALLOWED_TIERS: dict[TurnKind, set[ToolTier]] = {
    TurnKind.INSPECT: {ToolTier.READ},
    TurnKind.CREATE: {ToolTier.READ, ToolTier.WRITE},
    TurnKind.MODIFY: {ToolTier.READ, ToolTier.WRITE},
}


class ToolRegistry:
    """Holds tool specs and answers structural subset questions by TurnKind."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register_tool(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def tools_for(self, kind: TurnKind, *, include_privileged: bool = False) -> list[ToolSpec]:
        tiers = set(_ALLOWED_TIERS[kind])
        if include_privileged:
            tiers.add(ToolTier.PRIVILEGED)
        return [s for s in self._tools.values() if s.tier in tiers]

    def as_openai_tools(self, kind: TurnKind, *, include_privileged: bool = False) -> list[dict]:
        return [s.as_openai_tool() for s in self.tools_for(kind, include_privileged=include_privileged)]

    def names_for(self, kind: TurnKind, *, include_privileged: bool = False) -> list[str]:
        return [s.name for s in self.tools_for(kind, include_privileged=include_privileged)]


async def execute_tool(
    spec: ToolSpec,
    args: dict[str, Any],
    ctx: ToolContext,
    *,
    allowed_tiers: set[ToolTier] | None = None,
) -> ToolOutcome:
    """Run one tool spec with the safety envelope.

    * refuses a tool whose tier is not in ``allowed_tiers`` (defence-in-depth on
      top of the registry subset);
    * enforces ``spec.timeout_s`` via asyncio.wait_for;
    * converts any raised exception into a structured ``ToolError``;
    * returns a :class:`ToolOutcome` (normalising a bare ``ToolResult``).
    """
    if ctx.access_mode == AccessMode.READ_ONLY and spec.name not in READ_ONLY_TOOLS:
        return ToolOutcome(result=ToolResult(ok=False, error=ToolError(
            kind=ToolErrorKind.DENIED, message="仅可读取：此工具不能执行")))
    if ctx.access_mode == AccessMode.AUTO and spec.tier == ToolTier.PRIVILEGED:
        return ToolOutcome(result=ToolResult(ok=False, error=ToolError(
            kind=ToolErrorKind.DENIED, message="自动审批不允许 Python 执行，请由用户选择完全访问")))
    if allowed_tiers is not None and spec.tier not in allowed_tiers:
        return ToolOutcome(
            result=ToolResult(
                ok=False,
                error=ToolError(
                    kind=ToolErrorKind.DENIED,
                    message=f"tool {spec.name!r} (tier={spec.tier.value}) is not permitted in this turn kind",
                ),
            )
        )
    # The declared argument schema is a contract, not documentation (task §5-A).
    # Checked *before* the handler so a malformed call cannot half-execute: a
    # string where an array belongs used to reach the handler, where a truthy
    # ``views`` was forwarded as-is.
    problems = validate_tool_args(args, spec)
    if problems:
        return ToolOutcome(
            result=ToolResult(
                ok=False,
                error=ToolError(
                    kind=ToolErrorKind.SCHEMA,
                    message=(
                        f"{spec.name}: arguments do not match the tool schema: "
                        + "; ".join(problems[:4])
                        + (f" (+{len(problems) - 4} more)" if len(problems) > 4 else "")
                    ),
                    hint=(
                        "Nothing was applied. Call ir_get to obtain the current version, then "
                        "re-send ir_patch with base_version (integer) and ops (array of actual "
                        "patch operations). Do not send an empty object or invent operations."
                        if spec.name == "ir_patch" else "re-send the call with the declared argument types"
                    ),
                ),
            )
        )
    try:
        raw = await asyncio.wait_for(spec.handler(args, ctx), timeout=spec.timeout_s)
    except asyncio.TimeoutError:
        return ToolOutcome(
            result=ToolResult(
                ok=False,
                error=ToolError(
                    kind=ToolErrorKind.TIMEOUT,
                    message=f"{spec.name} exceeded timeout {spec.timeout_s}s",
                ),
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # handler bug or worker error -> never crash the turn
        kind = getattr(e, "error_kind", ToolErrorKind.RUNTIME)
        if not isinstance(kind, ToolErrorKind):
            kind = ToolErrorKind.RUNTIME
        return ToolOutcome(
            result=ToolResult(
                ok=False,
                error=ToolError(kind=kind, message=f"{type(e).__name__}: {e}"),
            )
        )
    if isinstance(raw, ToolOutcome):
        return raw
    if isinstance(raw, ToolResult):
        return ToolOutcome(result=raw)
    return ToolOutcome(
        result=ToolResult(
            ok=False,
            error=ToolError(
                kind=ToolErrorKind.RUNTIME,
                message=f"handler {spec.name!r} returned {type(raw).__name__}, expected ToolResult",
            ),
        )
    )


# ─── default tool wiring ───────────────────────────────────────────────────
# These imports are local to THIS module (owned by loopwright), not to teammates.
from tcad.tools.geo_tools import build_geo_tools  # noqa: E402
from tcad.tools.ir_tools import build_ir_tools  # noqa: E402
from tcad.tools.privileged import build_privileged_tools  # noqa: E402


def build_default_registry(services: Services, *, enable_privileged: bool = False) -> ToolRegistry:
    """Register the standard tool set.

    ``raw_python`` (privileged) is registered **only** when ``enable_privileged``
    is True — never by default (design §4.2 / §4.5).
    """
    reg = ToolRegistry()
    for spec in build_ir_tools(services).values():
        reg.register_tool(spec)
    for spec in build_geo_tools(services).values():
        reg.register_tool(spec)
    if enable_privileged:
        for spec in build_privileged_tools(services).values():
            reg.register_tool(spec)
    return reg
