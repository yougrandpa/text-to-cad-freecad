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

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field

from tcad.core.types import (
    GateReport,
    HookDecision,
    HookEvent,
    HookResult,
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


#: The error text of a turn that was stopped from outside, by a person.
#:
#: Phrased as what did *not* happen ("nothing after the stop was verified")
#: rather than as a failure of the model: an interrupted turn is an outcome the
#: user asked for, and the one thing that must never be implied is that the
#: remaining work was checked. ``ABORTED`` is already reported by the UI as
#: explicitly-not-success; this string is the same statement in the payload.
STOPPED_BY_USER = (
    "stopped by the user before the Gate passed — nothing after the stop was verified"
)


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

#: How much of a suspended call's payload is stored for a human to read.
#: Bounded so an approval file cannot grow with a runaway argument, and long
#: enough for the code a `raw_python` request is asking to run.
_APPROVAL_SUMMARY_CHARS = 4000


def _unparsable_args_hint(tc: Any, finish_reason: str | None) -> str:
    """What the model should do about arguments that did not parse.

    Keyed to the *cause*, because a truncated call and a malformed call need
    different next actions and telling the model the wrong one costs another
    whole step.
    """
    if finish_reason == "length":
        return (
            f"Your output hit the per-step token ceiling and the JSON was cut off "
            f"after {tc.args_raw_len} characters, so this call was discarded. Send a "
            f"SMALLER patch and split the work across several {tc.name} calls — for "
            f"example the sketches first, then the features, then the requirements — "
            f"instead of repeating the same call at the same size."
        )
    return (
        f"The provider reported finish_reason={finish_reason!r} and sent "
        f"{tc.args_raw_len} characters of arguments that were not valid JSON. Re-send "
        f"the call with well-formed JSON arguments."
    )


def _is_mutating(tool_name: str, spec: Any) -> bool:
    """Whether a *successful* call to this tool changes what was verified.

    ``ir_commit`` is excluded: it is the act of grading, not a change to the
    graded IR. Everything else at write or privileged tier mutates the design,
    so a pass that predates it no longer describes the current state.
    """
    if tool_name == "ir_commit":
        return False
    tier = getattr(spec, "tier", None)
    return tier in (ToolTier.WRITE, ToolTier.PRIVILEGED)


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

    ``stop_requested`` is an optional zero-argument predicate answering "has
    someone asked this turn to stop?". It exists because a stop arrives from
    *outside* the loop — another HTTP request, another task — and the loop is
    the only thing that can turn that into an honest outcome. Two things matter
    about it and they are deliberately separate:

    * the **predicate** is the reason. The loop consults it at every step
      boundary, so a stop that lands between two steps (or before the first one)
      still ends the turn as ``ABORTED`` rather than being missed;
    * the **cancellation** is the mechanism. A caller that wants the in-flight
      await to actually stop — the LLM HTTP request is where nearly all the
      wall-clock time goes — cancels the task as well. ``run_turn`` only
      converts a ``CancelledError`` into an outcome when the predicate says the
      cancellation was asked for; a cancellation from anywhere else (server
      shutdown, a client that hung up) keeps its ordinary asyncio meaning.
    """

    def __init__(
        self,
        services: Services,
        registry: Any,  # ToolRegistry
        budget_limits: BudgetLimits,
        config: LoopConfig | None = None,
        observer: Callable[[str, dict], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        context_assembler: Any = None,
        history_provider: Callable[[str, str], list] | None = None,
        hooks: Any = None,
    ):
        self.services = services
        self.registry = registry
        self.budget_limits = budget_limits
        self.config = config or LoopConfig()
        self.budget = Budget(budget_limits)
        self.strategy = make_strategy(self.config.default_strategy)
        self._candidate_reports: list[GateReport] = []
        self._last_commit_passed = False
        self._last_gate_report: GateReport | None = None
        self._compile_failures = 0
        self._idle_steps = 0
        self._observer = observer
        self._stop_requested = stop_requested
        # Budgeted context build (design §4.3). When absent the engine keeps the
        # minimal [system, user] shape, which is what the unit tests use.
        self._context_assembler = context_assembler
        # ``(thread_id, current_user_text) -> list[Message]``. The reader lives in
        # tcad.context.history; injecting it as a callable keeps the engine from
        # importing the session store and keeps it testable with a fake.
        self._history_provider = history_provider
        # A per-turn dispatcher (a front end's observer tap). When given, EVERY
        # dispatch in this turn goes through it instead of ``services.hooks``, so
        # one request can observe its own lifecycle without mutating a bundle
        # other requests are using.
        self._hooks = hooks

    # ─── hook dispatch ────────────────────────────────────────────────────

    def _dispatch(self, event: HookEvent, payload: dict) -> Any:
        """Dispatch through the per-turn hooks when set, else the shared bundle."""
        hooks = self._hooks if self._hooks is not None else getattr(self.services, "hooks", None)
        if hooks is None:
            return HookResult(decision=HookDecision.ALLOW, hook_name="none")
        return hooks.dispatch(event, payload)

    def _halt_on_hook(self, where: str, result: Any, turn: Turn) -> bool:
        """Apply a turn/step-level hook decision. True means "stop the turn".

        ``PRE_TURN`` and ``PRE_STEP`` are the two hook points the engine owns
        directly rather than routing through a tool call, and both used to have
        their return value thrown away — so a DENY from either was a no-op that
        looked exactly like an allow. Objective §5-D names all four PRE_* points
        and requires DENY *and* ASK to take effect; ASK especially, because ASK
        means "a human must look at this first" and letting the turn continue
        past it executes the very writes the ask was about.

        Both decisions stop the turn. They differ in what the stop *means*, which
        is the thing the user has to be able to read off the screen:

          * DENY  -> FAILED. A policy refused; no further work is possible.
          * ASK   -> AWAITING_APPROVAL. Nothing is wrong; a person has to decide.

        Neither is allowed to be silently downgraded to "continue".
        """
        decision = getattr(result, "decision", HookDecision.ALLOW)
        if decision == HookDecision.ALLOW:
            return False
        reason = getattr(result, "reason", "") or "no reason given"
        hook_name = getattr(result, "hook_name", "?")
        if decision == HookDecision.ASK:
            turn.state = TurnState.AWAITING_APPROVAL
            turn.error = (
                f"{where} hook '{hook_name}' requires approval before this turn can "
                f"continue: {reason}. Nothing further was executed — in particular no "
                f"write of any kind."
            )
        else:
            turn.state = TurnState.FAILED
            turn.error = f"{where} hook '{hook_name}' denied the turn: {reason}"
        return True

    # ─── interruption ─────────────────────────────────────────────────────

    def stop_was_requested(self) -> bool:
        """Whether an outside caller has asked this turn to stop.

        A predicate that itself raises must not break the turn — the same rule
        the observer follows. "I could not find out whether to stop" is not a
        reason to abort, and it is certainly not a reason to crash.
        """
        if self._stop_requested is None:
            return False
        try:
            return bool(self._stop_requested())
        except Exception:  # noqa: BLE001
            return False

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
        messages = await self._build_messages(user_msg, turn)
        privileged = bool(user_msg.privileged_requested) and self.config.allow_privileged
        allowed = self._allowed_tiers(turn.kind, privileged)

        # Per-turn loop state. The engine may be reused across turns (the CLI
        # REPL does), and state carried over would fail — or worse, silently
        # pass — the next turn for no reason. In particular a commit that
        # passed in turn N must not count as a pass in turn N+1, and candidate
        # reports from turn N must not be promotable by a later turn's
        # strategy.
        self._idle_steps = 0
        self._compile_failures = 0
        self._last_gate_report = None
        self._last_commit_passed = False
        self._candidate_reports = []

        # The budget is per-TURN, and the engine is reused (the CLI REPL runs
        # every turn through one engine). It was created once in ``__init__`` and
        # never reset, so turn N+1 began with turn N's step and token counts
        # already spent: under the bounded policy (``configs/policies/strict.yaml``)
        # a perfectly ordinary second turn could be reported EXHAUSTED having
        # never exceeded a limit of its own, and ``turn.steps`` reported the
        # session total rather than the turn's. With no ceiling configured the
        # numbers are only cosmetic, which is why nothing caught it.
        self.budget = Budget(self.budget_limits)

        # pre_turn hook (quota / content-safety pre-check).
        #
        # The decision is ACTED ON, not merely dispatched. It used to be
        # discarded — `self._dispatch(...)` with no receiver — so a policy that
        # returned DENY at the turn boundary was, in effect, not installed: the
        # turn ran to completion exactly as if the hook had allowed it. Objective
        # §5-D requires every PRE_* DENY/ASK to take effect.
        if self._halt_on_hook("pre_turn", self._dispatch(
            HookEvent.PRE_TURN,
            {"thread_id": thread.thread_id, "turn_id": turn.turn_id,
             "model_id": thread.model_id},
        ), turn):
            # `_drive` only enters its loop while the state is RUNNING, so a
            # terminal state here means the model is never called and no tool
            # ever runs — the halt is structural, not a request to the strategy.
            result = self._finalize(turn, messages, self._last_gate_report)
            self._dispatch(
                HookEvent.POST_TURN,
                {"thread_id": thread.thread_id, "turn_id": turn.turn_id,
                 "state": turn.state.value, "steps": self.budget.steps},
            )
            self._observe("turn_end", {
                "turn_id": turn.turn_id, "state": result.state.value,
                "steps": self.budget.steps,
                "tokens_in": self.budget.tokens_in, "tokens_out": self.budget.tokens_out,
                "error": result.error, "gate": None,
            })
            return result

        try:
            try:
                result = await self.strategy.run(self, turn, messages, allowed)
            except Exception:
                # A strategy raising must degrade to M1, never kill the turn.
                if isinstance(self.strategy, LoopUntilDoneStrategy):
                    raise
                result = await LoopUntilDoneStrategy().run(self, turn, messages, allowed)
        except asyncio.CancelledError:
            # The in-flight await was cancelled. That is the *mechanism* of a
            # stop, not its meaning: convert it into an outcome only when the
            # stop predicate says someone asked for exactly that. Any other
            # cancellation (server shutdown, the client's request being torn
            # down) is not ours to reinterpret — it propagates, and the cleanup
            # above it still runs.
            if not self.stop_was_requested():
                raise
            turn.state = TurnState.ABORTED
            turn.error = STOPPED_BY_USER
            result = self._finalize(turn, messages, self._last_gate_report)
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
            self._dispatch(
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
        # A stop asked for between two steps (or before the first one). Checked
        # before the pre_step hook: a step that will not run must not announce
        # itself, and the loop's own condition ends the turn on a changed state.
        #
        # This is what makes the stop predicate — not the cancellation — the
        # authority on the outcome. A cancellation can be missed (a task that
        # never started, a cancel that races the end of a step); this cannot.
        if self.stop_was_requested():
            turn.state = TurnState.ABORTED
            turn.error = STOPPED_BY_USER
            return StepYield()

        # pre_step hook (context placement / cost ceiling). Same rule as
        # pre_turn: the decision is acted on. A non-RUNNING state ends `_drive`'s
        # loop, so a denied step means no LLM call and no tool call at all.
        if self._halt_on_hook("pre_step", self._dispatch(
            HookEvent.PRE_STEP,
            {"thread_id": turn.thread_id, "turn_id": turn.turn_id,
             "state": turn.state.value},
        ), turn):
            return StepYield()
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
        halt_after_this = False
        for idx, tc in enumerate(reply.tool_calls):
            spec = self.registry.get(tc.name)
            if spec is None:
                outcome = ToolOutcome(
                    result=ToolResult(
                        ok=False,
                        error=ToolError(kind=ToolErrorKind.NOT_FOUND, message=f"unknown tool: {tc.name}"),
                    )
                )
            elif tc.args_error:
                # The arguments never parsed, so there is nothing to dispatch.
                # Passing the empty dict through to `execute_tool` — which is
                # what used to happen — produced "missing required property
                # 'base_version'; missing required property 'ops'": a statement
                # about keys the model had in fact been *in the middle of
                # writing* when its output was cut off at the per-step token
                # ceiling. The model's only rational response to that is to send
                # the same too-large patch again.
                #
                # So the outcome is built from what the client actually observed
                # (the parse error, the raw length) plus what the provider said
                # about why it stopped (`finish_reason`), and it prescribes the
                # fix that matches the cause.
                outcome = ToolOutcome(
                    result=ToolResult(
                        ok=False,
                        error=ToolError(
                            kind=ToolErrorKind.SCHEMA,
                            message=(
                                f"{tc.name}: the tool-call arguments were not usable, so "
                                f"NOTHING was applied — {tc.args_error}."
                            ),
                            hint=_unparsable_args_hint(tc, reply.finish_reason),
                        ),
                    )
                )
            else:
                # Engine-managed visual-checkpoint hint consumed by geo_view.
                # Set on THIS step's context, never on the shared services bundle:
                # two concurrent turns share that bundle, so a commit in one
                # session would open the visual checkpoint for another's step.
                ctx.visual_ok = (
                    True if turn.kind == TurnKind.INSPECT else self._last_commit_passed
                )
                if spec.tier == ToolTier.PRIVILEGED:
                    # privileged owns its own hook dispatch (triple gate).
                    outcome = await execute_tool(spec, tc.args, ctx, allowed_tiers=allowed)
                else:
                    hook_res = self._dispatch(
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
                        approval_id = self._request_approval(
                            tc.name, tc.args,
                            thread_id=turn.thread_id, turn_id=turn.turn_id,
                        )
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
                        # Everything AFTER this call in the same batch is skipped:
                        # a model that asks for six edits in one step and trips an
                        # ASK on the third must not have the fourth through sixth
                        # applied while a human is being asked about the third.
                        # Objective §5-D: "ASK 不能继续执行后续写操作".
                        halt_after_this = True
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
            self._dispatch(
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

            # A successful write after a passed commit invalidates that pass:
            # the IR the Gate graded is no longer the current IR, so "passed"
            # no longer describes the model's state. A denied/failed write
            # changed nothing and must not invalidate.
            #
            # The rule is STRUCTURAL (any write-tier tool except ir_commit), not a
            # hardcoded name list: a name list silently stops covering the first
            # write tool added after it was written, and "a pass survives a write"
            # is exactly the failure this exists to prevent.
            if (
                self._last_commit_passed
                and outcome.result.ok
                and _is_mutating(tc.name, spec)
            ):
                self._last_commit_passed = False

            if halt_after_this:
                # The turn is suspended pending approval, so the remaining calls
                # in this batch must NOT execute. They still get a `tool` message
                # each: the OpenAI wire format requires an answer for every
                # tool_call id, and a resumed transcript built without them would
                # be rejected by the provider — the suspension would look like a
                # protocol error instead of a pending decision.
                for skipped in reply.tool_calls[idx + 1:]:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": skipped.id,
                        "name": skipped.name,
                        "content": (
                            f"NOT executed: this turn was suspended for approval on "
                            f"{tc.name}, and no tool call after it was run. "
                            f"Re-issue this call once the approval is granted."
                        ),
                    })
                    self._observe("tool", {
                        "step": self.budget.steps,
                        "name": skipped.name,
                        "ok": False,
                        "content": "skipped: turn suspended for approval",
                        "error": {"kind": "denied",
                                  "message": "not executed: turn suspended pending approval",
                                  "feature_id": None},
                        "images": [],
                        "gate": None,
                    })
                break

            # ir_commit: the engine — not the tool — decides success.
            if tc.name == "ir_commit":
                gr = outcome.gate_report
                if gr is not None:
                    self._candidate_reports.append(gr)
                    self._last_gate_report = gr
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

    async def _build_messages(self, user_msg: UserMessage, turn: Turn) -> list[dict]:
        """Assemble the request context, budgeted (task book §5-D).

        Before this existed the model received ``[system, user]`` and nothing
        else: the conversation was written to SQLite but never read back, so a
        request like "change those four holes to diameter 8, leave the rest
        alone" had no "those four holes" to resolve against. The blocks added
        here are exactly the ones that make such a request answerable — prior
        turns, the requirement contract, the current IR (with stable ids), the
        current version and the previous Gate verdict.

        Degradation is deliberate and total: with no assembler, or if assembly
        raises, the turn falls back to the original two-message shape. Context is
        an optimisation for the model, never a precondition for running a turn.
        """
        if self._context_assembler is None:
            return self._init_messages(user_msg, turn)

        try:
            from tcad.context.assembler import (
                AssembleContext,
                to_openai_messages,
            )

            blocks = self._context_blocks(turn)
            history = self._load_history(turn.thread_id, user_msg.text)
            ctx = AssembleContext(
                system_prompt=self.config.system_prompt,
                requirements_text=blocks["requirements_text"],
                digest_text=blocks["digest_text"],
                gate_report_text=blocks["gate_report_text"],
                history=history,
            )
            assembled = await self._context_assembler.build(ctx, [])
            messages = to_openai_messages(assembled)
        except Exception:  # noqa: BLE001 — see the degradation note above
            return self._init_messages(user_msg, turn)

        # The current request always goes last, verbatim, exactly once.
        messages.append({"role": "user", "content": user_msg.text})
        return messages

    def _context_blocks(self, turn: Turn) -> dict[str, str]:
        """The three generated context blocks, each independently best-effort."""
        from tcad.context.requirements import render_requirements_text
        from tcad.context.verdict import render_verdict_text

        blocks = {"requirements_text": "", "digest_text": "", "gate_report_text": ""}
        store = getattr(self.services, "store", None)

        version: int | None = None
        try:
            version = int(store.current_version(turn.model_id))
        except Exception:  # noqa: BLE001
            version = None

        try:
            ir = store.load(turn.model_id, version) if version is not None else store.load(turn.model_id)
            blocks["requirements_text"] = render_requirements_text(ir)
        except Exception:  # noqa: BLE001 — no IR means no contract to show
            pass

        try:
            context = getattr(self.services, "context", None)
            if context is not None and version is not None:
                digest = context.digest(turn.model_id, version)
                blocks["digest_text"] = getattr(digest, "text", "") or ""
        except Exception:  # noqa: BLE001
            pass

        blocks["gate_report_text"] = self._previous_verdict_text(turn, version)
        return blocks

    def _previous_verdict_text(self, turn: Turn, version: int | None) -> str:
        """Last verdict: this turn's, else the one persisted by the last turn."""
        from tcad.context.verdict import render_verdict_text

        if self._last_gate_report is not None:
            return render_verdict_text(self._last_gate_report)

        store = getattr(self.services, "store", None)
        reader = getattr(store, "read_gate_report", None)
        if not callable(reader) or version is None:
            return ""
        try:
            return render_verdict_text(reader(turn.model_id, version))
        except Exception:  # noqa: BLE001
            return ""

    def _load_history(self, thread_id: str, current_text: str) -> list:
        if self._history_provider is None:
            return []
        try:
            return list(self._history_provider(thread_id, current_text) or [])
        except Exception:  # noqa: BLE001 — history is a convenience, not a guarantee
            return []

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
            hooks=self._hooks,
            visual_ok=False,
        )

    def _request_approval(
        self, tool_name: str, args: dict, *, thread_id: str | None = None, turn_id: str | None = None
    ) -> str | None:
        """Create an approval record for a suspended call, if a store is wired.

        The record is bound to the *exact* arguments via the one shared
        fingerprint helper (so the gate that later checks it computes the same
        string), to the session/turn that asked, and it carries a human-readable
        rendering of the payload so a person can see what they are approving.
        Returns the record id, or None when no approval store is available (the
        turn still suspends — it just cannot be resumed, and says so).
        """
        store = getattr(self.services, "approvals", None)
        if store is None:
            return None
        try:
            import json as _json

            from tcad.hooks.approval import args_fingerprint

            summary = _json.dumps(args if args is not None else {}, ensure_ascii=False,
                                  sort_keys=True, default=str)
            if len(summary) > _APPROVAL_SUMMARY_CHARS:
                summary = summary[:_APPROVAL_SUMMARY_CHARS] + "…"
            return store.request(
                tool_name,
                args_hash=args_fingerprint(args),
                thread_id=thread_id,
                turn_id=turn_id,
                args_summary=summary,
            ).id
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
