"""The execution loop — the E layer (design §4.1).

LoopEngine.run_turn drives one Turn:

    pre_turn hook
    -> [ pre_step hook -> budget check -> LLM -> tools (pre/post_tool_use)
         -> repeat ]  (until a green Gate or a hard terminal state)
    -> post_turn hook  (always, including on exception — try/finally)

Two non-negotiable invariants enforced here (design §4.1, §9, §10):

  1. The ONLY success condition is a green Gate. ``GateReport.passed == True``.
     A model that merely *says* it is done is not a termination (§4.1 decision 2);
     the model's only "claim completion" action is ``ir_commit``, after which the
     Gate independently judges. Budget survival (EXHAUSTED) is NOT success.

  2. The model never touches FreeCAD. Writes flow only through ir_patch /
     ir_commit (which mutate the IR and trigger the compile pipeline in the
     worker). Read tools talk to a worker that returns copies.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from tcad.core.types import (
    GateReport,
    HookDecision,
    HookEvent,
    Thread,
    ToolContext,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolTier,
    Turn,
    TurnKind,
    TurnState,
)
from tcad.loop.budget import Budget, BudgetLimits
from tcad.loop.strategies import LoopUntilDoneStrategy, make_strategy
from tcad.tools.base import Services, ToolOutcome, execute_tool
from tcad.worker.protocol import WORKER_METHODS  # noqa: F401  (ensures worker contract imported)


@dataclass
class StepYield:
    """What a single step produced for the strategy driver."""

    gate_report: GateReport | None = None


class UserMessage(BaseModel):
    """A user turn request. The run_turn payload.

    ``kind`` decides the tool subset (§4.2); ``privileged_requested`` opts into
    the privileged tier only if the static policy also allows it.
    """

    kind: TurnKind = TurnKind.CREATE
    text: str
    privileged_requested: bool = False


class TurnResult(BaseModel):
    """Final result of a Turn — always carries state, step count and token usage."""

    turn_id: str
    thread_id: str
    model_id: str
    state: TurnState
    gate_report: GateReport | None = None
    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None


class LoopConfig(BaseModel):
    """Engine configuration. Frozen-friendly; built from YAML in production."""

    default_strategy: str = "loop_until_done"
    llm_base_url: str = "http://127.0.0.1:8000/v1"
    llm_model: str = "qwen2.5-72b-instruct"
    llm_api_key: str = "EMPTY"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096
    llm_request_timeout_s: float = 120.0
    llm_max_retries: int = 2
    allow_privileged: bool = False
    visual_checkpoints: tuple[str, ...] = ("first_compile", "major_change", "final")
    artifact_exports: tuple[str, ...] = ("step", "stl")
    system_prompt: str = (
        "You are a parametric CAD agent. You design by mutating an intermediate "
        "representation (IR) via ir_patch, then ir_commit to compile and gate. "
        "Never claim success unless ir_commit returns passed=true. Read tools "
        "(ir_get/ir_digest/ir_list_features/geo_*) never change the design."
    )
    workdir: str = "."
    data_dir: str = "data"


class LoopEngine:
    def __init__(
        self,
        services: Services,
        registry: Any,  # ToolRegistry
        budget_limits: BudgetLimits,
        config: LoopConfig | None = None,
    ):
        self.services = services
        self.registry = registry
        self.budget_limits = budget_limits
        self.config = config or LoopConfig()
        self.budget = Budget(budget_limits)
        self.strategy = make_strategy(self.config.default_strategy)
        self._candidate_reports: list[GateReport] = []
        self._last_commit_passed = False
        self._compile_failures = 0

    # ─── public entry point ───────────────────────────────────────────────

    async def run_turn(self, thread: Thread, user_msg: UserMessage) -> TurnResult:
        turn = self._new_turn(thread, user_msg)
        messages = self._init_messages(user_msg, turn)
        privileged = bool(user_msg.privileged_requested) and self.config.allow_privileged
        allowed = self._allowed_tiers(turn.kind, privileged)

        # pre_turn hook (quota / content-safety pre-check).
        self.services.hooks.dispatch(
            HookEvent.PRE_TURN,
            {"thread_id": thread.thread_id, "turn_id": turn.turn_id, "model_id": thread.model_id},
        )

        try:
            try:
                result = await self.strategy.run(self, turn, messages, allowed)
            except Exception:
                # A strategy raising must degrade to M1, never kill the turn.
                if isinstance(self.strategy, LoopUntilDoneStrategy):
                    raise
                result = await LoopUntilDoneStrategy().run(self, turn, messages, allowed)
        except Exception as e:  # final safety net -> FAILED, still post_turn
            result = TurnResult(
                turn_id=turn.turn_id,
                thread_id=thread.thread_id,
                model_id=thread.model_id,
                state=TurnState.FAILED,
                steps=self.budget.steps,
                tokens_in=self.budget.tokens_in,
                tokens_out=self.budget.tokens_out,
                error=f"{type(e).__name__}: {e}",
            )
        finally:
            # post_turn ALWAYS fires — including when the LLM raised (design §4.5).
            self.services.hooks.dispatch(
                HookEvent.POST_TURN,
                {
                    "thread_id": thread.thread_id,
                    "turn_id": turn.turn_id,
                    "state": turn.state.value,
                    "steps": self.budget.steps,
                },
            )
        return result

    # ─── one step: pre_step -> LLM -> tools ───────────────────────────────

    async def _step(self, turn: Turn, messages: list[dict], allowed: set) -> StepYield:
        # pre_step hook (context placement / cost ceiling).
        self.services.hooks.dispatch(
            HookEvent.PRE_STEP,
            {"thread_id": turn.thread_id, "turn_id": turn.turn_id, "state": turn.state.value},
        )
        # budget: step + wall clock.
        self.budget.check_step()
        step_start = time.monotonic()

        reply = await self.services.llm.chat(
            messages=messages,
            tools=self.registry.as_openai_tools(
                turn.kind, include_privileged=ToolTier.PRIVILEGED in allowed
            ),
            temperature=self.config.llm_temperature,
        )
        self.budget.add_tokens(reply.usage.prompt_tokens, reply.usage.completion_tokens)

        # Build the assistant message (OpenAI function-calling shape).
        assistant: dict[str, Any] = {"role": "assistant", "content": reply.text or ""}
        gate_report: GateReport | None = None
        if reply.tool_calls:
            assistant["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                }
                for tc in reply.tool_calls
            ]
        messages.append(assistant)

        # A model that returns text but no tool call is NOT a termination.
        if not reply.tool_calls:
            return StepYield()

        ctx = self._make_tool_context(turn)
        for tc in reply.tool_calls:
            spec = self.registry.get(tc.name)
            if spec is None:
                outcome = ToolOutcome(
                    result=ToolResult(
                        ok=False,
                        error=ToolError(kind=ToolErrorKind.NOT_FOUND, message=f"unknown tool: {tc.name}"),
                    )
                )
            else:
                # engine-managed visual-checkpoint hint consumed by geo_view.
                services_any: Any = self.services
                services_any._visual_ok = (
                    True if turn.kind == TurnKind.INSPECT else self._last_commit_passed
                )
                if spec.tier == ToolTier.PRIVILEGED:
                    # privileged owns its own hook dispatch (triple gate).
                    outcome = await execute_tool(spec, tc.args, ctx, allowed_tiers=allowed)
                else:
                    hook_res = self.services.hooks.dispatch(
                        HookEvent.PRE_TOOL_USE,
                        {
                            "tool_name": tc.name,
                            "tier": spec.tier.value,
                            "args": tc.args,
                            "thread_id": turn.thread_id,
                            "turn_id": turn.turn_id,
                            "model_id": turn.model_id,
                        },
                    )
                    if hook_res.decision == HookDecision.DENY:
                        outcome = ToolOutcome(
                            result=ToolResult(
                                ok=False,
                                error=ToolError(
                                    kind=ToolErrorKind.DENIED,
                                    message=hook_res.reason or "denied by policy",
                                ),
                            )
                        )
                    elif hook_res.decision == HookDecision.ASK:
                        turn.state = TurnState.AWAITING_APPROVAL
                        outcome = ToolOutcome(
                            result=ToolResult(
                                ok=False,
                                content=f"Approval required for {tc.name}: {hook_res.reason}. Turn suspended.",
                            )
                        )
                    else:
                        args = hook_res.mutated_args if hook_res.mutated_args is not None else tc.args
                        outcome = await execute_tool(spec, args, ctx, allowed_tiers=allowed)

            # ── everything below must run for EVERY tool call, including one that
            # did not resolve to a registered tool. This block used to sit inside
            # the `else:` above, which meant an unknown tool produced no `tool`
            # message at all: the model got zero feedback, never learned the call
            # failed, and spun until the budget tripped. post_tool_use is
            # documented as firing for every tool — so it is dedented to the loop
            # body deliberately. Do not re-indent it into a branch.
            self.services.hooks.dispatch(
                HookEvent.POST_TOOL_USE,
                {
                    "tool_name": tc.name,
                    "ok": outcome.result.ok,
                    "thread_id": turn.thread_id,
                    "turn_id": turn.turn_id,
                },
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": _result_text(outcome.result),
                }
            )

            # ir_commit: the engine — not the tool — decides success.
            if tc.name == "ir_commit":
                gr = outcome.gate_report
                if gr is not None:
                    self._candidate_reports.append(gr)
                    gate_report = gr
                    if gr.passed:
                        self._last_commit_passed = True
                        self._compile_failures = 0
                else:
                    err = outcome.result.error
                    if err and err.kind in (ToolErrorKind.COMPILE, ToolErrorKind.SOLVER):
                        self._compile_failures += 1
                        if self._compile_failures > self.budget_limits.max_compile_retries:
                            turn.state = TurnState.FAILED
                            turn.error = "too many consecutive compile failures"
                            return StepYield()

        self.budget.check_step_timeout(step_start)
        turn.steps = self.budget.steps
        return StepYield(gate_report=gate_report)

    # ─── isolated critic (M3) ─────────────────────────────────────────────

    async def _critic_step(self, turn: Turn, messages: list[dict], report: GateReport) -> None:
        """Advisory-only critic. Appends a critique; never changes success."""
        critic_msgs = [
            {
                "role": "system",
                "content": (
                    "You are an independent critic. Given the IR digest and the gate "
                    "report, list any ENGINEERING or GEOMETRY risks the generator may "
                    "have missed (machinability, wall thickness, assembly reach, "
                    "proportions). Output advisory findings only — do not change the design."
                ),
            },
            {
                "role": "user",
                "content": f"Gate passed={report.passed}. Findings so far: "
                + (report.model_dump_json(indent=2) or ""),
            },
        ]
        try:
            reply = await self.services.llm.chat(messages=critic_msgs, temperature=self.config.llm_temperature)
            messages.append(
                {
                    "role": "assistant",
                    "content": f"[adversarial critic] {reply.text or '(no findings)'}",
                }
            )
        except Exception:
            # Critic is best-effort; never affects the turn outcome.
            pass

    # ─── helpers ──────────────────────────────────────────────────────────

    def _new_turn(self, thread: Thread, user_msg: UserMessage) -> Turn:
        try:
            base_version = self.services.store.current_version(thread.model_id)
        except Exception:
            base_version = 0
        turn = Turn(
            turn_id=f"t{int(time.monotonic()*1e6)}",
            thread_id=thread.thread_id,
            model_id=thread.model_id,
            kind=user_msg.kind,
            base_ir_version=base_version,
        )
        thread.turns.append(turn.turn_id)
        return turn

    def _init_messages(self, user_msg: UserMessage, turn: Turn) -> list[dict]:
        return [
            {"role": "system", "content": self.config.system_prompt},
            {"role": "user", "content": user_msg.text},
        ]

    def _allowed_tiers(self, kind: TurnKind, privileged: bool) -> set:
        from tcad.tools.base import _ALLOWED_TIERS

        tiers = set(_ALLOWED_TIERS[kind])
        if privileged:
            tiers.add(ToolTier.PRIVILEGED)
        return tiers

    def _make_tool_context(self, turn: Turn) -> ToolContext:
        return ToolContext(
            thread_id=turn.thread_id,
            turn_id=turn.turn_id,
            model_id=turn.model_id,
            workdir=self.config.workdir,
            data_dir=self.config.data_dir,
            worker=getattr(self.services, "worker", None),
        )

    def _finalize(self, turn: Turn, messages: list[dict], gate_report: GateReport | None) -> TurnResult:
        return TurnResult(
            turn_id=turn.turn_id,
            thread_id=turn.thread_id,
            model_id=turn.model_id,
            state=turn.state,
            gate_report=gate_report,
            steps=self.budget.steps,
            tokens_in=self.budget.tokens_in,
            tokens_out=self.budget.tokens_out,
            error=turn.error,
        )

    # ─── production wiring (lazy imports of collaborators) ────────────────

    @staticmethod
    def build_default_services(config: LoopConfig) -> Services:
        """Wire concrete collaborators.

        Every teammate module is imported *lazily inside this function* so an
        import error here cannot break the engine's unit tests (which inject
        fakes instead). Module paths below are the collaborators' agreed homes.
        """
        from tcad.context.digest import ContextService as _C  # type: ignore
        from tcad.hooks.dispatcher import HookDispatcher as _H  # type: ignore
        from tcad.render.raster import Renderer as _R  # type: ignore
        from tcad.store.ir_store import IrStore as _S  # type: ignore
        from tcad.verify.gate import Gate as _G  # type: ignore
        from tcad.worker.handle import WorkerHandle as _W  # type: ignore
        from tcad.llm.client import OpenAIClient as _L  # type: ignore

        class _Services:
            store = _S(data_dir=config.data_dir)
            worker = _W()
            gate = _G()
            renderer = _R()
            hooks = _H()
            context = _C()
            llm = _L(
                base_url=config.llm_base_url,
                api_key=config.llm_api_key,
                model=config.llm_model,
                request_timeout_s=config.llm_request_timeout_s,
                max_retries=config.llm_max_retries,
                temperature=config.llm_temperature,
                max_tokens=config.llm_max_tokens,
            )

        return _Services()  # type: ignore[return-value]


def _result_text(result: ToolResult) -> str:
    if result.error:
        e = result.error
        text = f"[tool error: {e.kind.value}] {e.message}"
        if e.feature_id:
            text += f" (feature_id={e.feature_id})"
        if e.hint:
            text += f" — hint: {e.hint}"
        return text
    return result.content or "(ok)"
