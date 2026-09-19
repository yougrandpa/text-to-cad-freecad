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
``GET  /models/{id}/artifacts/{path}``  fetch one artefact (PNG / STEP / STL)
``GET  /models/{id}/render``            render a view on demand — see below
``POST /chat``                          run a turn; Server-Sent Events stream
``GET  /approvals``                     pending approvals
``POST /approvals/{approval_id}``       grant or deny one
``GET  /threads``                       conversation list
``GET  /threads/{id}/messages``         conversation history
``GET  /settings/providers``            provider presets (never credentials)
``GET  /settings/llm``                  the settings in force (key masked)
``PUT  /settings/llm``                  change them; hot-swaps a live stack
``GET  /settings/models``               ask the provider what models it has
``POST /settings/llm/probe``            can this configuration actually talk?
``GET  /ui``                            the single-page front end

Two design notes that are easy to get wrong:

**`/render` is not the model's eye.** The harness's own view of geometry is the
`geo_view` *tool*, which is gated on declared visual checkpoints because every
image it returns occupies context tokens. A human looking at their own part is a
different activity with a different cost model, so it gets a different path: no
hook dispatch, no budget, cached on disk by (version, view, style, size).

**Settings work before the worker does.** ``/settings/*`` deliberately does not
call ``svc()``: if FreeCADCmd fails to start, being unable to change the model
configuration is exactly when you most need to. A PUT with no live stack writes
``settings.json`` and says so, and the configuration takes effect on startup.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
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


class LlmSettingsPatch(BaseModel):
    """A partial update to the LLM settings.

    The difference between "absent" and "explicitly null" is load-bearing and is
    honoured through ``model_fields_set``:

    * ``api_key`` **absent** — keep the stored credential (the common case: the
      UI never has the plaintext to send back);
    * ``api_key: null`` — deliberately clear it;
    * ``api_key: "..."`` — set it.

    Same rule for ``base_url`` / ``model`` / ``context_window``: absent means
    "leave alone", null-or-empty means "inherit from the preset".
    """

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    temperature: float | None = None
    max_tokens_per_step: int | None = None
    request_timeout_s: float | None = None
    max_retries: int | None = None
    use_env_proxy: bool | None = None
    context_window: int | None = None


# Fields that should fall back to the newly-chosen preset when the provider
# changes. Without this, switching deepseek -> ollama would keep DeepSeek's
# base_url and quietly send the request to the wrong company.
_PRESET_INHERITED = ("model", "base_url", "api_key_env", "context_window")


def _merge_llm(current: Any, patch: LlmSettingsPatch):
    from pydantic import ValidationError

    from tcad.config.settings import LlmSettings

    data = current.model_dump()
    provided = patch.model_fields_set
    for key in provided:
        data[key] = getattr(patch, key)

    if "provider" in provided and patch.provider != current.provider:
        for key in _PRESET_INHERITED:
            if key not in provided:
                data[key] = "" if key != "context_window" else None

    try:
        return LlmSettings.model_validate(data)
    except ValidationError as exc:
        # A bad value from the client is a 4xx, not a 500: the field validators
        # on LlmSettings (temperature range, token count) are the contract.
        raise HTTPException(422, f"配置无效：{exc}") from exc


_ALLOWED_VIEWS = {"iso", "front", "top", "right"}


def _resolve_artifact(root: Path, file_path: str) -> Path:
    """Resolve ``file_path`` to a real file inside ``root``, or refuse.

    A Starlette ``{file_path:path}`` converter accepts ``../../etc/passwd``
    verbatim, so containment has to be enforced here. The check is performed on
    the **resolved** path — after ``..`` and symlinks — because comparing the
    literal string is the version that looks right and is not.
    """
    resolved_root = Path(root).resolve()
    target = (resolved_root / file_path).resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise HTTPException(400, "path escapes the artifact directory")
    if not target.is_file():
        raise HTTPException(404, f"no such artifact: {file_path}")
    return target


def artifact_url_for(path: str | None) -> str | None:
    """Map an on-disk artefact path to the URL that serves it.

    The engine and the worker report absolute filesystem paths — that is what
    they have. A browser cannot use those, so anything headed for the client
    needs the mechanical translation: ``<...>/artifacts/<model>/v<n>/<file>``
    becomes ``/models/<model>/artifacts/<file>?version=<n>``.

    Returns ``None`` rather than guessing when the path does not have that
    shape; a wrong link is worse than no link.
    """
    if not path:
        return None
    marker = "/artifacts/"
    index = path.find(marker)
    if index < 0:
        return None
    parts = path[index + len(marker):].split("/")
    if len(parts) < 3:
        return None
    model_id, version_dir, *rest = parts
    filename = "/".join(rest)
    version = version_dir[1:] if version_dir.startswith("v") else ""
    query = f"?version={version}" if version.isdigit() else ""
    return f"/models/{model_id}/artifacts/{filename}{query}"


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
    app.state.session_db = None

    def svc() -> Any:
        if app.state.services is None:
            if app.state.config is None:
                raise HTTPException(503, "server has no services configured")
            from tcad.core.wiring import build_services

            app.state.services = build_services(app.state.config)
        return app.state.services

    def cfg() -> Config:
        """The configuration in force — **without** building a service stack.

        The settings endpoints must keep working when FreeCADCmd will not start;
        "I cannot reach the model provider *and* I cannot change which provider
        I am pointing at" is a trap with no way out.
        """
        if app.state.services is not None:
            return app.state.services.config
        if app.state.config is None:
            from tcad.config.loader import REPO_ROOT, load_default_config, resolve_paths

            app.state.config = resolve_paths(load_default_config(), root=REPO_ROOT)
        return app.state.config

    def db():
        """Conversation store.

        Opened with ``check_same_thread=False`` on purpose: uvicorn runs the sync
        endpoints on a worker-thread pool, not on the event-loop thread, so a
        thread-bound connection would raise the moment a second request arrived.
        """
        if app.state.session_db is None:
            from tcad.store.session_db import SessionDB

            app.state.session_db = SessionDB(
                cfg().storage.sqlite_file(), check_same_thread=False
            )
        return app.state.session_db

    def current_llm():
        """The LLM settings actually in force.

        A live stack is authoritative: it holds the ``RuntimeSettings`` that
        ``apply_llm_settings`` installed, including the provider. Rebuilding them
        from ``Config`` instead would report a provider *guessed from base_url*,
        which can differ from the one in use — the UI would then show one
        provider while requests went to another.
        """
        from tcad.config.settings import SettingsError, effective

        if app.state.services is not None:
            live = getattr(app.state.services, "settings", None)
            if live is not None:
                return live

        c = cfg()
        try:
            return effective(c, c.storage.data_dir)
        except SettingsError as exc:
            raise HTTPException(500, f"设置文件损坏：{exc}") from exc

    # ── liveness ──────────────────────────────────────────────────────────

    @app.get("/health")
    def health() -> dict:
        """Liveness, plus **which** instance this is.

        ``data_dir`` is here for a reason that cost real confusion: two servers
        can run on different ports with different data directories, and nothing
        in the UI distinguished them. Seeing the wrong model in the header then
        looks like "my configuration was lost" when it is really "this is a
        different server". Reporting the directory makes the instance
        identifiable at a glance.

        ``budget`` reports which work ceilings are in force. A turn with none set
        can run for a long time; that is a deliberate choice, but it should be
        visible rather than something you infer from a turn that never ends.
        """
        out: dict = {"status": "ok", "worker_alive": None, "data_dir": None, "budget": None}
        try:
            c = cfg()
            out["data_dir"] = str(c.storage.data_dir)
            from tcad.loop.budget import LIMIT_NAMES

            limits = {n: getattr(c.loop, n) for n in LIMIT_NAMES}
            out["budget"] = {
                "limits": limits,
                "unbounded": [n for n, v in limits.items() if v is None],
                "unbounded_all": all(v is None for v in limits.values()),
            }
        except Exception:  # noqa: BLE001 — liveness must not depend on this
            pass
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
        try:
            # One `load()` for both the existence check and the version — see the
            # note on `StoreAdapter.current_version`.
            ir = s.store.load(model_id, version)
        except FileNotFoundError as exc:
            raise HTTPException(404, f"no such model: {model_id}") from exc
        v = int(ir.version)
        d = s.store.artifact_dir(model_id, v)
        files = (
            sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())
            if d.exists()
            else []
        )
        return {"model_id": model_id, "version": v, "dir": str(d), "files": files}

    @app.get("/models/{model_id}/artifacts/{file_path:path}")
    def get_artifact(
        model_id: str, file_path: str, version: int | None = None
    ) -> FileResponse:
        """Serve one artefact (PNG / STEP / STL / BREP).

        The containment check is the point of this endpoint: ``{file_path:path}``
        happily accepts ``../../etc/passwd``, so the resolved target is verified
        to sit inside the artefact directory before anything is opened.
        """
        s = svc()
        try:
            ir = s.store.load(model_id, version)
        except FileNotFoundError as exc:
            raise HTTPException(404, f"no such model: {model_id}") from exc
        root = s.store.artifact_dir(model_id, int(ir.version))
        return FileResponse(_resolve_artifact(root, file_path))

    @app.get("/models/{model_id}/render")
    def render_view(
        model_id: str,
        view: str = "iso",
        style: str | None = None,
        width: int | None = None,
        height: int | None = None,
        version: int | None = None,
        force: bool = False,
    ) -> FileResponse:
        """Render one orthographic view of the current geometry.

        This is the *human's* view path, deliberately separate from the model's
        ``geo_view`` tool: no hook dispatch, no token budget, no checkpoint
        requirement. Results are cached on disk keyed by
        ``(version, view, style, size)``, so reloading a page costs nothing.
        """
        from tcad.core.types import Mesh
        from tcad.worker.protocol import M_TESSELLATE

        if view not in _ALLOWED_VIEWS:
            raise HTTPException(
                400, f"unknown view {view!r}; expected one of {sorted(_ALLOWED_VIEWS)}"
            )
        s = svc()
        c = s.config
        try:
            # Load once and take the version from the document. Asking
            # `current_version()` first is a trap: it returns 0 both for "no such
            # model" and for "a model that happens to be at v0", which is exactly
            # what a freshly created one is.
            ir = s.store.load(model_id, version)   # version=None => latest
            v = int(ir.version)
        except FileNotFoundError as exc:
            # A model that does not exist yet is a 404, not a 500 — the UI keys
            # its "no model yet" message off this status.
            raise HTTPException(404, f"no such model: {model_id}") from exc
        style = style or c.context.render.style
        w = int(width or c.context.render.width)
        h = int(height or c.context.render.height)
        if not (16 <= w <= 4096 and 16 <= h <= 4096):
            raise HTTPException(400, "width/height must be within [16, 4096]")

        d = s.store.artifact_dir(model_id, v)
        d.mkdir(parents=True, exist_ok=True)
        cached = d / f"view-{view}-{style}-{w}x{h}.png"
        if cached.is_file() and not force:
            return FileResponse(cached, media_type="image/png")

        res = s.worker.request(
            M_TESSELLATE,
            {"ir": ir.model_dump(), "out_dir": str(d)},
            timeout_s=180.0,
        )
        if not res.get("ok"):
            err = res.get("error") or {}
            raise HTTPException(
                422,
                f"无法网格化模型：{err.get('message', 'tessellate failed')}",
            )
        mesh_data = (res.get("result") or {}).get("mesh")
        if not mesh_data:
            raise HTTPException(422, "worker 未返回网格")
        mesh = Mesh.model_validate(mesh_data)
        images = s.renderer.render(
            mesh, out_dir=str(d), views=[view], style=style, width=w, height=h
        )
        if not images:
            raise HTTPException(500, "渲染未产生图像")
        produced = Path(images[0].path)
        if produced != cached and produced.is_file():
            # normalise the name so the cache key is stable across strategies
            try:
                produced.replace(cached)
            except OSError:
                pass
        target = cached if cached.is_file() else produced
        return FileResponse(target, media_type="image/png")

    # ── the turn ──────────────────────────────────────────────────────────

    @app.post("/chat")
    async def chat(req: ChatRequest) -> StreamingResponse:
        s = svc()
        thread_id = req.thread_id or f"th-{req.model_id}"

        def remember_user() -> None:
            try:
                d = db()
                if d.get_thread(thread_id) is None:
                    d.create_thread(req.model_id, thread_id=thread_id)
                d.add_message(thread_id, "user", req.text)
            except Exception:  # noqa: BLE001 — history is a convenience, not a guarantee
                pass

        async def gen() -> AsyncIterator[str]:
            original_hooks = s.hooks
            tap = HookEventTap(original_hooks)
            s.hooks = tap  # the engine reads services.hooks at call time

            agent_events: deque[dict] = deque()

            def observe(kind: str, data: dict) -> None:
                """Called synchronously by the engine as it progresses.

                Everything here is best-effort: a viewer must never be able to
                affect a turn, and the engine already swallows anything this
                raises.
                """
                try:
                    if kind == "tool":
                        # Images arrive as absolute paths. Attach the URL the
                        # browser can actually fetch, so the viewport can update
                        # *while* the turn is running instead of only at the end.
                        for image in data.get("images") or []:
                            image["url"] = artifact_url_for(image.get("path"))
                    agent_events.append({"kind": kind, **data})
                    if kind == "model" and (data.get("text") or "").strip():
                        db().add_message(thread_id, "assistant", data["text"])
                except Exception:  # noqa: BLE001
                    pass

            task: asyncio.Task | None = None
            try:
                remember_user()
                # Inside the try on purpose. A client that disconnects between
                # the `start` frame and the first progress frame raises
                # GeneratorExit right here, and a generator closed before its
                # `try` is entered never runs its `finally` — which is how the
                # turn task below would leak.
                yield _sse(
                    "start",
                    {"model_id": req.model_id, "kind": req.kind.value, "thread_id": thread_id},
                )
                task = asyncio.create_task(run_turn_request(s, req, observer=observe))
                while not task.done():
                    for ev in tap.drain():
                        yield _sse("progress", ev)
                    while agent_events:
                        yield _sse("agent", agent_events.popleft())
                    await asyncio.sleep(0.02)
                result = await task
                for ev in tap.drain():
                    yield _sse("progress", ev)
                while agent_events:
                    yield _sse("agent", agent_events.popleft())
                yield _sse("result", json.loads(result.model_dump_json()))
            except asyncio.CancelledError:  # client hung up
                raise
            except Exception as exc:  # noqa: BLE001
                yield _sse("error", {"type": type(exc).__name__, "message": str(exc)})
            finally:
                # A turn must not outlive the client that asked for it.
                #
                # This mattered less when the loop had a step ceiling: a turn
                # orphaned by a closed tab would hit `max_steps_per_turn` and
                # die on its own. With no work ceiling it simply keeps running —
                # spending tokens and holding the worker — for as long as nobody
                # stops it, and nothing else here would.
                if task is not None and not task.done():
                    task.cancel()
                # Unwrap the tap. Assigning it permanently would nest one tap per
                # request, so a long-lived server would accumulate them.
                s.hooks = original_hooks

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

    # ── settings ──────────────────────────────────────────────────────────

    @app.get("/settings/providers")
    def list_provider_presets() -> dict:
        from tcad.config.providers import public_providers

        return {"providers": public_providers()}

    @app.get("/settings/llm")
    def read_llm_settings() -> dict:
        from tcad.config.settings import settings_path

        c = cfg()
        rs = current_llm()
        path = settings_path(c.storage.data_dir)
        return {
            "settings": rs.llm.masked(),
            "persisted": path.exists(),
            "settings_file": str(path),
            "hot_swap_available": app.state.services is not None,
        }

    @app.put("/settings/llm")
    def write_llm_settings(patch: LlmSettingsPatch) -> dict:
        from tcad.config.settings import RuntimeSettings, apply_to_config, save_runtime_settings

        c = cfg()
        new_llm = _merge_llm(current_llm().llm, patch)
        new_settings = RuntimeSettings(llm=new_llm)

        if app.state.services is None:
            # No live stack to reconfigure. Persist and report honestly rather
            # than failing: this is precisely the situation (a worker that will
            # not start) where the operator most needs to change the settings.
            save_runtime_settings(c.storage.data_dir, new_settings)
            app.state.config = apply_to_config(c, new_settings)
            return {
                "applied": "persisted",
                "settings": new_llm.masked(),
                "note": "服务栈尚未构建；配置已保存，将在启动时生效",
            }

        from tcad.core.wiring import apply_llm_settings

        try:
            descriptor = apply_llm_settings(app.state.services, new_settings)
        except Exception as exc:  # noqa: BLE001 — bad input, not a server fault
            raise HTTPException(400, f"配置无法应用：{exc}") from exc
        return {
            "applied": "hot-swapped",
            "descriptor": descriptor,
            "settings": new_llm.masked(),
        }

    @app.post("/settings/llm/probe")
    async def probe_llm_settings(patch: LlmSettingsPatch | None = None) -> dict:
        """Ask whether this configuration can actually reach a model.

        Accepts an optional patch so the UI can test *before* committing — a
        probe that required saving first would leave a broken configuration in
        the file every time someone mistyped a key.
        """
        from tcad.llm.hotswap import probe_llm

        llm = current_llm().llm
        if patch is not None:
            llm = _merge_llm(llm, patch)
        return (await probe_llm(llm)).model_dump(mode="json")

    @app.get("/settings/models")
    async def list_provider_models(provider: str | None = None, live: bool = True) -> dict:
        """The provider's own model list, with the preset's candidates as fallback.

        Model names go stale (see ``tcad/config/providers.py``); asking the
        provider is the only way to be right.
        """
        from tcad.config.providers import get_provider
        from tcad.llm.hotswap import probe_llm

        llm = current_llm().llm
        if provider and provider != llm.provider:
            llm = _merge_llm(llm, LlmSettingsPatch(provider=provider))
        preset = get_provider(llm.provider)
        fallback = list(preset.models) if preset else []
        if not live:
            return {"provider": llm.provider, "models": fallback, "source": "preset"}

        result = await probe_llm(llm, timeout_s=12.0)
        return {
            "provider": llm.provider,
            "models": result.models or fallback,
            "source": "live" if result.models else "preset",
            "ok": result.ok,
            "error": result.error,
            "latency_ms": result.latency_ms,
        }

    # ── conversations ─────────────────────────────────────────────────────

    @app.get("/threads")
    def list_threads(model_id: str | None = None, limit: int = 100) -> dict:
        return {"threads": db().list_threads(model_id, limit)}

    @app.get("/threads/{thread_id}/messages")
    def list_thread_messages(thread_id: str, limit: int = 500) -> dict:
        return {"thread_id": thread_id, "messages": db().list_messages(thread_id, limit)}

    # ── front end ─────────────────────────────────────────────────────────

    # Mounted only when the files are present, so the API stays usable (and the
    # test suite stays runnable) without them.
    ui_dir = Path(__file__).resolve().parent / "ui"
    if ui_dir.is_dir():
        from fastapi.responses import RedirectResponse
        from fastapi.staticfiles import StaticFiles

        app.mount("/ui", StaticFiles(directory=str(ui_dir), html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def index_redirect() -> Any:
            return RedirectResponse("/ui/")

    return app


# ══════════════════════════════════════════════════════════════════════════
# one turn, driven by the engine
# ══════════════════════════════════════════════════════════════════════════


async def run_turn_request(services: Any, req: ChatRequest, observer: Any = None):
    """Build the engine and run a single turn. Returns a ``TurnResult``.

    ``observer`` is forwarded to the engine so a front end can see the model's
    text and tool calls as they happen (``(kind, data) -> None``).
    """
    from tcad.core.types import Thread
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
    limits = budget_limits_from_config(cfg)
    engine = LoopEngine(services, registry, limits, loop_cfg, observer=observer)
    return await engine.run_turn(
        thread, UserMessage(kind=req.kind, text=req.text, privileged_requested=req.privileged_requested)
    )


def budget_limits_from_config(cfg: Config):
    """Map the configured loop ceilings onto ``BudgetLimits``.

    Extracted so the ``None`` handling is directly testable. Every ceiling is
    optional and ``None`` means "no cap", so the values must be passed through
    verbatim — the previous ``int(...)``/``float(...)`` coercion raised
    ``TypeError`` on ``None``, which is the shipped default.
    """
    from tcad.loop.budget import BudgetLimits

    return BudgetLimits(
        max_steps_per_turn=cfg.loop.max_steps_per_turn,
        max_tokens_per_turn=cfg.loop.max_tokens_per_turn,
        step_timeout_s=cfg.loop.step_timeout_s,
        turn_wall_clock_s=cfg.loop.turn_wall_clock_s,
        max_compile_retries=cfg.loop.max_compile_retries,
    )


def turn_succeeded(result: Any) -> bool:
    """Convenience for clients: only a green Gate counts as done."""
    return result.state is TurnState.SUCCEEDED
