"""FastAPI front end — the "talk to it" surface for the harness.

A thin shell over :func:`tcad.core.wiring.build_services`. The interesting design
decision here is how progress reaches the client: ``LoopEngine.run_turn`` returns
a single result at the end, so there is no natural stream. Rather than invent a
second event system, the server **taps the hook dispatcher** — every
``pre_step`` / ``pre_tool_use`` / ``pre_commit`` / ``on_gate_result`` the engine
already dispatches becomes an SSE frame. The lifecycle hooks double as the
observability surface, which is exactly what the design intends them for.

Endpoints
---------
``GET  /health``                        liveness + whether the worker is up
``POST /models``                        seed a model with an empty IR
``GET  /models/{id}/ir``                the IR snapshot (optional ``?version=``)
``GET  /models/{id}/artifacts``         artefact file listing
``POST /chat``                          run a turn; Server-Sent Events stream
``GET  /approvals``                     pending approvals
``POST /approvals/{approval_id}``       grant or deny one
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from tcad.config.schema import Config
from tcad.core.types import TurnKind, TurnState
from tcad.ir.schema import IrDocument, RequirementSpec

# ══════════════════════════════════════════════════════════════════════════
# request models
# ══════════════════════════════════════════════════════════════════════════


class CreateModelRequest(BaseModel):
    model_id: str
    raw_requirement: str = ""
    """The user's own words. Kept on the IR so requirement provenance survives."""


class ChatRequest(BaseModel):
    model_id: str
    text: str
    thread_id: str | None = None
    kind: TurnKind = TurnKind.CREATE
    privileged_requested: bool = False


class ApprovalDecision(BaseModel):
    granted: bool


# ══════════════════════════════════════════════════════════════════════════
# the hook tap
# ══════════════════════════════════════════════════════════════════════════


class HookEventTap:
    """Wraps a hook dispatcher and records every dispatch.

    Delegates the decision unchanged — this is observation only, and must never
    alter what the safety layer decides.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.events: deque[dict] = deque(maxlen=512)

    def dispatch(self, event: Any, payload: dict) -> Any:
        result = self._inner.dispatch(event, payload)
        try:
            self.events.append(
                {
                    "event": getattr(event, "value", str(event)),
                    "decision": getattr(result.decision, "value", str(result.decision)),
                    "hook": result.hook_name,
                    "reason": result.reason,
                    "payload": _jsonable(payload),
                }
            )
        except Exception:  # noqa: BLE001 — tap failures must never affect the turn
            pass
        return result

    def drain(self) -> list[dict]:
        out = list(self.events)
        self.events.clear()
        return out


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str))


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


# ══════════════════════════════════════════════════════════════════════════
# app factory
# ══════════════════════════════════════════════════════════════════════════


def create_app(services: Any = None, *, config: Config | None = None) -> FastAPI:
    """Build the app. ``services`` is injected so tests can run without FreeCAD."""
    app = FastAPI(title="tcad", version="0.1.0")
    app.state.services = services
    app.state.config = config

    def svc() -> Any:
        if app.state.services is None:
            if app.state.config is None:
                raise HTTPException(503, "server has no services configured")
            from tcad.core.wiring import build_services

            app.state.services = build_services(app.state.config)
        return app.state.services

    # ── liveness ──────────────────────────────────────────────────────────

    @app.get("/health")
    def health() -> dict:
        out = {"status": "ok", "worker_alive": None}
        if app.state.services is not None:
            handle = getattr(app.state.services, "_worker_handle", None)
            out["worker_alive"] = bool(handle.is_alive()) if handle else None
        return out

    # ── models ────────────────────────────────────────────────────────────

    @app.post("/models")
    def create_model(req: CreateModelRequest) -> dict:
        s = svc()
        # Probe existence by loading, not by `current_version() > 0`: version 0 is
        # a perfectly valid seeded model and comparing against 0 would let a
        # second create silently overwrite it.
        try:
            s.store.load(req.model_id)
            exists = True
        except FileNotFoundError:
            exists = False
        except Exception:  # noqa: BLE001 — an unreadable model is not a free slot
            exists = True
        if exists:
            raise HTTPException(409, f"model {req.model_id!r} already exists")

        ir = IrDocument(
            model_id=req.model_id,
            version=0,
            requirements=RequirementSpec(raw_text=req.raw_requirement),
        )
        created = s.store.create(req.model_id, ir)
        return {"model_id": req.model_id, "version": int(created.version)}

    @app.get("/models/{model_id}/ir")
    def get_ir(model_id: str, version: int | None = None) -> dict:
        s = svc()
        try:
            ir = s.store.load(model_id, version)
        except FileNotFoundError as exc:
            raise HTTPException(404, f"no such model/version: {exc}") from exc
        return json.loads(ir.model_dump_json())

    @app.get("/models/{model_id}/artifacts")
    def list_artifacts(model_id: str, version: int | None = None) -> dict:
        s = svc()
        v = version if version is not None else s.store.current_version(model_id)
        d = s.store.artifact_dir(model_id, v)
        files = (
            sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())
            if d.exists()
            else []
        )
        return {"model_id": model_id, "version": v, "dir": str(d), "files": files}

    # ── the turn ──────────────────────────────────────────────────────────

    @app.post("/chat")
    async def chat(req: ChatRequest) -> StreamingResponse:
        s = svc()

        async def gen() -> AsyncIterator[str]:
            tap = HookEventTap(s.hooks)
            s.hooks = tap  # the engine reads services.hooks at call time
            yield _sse("start", {"model_id": req.model_id, "kind": req.kind.value})
            try:
                task = asyncio.create_task(run_turn_request(s, req))
                while not task.done():
                    for ev in tap.drain():
                        yield _sse("progress", ev)
                    await asyncio.sleep(0.02)
                result = await task
                for ev in tap.drain():
                    yield _sse("progress", ev)
                yield _sse("result", json.loads(result.model_dump_json()))
            except asyncio.CancelledError:  # client hung up
                raise
            except Exception as exc:  # noqa: BLE001
                yield _sse("error", {"type": type(exc).__name__, "message": str(exc)})

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── approvals ─────────────────────────────────────────────────────────

    @app.get("/approvals")
    def list_approvals() -> dict:
        s = svc()
        store = getattr(s, "approvals", None)
        if store is None:
            return {"pending": []}
        records = store._load() if hasattr(store, "_load") else []
        pending = [
            r.model_dump(mode="json")
            for r in records
            if not getattr(r, "granted", False) and getattr(r, "resolved_at", None) is None
        ]
        return {"pending": pending}

    @app.post("/approvals/{approval_id}")
    def decide_approval(approval_id: str, decision: ApprovalDecision) -> dict:
        s = svc()
        store = getattr(s, "approvals", None)
        if store is None:
            raise HTTPException(503, "no approval store configured")
        rec = store.resolve(approval_id, granted=decision.granted)
        if rec is None:
            raise HTTPException(404, f"no such approval: {approval_id}")
        return rec.model_dump(mode="json")

    return app


# ══════════════════════════════════════════════════════════════════════════
# one turn, driven by the engine
# ══════════════════════════════════════════════════════════════════════════


async def run_turn_request(services: Any, req: ChatRequest):
    """Build the engine and run a single turn. Returns a ``TurnResult``."""
    from tcad.core.types import Thread
    from tcad.loop.budget import BudgetLimits
    from tcad.loop.engine import LoopEngine, UserMessage
    from tcad.tools.base import build_default_registry

    cfg = services.config
    loop_cfg = services.loop_config
    thread = Thread(
        thread_id=req.thread_id or f"th-{req.model_id}", model_id=req.model_id
    )
    registry = build_default_registry(
        services, enable_privileged=bool(cfg.policy.allow_privileged)
    )
    limits = BudgetLimits(
        max_steps_per_turn=int(cfg.loop.max_steps_per_turn),
        max_tokens_per_turn=int(cfg.loop.max_tokens_per_turn),
        step_timeout_s=float(cfg.loop.step_timeout_s),
        turn_wall_clock_s=float(cfg.loop.turn_wall_clock_s),
        max_compile_retries=int(cfg.loop.max_compile_retries),
    )
    engine = LoopEngine(services, registry, limits, loop_cfg)
    return await engine.run_turn(
        thread,
        UserMessage(
            kind=req.kind,
            text=req.text,
            privileged_requested=req.privileged_requested,
        ),
    )


def turn_succeeded(result: Any) -> bool:
    """Convenience for clients: only a green Gate counts as done."""
    return result.state is TurnState.SUCCEEDED
