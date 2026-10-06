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
``GET  /models/{id}/mesh``              bounded, versioned mesh for the 3D viewport
``POST /chat``                          run a turn; Server-Sent Events stream
``POST /chat/interrupt``                stop the turn named by ``request_id``
``GET  /approvals``                     pending approvals
``POST /approvals/{approval_id}``       grant or deny one
``GET  /threads``                       conversation list
``GET  /threads/{id}/messages``         conversation history
``GET  /sessions``                      conversations + the part each one built
``POST /sessions``                      start one: a thread and its model together
``GET  /settings/providers``            provider presets (never credentials)
``GET  /settings/llm``                  the settings in force (key masked)
``PUT  /settings/llm``                  change them; hot-swaps a live stack
``GET  /settings/models``               ask the provider what models it has
``POST /settings/llm/probe``            can this configuration actually talk?
``GET  /ui``                            the single-page front end

Three design notes that are easy to get wrong:

**A session is a conversation *and* the one part it builds.** They are created
together and the binding never changes: a conversation's whole history is about
the part it was building, so repointing it at another part would make its own
transcript a lie. ``/sessions`` therefore reports the model and its IR version
alongside the title — a client switching sessions has to switch the viewport,
artefacts and inspector too, not just the transcript.

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
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator, get_args

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from tcad.core.access import AccessMode

from tcad.config.schema import Config
from tcad.core.types import RenderStyle, TurnKind, TurnState
from tcad.ir.schema import IrDocument, RequirementSpec
from tcad.selection.types import SelectionContext, SelectionError

# ══════════════════════════════════════════════════════════════════════════
# request models
# ══════════════════════════════════════════════════════════════════════════


def _safe_id_field(kind: str):
    """A pydantic validator that rejects an id which must not become a path.

    Done at the request model so a bad id is a 422 with a readable message,
    instead of a 500 from deep inside the store — and so the same rule is applied
    to every endpoint that takes an id, including the ones added later. ``None``
    passes through (optional ids are optional, not valid).
    """

    def _check(value):
        if value is None:
            return None
        from tcad.core.ids import InvalidIdentifier, ensure_safe_id

        try:
            return ensure_safe_id(value, kind=kind)
        except InvalidIdentifier as exc:
            raise ValueError(str(exc)) from exc

    return _check


class CreateModelRequest(BaseModel):
    model_id: str
    raw_requirement: str = ""
    """The user's own words. Kept on the IR so requirement provenance survives."""

    _validate_model_id = field_validator("model_id")(_safe_id_field("model_id"))


class CreateSessionRequest(BaseModel):
    """Start a conversation. Everything is optional.

    ``model_id`` exists for tests and for a caller that wants a predictable
    handle; the normal case lets the server mint one so two clicks of "new"
    cannot collide.
    """

    model_id: str | None = None
    raw_requirement: str = ""

    _validate_model_id = field_validator("model_id")(_safe_id_field("model_id"))


class ChatRequest(BaseModel):
    model_id: str
    text: str
    thread_id: str | None = None
    selection_context: SelectionContext | None = None
    operation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    @model_validator(mode="after")
    def require_operation_id(self):
        if self.selection_context is not None and self.operation_id is None:
            raise ValueError("Reference requests require operation_id for safe retries")
        return self

    _validate_model_id = field_validator("model_id")(_safe_id_field("model_id"))
    _validate_thread_id = field_validator("thread_id")(_safe_id_field("thread_id"))
    kind: TurnKind = TurnKind.CREATE
    privileged_requested: bool = False
    access_mode: AccessMode = AccessMode.AUTO
    request_id: str | None = None
    """The client's name for *this* turn, used to address it later.

    Minted by the client rather than the server for one reason: a stop can then
    name the turn before any frame has come back. A server-minted id leaves a
    window in which the client knows a turn is running and the server has not
    yet learned what to call it — exactly the window in which "stop" would fail
    while the model kept generating. Omitted by callers that do not need it; the
    server mints one then.
    """


class InterruptRequest(BaseModel):
    """Stop the running turn with this ``request_id``.

    Named by turn, not by thread: a thread can have a turn starting up, and
    "stop whatever is running in this conversation" is ambiguous precisely when
    it matters. The id the client minted is the one thing that identifies the
    turn it is looking at.
    """

    request_id: str


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
    supports_vision: bool | None = None


# Fields that should fall back to the newly-chosen preset when the provider
# changes. Without this, switching deepseek -> ollama would keep DeepSeek's
# base_url and quietly send the request to the wrong company.
_PRESET_INHERITED = ("model", "base_url", "api_key_env", "context_window", "supports_vision")


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
                data[key] = None if key in {"context_window", "supports_vision"} else ""

    try:
        return LlmSettings.model_validate(data)
    except ValidationError as exc:
        # A bad value from the client is a 4xx, not a 500: the field validators
        # on LlmSettings (temperature range, token count) are the contract.
        raise HTTPException(422, f"配置无效：{exc}") from exc


_ALLOWED_VIEWS = {"iso", "front", "top", "right"}

#: Render styles that may appear in the ``/render`` cache key. Derived from the
#: ``RenderStyle`` literal so the two cannot drift; a style that is not rendered
#: by the rasteriser must not reach a filename either.
#
# ``style`` used to be validated by nothing: it was interpolated straight into
# ``view-{view}-{style}-{w}x{h}.png``. ``view`` has an allowlist, so the
# traversal reads as "handled" — but ``style`` walked past it, and
# ``style=../../../../tmp/x`` resolves to ``<data>/artifacts/tmp/x-800x600.png``,
# outside the version directory the cache lives in. The same value is the file
# the endpoint *serves* when it already exists and the file ``produced.replace()``
# moves the render onto, so it was both a read and a write outside the boundary.
_ALLOWED_STYLES = frozenset(get_args(RenderStyle))


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


def _model_exists(store: Any, model_id: str) -> bool:
    """Whether this model already exists, for whoever is about to create one.

    Extracted because two endpoints need the same answer and must not drift:
    ``POST /models`` and ``POST /sessions`` both seed a model, and seeding over an
    existing one **silently resets it to v0** — the part is gone, with no error
    anywhere. One implementation, one meaning of "exists".

    Prefers ``exists()`` when the store offers it (cheap, no JSON parsing). The
    ``load()`` fallback keeps a store that only implements the older surface
    working, and treats *unreadable* as existing: a corrupt model is not a free
    slot, and reporting it as one would overwrite whatever is salvageable.
    """
    probe = getattr(store, "exists", None)
    if callable(probe):
        return bool(probe(model_id))
    try:
        store.load(model_id)
        return True
    except FileNotFoundError:
        return False
    except Exception:  # noqa: BLE001 — an unreadable model is not a free slot
        return True


def _verdict_or_none(store: Any, model_id: str, version: int | None) -> dict | None:
    """The verdict, or ``None`` when the store cannot produce one.

    Best-effort on purpose: a listing that raises because one session's model is
    unreadable is worse than a listing that says "unknown" for that row.
    """
    fn = getattr(store, "verdict", None)
    if not callable(fn) or version is None:
        return None
    try:
        return fn(model_id, version)
    except Exception:  # noqa: BLE001
        return None


def _verified_or_none(store: Any, model_id: str, version: int | None) -> bool | None:
    v = _verdict_or_none(store, model_id, version)
    return None if v is None else bool(v.get("verified"))


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
    for category in ("exports", "animations"):
        marker = f"/derived/{category}/"
        if marker in path:
            return f"/derived/{category}/" + path.split(marker, 1)[1]
    object_marker = "/artifact_sets/"
    if object_marker in path:
        parts = path.split(object_marker, 1)[1].split("/")
        if len(parts) == 2:
            return f"/artifact-sets/sha256:{parts[0]}/files/{parts[1]}"
    snapshot_marker = "/derived/snapshots/"
    if snapshot_marker in path:
        parts = path.split(snapshot_marker, 1)[1].split("/")
        if len(parts) == 4:
            digest, model_id, key, filename = parts
            return f"/artifact-sets/sha256:{digest}/snapshots/{model_id}/{key}/{filename}"
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
        return self._record(event, payload, result)

    def dispatch_with_access(self, event, payload, mode):
        from tcad.hooks.access import AccessHooks
        result = AccessHooks(self._inner, mode).dispatch(event, payload)
        return self._record(event, {**payload, "access_mode": mode.value}, result)

    def _record(self, event, payload, result):
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


# ══════════════════════════════════════════════════════════════════════════
# stopping a running turn
# ══════════════════════════════════════════════════════════════════════════

#: How many "stop this turn" requests to remember that named a turn which had
#: not registered yet. Bounded because the ids come from callers: an unbounded
#: map keyed by client input is a slow leak. 64 is far more than the one-strided
#: race this exists for, and entries are consumed (or evicted) within a request.
_PENDING_STOP_LIMIT = 64

#: How long such a remembered stop stays valid.
#:
#: It exists for the race between "the client pressed stop" and "the server
#: registered the turn", which is milliseconds wide. Holding it forever would be
#: worse than dropping it: a stop aimed at a turn that never appeared would sit
#: there waiting to stop some *unrelated* later turn that happened to reuse the
#: id — the caller would get an abort it never asked for, with no way to see why.
#: (Observed for real: a probe that reused its request id across runs had its
#: second run stopped before the first step.) 30s is orders of magnitude more
#: than the race needs and still expires long before a reused id could show up.
_PENDING_STOP_TTL_S = 30.0


class RunningTurn:
    """A turn that is running right now, so another request can stop it.

    Two facts travel together here and must not be conflated:

    * ``stopped`` is the **reason** — somebody asked for this turn to end. The
      engine reads it through a predicate, so a stop that arrives between two
      steps, or before the very first one, is still turned into an honest
      ``ABORTED`` outcome instead of being missed;
    * ``task.cancel()`` is the **mechanism** — it is what actually interrupts
      the await in flight. Nearly all of a turn's wall-clock time is one LLM
      HTTP request, so without the cancellation "stop" would only take effect
      at the next step boundary, which can be a full model timeout away.

    ``request_stop`` is idempotent: the UI may deliver a click and a keyboard
    shortcut for the same turn, and a second cancel on a finished task is a
    no-op rather than an error.
    """

    def __init__(self, request_id: str, thread_id: str, model_id: str | None = None) -> None:
        self.request_id = request_id
        self.thread_id = thread_id
        self.model_id = model_id
        self.task: asyncio.Task | None = None
        self.stopped = False

    def is_stop_requested(self) -> bool:
        return self.stopped

    def request_stop(self) -> None:
        self.stopped = True
        if self.task is not None and not self.task.done():
            self.task.cancel()


def _take_pending_stop(
    pending: dict[str, float], request_id: str, *, now: float | None = None
) -> bool:
    """Consume a stop that arrived before its turn existed. False if none did.

    A recorded-but-stale entry answers False as well: it is consumed either way
    (so nothing accumulates), but a stop aimed at a turn that never showed up
    must not arm a later, unrelated one.
    """
    deadline = pending.pop(request_id, None)
    if deadline is None:
        return False
    return (time.monotonic() if now is None else now) < deadline


def _remember_pending_stop(pending: dict[str, float], request_id: str) -> None:
    """Record a stop for a turn that has not registered yet (bounded, expiring)."""
    if request_id not in pending and len(pending) >= _PENDING_STOP_LIMIT:
        oldest = min(pending, key=lambda key: pending[key])
        pending.pop(oldest, None)
    pending[request_id] = time.monotonic() + _PENDING_STOP_TTL_S


def aborted_turn_result(turn_id: str, thread_id: str, model_id: str) -> Any:
    """The result of a turn stopped before the engine could report one itself.

    Only reachable when the cancellation lands before ``run_turn`` has entered
    its own body — everything later is converted into an ``ABORTED`` result by
    the engine, which knows the real step and token counts. Reporting zeroes
    here is a statement about what the server actually knows, not a guess, and
    it still gives the client the one thing that must never be missing: a
    terminal verdict that says the turn was stopped and was not verified.
    """
    from tcad.loop.engine import STOPPED_BY_USER, TurnResult

    return TurnResult(
        turn_id=turn_id,
        thread_id=thread_id,
        model_id=model_id,
        state=TurnState.ABORTED,
        error=STOPPED_BY_USER,
    )


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
    app.state.light_store = None
    #: request_id -> RunningTurn for every turn currently in flight. The client
    #: mints the id, so a stop can address a turn the server has not finished
    #: learning about; see `_take_pending_stop` for the other half of that race.
    app.state.turns = {}
    app.state.pending_stops = {}
    from tcad.server.mesh import (
        MAX_ACTIVE_MESH_REQUESTS, MAX_TOLERANCE, MIN_TOLERANCE,
        MeshPreviewCache, MeshPreviewResponse,
    )

    app.state.mesh_cache = MeshPreviewCache()
    app.state.feature_highlight_cache = MeshPreviewCache(max_entries=32, max_bytes=8 * 1024 * 1024)
    app.state.mesh_preview_jobs = set()

    def svc() -> Any:
        if app.state.services is None:
            if app.state.config is None:
                raise HTTPException(503, "server has no services configured")
            from tcad.core.wiring import build_services

            app.state.services = build_services(app.state.config)
        # Attach the conversation store so a turn can replay prior messages into
        # the model's context (tcad.context.history). Best-effort: a stack whose
        # config cannot open a session DB still serves geometry, it just cannot
        # remember the conversation.
        try:
            app.state.services.session_db = db()
        except Exception:  # noqa: BLE001
            pass
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

    def session_store():
        """The store used for read-only session listing.

        Prefers a live stack when one exists — it is authoritative, and it is
        what an injected store (tests, embedding) replaces. Only when nothing has
        been built yet does this fall back to a bare store, so that listing
        sessions does not start FreeCAD just to read version numbers.
        """
        if app.state.services is not None:
            return app.state.services.store
        if app.state.light_store is None:
            from tcad.core.wiring import StoreAdapter

            app.state.light_store = StoreAdapter(cfg().storage.data_dir)
        return app.state.light_store

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
            out["selection_enabled"] = c.selection.enabled
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

    @app.get("/models/{model_id}/selection-targets")
    def reference_catalog(model_id: str) -> dict:
        from tcad.selection.resolve import SelectionResolver
        if not cfg().selection.enabled:
            raise HTTPException(403, {"code": "selection_disabled", "message": "Feature references are disabled."})
        try:
            return SelectionResolver(cfg().storage.data_dir, session_store()).catalog(model_id)
        except SelectionError as exc:
            raise HTTPException(exc.status, exc.detail()) from exc

    @app.get("/artifact-sets/{artifact_id}")
    def get_artifact_set(artifact_id: str, model_id: str) -> dict:
        """Pinned build metadata; independent of the current source version."""
        from tcad.inspect.artifact import ArtifactReader

        reader = ArtifactReader(cfg().storage.data_dir)
        try:
            manifest, root = reader.resolve(model_id, artifact_id=artifact_id)
            report = reader.gate_report(manifest, root)
            return {**manifest.model_dump(mode="json"), "gate_report": report.model_dump(mode="json")}
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/artifact-sets/{artifact_id}/measurements")
    def get_artifact_measurements(artifact_id: str, model_id: str) -> dict:
        from tcad.inspect.artifact import ArtifactReader

        reader = ArtifactReader(cfg().storage.data_dir)
        try:
            manifest, root = reader.resolve(model_id, artifact_id=artifact_id)
            return {"artifact_id": manifest.artifact_id, "status": manifest.status.value,
                    "model_id": manifest.model_id, "ir_version": manifest.ir_version,
                    "digest": reader.digest(manifest, root).model_dump(mode="json")}
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/artifact-sets/{artifact_id}/scene", response_model=MeshPreviewResponse)
    def get_artifact_scene(artifact_id: str, model_id: str) -> Response:
        from tcad.server.mesh import mesh_preview
        return mesh_preview(cfg().storage.data_dir, app.state.mesh_cache, model_id,
                            version=None, tolerance=0.5, force=False, artifact_id=artifact_id)

    @app.get("/artifact-sets/{artifact_id}/highlight")
    async def get_feature_highlight(artifact_id: str, model_id: str, body_id: str, kind: str, node_id: str) -> Response:
        from tcad.server.feature_highlight import feature_highlight
        jobs = app.state.mesh_preview_jobs
        if len(jobs) >= MAX_ACTIVE_MESH_REQUESTS:
            raise HTTPException(429, "selection preview is busy; retry shortly", headers={"Retry-After": "1"})
        job = asyncio.create_task(run_in_threadpool(feature_highlight, cfg().storage.data_dir,
            app.state.feature_highlight_cache, svc().worker, model_id, artifact_id, body_id, kind, node_id))
        jobs.add(job)

        def release(finished: asyncio.Task) -> None:
            jobs.discard(finished)
            if not finished.cancelled():
                finished.exception()

        job.add_done_callback(release)
        return await asyncio.shield(job)

    @app.get("/artifact-sets/{artifact_id}/inspect/{kind}")
    def inspect_artifact(artifact_id: str, kind: str, model_id: str) -> dict:
        from tcad.inspect.artifact import ArtifactReader
        from tcad.inspect.cache import InspectionCache
        try:
            reader = ArtifactReader(cfg().storage.data_dir)
            manifest, root = reader.resolve(model_id, artifact_id=artifact_id)
            return {"artifact_id": manifest.artifact_id, "status": manifest.status.value,
                    "data": InspectionCache(reader.data_dir).query(reader, manifest, root, kind)}
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/artifact-sets/{artifact_id}/files/{filename}")
    def get_artifact_file(artifact_id: str, filename: str) -> FileResponse:
        from tcad.inspect.artifact import ArtifactReader
        from tcad.artifacts.manifest import ArtifactSet
        try:
            reader = ArtifactReader(cfg().storage.data_dir)
            manifest = ArtifactSet.model_validate_json((reader.object_dir(artifact_id) / "manifest.json").read_bytes())
            manifest, root = reader.resolve(manifest.model_id, artifact_id=artifact_id)
            reader.read_file(manifest, root, filename)
            return FileResponse(_resolve_artifact(root, filename), headers={
                "X-Artifact-ID": artifact_id, "X-Artifact-Status": manifest.status.value})
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/derived/{category}/{file_path:path}")
    def get_derived_file(category: str, file_path: str) -> FileResponse:
        if category not in {"exports", "animations"}:
            raise HTTPException(404, "no such derived artifact")
        return FileResponse(_resolve_artifact(Path(cfg().storage.data_dir) / "derived" / category, file_path))

    @app.get("/build-jobs")
    def list_build_jobs(model_id: str | None = None) -> dict:
        from tcad.build.jobs import BuildJob
        root = Path(cfg().storage.data_dir) / "build_jobs"
        jobs = []
        for path in root.glob("job-*.json"):
            try:
                job = BuildJob.model_validate_json(path.read_bytes())
            except (OSError, ValueError):
                continue
            if model_id is None or job.model_id == model_id:
                jobs.append(job.model_dump(mode="json"))
        return {"jobs": sorted(jobs, key=lambda j: j["created_at"], reverse=True)[:100]}

    @app.get("/build-jobs/{job_id}")
    def get_build_job(job_id: str) -> dict:
        from tcad.build.jobs import BuildJob
        from tcad.core.ids import contained_path, ensure_safe_id
        try:
            path = contained_path(cfg().storage.data_dir, "build_jobs", ensure_safe_id(job_id, kind="job_id") + ".json")
            return BuildJob.model_validate_json(path.read_bytes()).model_dump(mode="json")
        except FileNotFoundError as exc:
            raise HTTPException(404, "no such build job") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/build-jobs/{job_id}/cancel")
    def cancel_build_job(job_id: str) -> dict:
        get_build_job(job_id)  # validate before touching running services
        runtime = getattr(app.state.services, "build_runtime", None)
        return {"cancelled": bool(runtime and runtime.cancel(job_id))}

    @app.get("/artifact-sets/{artifact_id}/snapshots/{model_id}/{key}/{filename}")
    def get_snapshot(artifact_id: str, model_id: str, key: str, filename: str) -> FileResponse:
        from tcad.inspect.artifact import ArtifactReader
        from tcad.render.snapshot import snapshot_dir, VIEWS
        try:
            reader = ArtifactReader(cfg().storage.data_dir)
            reader.resolve(model_id, artifact_id=artifact_id)
            if filename not in {f"{view}.png" for view in VIEWS}:
                raise HTTPException(404, "no such snapshot")
            root = snapshot_dir(reader.data_dir, artifact_id, model_id, key)
            return FileResponse(_resolve_artifact(root, filename), media_type="image/png")
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/models")
    def create_model(req: CreateModelRequest) -> dict:
        s = svc()
        # Probe existence by loading, not by `current_version() > 0`: version 0 is
        # a perfectly valid seeded model and comparing against 0 would let a
        # second create silently overwrite it.
        if _model_exists(s.store, req.model_id):
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
        store = session_store()
        try:
            ir = store.load(model_id, version)
        except FileNotFoundError as exc:
            raise HTTPException(404, f"no such model/version: {exc}") from exc
        return json.loads(ir.model_dump_json())

    @app.get("/models/{model_id}/artifacts")
    def list_artifacts(model_id: str, version: int | None = None) -> dict:
        store = session_store()
        try:
            # One `load()` for both the existence check and the version — see the
            # note on `StoreAdapter.current_version`.
            ir = store.load(model_id, version)
        except FileNotFoundError as exc:
            raise HTTPException(404, f"no such model: {model_id}") from exc
        v = int(ir.version)
        d = store.artifact_dir(model_id, v)
        files = (
            sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())
            if d.exists()
            else []
        )
        artifact_meta = {}
        if (d / "manifest.json").is_file():
            try:
                from tcad.artifacts.manifest import ArtifactSet
                manifest = ArtifactSet.model_validate_json((d / "manifest.json").read_text(encoding="utf-8"))
                artifact_meta = {"artifact_id": manifest.artifact_id, "status": manifest.status.value}
            except (OSError, ValueError):
                # Legacy listings remain available; pinned queries validate the
                # full schema and report unreadable evidence explicitly.
                pass
        return {
            "model_id": model_id, "version": v, "dir": str(d), "files": files,
            **artifact_meta,
            # Artifacts alone say "files exist"; the verdict says whether THIS
            # version was graded green. A client must not have to infer the
            # second from the first.
            "verdict": _verdict_or_none(store, model_id, v),
        }

    @app.get("/models/{model_id}/verdict")
    def get_verdict(model_id: str, version: int | None = None) -> dict:
        """Is the current version verified on disk — and if not, why not.

        Exists because the honest answer is not a boolean the client can derive:
        a model at v5 whose v5 never passed is a different situation from one
        whose v4 passed and was then edited, and the reason string distinguishes
        them.
        """
        store = session_store()
        try:
            return store.verdict(model_id, version)
        except FileNotFoundError as exc:
            raise HTTPException(404, f"no such model: {model_id}") from exc

    @app.get("/models/{model_id}/artifacts/{file_path:path}")
    def get_artifact(
        model_id: str, file_path: str, version: int | None = None
    ) -> FileResponse:
        """Serve one artefact (PNG / STEP / STL / BREP).

        The containment check is the point of this endpoint: ``{file_path:path}``
        happily accepts ``../../etc/passwd``, so the resolved target is verified
        to sit inside the artefact directory before anything is opened.
        """
        store = session_store()
        try:
            ir = store.load(model_id, version)
        except FileNotFoundError as exc:
            raise HTTPException(404, f"no such model: {model_id}") from exc
        root = store.artifact_dir(model_id, int(ir.version))
        return FileResponse(_resolve_artifact(root, file_path))

    @app.get("/models/{model_id}/mesh", response_model=MeshPreviewResponse)
    async def get_mesh(
        model_id: str,
        version: int | None = Query(default=None, ge=0),
        tolerance: float = Query(default=0.5, ge=MIN_TOLERANCE, le=MAX_TOLERANCE),
        force: bool = False,
        artifact_id: str | None = None,
    ) -> Response:
        """Human preview only; mesh availability never establishes Gate success.

        Without a selector, read the latest published artifact. The scene's
        precision is fixed at build time; queries never rebuild source geometry.
        Async admission bounds preview waiters before they can occupy shared
        ASGI threads. Disk IO, validation and JSON encoding remain in the
        thread pool.
        """
        from tcad.server.mesh import mesh_preview

        jobs = app.state.mesh_preview_jobs
        # No await between admission and registration: competing requests cannot
        # exceed the cap, and overload never waits for a thread-pool token.
        if len(jobs) >= MAX_ACTIVE_MESH_REQUESTS:
            raise HTTPException(
                429, "mesh preview is busy; retry shortly",
                headers={"Retry-After": "1"},
            )

        def build_preview() -> Response:
            return mesh_preview(
                cfg().storage.data_dir, app.state.mesh_cache, model_id,
                version=version, tolerance=tolerance, force=force, artifact_id=artifact_id,
            )

        job = asyncio.create_task(run_in_threadpool(build_preview))
        jobs.add(job)

        def release_preview(finished: asyncio.Task) -> None:
            jobs.discard(finished)
            # A disconnected client no longer awaits a failed build. Retrieve
            # its exception so that outcome does not become an unhandled task.
            if not finished.cancelled():
                finished.exception()

        job.add_done_callback(release_preview)
        # Cancelling an HTTP request cannot stop a running sync scene read. Keep
        # its slot until the actual work finishes, rather than admitting another
        # job on each click/AbortController cancellation.
        return await asyncio.shield(job)

    @app.get("/models/{model_id}/render")
    def render_view(
        model_id: str,
        view: str = "iso",
        style: str | None = None,
        width: int | None = None,
        height: int | None = None,
        version: int | None = None,
        force: bool = False,
        artifact_id: str | None = None,
        driver_angle_deg: float = Query(default=0, ge=-720, le=720),
        frame_index: int = Query(default=0, ge=0, le=599),
    ) -> FileResponse:
        """Render one orthographic view of a published artifact.

        This is the *human's* view path, deliberately separate from the model's
        ``geo_view`` tool: no hook dispatch, no token budget, no checkpoint
        requirement. Both paths use the saved artifact scene. Derived PNGs live
        in an isolated cache keyed by artifact identity and render settings.
        """
        from tcad.core.wiring import build_renderer
        from tcad.inspect.artifact import ArtifactReader
        from tcad.render.snapshot import render_snapshot

        if view not in _ALLOWED_VIEWS:
            raise HTTPException(400, f"unknown view {view!r}")
        c = cfg()
        style = style or c.context.render.style
        if style not in _ALLOWED_STYLES:
            raise HTTPException(400, f"unknown style {style!r}")
        w, h = int(width or c.context.render.width), int(height or c.context.render.height)
        if not (16 <= w <= 4096 and 16 <= h <= 4096):
            raise HTTPException(400, "width/height must be within [16, 4096]")
        reader = ArtifactReader(c.storage.data_dir)
        try:
            manifest, root = reader.resolve(model_id, version, artifact_id)
            renderer = getattr(app.state.services, "renderer", None) or build_renderer(c.context.render)
            images = render_snapshot(reader, manifest, root, renderer, views=[view],
                                     style=style, width=w, height=h, force=force,
                                     angle=driver_angle_deg, frame=frame_index)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return FileResponse(images[0].path, media_type="image/png", headers={
            "X-Artifact-ID": manifest.artifact_id,
            "X-Artifact-Version": str(manifest.ir_version),
            "X-Artifact-Status": manifest.status.value,
        })

    # ── the turn ──────────────────────────────────────────────────────────

    @app.post("/chat")
    async def chat(req: ChatRequest) -> StreamingResponse:
        s = svc()
        thread_id = req.thread_id or f"th-{req.model_id}"
        # The model is a property of the conversation, not of the request.
        #
        # Continuing an existing session must run against the model that session
        # has been building. If the client disagrees we refuse loudly: silently
        # honouring either side produces the worst outcome — a turn that edits one
        # part while the viewport shows another, which reads as corrupted output
        # rather than as a caller mistake.
        existing = db().get_thread(thread_id)
        if existing is not None and existing.model_id != req.model_id:
            raise HTTPException(
                409,
                f"thread {thread_id!r} builds model {existing.model_id!r}, "
                f"but this request targets {req.model_id!r}. A session cannot be "
                f"repointed at another model; open or create a session for it instead.",
            )
        model_id = existing.model_id if existing is not None else req.model_id
        turn_id = req.request_id or f"c{uuid.uuid4().hex[:12]}"
        operations = None
        request_hash = None
        if req.operation_id is not None:
            from tcad.server.operations import OperationStore, fingerprint
            operations = OperationStore(cfg().storage.data_dir)
            request_hash = fingerprint({**req.model_dump(mode="json", exclude={"request_id"}),
                                        "model_id": model_id, "thread_id": thread_id})
            try:
                replay = operations.read(req.operation_id, request_hash)
            except SelectionError as exc:
                raise HTTPException(exc.status, exc.detail()) from exc
            if replay is not None:
                async def replay_events():
                    yield _sse("start", {"model_id": model_id, "thread_id": thread_id,
                                         "kind": req.kind.value, "request_id": replay["request_id"],
                                         "operation_id": req.operation_id, "replayed": True})
                    yield _sse(replay["event"], replay["data"])
                return StreamingResponse(replay_events(), media_type="text/event-stream",
                                         headers={"Cache-Control": "no-cache"})
        if req.selection_context is not None:
            from tcad.selection.resolve import SelectionResolver
            try:
                if not cfg().selection.enabled:
                    raise SelectionError("selection_disabled", "Feature references are disabled.", 403)
                SelectionResolver(cfg().storage.data_dir, s.store).resolve(
                    model_id, req.selection_context,
                    inspection=req.kind == TurnKind.INSPECT or req.access_mode == AccessMode.READ_ONLY)
            except SelectionError as exc:
                raise HTTPException(exc.status, exc.detail()) from exc
        # One id, one running turn. This is the *fast* refusal — a request that
        # arrives while the turn is already registered gets a clean 409. The
        # generator re-checks at registration, because between this check and
        # that one there is a window in which two requests can both pass.
        if turn_id in app.state.turns:
            raise HTTPException(
                409,
                f"request_id {turn_id!r} is already running; a turn id must be "
                f"unique while it is in flight (mint a new one per request)",
            )

        def active_conflict() -> RunningTurn | None:
            # Separate request ids do not make it safe to edit the same part
            # concurrently. Both its conversation history and IR tool sequence
            # must belong to one turn until it finishes (or is stopped).
            return next((
                turn for turn in app.state.turns.values()
                if turn.thread_id == thread_id or turn.model_id == model_id
            ), None)

        conflict = active_conflict()
        if conflict is not None:
            raise HTTPException(
                409, f"thread/model already has an active turn {conflict.request_id!r}; "
                "stop that turn or wait for it to finish before sending another message",
            )

        if operations is not None:
            try:
                operations.begin(req.operation_id, request_hash, turn_id)
            except SelectionError as exc:
                raise HTTPException(exc.status, exc.detail()) from exc

        def remember_user() -> None:
            try:
                d = db()
                if d.get_thread(thread_id) is None:
                    d.create_thread(model_id, thread_id=thread_id)
                d.add_message(thread_id, "user", req.text)
            except Exception:  # noqa: BLE001 — history is a convenience, not a guarantee
                pass

        async def gen() -> AsyncIterator[str]:
            # A per-request tap, handed to the engine — NOT assigned onto
            # ``services``. Assigning it (and restoring it in ``finally``) is
            # shared mutable state: two concurrent /chat requests would each
            # overwrite the other's tap, and the first to finish would restore
            # the original dispatcher while the second was still mid-turn, losing
            # that turn's lifecycle events and hook decisions.
            tap = HookEventTap(s.hooks)

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
            running = RunningTurn(turn_id, thread_id, model_id)
            registered = False
            outcome_saved = False

            def terminal(event: str, data: dict) -> str:
                nonlocal outcome_saved
                if operations is not None:
                    operations.finish(req.operation_id, event, data)
                outcome_saved = True
                return _sse(event, data)

            try:
                # Registration is the authority, not the check in `chat()`: two
                # requests with the same id can both pass that check before
                # either generator has run, and the second would overwrite the
                # first's entry — making the first turn impossible to stop, which
                # is the exact failure this feature exists to remove. The 409
                # window has closed by now (the response has started), so the
                # refusal travels as an error frame instead.
                if turn_id in app.state.turns:
                    yield terminal(
                        "error",
                        {
                            "type": "DuplicateRequestId",
                            "message": (
                                f"request_id {turn_id!r} is already running; a turn id "
                                f"must be unique while it is in flight"
                            ),
                        },
                    )
                    return
                conflict = active_conflict()
                if conflict is not None:
                    # The response headers have already started. Recheck at the
                    # same no-await registration boundary as duplicate ids, so
                    # two requests accepted together cannot both mutate a part.
                    yield terminal("error", {
                        "type": "ActiveTurnConflict",
                        "message": f"thread/model already has an active turn {conflict.request_id!r}",
                    })
                    return
                remember_user()
                # Registered before the `start` frame, so there is no moment in
                # which the client can see a turn that the server cannot stop.
                app.state.turns[turn_id] = running
                registered = True
                # ...and a stop that raced the `start` frame — it was already
                # remembered by /chat/interrupt — is honoured here rather than
                # having the earlier request report a success it did not achieve.
                if _take_pending_stop(app.state.pending_stops, turn_id):
                    running.request_stop()
                # Inside the try on purpose. A client that disconnects between
                # the `start` frame and the first progress frame raises
                # GeneratorExit right here, and a generator closed before its
                # `try` is entered never runs its `finally` — which is how the
                # turn task below would leak.
                yield _sse(
                    "start",
                    {
                        "model_id": model_id,
                        "kind": req.kind.value,
                        "thread_id": thread_id,
                        "request_id": turn_id,
                        "operation_id": req.operation_id,
                    },
                )
                task = asyncio.create_task(
                    run_turn_request(
                        s,
                        req,
                        observer=observe,
                        model_id=model_id,
                        stop_requested=running.is_stop_requested,
                        hooks=tap,
                    )
                )
                running.task = task
                while not task.done():
                    for ev in tap.drain():
                        yield _sse("progress", ev)
                    while agent_events:
                        yield _sse("agent", agent_events.popleft())
                    await asyncio.sleep(0.02)
                try:
                    result = await task
                except asyncio.CancelledError:
                    # The turn task was cancelled. When someone *asked* for that
                    # stop it is an outcome, and the stream must still conclude
                    # with a verdict — the whole point of the feature is that a
                    # stop is visible, not that the text quietly stops. A
                    # cancellation from anywhere else (this generator being torn
                    # down, server shutdown) is not ours to reinterpret.
                    if not (running.is_stop_requested() and task.cancelled()):
                        raise
                    result = aborted_turn_result(turn_id, thread_id, model_id)
                for ev in tap.drain():
                    yield _sse("progress", ev)
                while agent_events:
                    yield _sse("agent", agent_events.popleft())
                yield terminal("result", json.loads(result.model_dump_json()))
            except asyncio.CancelledError:  # client hung up
                raise
            except Exception as exc:  # noqa: BLE001
                yield terminal("error", {"type": type(exc).__name__, "message": str(exc),
                                         **({"code": exc.code} if isinstance(exc, SelectionError) else {})})
            finally:
                def release_turn(_task=None) -> None:
                    if registered and app.state.turns.get(turn_id) is running:
                        app.state.turns.pop(turn_id, None)

                # A turn must not outlive the client that asked for it.
                #
                # This mattered less when the loop had a step ceiling: a turn
                # orphaned by a closed tab would hit `max_steps_per_turn` and
                # die on its own. With no work ceiling it simply keeps running —
                # spending tokens and holding the worker — for as long as nobody
                # stops it, and nothing else here would.
                if task is not None and not task.done():
                    # Cancellation is a request, not proof the engine finished
                    # unwinding. Keep the model occupied until cleanup is done,
                    # otherwise a reconnect could start a new edit while the
                    # old turn is still aborting a shared worker call.
                    task.add_done_callback(release_turn)
                    task.cancel()
                else:
                    release_turn()
                # Cancel/retain the active turn before writing the ledger: disk
                # errors must not prevent worker cleanup or admit a competing edit.
                if operations is not None and not outcome_saved:
                    operations.finish(req.operation_id, "error", {
                        "type": "OperationInterrupted", "code": "operation_interrupted",
                        "message": "Connection interrupted. Inspect the current source; retrying this operation only returns this outcome.",
                    })
                # Nothing to unwrap: the tap was passed to the engine, never
                # assigned onto the shared services bundle.

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/chat/interrupt")
    async def interrupt_chat(req: InterruptRequest) -> dict:
        """Stop the turn with this ``request_id``.

        ``async def`` on purpose: the body only touches in-memory state, and
        cancelling the turn's task is only safe from the event loop's own thread
        (a sync endpoint runs in a worker thread, where ``Task.cancel()`` is not
        thread-safe).

        Deliberately 200 even when nothing is running. "No such turn" is an
        answer, not a server fault, and the caller has to be able to tell it
        apart from "the stop itself failed". ``stage`` says where the request
        landed — ``running`` means a turn was told to stop, ``pending`` means it
        named a turn the server has not registered yet and will be honoured at
        registration if it appears.
        """
        turn = app.state.turns.get(req.request_id)
        if turn is not None:
            turn.request_stop()
            return {
                "request_id": req.request_id,
                "thread_id": turn.thread_id,
                "interrupted": True,
                "stage": "running",
            }

        # Not registered — yet. It may be a turn whose first frame has not been
        # written. Remember it (bounded), so a stop that raced the `start` frame
        # is honoured at registration instead of silently doing nothing while
        # the model carries on generating.
        _remember_pending_stop(app.state.pending_stops, req.request_id)
        return {
            "request_id": req.request_id,
            "thread_id": None,
            "interrupted": True,
            "stage": "pending",
            "note": "该回合尚未注册；它若随后开始，会在第一步之前停止。",
        }

    # ── approvals ─────────────────────────────────────────────────────────

    @app.get("/approvals")
    def list_approvals(thread_id: str | None = None) -> dict:
        s = svc()
        store = getattr(s, "approvals", None)
        if store is None:
            return {"pending": []}
        records = store._load() if hasattr(store, "_load") else []
        pending = [
            r.model_dump(mode="json")
            for r in records
            if not getattr(r, "granted", False) and getattr(r, "resolved_at", None) is None
            and (thread_id is None or r.thread_id == thread_id)
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
            "free_models": result.free_models,
            "source": "live" if result.models else "preset",
            "ok": result.ok,
            "error": result.error,
            "latency_ms": result.latency_ms,
        }

    @app.post("/settings/models")
    async def list_draft_models(patch: LlmSettingsPatch) -> dict:
        """List models using the dialog's draft endpoint/key without saving."""
        from tcad.config.providers import get_provider
        from tcad.llm.hotswap import probe_llm

        llm = _merge_llm(current_llm().llm, patch)
        preset = get_provider(llm.provider)
        result = await probe_llm(llm, timeout_s=12.0)
        return {
            "provider": llm.provider,
            "models": result.models or (list(preset.models) if preset else []),
            "free_models": result.free_models,
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

    # ── sessions ──────────────────────────────────────────────────────────
    #
    # A "session" is one conversation bound to one model. The two are created
    # together and the binding never changes: a conversation's whole history is
    # about the part it was building, so repointing it at another part would make
    # its own transcript a lie. Switching sessions therefore switches both.

    @app.get("/sessions")
    def list_sessions(limit: int = 100) -> dict:
        """Conversations, most recently active first.

        Carries enough to render the list *and* to switch to an entry: the UI
        needs `model_id` to know what to load, and `ir_version` to tell a session
        that produced geometry from one that never got anywhere.
        """
        store = session_store()
        sessions = []
        for row in db().list_threads(limit=limit):
            try:
                version = store.latest_version(row["model_id"])
            except Exception:  # noqa: BLE001 — an unreadable model must not hide the list
                version = None
            # `ir_version` alone reads as progress. A session whose latest version
            # was never verified must not look like one that was.
            sessions.append({
                **row, "ir_version": version,
                "verified": _verified_or_none(store, row["model_id"], version),
            })
        return {"sessions": sessions}

    @app.post("/sessions")
    def create_session(req: CreateSessionRequest) -> dict:
        """Start a new conversation, creating its model at the same time.

        Both at once because a session without a model is a usable-but-broken
        state: the user could type into it and every turn would fail on a missing
        store. The model id is derived from the session id so the pairing is
        obvious on disk.

        Seeding an empty IR needs no geometry kernel, so this does not build the
        service stack — FreeCAD starts when the first turn actually needs it.

        The model is created **before** the thread on purpose. Creating the
        thread first and failing to seed the model would leave a conversation
        that looks usable and fails on its first turn; this order leaves at worst
        an orphan model, which is inert and harmless.

        A ``model_id`` that already exists is refused rather than reused. Seeding
        is not idempotent — it rewrites ``v0.json`` — so accepting one would let a
        single request wipe a part the user had been building, and the response
        would still say ``ir_version: 0`` as if that were new. Continuing an
        existing conversation is a ``POST /chat`` with its ``thread_id``; it does
        not need a fresh model.
        """
        store = session_store()
        model_id = req.model_id or f"part-{uuid.uuid4().hex[:8]}"
        if _model_exists(store, model_id):
            raise HTTPException(
                409,
                f"model {model_id!r} already exists; creating a session for it would "
                f"reset it to v0 and destroy the part. Send to the existing session "
                f"instead (POST /chat with its thread_id), or omit model_id to let "
                f"the server mint a new one.",
            )
        ir = IrDocument(
            model_id=model_id,
            version=0,
            requirements=RequirementSpec(raw_text=req.raw_requirement),
        )
        try:
            store.create(model_id, ir)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"could not create model {model_id!r}: {exc}") from exc

        thread = db().create_thread(model_id=model_id, context_state="full")
        return {
            "thread_id": thread.thread_id,
            "model_id": thread.model_id,
            "ir_version": 0,
        }

    # ── front end ─────────────────────────────────────────────────────────

    # Mounted only when the files are present, so the API stays usable (and the
    # test suite stays runnable) without them.
    ui_dir = Path(__file__).resolve().parent / "ui"
    if ui_dir.is_dir():
        from fastapi.responses import RedirectResponse
        from fastapi.staticfiles import StaticFiles

        class LiveUIFiles(StaticFiles):
            # No build manifest exists to version native modules. Serve current
            # assets after a server update, including on conditional reloads.
            def is_not_modified(self, response_headers, request_headers):
                return False

            async def get_response(self, path, scope):
                response = await super().get_response(path, scope)
                response.headers["Cache-Control"] = "no-store"
                return response

        viewer_dir = Path(__file__).resolve().parents[1] / "viewer"
        app.mount("/viewer", LiveUIFiles(directory=str(viewer_dir), html=True), name="viewer")
        app.mount("/ui", LiveUIFiles(directory=str(ui_dir), html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def index_redirect() -> Any:
            return RedirectResponse("/ui/")

    return app


# ══════════════════════════════════════════════════════════════════════════
# one turn, driven by the engine
# ══════════════════════════════════════════════════════════════════════════


async def run_turn_request(
    services: Any,
    req: ChatRequest,
    observer: Any = None,
    model_id: str | None = None,
    stop_requested: Any = None,
    history_provider: Any = None,
    hooks: Any = None,
):
    """Build the engine and run a single turn. Returns a ``TurnResult``.

    ``observer`` is forwarded to the engine so a front end can see the model's
    text and tool calls as they happen (``(kind, data) -> None``).

    ``stop_requested`` is the engine's "has someone asked this turn to stop?"
    predicate. It is how an interruption from another request becomes an
    ``ABORTED`` TurnResult instead of a turn that simply vanishes.

    ``history_provider`` is ``(thread_id, current_text) -> list[Message]``. It
    defaults to reading the conversation store attached to ``services`` as
    ``session_db``, which is what turns "the transcript is in SQLite" into "the
    model was told the transcript". Pass it explicitly to override.

    ``hooks`` is a per-turn dispatcher (typically an observer tap). It is passed
    down to the engine rather than assigned onto ``services``, so two concurrent
    requests cannot clobber each other's tap.

    ``model_id`` overrides ``req.model_id``. The caller resolves it from the
    session, because a conversation's model is recorded once and must not be
    re-decided per request — a turn that ran against the wrong model would edit
    one part while the viewport showed another.
    """
    from tcad.core.types import Thread
    from tcad.loop.engine import LoopEngine, UserMessage
    from tcad.tools.base import build_default_registry

    cfg = services.config
    loop_cfg = services.loop_config
    target_model = model_id or req.model_id
    thread = Thread(
        thread_id=req.thread_id or f"th-{target_model}", model_id=target_model
    )
    registry = build_default_registry(
        services, enable_privileged=(req.access_mode == AccessMode.FULL)
    )
    limits = budget_limits_from_config(cfg)
    engine = LoopEngine(
        services,
        registry,
        limits,
        loop_cfg,
        observer=observer,
        stop_requested=stop_requested,
        context_assembler=getattr(services, "context_assembler", None),
        history_provider=history_provider or _default_history_provider(services),
        hooks=hooks,
    )
    from tcad.build.scheduler import build_observer
    loop = asyncio.get_running_loop()
    callback = (lambda payload: loop.call_soon_threadsafe(observer, "build", payload)) if observer else None
    token = build_observer.set(callback)
    try:
        return await engine.run_turn(
            thread, UserMessage(kind=req.kind, text=req.text,
                                privileged_requested=req.privileged_requested, access_mode=req.access_mode,
                                selection_context=req.selection_context)
        )
    finally:
        build_observer.reset(token)


def _default_history_provider(services: Any):
    """Read prior turns from the session store, when one is attached.

    ``None`` when there is no conversation store: the engine then builds the
    minimal ``[system, user]`` context, which is the correct behaviour for an
    embedder that keeps its own transcript.
    """
    session_db = getattr(services, "session_db", None)
    if session_db is None:
        return None

    def _load(thread_id: str, current_text: str):
        from tcad.context.history import load_thread_history

        return load_thread_history(session_db, thread_id, current_text=current_text)

    return _load


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
