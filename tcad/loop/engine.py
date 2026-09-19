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
from typing import Any, Callable

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


#: How many *consecutive* steps may return no tool call before the turn is
#: declared stuck.
#:
#: This is a stall detector, not a work limit, and the distinction is the whole
#: point: it bounds no total quantity of steps, tokens or time. Any tool call
#: resets it, so a turn that is getting anywhere — however slowly — is never cut
#: off by it. It exists only because a step that calls no tool cannot change the
#: world, so repeating it is pure waste; without it, and with the shipped
#: no-ceiling budget, an endlessly narrating model would loop forever.
MAX_IDLE_STEPS = 3


class LoopEngine:
    """Runs one Turn at a time.

    ``observer`` is an optional ``(kind, data) -> None`` sink. It exists so a
    front end can show *what the model actually did* — its text, which tool it
    called, what came back. That information is not recoverable from the hook
    stream (hooks are a safety boundary and carry only what a policy needs) nor
    from the final ``TurnResult`` (which is a summary).

    Two rules keep it harmless:

    * it is **the observer's problem, never the turn's** — any exception it
      raises is swallowed;
    * it is **read-only by construction** — it receives a fresh dict that the
      engine has already finished using, so it cannot mutate the conversation.
    """

    def __init__(
        self,
        services: Services,
        registry: Any,  # ToolRegistry
        budget_limits: BudgetLimits,
        config: LoopConfig | None = None,
        observer: Callable[[str, dict], None] | None = None,
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
        self._idle_steps = 0
        self._observer = observer

    # ─── observation ──────────────────────────────────────────────────────

    def _observe(self, kind: str, data: dict) -> None:
        if self._observer is None:
            return
        try:
            self._observer(kind, data)
        except Exception:  # noqa: BLE001 — a broken viewer must not break a turn
            pass

    # ─── public entry point ───────────────────────────────────────────────

    async def run_turn(self, thread: Thread, user_msg: UserMessage) -> TurnResult:
        turn = self._new_turn(thread, user_msg)
        messages = self._init_messages(user_msg, turn)
        privileged = bool(user_msg.privileged_requested) and self.config.allow_privileged
        allowed = self._allowed_tiers(turn.kind, privileged)

        # Per-turn loop state. The engine may be reused across turns (the CLI
        # REPL does), and a stall counter carried over would fail the next turn
        # for no reason.
        self._idle_steps = 0
        self._compile_failures = 0

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

        self._observe(
            "turn_end",
            {
                "turn_id": turn.turn_id,
                "state": result.state.value,
                "steps": self.budget.steps,
                "tokens_in": self.budget.tokens_in,
                "tokens_out": self.budget.tokens_out,
                "error": result.error,
                "gate": (
                    {
                        "passed": result.gate_report.passed,
                        "ir_version": result.gate_report.ir_version,
                        "blocking_failures": list(result.gate_report.blocking_failures),
                        "advisory_findings": list(result.gate_report.advisory_findings),
                        "skipped_checks": list(result.gate_report.skipped_checks),
                    }
                    if result.gate_report is not None
                    else None
                ),
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

        if reply.reasoning_content:
            # Echo the model's thinking back verbatim when it produced any.
            #
            # DeepSeek's thinking mode rejects the next request with a 400 —
            # "The `reasoning_content` in the thinking mode must be passed back
            # to the API" — when an assistant message carrying tool calls is
            # replayed without it. A turn is a multi-step loop, so every step
            # after the first replays the previous assistant message: omitting
            # this breaks *any* thinking model at step 2, which is precisely
            # where it was first observed in the wild. Providers that emit no
            # reasoning never produce the key, and providers that do not use it
            # ignore the field.
            assistant["reasoning_content"] = reply.reasoning_content

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

        self._observe(
            "model",
            {
                "step": self.budget.steps,
                "text": reply.text or "",
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "args": tc.args}
                    for tc in reply.tool_calls
                ],
                "usage": {
                    "prompt_tokens": reply.usage.prompt_tokens,
                    "completion_tokens": reply.usage.completion_tokens,
                },
                "finish_reason": reply.finish_reason,
            },
        )

        # A model that returns text but no tool call is NOT a termination.
        #
        # But it also cannot make progress on its own: the only way forward is
        # another request, and the tool set will not have changed. This used to
        # be harmless because `max_steps_per_turn` eventually cut the turn off —
        # with no work ceiling (the shipped default) an endlessly narrating model
        # would loop forever, spending tokens and never ending.
        #
        # So the loop needs its own stall detector. This is deliberately NOT a
        # budget: it does not bound how much work a turn may do, it bounds how
        # many *consecutive no-ops* are treated as "still thinking". A model that
        # is doing anything at all resets it. A few steps are tolerated because
        # each reply is appended to the conversation, so the model does get to
        # react to its own narration before we call it stuck.
        if not reply.tool_calls:
            self._idle_steps += 1
            if self._idle_steps > MAX_IDLE_STEPS:
                turn.state = TurnState.FAILED
                turn.error = (
                    f"no progress: {self._idle_steps} consecutive steps without a tool call. "
                    "The model stopped acting before the Gate passed, so nothing was verified "
                    "and nothing changed."
                )
                return StepYield()
            return StepYield()
        self._idle_steps = 0

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
                        # Record the request so the turn can actually be RESUMED.
                        # Without this the harness could suspend on a hook's ASK
                        # and had nothing to approve: the privileged gate looks up
                        # an approval that no component ever created, so the call
                        # could never be let through. The id is surfaced in the
                        # message so a client knows what to POST /approvals.
                        approval_id = self._request_approval(tc.name, tc.args)
                        extra = (
                            f" approval_id={approval_id}." if approval_id else
                            " (no approval store configured — this turn cannot be resumed)."
                        )
                        outcome = ToolOutcome(
                            result=ToolResult(
                                ok=False,
                                content=(
                                    f"Approval required for {tc.name}: {hook_res.reason}."
                                    f" Turn suspended;{extra}"
                                ),
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

            self._observe(
                "tool",
                {
                    "step": self.budget.steps,
                    "name": tc.name,
                    "ok": outcome.result.ok,
                    "content": (outcome.result.content or "")[:6000],
                    "error": (
                        {
                            "kind": getattr(outcome.result.error.kind, "value", str(outcome.result.error.kind)),
                            "message": outcome.result.error.message,
                            "feature_id": outcome.result.error.feature_id,
                        }
                        if outcome.result.error is not None
                        else None
                    ),
                    "images": [i.model_dump(mode="json") for i in (outcome.result.images or [])],
                    "gate": (
                        {
                            "passed": outcome.gate_report.passed,
                            "ir_version": outcome.gate_report.ir_version,
                            "blocking_failures": list(outcome.gate_report.blocking_failures),
                            "advisory_findings": list(outcome.gate_report.advisory_findings),
                            "skipped_checks": list(outcome.gate_report.skipped_checks),
                        }
                        if outcome.gate_report is not None
                        else None
                    ),
                },
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
                        if not self.budget.allow_more_compile_retries(self._compile_failures):
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

    def _request_approval(self, tool_name: str, args: dict) -> str | None:
        """Create an approval record for a suspended call, if a store is wired.

        Returns the record id, or None when no approval store is available (the
        turn still suspends — it just cannot be resumed, and says so).
        """
        store = getattr(self.services, "approvals", None)
        if store is None:
            return None
        try:
            import hashlib
            import json as _json

            digest = hashlib.sha256(
                _json.dumps(args, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:16]
            return store.request(tool_name, args_hash=digest).id
        except Exception:  # noqa: BLE001 — never let bookkeeping break the loop
            return None

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

    # ─── production wiring ────────────────────────────────────────────────

    @staticmethod
    def build_default_services(config: Any = None) -> Services:
        """Forwarder to :func:`tcad.core.wiring.build_services`.

        The real wiring cannot live here: it needs the FreeCAD binary path, the
        verify thresholds, the hook policy and the storage layout, none of which
        ``LoopConfig`` carries. It used to be a stub in this file with invented
        import paths (``tcad.worker.handle``, ``ContextService``, ``Renderer``)
        that raised ``ImportError`` the moment anything called it — a placeholder
        that only survived because nothing did.

        Pass a full :class:`tcad.config.schema.Config`.
        """
        if config is None:
            raise TypeError(
                "build_default_services() requires a full Config — call "
                "tcad.core.wiring.build_services(config) instead"
            )
        from tcad.config.schema import Config as _Config

        if not isinstance(config, _Config):
            raise TypeError(
                f"expected tcad.config.schema.Config, got {type(config).__name__}; "
                "LoopConfig alone cannot express the FreeCAD path, verify thresholds "
                "or hook policy — call tcad.core.wiring.build_services()"
            )
        from tcad.core.wiring import build_services

        return build_services(config)


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
