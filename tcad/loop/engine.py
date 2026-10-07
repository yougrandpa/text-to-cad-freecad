"""The execution loop — the E layer (design §4.1).

LoopEngine.run_turn drives one Turn:

    pre_turn hook
    -> [ pre_step hook -> budget check -> LLM -> tools (pre/post_tool_use)
         -> repeat ]  (until a green Gate or a hard terminal state)
    -> post_turn hook  (always, including on exception — try/finally)

Two non-negotiable invariants enforced here (design §4.1, §9, §10):

  1. Success requires a current passed Gate and, in production, a validated
     design_review backed by user-sourced measured constraints. Missing evidence
     is a draft pending acceptance. Model narration and budget survival are not
     proof of functional completion.

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
from tcad.core.access import AccessMode, READ_ONLY_TOOLS
from tcad.hooks.access import AccessHooks

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
from tcad.ir.schema import IrPatch, IrPatchOp
from tcad.llm.client import describe_llm_failure
from tcad.loop.budget import Budget, BudgetLimits
from tcad.loop.recovery import RepeatedFailures
from tcad.loop.strategies import LoopUntilDoneStrategy, make_strategy
from tcad.tools.base import Services, ToolOutcome, execute_tool
from tcad.selection.types import SelectionContext


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
    access_mode: AccessMode | None = None
    selection_context: SelectionContext | None = None


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
    completion_review: dict | None = None


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
    context_window_tokens: int = Field(default=128_000, gt=0)
    repeated_tool_failure_limit: int | None = Field(default=3, ge=2)
    # Embedded geometry-only callers retain the old contract. Production wiring
    # explicitly enables review from YAML (default true).
    require_design_review: bool = False
    allow_privileged: bool = False
    visual_checkpoints: tuple[str, ...] = ("first_compile", "major_change", "final")
    artifact_exports: tuple[str, ...] = ("step", "stl")
    system_prompt: str = (
        "You are a parametric CAD agent. You design by mutating an intermediate "
        "representation (IR) via ir_patch, then ir_commit to compile and gate. "
        "Choose single-body or multi-body modeling based on the requirements and their complexity. "
        "Use one Body for an integral part, even with many features; split into multiple Bodies "
        "when separate components, manufacturing boundaries or relative motion require it. "
        "Feature count alone does not require splitting. Honor explicit single-part or assembly intent. "
        "Always specify body_id for new sketches/features in a multi-body model. "
        "Configure assembly joints for actual connections and requested motion; "
        "a multi-body compound alone does not establish assembly constraints. "
        "Batch dependent feature edits in one patch and commit at meaningful milestones, "
        "not after every primitive. Prefer ir_digest for measurements and ir_get(ids=[...]) "
        "for exact targeted state. Discover operation contracts before unfamiliar authoring. "
        "After a valid motion assembly, geo_check_motion checks multiple angles in one build; "
        "choose only needed body pairs. Render only views needed for visual evidence. "
        "Never claim success unless ir_commit returns passed=true. Read tools "
        "(ir_get/ir_digest/ir_list_features/geo_*) never change the design. "
        "A later edit invalidates an earlier passed Gate: commit and verify the current version. "
        "After a tool error, inspect the feedback and change the failing operation; "
        "repeating an identical failure without changing the design will stop the turn."
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
    if tool_name in ("ir_commit", "design_review"):
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
        if self.config.require_design_review:
            from tcad.agent.prompts import with_design_review
            self.config = self.config.model_copy(update={"system_prompt": with_design_review(self.config.system_prompt)})
        self.budget = Budget(budget_limits)
        self.strategy = make_strategy(self.config.default_strategy)
        self._candidate_reports: list[GateReport] = []
        self._last_commit_passed = False
        self._visual_checkpoint = False
        self._last_gate_report: GateReport | None = None
        self._compile_failures = 0
        self._idle_steps = 0
        self._repeated_failures = RepeatedFailures(self.config.repeated_tool_failure_limit)
        self._trace_start = 0
        self._current_user_message: UserMessage | None = None
        self._pinned_history: list[dict] = []
        self._completion_review: dict | None = None
        self._request_text: str | None = None
        self._edit_precondition = None
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
        self._base_hooks = hooks if hooks is not None else getattr(services, "hooks", None)
        self._access_mode = None
        self._authoring_topics = set()
        self._authoring_workflows = set()
        #: Feature ops whose scoped field set is exposed on ir_patch, most
        #: recently requested last (task P1-3: help unlocks fields, not tables).
        self._authoring_features: list[str] = []
        #: Tool name -> schema-rejected call kept for local repair (task P0-2).
        self._retained_calls: dict[str, dict] = {}

    # ─── hook dispatch ────────────────────────────────────────────────────

    def _authoring_surface(self, tools, model_id=None):
        if not self.config.require_design_review:
            return tools
        import copy
        from tcad.agent.workflows import WORKFLOWS, SPECIALIZED_TOOLS
        workflow_tools=set().union(*(set(WORKFLOWS[name]['tools']) for name in self._authoring_workflows))
        scopes={'sketch':{'add_sketch','update_sketch'},
                'feature':{'add_feature','update_feature'},
                'requirements':{'update_requirement'}}
        selected=set().union(*(scopes.get(t,set()) for t in self._authoring_topics))
        if selected or 'patch' in self._authoring_topics:
            selected.update({'add_body','update_body','remove_body','remove_feature','rename'})
        result=[]
        saved_animation=False
        if model_id is not None:
            try:
                saved_animation=self.services.store.load(model_id).assembly is not None
            except Exception:
                pass
        for tool in tools:
            name=tool['function']['name']
            if name in SPECIALIZED_TOOLS and name not in workflow_tools:
                continue
            if saved_animation and name=='geo_check_motion':
                continue  # Saved frames use assembly_simulate(check_pairs), not crank angles.
            if name=='assembly_configure' and not self._authoring_topics & {'assembly','patch'}:
                continue
            if name=='ir_patch':
                if not selected:
                    continue
                tool=copy.deepcopy(tool)
                items=tool['function']['parameters']['properties']['ops']['items']
                items['anyOf']=[b for b in items['anyOf'] if b['properties']['op']['enum'][0] in selected]
                if self._authoring_features and selected & {'add_feature','update_feature'}:
                    # Only the fields of the operation(s) help was actually read
                    # for. The full table stays one `ir_help` call away.
                    from tcad.tools.feature_scope import scope_branches
                    items['anyOf']=scope_branches(items['anyOf'], self._authoring_features)
            result.append(tool)
        return result

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
        if user_msg.access_mode == AccessMode.READ_ONLY:
            user_msg = user_msg.model_copy(update={"kind": TurnKind.INSPECT})
        self._access_mode = user_msg.access_mode
        self._edit_precondition = None
        if user_msg.selection_context is not None:
            from tcad.selection.precondition import EditPrecondition
            from tcad.selection.resolve import SelectionResolver
            from tcad.selection.types import SelectionError
            if not self.services.config.selection.enabled:
                raise SelectionError("selection_disabled", "Feature references are disabled.", 403)
            resolver = SelectionResolver(self.config.data_dir, self.services.store)
            snapshot, targets = resolver.resolve(thread.model_id, user_msg.selection_context,
                                                 inspection=user_msg.kind == TurnKind.INSPECT)
            self._edit_precondition = EditPrecondition(targets, snapshot.ir.model_copy(deep=True), resolver.reader)
        if user_msg.access_mode is not None:
            self._hooks = AccessHooks(self._base_hooks, user_msg.access_mode)
        else:
            self._hooks = self._base_hooks
        turn = self._new_turn(thread, user_msg)
        privileged = bool(user_msg.privileged_requested) and self.config.allow_privileged
        if user_msg.access_mode is not None:
            privileged = user_msg.access_mode == AccessMode.FULL and turn.kind != TurnKind.INSPECT
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
        self._visual_checkpoint = False
        self._candidate_reports = []
        self._repeated_failures = RepeatedFailures(self.config.repeated_tool_failure_limit)
        self._retained_calls = {}
        self._current_user_message = user_msg
        self._completion_review = None
        self._request_text = None
        self._pinned_history = []
        messages = await self._build_messages(user_msg, turn)
        self._trace_start = len(messages)

        # The budget is per-TURN, and the engine is reused (the CLI REPL runs
        # every turn through one engine). It was created once in ``__init__`` and
        # never reset, so turn N+1 began with turn N's step and token counts
        # already spent: under the bounded policy (``configs/policies/strict.yaml``)
        # a perfectly ordinary second turn could be reported EXHAUSTED having
        # never exceeded a limit of its own, and ``turn.steps`` reported the
        # session total rather than the turn's. With no ceiling configured the
        # numbers are only cosmetic, which is why nothing caught it.
        self.budget = Budget(self.budget_limits)
        self._authoring_topics = set()
        self._authoring_workflows = set()
        self._authoring_features = []

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
            if self.config.require_design_review and turn.kind != TurnKind.INSPECT:
                ir = self.services.store.load(turn.model_id)
                text = ir.requirements.raw_text
                if not text:
                    # Upgrade old sessions whose first user request was never
                    # recorded in the IR. Ignore model narration as provenance.
                    text = "\n\n".join(message.content for message in
                        self._load_history(turn.thread_id, user_msg.text)
                        if message.role == "user" and message.content.strip())
                original_text = ir.requirements.raw_text
                if user_msg.text.strip() and user_msg.text not in text:
                    text = (text + "\n\n用户追加需求：\n" if text else "") + user_msg.text
                if text != original_text:
                    provenance = IrPatch(
                        base_version=ir.version, ops=[IrPatchOp(
                            op="update_requirement", payload={"raw_text": text},
                            reason="Preserve the user's original request for acceptance provenance",
                        )],
                    )
                    if self._edit_precondition is None:
                        self.services.store.apply_patch(turn.model_id, provenance)
                    else:
                        from tcad.tools.ir_tools import ir_patch_handler
                        self._request_text = text
                        applied = await ir_patch_handler(self.services, provenance.model_dump(mode="json"),
                                                         self._make_tool_context(turn))
                        if not applied.ok:
                            raise ValueError(applied.error.message)
                self._request_text = text
                messages = await self._build_messages(user_msg, turn)
                self._trace_start = len(messages)
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
                error=describe_llm_failure(e),
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

        tools = self.registry.as_openai_tools(
            turn.kind, include_privileged=ToolTier.PRIVILEGED in allowed
        )
        # Detailed editing contracts are discoverable, not repeated on every
        # primitive-building step. Keep the registry complete for existing clients.
        tools = self._authoring_surface(tools,turn.model_id)
        tools = [tool for tool in tools if self._selection_tool_available(tool["function"]["name"])]
        if self._access_mode == AccessMode.READ_ONLY:
            tools = [tool for tool in tools if tool["function"]["name"] in READ_ONLY_TOOLS]
        await self._prepare_step_context(turn, messages, tools)
        reply = await self.services.llm.chat(
            messages=messages,
            tools=tools,
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
            if self._access_mode == AccessMode.READ_ONLY and (reply.text or "").strip():
                turn.state = TurnState.INSPECTED
                return StepYield()
            self._idle_steps += 1
            if self._idle_steps > MAX_IDLE_STEPS:
                if self.config.require_design_review and self.build_is_current(turn, self._last_gate_report):
                    turn.state = TurnState.DRAFT
                    self._completion_review = {
                        "verified": False, "scope": "recorded_constraints",
                        "summary": "构建已通过，但模型没有提交最终需求复核。",
                        "checklist": [], "remaining_work": ["缺少最终需求清单与测量证据，功能仍待验收。"],
                        "ir_version": self._last_gate_report.ir_version,
                        "note": "实际机械功能和需求完整性仍需用户验收。",
                    }
                    return StepYield(gate_report=self._last_gate_report)
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
        from tcad.context.tool_images import tool_image_feedback

        render_messages: list[dict] = []
        descriptor = getattr(self.services.llm, "descriptor", {})
        supports_vision = isinstance(descriptor, dict) and descriptor.get("supports_vision") is True
        halt_after_this = False
        for idx, tc in enumerate(reply.tool_calls):
            spec = self.registry.get(tc.name)
            call_args = tc.args
            if tc.name == "ir_commit":
                # The most recent attempted commit owns the verification state.
                # A failed recommit cannot reuse an earlier green report.
                self._last_commit_passed = False
                self._visual_checkpoint = False
                self._completion_review = None
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
                # Local repair (task P0-2): a schema-rejected call is retained,
                # and a reply that supplies ONLY the missing fields completes
                # it. The merged payload then walks the ordinary path below —
                # hook dispatch, access checks, validate_tool_args — so
                # retention is a convenience for the model, never a bypass.
                call_args, merged_from_retained = self._prepare_call_args(
                    tc.name, tc.args, spec)
                # Engine-managed visual-checkpoint hint consumed by geo_view.
                # Set on THIS step's context, never on the shared services bundle:
                # two concurrent turns share that bundle, so a commit in one
                # session would open the visual checkpoint for another's step.
                ctx.visual_ok = (
                    True if turn.kind == TurnKind.INSPECT else self._visual_checkpoint
                )
                if spec.tier == ToolTier.PRIVILEGED:
                    # privileged owns its own hook dispatch (triple gate).
                    outcome = await execute_tool(spec, call_args, ctx, allowed_tiers=allowed)
                else:
                    hook_res = self._dispatch(
                        HookEvent.PRE_TOOL_USE,
                        {
                            "tool_name": tc.name,
                            "tier": spec.tier.value,
                            "args": call_args,
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
                            tc.name, call_args,
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
                        if hook_res.mutated_args is not None:
                            call_args = hook_res.mutated_args
                        outcome = await execute_tool(spec, call_args, ctx, allowed_tiers=allowed)

                # Retention bookkeeping runs for every executed call in this
                # branch: a success clears the retained predecessor; a schema
                # rejection keeps (or refreshes) it so the next reply can fill
                # only the missing fields.
                self._settle_retained_call(tc.name, call_args, spec, outcome,
                                           completed=merged_from_retained)

            if tc.name == 'ir_help' and outcome.result.ok:
                self._authoring_topics.add(call_args.get('topic'))
                if call_args.get('topic') == 'feature' and call_args.get('feature_op'):
                    from tcad.tools.feature_scope import MAX_SCOPED_OPS
                    op = call_args['feature_op']
                    if op in self._authoring_features:
                        self._authoring_features.remove(op)
                    self._authoring_features.append(op)
                    del self._authoring_features[:-MAX_SCOPED_OPS]
                if call_args.get('topic') == 'workflow' and call_args.get('workflow'):
                    self._authoring_workflows.add(call_args['workflow'])
            if tc.name == "design_review" and outcome.result.ok:
                if not self.config.require_design_review:
                    outcome.result = ToolResult(ok=False, error=ToolError(
                        kind=ToolErrorKind.DENIED, message="design_review requires review-enabled mode"))
                elif not self.build_is_current(turn, self._last_gate_report):
                    outcome.result = ToolResult(ok=False, error=ToolError(
                        kind=ToolErrorKind.SEMANTIC,
                        message="Commit and verify the current IR before reviewing it; old Gate evidence is invalid."))
                elif len(reply.tool_calls) != 1:
                    outcome.result = ToolResult(ok=False, error=ToolError(
                        kind=ToolErrorKind.SEMANTIC,
                        message="Call design_review alone, after the final build, in a separate step."))
                else:
                    from tcad.loop.completion import DesignReview, validate_review
                    self._completion_review = validate_review(
                        DesignReview.model_validate(call_args), self.services.store.load(turn.model_id),
                        self._last_gate_report, self._request_text or "",
                    )
                    # A recorded design degradation (a user-required planned part
                    # that was lost or simplified) keeps the delivery a draft
                    # pending acceptance, no matter how green the Gate is.
                    try:
                        from tcad.loop.intent import degradation_items
                        degradations = degradation_items(
                            self.config.data_dir, turn.model_id,
                            self.services.store.load(turn.model_id))
                    except Exception:  # noqa: BLE001 — review bookkeeping is best-effort
                        degradations = []
                    if degradations:
                        self._completion_review["remaining_work"] = (
                            list(self._completion_review["remaining_work"]) + degradations)
                        self._completion_review["verified"] = False
                        self._completion_review["note"] = (
                            self._completion_review.get("note", "")
                            + " 设计退化已记录；交付保持待审草稿。").strip()
                    outcome.result = ToolResult(ok=True, content=json.dumps(self._completion_review, ensure_ascii=False))
                    gate_report = self._last_gate_report
                    if not self._completion_review["verified"]:
                        turn.state = TurnState.DRAFT

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
            render_message, render_note = tool_image_feedback(
                outcome.result, name=tc.name, call_id=tc.id,
                data_dir=self.config.data_dir, supports_vision=supports_vision,
            )
            if render_message is not None:
                render_messages.append(render_message)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": _result_text(outcome.result) + render_note,
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
                            "hint": outcome.result.error.hint,
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
                outcome.result.ok
                and _is_mutating(tc.name, spec)
            ):
                self._last_commit_passed = False
                self._visual_checkpoint = False
                self._completion_review = None
                self._repeated_failures.clear()

            gate_failed = outcome.gate_report is not None and not outcome.gate_report.passed
            if turn.state == TurnState.RUNNING and (not outcome.result.ok or gate_failed):
                try:
                    current_version = int(self.services.store.current_version(turn.model_id))
                except Exception:  # no current snapshot is still a failed attempt
                    current_version = None
                effective_error = outcome.result.error
                if effective_error is None and gate_failed:
                    effective_error = ToolError(
                        kind=ToolErrorKind.SEMANTIC,
                        message="Gate failed: " + json.dumps(
                            outcome.gate_report.blocking_failures, sort_keys=True
                        ),
                    )
                repeats = self._repeated_failures.record(tc.name, call_args, effective_error, current_version)
                limit = self.config.repeated_tool_failure_limit
                if limit is not None and repeats >= limit:
                    turn.state = TurnState.FAILED
                    turn.error = (
                        f"no progress: {tc.name} failed identically {repeats} times at "
                        f"IR version {current_version}. The turn was stopped; prior edits "
                        "are preserved, but no new success is claimed. Inspect the tool "
                        "error and change the failing operation before continuing."
                    )
                    halt_after_this = True

            if halt_after_this:
                # The turn is suspended pending approval, so the remaining calls
                # in this batch must NOT execute. They still get a `tool` message
                # each: the OpenAI wire format requires an answer for every
                # tool_call id, and a resumed transcript built without them would
                # be rejected by the provider — the suspension would look like a
                # protocol error instead of a pending decision.
                why = (f"this turn was suspended for approval on {tc.name}"
                       if turn.state == TurnState.AWAITING_APPROVAL else turn.error)
                for skipped in reply.tool_calls[idx + 1:]:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": skipped.id,
                        "name": skipped.name,
                        "content": (
                            f"NOT executed: {why}; no tool call after it was run. "
                            "Re-issue this call only in a new authorized continuation."
                        ),
                    })
                    self._observe("tool", {
                        "step": self.budget.steps,
                        "name": skipped.name,
                        "ok": False,
                        "content": f"skipped: {why}",
                        "error": {"kind": "denied",
                                  "message": f"not executed: {why}",
                                  "feature_id": None},
                        "images": [],
                        "gate": None,
                    })
                break

            # ir_commit: the engine — not the tool — decides success.
            if tc.name == "ir_commit":
                gr = outcome.gate_report
                if gr is not None:
                    # A graded build has a saved scene even when the Gate fails.
                    # Its geometry is useful for diagnosis, not completion proof.
                    self._visual_checkpoint = True
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

        # User image blocks must follow the entire tool-result batch; placing
        # them between results would violate the provider's tool-call protocol.
        messages.extend(render_messages)
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
            *self._access_messages(),
            *self._selection_messages(),
            *([{"role": "system", "content": "Authoritative user requirements:\n" + self._request_text}]
              if self._request_text else []),
            {"role": "user", "content": user_msg.text},
        ]

    async def _prepare_step_context(self, turn: Turn, messages: list[dict], tools: list[dict]) -> None:
        """Bound the actual outgoing request, including schemas and output reserve.

        When a long turn reaches the estimate, rebuild authoritative CAD context
        and omit only whole older tool batches. Latest replies retain call IDs,
        arguments, result messages and provider reasoning fields unchanged.
        This is deterministic checkpointing, not an invented LLM summary.
        """
        from tcad.context.turn_compaction import (
            compact_turn, estimate_request_tokens, input_budget,
        )

        window = self.config.context_window_tokens
        reserve = self.config.llm_max_tokens
        estimated = estimate_request_tokens(messages, tools)
        if estimated <= input_budget(window, reserve):
            return
        if self._current_user_message is None:
            raise RuntimeError("cannot rebuild context without the current user request")
        # The existing assembler handles previous conversation history. Refresh
        # state from the actual store, never from a model-written summary.
        prefix = await self._build_messages(self._current_user_message, turn, require_state=True)
        trace = messages[self._trace_start:]
        compacted = compact_turn(
            prefix, trace, tools, window_tokens=window,
            max_output_tokens=reserve, keep_recent_batches=2,
        )
        messages[:] = compacted.messages
        self._trace_start = len(prefix) + (1 if compacted.dropped_batches else 0)
        self._observe("context", {
            "action": "compacted", "dropped_batches": compacted.dropped_batches,
            "estimated_tokens_before": estimated,
            "estimated_tokens_after": compacted.estimated_tokens,
            "input_budget": compacted.input_budget,
            "note": "Earlier tool rounds omitted; current IR/requirements/Gate refreshed. Estimates are approximate.",
        })

    async def _build_messages(
        self, user_msg: UserMessage, turn: Turn, *, require_state: bool = False,
    ) -> list[dict]:
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
        if self._context_assembler is None and not require_state:
            return self._init_messages(user_msg, turn)

        try:
            from tcad.context.assembler import (
                AssembleContext,
                to_openai_messages,
            )

            blocks = self._context_blocks(turn)
            if require_state and not all(blocks[key].strip() for key in (
                "requirements_text", "digest_text"
            )):
                raise RuntimeError(
                    "cannot refresh current CAD requirements and IR digest for safe context compaction; "
                    "earlier tool history was not discarded"
                )
            if require_state:
                # Re-running history degradation as the IR grows could erase a
                # prior user constraint. Pin exactly the history/summary chosen
                # at turn start; refresh only deterministic CAD state blocks.
                return [
                    {"role": "system", "content": self.config.system_prompt},
                    *({"role": "system", "content": text} for text in blocks.values() if text),
                    *self._pinned_history,
                    {"role": "user", "content": user_msg.text},
                ]
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
            self._pinned_history = to_openai_messages([
                message for message in assembled
                if message.kind not in ("system", "requirements", "digest", "gate")
            ])
        except Exception:  # noqa: BLE001 — see the degradation note above
            if require_state:
                raise
            return self._init_messages(user_msg, turn)

        # The current request always goes last, verbatim, exactly once.
        messages.append({"role": "user", "content": user_msg.text})
        messages[0:0] = self._access_messages()
        messages[0:0] = self._selection_messages()
        return messages

    def _selection_tool_available(self, name: str) -> bool:
        spec = self.registry.get(name)
        if self._edit_precondition is not None and spec.tier == ToolTier.PRIVILEGED:
            return False
        capability = spec.selection_capability
        return capability is None or (self._edit_precondition is not None
                                      and self._edit_precondition.supports(capability))

    def _selection_messages(self):
        return ([{"role": "system", "content": self._edit_precondition.prompt()}]
                if self._edit_precondition is not None else [])

    def _access_messages(self):
        if self._access_mode is None:
            return []
        instruction = {
            AccessMode.READ_ONLY: "Read-only inspection. Do not design, patch, commit, export or run Python. "
                "Use inspection tools if needed, then answer the user in plain text. This does not verify a new build.",
            AccessMode.AUTO: "CAD read/write calls are auto-approved within configured path restrictions. "
                "Python execution is unavailable. Continue building and reviewing the requested design.",
            AccessMode.FULL: "The operator selected full access for this turn. CAD tools and raw_python "
                "are available without tool approval or Python sandbox isolation. Build and requirement "
                "verification still apply; arbitrary Python output cannot prove functional completion.",
        }[self._access_mode]
        return [{"role": "system", "content": instruction}]

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
            request_text=self._request_text,
            access_mode=self._access_mode,
            edit_precondition=self._edit_precondition,
        )

    # ─── retained rejected calls (local repair, task P0-2) ────────────────
    #
    # A call rejected for missing fields used to be erased. The model's only
    # recorded remedy was to re-send the whole payload — and rewriting a long
    # batch is exactly where parts/topic-class field swaps creep in (observed:
    # a compact-recipe batch came back with its `parts` written into `topic`).
    # So the rejected call is kept, and a later reply that supplies only the
    # missing fields completes it through the ordinary checks.

    def _prepare_call_args(self, name: str, args: dict, spec) -> tuple[dict, bool]:
        """Merge a partial retry with the call it is completing.

        Returns ``(args_to_run, merged)``. A retry containing only missing
        required fields completes the retained call; a complete replacement
        supersedes it. Other incomplete calls merge over it. The
        merged payload is what the hook dispatch, access checks and
        ``validate_tool_args`` see — there is no separate trust path.
        """
        from tcad.tools.schema_check import validate_tool_args

        retained = self._retained_calls.get(name)
        if not retained or not isinstance(args, dict):
            return args, False
        missing = set(spec.params_schema.get('required') or []) - set(retained)
        if missing and set(args) <= missing:
            return {**retained, **args}, True
        if not validate_tool_args(args, spec):
            self._retained_calls.pop(name, None)  # a complete call replaces it
            return args, False
        return {**retained, **args}, True

    def _settle_retained_call(self, name: str, args: dict, spec, outcome, *, completed: bool) -> None:
        """Keep a call rejected for missing fields; clear it on success."""
        error = outcome.result.error
        if outcome.result.ok:
            if completed:
                self._retained_calls.pop(name, None)
                outcome.result.content = (
                    "(completed the retained call with only its missing fields) "
                    + (outcome.result.content or ""))
            return
        if error is None or error.kind != ToolErrorKind.SCHEMA:
            if completed:
                self._retained_calls.pop(name, None)
            return
        from tcad.tools.schema_check import validate_tool_args

        missing = []
        for problem in validate_tool_args(args, spec):
            marker = "missing required property "
            index = problem.find(marker)
            if index < 0:
                missing = []  # a type/shape error is not repaired by adding fields
                break
            missing.append(problem[index + len(marker):].strip().split(" ")[0].strip("'"))
        if not missing:
            self._retained_calls.pop(name, None)
            return
        self._retained_calls[name] = dict(args)
        fields = ", ".join(sorted(set(missing)))
        error.hint = (
            f"The call was REJECTED but RETAINED untouched. Reply with ONLY the missing "
            f"field(s): {fields}. They are merged into the retained call and re-validated "
            f"before anything runs — do not resend the fields that already parsed, and do "
            f"not rewrite the payload from scratch."
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
        current_pass = self.gate_is_current(turn, gate_report)
        if turn.state == TurnState.SUCCEEDED and not current_pass:
            turn.state = TurnState.FAILED
            turn.error = "the current IR version has no valid passed Gate; commit and verify it again"
        if gate_report is not None and gate_report.passed and not self.build_is_current(turn, gate_report):
            # Keep historical reports for diagnostics/context, but do not send a
            # green completion card for an unverified or approval-blocked turn.
            gate_report = None
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
            completion_review=self._completion_review,
        )

    def gate_is_current(self, turn: Turn, report: GateReport | None) -> bool:
        """A build pass cannot finish a review-enabled design turn."""
        reviewed = (not self.config.require_design_review or turn.kind == TurnKind.INSPECT
                    or bool(self._completion_review and self._completion_review["verified"]))
        return reviewed and self.build_is_current(turn, report)

    def build_is_current(self, turn: Turn, report: GateReport | None) -> bool:
        """Geometry verification is separate from requirement acceptance."""
        if (turn.state not in (TurnState.RUNNING, TurnState.SUCCEEDED, TurnState.DRAFT)
                or not self._last_commit_passed or report is None or not report.passed
                or self._last_gate_report is not report or report.model_id != turn.model_id):
            return False
        try:
            return int(self.services.store.current_version(turn.model_id)) == report.ir_version
        except Exception:  # an unreadable current version cannot be verified
            return False

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
