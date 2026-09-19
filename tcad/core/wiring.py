"""Production wiring: bind the library-shaped collaborators to the Protocols.

Why this module exists
----------------------
Each subsystem was built as a self-contained library with its own natural shape.
The loop and the tools, however, were written against the narrow Protocols in
``tcad/tools/base.py``. Those two shapes do not line up on their own:

  * ``IrStore`` has no ``validate_patch`` / ``validate_document`` / ``persist_digest``
    (validation lives in ``tcad/ir/validate.py``, digest persistence in
    ``tcad/store/artifacts.py``).
  * ``tcad/render/raster.py`` exposes ``render_views()``, not a ``Renderer`` object.
  * ``tcad/context/digest.py`` exposes ``render_digest_text()``, not a
    ``ContextService``.
  * ``WorkerHandle.request`` is **async**, while ``commit.py`` and ``geo_tools.py``
    call ``services.worker.request(...)`` **synchronously** — and expect the
    ``{"ok": ..., "result": ...}`` envelope, whereas ``WorkerHandle`` returns the
    bare result and raises on failure.

This module is the single place those mismatches are reconciled. Nothing else in
the codebase should need to know about them.

Nothing here is lazy about correctness: every adapter is a thin, typed shim with
no business logic of its own.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from tcad.config.schema import Config
from tcad.core.types import (
    CheckContext,
    GeometryDigest,
    ImageRef,
    ToolError,
    ToolErrorKind,
)
from tcad.core.worker_client import WorkerCallFailed, WorkerError, WorkerHandle
from tcad.ir.schema import FeatureSpec, IrDocument, IrPatch
from tcad.ir.validate import ValidationIssue, validate_ir
from tcad.store.artifacts import ArtifactStore
from tcad.store.ir_store import IrStore

# ── image token accounting ────────────────────────────────────────────────
# Proportionally scaled from the default render size, which is what
# `context.budget.images` (2400 tokens for 3 views) was written against.
# UNVERIFIED ESTIMATE — calibrate against the real model (design §12-6).
_BASE_IMAGE_TOKENS = 800
_BASE_IMAGE_PIXELS = 768 * 576


def estimate_image_tokens(width: int, height: int) -> int:
    return max(1, round(_BASE_IMAGE_TOKENS * (width * height) / _BASE_IMAGE_PIXELS))


# ══════════════════════════════════════════════════════════════════════════
# 1. worker: async handle -> sync envelope
# ══════════════════════════════════════════════════════════════════════════


class SyncWorkerClient:
    """Synchronous, envelope-shaped view of :class:`WorkerHandle`.

    ``WorkerHandle`` deliberately exposes both an async ``request`` and a
    blocking ``request_sync``. The tools and the commit pipeline are synchronous
    and check ``res.get("ok")``, so they get this shim instead of the handle.
    """

    def __init__(self, handle: WorkerHandle) -> None:
        self._handle = handle

    @property
    def handle(self) -> WorkerHandle:
        """The raw handle — needed by checks that require ``request_sync``."""
        return self._handle

    def request(
        self, method: str, params: dict | None = None, *, timeout_s: float = 30.0
    ) -> dict:
        try:
            result = self._handle.request_sync(method, params or {}, timeout_s=timeout_s)
        except WorkerCallFailed as exc:
            return {
                "ok": False,
                "error": {
                    "kind": exc.kind.value,
                    "message": str(exc),
                    "feature_id": exc.feature_id,
                    "traceback": getattr(exc, "traceback", ""),
                },
            }
        except WorkerError as exc:
            # Transport-level failure (timeout, crash, unknown method).
            return {
                "ok": False,
                "error": {"kind": exc.kind.value, "message": str(exc), "feature_id": None},
            }
        return {"ok": True, "result": result}

    def is_alive(self) -> bool:
        return self._handle.is_alive()

    def close(self) -> None:
        self._handle.close()


# ══════════════════════════════════════════════════════════════════════════
# 2. store: IrStore + validate + artifacts -> Store Protocol
# ══════════════════════════════════════════════════════════════════════════


class StoreAdapter:
    """Implements the ``Store`` Protocol from ``tcad/tools/base.py``.

    Composition, not inheritance: ``IrStore`` owns the versioned snapshots and
    the append-only event log, ``validate_ir`` owns the semantic checks, and
    ``ArtifactStore`` owns the on-disk artefacts. This class is the seam where
    the three become the single ``store`` the tools expect.
    """

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.data_dir = Path(data_dir)
        self.ir_store = IrStore(self.data_dir)
        self.artifacts = ArtifactStore(self.data_dir)

    # ── paths (used by the Gate's disk-only loader) ──

    def snapshot_path(self, model_id: str, version: int) -> Path:
        """Exactly where IrStore writes ``v{n}.json``. Kept here so the Gate can
        read the snapshot from disk without reaching into IrStore privates."""
        return self.data_dir / "models" / model_id / f"v{version}.json"

    def artifact_dir(self, model_id: str, version: int) -> Path:
        return self.artifacts.dir_for(model_id, version)

    # ── delegation ──

    def load(self, model_id: str, version: int | None = None) -> IrDocument:
        return self.ir_store.load(model_id, version)

    def current_version(self, model_id: str) -> int:
        """The latest version number, or ``0``.

        **``0`` is ambiguous by construction**: it means "no such model" *and*
        "a model at version 0" — and a freshly created model *is* at version 0.

        Acceptable for "what number shall the next snapshot get?". Wrong for
        "does this model exist?", where the answer decides between a 404 and a
        500. Use :meth:`load` when the distinction matters; the server's render
        endpoint learned this the hard way.
        """
        try:
            return int(self.ir_store.load(model_id).version)
        except FileNotFoundError:
            return 0

    def apply_patch(self, model_id: str, patch: IrPatch):
        return self.ir_store.apply_patch(model_id, patch)

    def create(self, model_id: str, ir: IrDocument) -> IrDocument:
        return self.ir_store.create(model_id, ir)

    # ── validation, translated into the tools' error vocabulary ──

    @staticmethod
    def _to_tool_errors(issues: list[ValidationIssue]) -> list[ToolError]:
        # Only "error" severity blocks. Warnings are deliberately dropped here:
        # surfacing them as ToolErrors would make them block by accident.
        return [
            ToolError(
                kind=ToolErrorKind.SEMANTIC,
                message=f"[{i.code}] {i.message}",
                feature_id=i.target_id,
                hint="Fix the referenced id and retry the patch.",
            )
            for i in issues
            if i.severity == "error"
        ]

    def validate_document(self, ir: IrDocument) -> list[ToolError]:
        return self._to_tool_errors(validate_ir(ir))

    def validate_patch(self, ir: IrDocument, patch: IrPatch) -> list[ToolError]:
        """Validate the document that *would result* from the patch.

        The patch is applied in memory only — nothing is persisted here, so a
        rejected patch leaves no trace.
        """
        from tcad.ir.patch import apply_patch as _apply_patch

        try:
            outcome = _apply_patch(ir, patch)
        except Exception as exc:  # noqa: BLE001 — any patch failure is a semantic error
            return [
                ToolError(
                    kind=ToolErrorKind.SEMANTIC,
                    message=f"patch rejected: {type(exc).__name__}: {exc}",
                    feature_id=getattr(exc, "feature_id", None),
                )
            ]
        candidate = getattr(outcome, "ir", None) or outcome
        if not isinstance(candidate, IrDocument):
            return [
                ToolError(
                    kind=ToolErrorKind.RUNTIME,
                    message=f"patch produced an unexpected type: {type(candidate).__name__}",
                )
            ]
        return self.validate_document(candidate)

    def persist_digest(
        self, model_id: str, ir_version: int, digest: GeometryDigest
    ) -> None:
        self.artifacts.write_digest(model_id, ir_version, digest)


# ══════════════════════════════════════════════════════════════════════════
# 3. renderer: render_views() -> Renderer Protocol
# ══════════════════════════════════════════════════════════════════════════


class RendererAdapter:
    """Software rasteriser behind the ``Renderer`` Protocol.

    The worker has already been asked for the mesh by the caller; this only
    turns triangles into PNG files on disk, so the model can be shown what it
    drew (design §6.5). FreeCAD itself cannot rasterise headlessly.
    """

    def __init__(self, supersample: int = 2) -> None:
        self.supersample = supersample

    def render(
        self,
        mesh: Any,
        *,
        out_dir: str,
        views: list[str],
        style: str,
        width: int,
        height: int,
    ) -> list[ImageRef]:
        from tcad.render.png import write_png
        from tcad.render.raster import render_views

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        buffers = render_views(
            mesh,
            views=list(views),
            width=int(width),
            height=int(height),
            style=style,  # type: ignore[arg-type]
            supersample=self.supersample,
        )
        images: list[ImageRef] = []
        for view, arr in buffers.items():
            path = out / f"{view}.png"
            w, h = write_png(arr, str(path))
            images.append(
                ImageRef(
                    path=str(path),
                    view=str(view),
                    width=int(w),
                    height=int(h),
                    tokens_estimate=estimate_image_tokens(w, h),
                )
            )
        return images


# ══════════════════════════════════════════════════════════════════════════
# 4. context: digest on disk -> ContextService Protocol
# ══════════════════════════════════════════════════════════════════════════


class ContextServiceAdapter:
    """Serves the ``GeometryDigest`` for a version, with its text rendering.

    Reads the digest **from disk** (written by the worker's introspect step via
    the artefact store). If it is absent the service returns a structure-only
    digest with ``measurements_available=False`` rather than inventing numbers —
    ``render_digest_text`` then stamps the ``[未验证几何]`` banner so the model
    knows it is flying blind.
    """

    def __init__(self, store: StoreAdapter) -> None:
        self._store = store

    def digest(self, model_id: str, ir_version: int) -> GeometryDigest:
        from tcad.context.digest import render_digest_text

        ir = self._store.load(model_id, ir_version)
        digest = self._store.artifacts.read_digest(model_id, ir_version)
        if digest is None:
            digest = GeometryDigest(
                model_id=model_id,
                ir_version=ir_version,
                measurements_available=False,
                feature_chain=[
                    _feature_digest_entry(f)
                    for body in ir.bodies
                    for f in body.features
                ],
            )
        digest.text = render_digest_text(digest, ir)
        return digest


def _feature_digest_entry(f: FeatureSpec):
    from tcad.core.types import FeatureDigest

    return FeatureDigest(
        id=f.id, name=f.name, op=f.op, params=dict(f.params), suppressed=f.suppress
    )


# ══════════════════════════════════════════════════════════════════════════
# 5. the Gate's disk-only context loader
# ══════════════════════════════════════════════════════════════════════════


def build_context_loader(
    store: StoreAdapter, worker: WorkerHandle | None
) -> Callable[[str, int], CheckContext]:
    """Return ``(model_id, ir_version) -> CheckContext`` reading only from disk.

    This is the structural half of "the generator must not grade its own paper"
    (design §4.6). The loader hands the Gate file paths, never live objects.

    It deliberately does **not** propagate a missing-digest error: the Gate
    already knows how to report "cannot attest", and turning that into a raised
    exception would kill the whole Turn instead of producing a legible, fail-closed
    ``GateReport``. A missing IR snapshot *is* propagated — at that point there is
    nothing to grade at all.
    """

    def _load(model_id: str, ir_version: int) -> CheckContext:
        from tcad.verify.context import CheckContextError, build_check_context

        artifact_dir = store.artifact_dir(model_id, ir_version)
        ir_path = store.snapshot_path(model_id, ir_version)
        try:
            return build_check_context(
                model_id=model_id,
                ir_version=ir_version,
                artifact_dir=str(artifact_dir),
                ir_path=str(ir_path),
                worker=worker,
            )
        except (CheckContextError, FileNotFoundError):
            # Degrade to "cannot measure": either the digest is missing
            # (DigestNotFoundError) or the artifact directory was never created
            # (CheckContextError). Both mean the Gate has nothing to measure, and
            # the Gate already reports that as not-passed with
            # `gate:cannot_attest_no_measurements`. Raising instead would kill the
            # whole Turn over a missing file, which is worse.
            #
            # A missing IR *snapshot* still propagates: `store.load` below raises,
            # and at that point there is genuinely nothing to grade.
            ir = store.load(model_id, ir_version)
            digest = ContextServiceAdapter(store).digest(model_id, ir_version)
            return CheckContext(
                model_id=model_id,
                ir_version=ir_version,
                ir=ir,
                artifact_dir=str(artifact_dir),
                exports={},
                digest=digest,
                worker=worker,
            )

    return _load


# ══════════════════════════════════════════════════════════════════════════
# 6. hooks: config-driven list + injected built-ins
# ══════════════════════════════════════════════════════════════════════════


def build_sandbox_probe(cfg: Config) -> Callable[[], bool]:
    """Return a callable reporting whether the sandbox is healthy.

    With no ``policy.sandbox_probe`` configured the probe is *always False*, so
    the privileged gate's third condition can never be satisfied. That is the
    fail-closed default the design asks for: privileged tools stay unreachable
    until an operator explicitly configures a working sandbox.
    """
    cmd = cfg.policy.sandbox_probe
    if not cmd:
        return lambda: False

    def _probe() -> bool:
        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, timeout=5.0  # noqa: S602
            )
        except Exception:  # noqa: BLE001
            return False
        return proc.returncode == 0

    return _probe


def build_hooks(cfg: Config, data_dir: str | os.PathLike[str]):
    """Build the dispatcher, injecting the built-in guards' dependencies.

    The config declares *which* guards are active; the dependencies those guards
    need (an approval store, a sandbox probe, the deny-globs) can only be known
    here. ``HookDispatcher`` consults its ``registry`` before trying to import a
    module path, so the built-ins are supplied by name and any remaining entry in
    ``policy.hooks`` is still resolved from its ``module`` as documented.
    """
    from tcad.hooks.approval import JsonFileApprovalStore
    from tcad.hooks.dispatcher import HookDispatcher
    from tcad.hooks.policy import NetworkGuard, PathGuard, PrivilegedTripleGate

    approvals = JsonFileApprovalStore(
        str(Path(data_dir) / "approvals.json"), ttl_s=float(cfg.policy.approval_ttl_s)
    )
    sandbox_ok = build_sandbox_probe(cfg)

    registry: dict[str, Callable] = {
        "path_guard": PathGuard(list(cfg.policy.deny_globs)),
        "network_guard": NetworkGuard(False),
        "privileged_triple_gate": PrivilegedTripleGate(
            allow_privileged=bool(cfg.policy.allow_privileged),
            approval_lookup=lambda tool_name: approvals.lookup_valid(tool_name),
            sandbox_ok=sandbox_ok,
        ),
    }
    return HookDispatcher(cfg.policy.hooks, registry), approvals


# ══════════════════════════════════════════════════════════════════════════
# 7. the entry point
# ══════════════════════════════════════════════════════════════════════════


def build_services(cfg: Config, *, start_worker: bool = True):
    """Wire a complete, live collaborator stack.

    ``start_worker=False`` builds everything except the FreeCAD subprocess —
    useful for tests and for a server that wants to defer the (slow) process
    start until the first request.
    """
    from tcad.config.loader import REPO_ROOT, resolve_paths
    from tcad.config.settings import effective as effective_settings
    from tcad.llm.hotswap import HotSwapLlm
    from tcad.loop.engine import LoopConfig
    from tcad.verify.checks_solid import VerifyConfig
    from tcad.verify.gate import Gate

    cfg = resolve_paths(cfg, root=REPO_ROOT)
    data_dir = cfg.storage.data_dir

    # settings.json (written by the UI) wins over the YAML when it exists.
    runtime_settings = effective_settings(cfg, data_dir)

    handle = WorkerHandle(
        cfg.runtime.freecad_cmd,
        REPO_ROOT,
        request_timeout_s=float(cfg.runtime.worker_request_timeout_s),
        startup_timeout_s=float(cfg.runtime.worker_startup_timeout_s),
    )
    if start_worker:
        handle.start()

    store = StoreAdapter(data_dir)
    renderer = RendererAdapter(supersample=cfg.context.render.supersample)
    context = ContextServiceAdapter(store)
    hooks, approvals = build_hooks(cfg, data_dir)

    def _threshold(check_id: str, attr: str, default: float) -> float:
        """Read a per-check threshold, tolerating a config that omits the entry."""
        entry = cfg.verify.check(check_id)
        value = getattr(entry, attr, None) if entry is not None else None
        return default if value is None else value

    gate = Gate(
        build_context_loader(store, handle),
        VerifyConfig(
            bbox_tol_mm=_threshold("bbox_spec", "tol_mm", 0.05),
            mass_tol_ratio=_threshold("mass_spec", "tol_ratio", 0.01),
            solid_count_expect=int(_threshold("solid_count", "expect", 1)),
            round_trip_tol_ratio=_threshold("round_trip", "tol_ratio", 1e-6),
            wall_thickness_min_mm=_threshold("wall_thickness", "min_mm", 1.0),
        ),
    )
    # Hot-swappable: the engine holds this object forever and reads `.chat` at
    # call time, so reconfiguring the model never requires rebuilding this
    # stack (and therefore never restarts the FreeCAD worker).
    llm = HotSwapLlm(runtime_settings.llm)

    rs = runtime_settings.llm
    loop_config = LoopConfig(
        default_strategy=cfg.loop.default_strategy,
        llm_base_url=rs.resolved_base_url(),
        llm_model=rs.resolved_model(),
        llm_api_key=rs.resolved_api_key() or "EMPTY",
        llm_temperature=float(rs.temperature),
        llm_max_tokens=int(rs.max_tokens_per_step),
        llm_request_timeout_s=float(rs.request_timeout_s),
        llm_max_retries=int(rs.max_retries),
        allow_privileged=bool(cfg.policy.allow_privileged),
        visual_checkpoints=tuple(cfg.context.visual_checkpoints),
        artifact_exports=tuple(cfg.storage.artifact_exports),
        workdir=str(REPO_ROOT),
        data_dir=data_dir,
    )

    return SimpleNamespace(
        store=store,
        worker=SyncWorkerClient(handle),
        gate=gate,
        renderer=renderer,
        hooks=hooks,
        context=context,
        llm=llm,
        approvals=approvals,
        config=cfg,
        loop_config=loop_config,
        settings=runtime_settings,
        _worker_handle=handle,
    )


# ══════════════════════════════════════════════════════════════════════════
# runtime reconfiguration
# ══════════════════════════════════════════════════════════════════════════


def apply_llm_settings(
    services: Any, settings: RuntimeSettings, *, persist: bool = True
) -> dict:
    """Point a **live** service stack at new LLM settings.

    Deliberately does not rebuild the stack: the FreeCAD worker process keeps
    running, the store keeps its handles, the Gate keeps its configuration.
    Only three things change:

    * the swappable client (``services.llm``),
    * the engine's ``LoopConfig`` — which is where ``llm_temperature`` is read
      from on *every* call (``engine.py:189``), so leaving it stale would make
      the UI show one temperature and the model receive another,
    * the effective ``Config``, as a **copy**, so anything already holding the
      old one does not observe a mid-turn change.

    Atomic by construction: ``HotSwapLlm.configure`` builds the replacement
    first, so a bad configuration raises here and leaves the working model in
    place. Returns the new descriptor.
    """
    from tcad.config.settings import apply_to_config, save_runtime_settings

    services.llm.configure(settings.llm)

    rs = settings.llm
    lc = services.loop_config
    lc.llm_base_url = rs.resolved_base_url()
    lc.llm_model = rs.resolved_model()
    lc.llm_api_key = rs.resolved_api_key() or "EMPTY"
    lc.llm_temperature = float(rs.temperature)
    lc.llm_max_tokens = int(rs.max_tokens_per_step)
    lc.llm_request_timeout_s = float(rs.request_timeout_s)
    lc.llm_max_retries = int(rs.max_retries)

    services.config = apply_to_config(services.config, settings)
    services.settings = settings

    if persist:
        data_dir = services.config.storage.data_dir
        save_runtime_settings(data_dir, settings)

    return services.llm.descriptor
