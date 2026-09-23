"""End-to-end proof that the WIRED stack works, not just the pieces.

Every other test in this repo exercises one subsystem, usually with fakes for the
rest. This one starts from a real `Config`, calls the real
`tcad.core.wiring.build_services()`, and drives the real commit pipeline
(`tcad.loop.commit.run_commit`) against a real FreeCADCmd process.

It exists because "all the unit tests pass" and "the system works" are different
claims. The adapters in `tcad/core/wiring.py` reconcile several genuine shape
mismatches between the collaborators (async worker vs sync callers, library
functions vs Protocol objects, bare results vs ok/error envelopes); if any of
them were wrong, every unit test would still be green and nothing would run.

Run:
    cd /Users/slg/workspace/text_to_cad && \
    .venv/bin/python -m pytest tests/contract/test_wired_pipeline.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from tcad.config.loader import load_default_config, resolve_paths
from tcad.core.wiring import SyncWorkerClient, build_services
from tcad.core.worker_client import WorkerHandle
from tcad.loop.commit import run_commit
from tcad.store.artifacts import read_build_stamp

REPO_ROOT = Path(__file__).resolve().parents[2]
FREECAD_CMD = os.environ.get(
    "TCAD_FREECAD_CMD",
    str(REPO_ROOT / "free-cad" / "FreeCAD" / "build" / "debug" / "bin" / "FreeCADCmd"),
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(
        not Path(FREECAD_CMD).exists(),
        reason="FreeCADCmd build not found",
    ),
]

GOLD_VOLUME = 24000.0


# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def services(tmp_path_factory):
    """A fully wired, live stack on a throwaway data directory."""
    data_dir = tmp_path_factory.mktemp("wired_data")
    cfg = load_default_config()
    cfg.storage.data_dir = str(data_dir)
    cfg.storage.sqlite_path = str(data_dir / "tcad.sqlite3")
    cfg.runtime.freecad_cmd = FREECAD_CMD
    resolve_paths(cfg, root=REPO_ROOT)

    svc = build_services(cfg)
    try:
        yield svc
    finally:
        svc._worker_handle.close()


def rect_pad_ir(model_id: str):
    from tests.contract.test_e2e_pipeline import make_ir

    return make_ir(model_id=model_id)


# ══════════════════════════════════════════════════════════════════════════
# 1. the stack actually assembles
# ══════════════════════════════════════════════════════════════════════════


def test_build_services_produces_a_live_stack(services):
    for attr in ("store", "worker", "gate", "renderer", "hooks", "context", "llm"):
        assert hasattr(services, attr), f"missing collaborator: {attr}"
    assert isinstance(services.worker, SyncWorkerClient)
    assert services._worker_handle.is_alive(), "FreeCADCmd should be running"


def test_sync_worker_adapter_speaks_the_envelope_language(services):
    """The tools and commit.py call `request(...)` synchronously and then read
    `res["ok"]` / `res["result"]`. The underlying handle is async and returns a
    bare result — this asserts the adapter reconciles both."""
    res = services.worker.request("ping", {}, timeout_s=30.0)
    assert isinstance(res, dict) and res["ok"] is True
    assert isinstance(res["result"], dict)

    bad = services.worker.request("definitely_not_a_method")
    assert bad["ok"] is False
    assert bad["error"]["kind"] == "not_found"
    assert "message" in bad["error"]


def test_store_adapter_implements_the_store_protocol(services):
    """IrStore alone lacks validate_* / persist_digest; the adapter supplies them."""
    ir = rect_pad_ir("wired_store")
    services.store.create("wired_store", ir)

    loaded = services.store.load("wired_store")
    assert loaded.model_id == "wired_store"
    assert isinstance(services.store.current_version("wired_store"), int)

    # a good document validates cleanly
    assert services.store.validate_document(loaded) == []

    # a broken one is rejected with an attributable error
    from tcad.ir.schema import FeatureSpec

    loaded.bodies[0].features.append(
        FeatureSpec(id="ft_bad", name="bad", op="pad", profile_sketch="does_not_exist")
    )
    errors = services.store.validate_document(loaded)
    assert errors, "a dangling profile_sketch must be reported"
    assert all(e.kind.value == "semantic" for e in errors)
    assert any(e.feature_id == "ft_bad" for e in errors), [e.feature_id for e in errors]


def test_hook_stack_contains_the_builtin_guards(services):
    """The config declares the guards by name; the wiring injects their
    dependencies through the dispatcher registry."""
    names = {h.name for h in services.hooks.hooks}
    assert {"path_guard", "privileged_triple_gate"} <= names

    # fail-closed: no sandbox probe configured -> the privileged gate denies.
    from tcad.core.types import HookDecision, HookEvent, ToolTier

    res = services.hooks.dispatch(
        HookEvent.PRE_TOOL_USE,
        {"tool_name": "raw_python", "tier": ToolTier.PRIVILEGED.value, "args": {}},
    )
    assert res.decision is HookDecision.DENY
    assert "privileged" in (res.reason + res.hook_name).lower()


def test_path_guard_is_active_on_the_default_deny_list(services):
    from tcad.core.types import HookDecision, HookEvent

    res = services.hooks.dispatch(
        HookEvent.PRE_TOOL_USE,
        {"tool_name": "asset_export", "args": {"path": os.path.expanduser("~/.ssh/id_rsa")}},
    )
    assert res.decision is HookDecision.DENY


# ══════════════════════════════════════════════════════════════════════════
# 2. the commit pipeline runs end to end through the wired stack
# ══════════════════════════════════════════════════════════════════════════


def test_full_commit_pipeline_passes_the_gate(services):
    """compile in FreeCAD -> export to disk -> persist digest -> disk-only Gate."""
    model_id = "wired_commit"
    ir = rect_pad_ir(model_id)
    created = services.store.create(model_id, ir)
    version = int(created.version)

    result, report = asyncio.run(
        run_commit(
            services,
            model_id=model_id,
            ir_version=version,
            message="initial build",
            workdir=str(REPO_ROOT),
            data_dir=str(services.store.data_dir),
        )
    )

    assert report is not None, f"pipeline never reached the Gate: {result.content}"
    assert report.passed is True, (
        f"Gate failed: {report.blocking_failures}\n"
        f"{[(r.check_id, r.status.value, r.message) for r in report.results]}"
    )
    assert result.ok is True

    # The digest the Gate consumed was re-read from disk, so it must exist there.
    digest_path = services.store.artifact_dir(model_id, version) / "digest.json"
    assert digest_path.is_file(), "the Gate cannot have read a digest that was never written"
    digest = json.loads(digest_path.read_text(encoding="utf-8"))
    assert digest["volume"] == pytest.approx(GOLD_VOLUME)
    assert digest["topology"]["faces"] == 6
    assert digest["measurements_available"] is True

    # And so must the exports.
    step = services.store.artifact_dir(model_id, version) / f"{model_id}.step"
    assert step.is_file() and step.stat().st_size > 0


def test_gate_reads_the_digest_from_disk_not_from_the_pipeline(services):
    """Tamper with the on-disk digest and the Gate's verdict must change.

    If the Gate were grading the pipeline's in-memory digest, editing the file
    would have no effect. This is the structural check behind "the generator must
    not grade its own paper".
    """
    model_id = "wired_tamper"
    ir = rect_pad_ir(model_id)
    created = services.store.create(model_id, ir)
    version = int(created.version)

    _, report = asyncio.run(
        run_commit(
            services, model_id=model_id, ir_version=version, message="build",
            workdir=str(REPO_ROOT), data_dir=str(services.store.data_dir),
        )
    )
    assert report is not None and report.passed is True

    # Corrupt the persisted measurement on disk.
    digest_path = services.store.artifact_dir(model_id, version) / "digest.json"
    payload = json.loads(digest_path.read_text(encoding="utf-8"))
    payload["topology"]["faces"] = 99
    payload["volume"] = 1.0
    digest_path.write_text(json.dumps(payload), encoding="utf-8")

    # round_trip compares the digest against the STEP re-read from disk, so a
    # tampered digest must show up as a mismatch.
    report2 = services.gate.evaluate(model_id, version)
    rt = next(r for r in report2.results if r.check_id == "round_trip")
    assert rt.status.value == "fail", (
        "editing digest.json on disk did not change the verdict — the Gate is "
        "reading from memory, not from disk"
    )
    assert report2.passed is False


def test_missing_digest_cannot_attest_and_does_not_explode(services):
    """A commit whose digest never got written must produce a *not-passed*
    report with a legible reason — never an exception that kills the Turn."""
    model_id = "wired_nodigest"
    ir = rect_pad_ir(model_id)
    created = services.store.create(model_id, ir)
    version = int(created.version)

    # Seed the snapshot + say nothing else: no digest.json, no exports.
    report = services.gate.evaluate(model_id, version)

    assert report.passed is False
    assert "gate:cannot_attest_no_measurements" in report.blocking_failures
    assert report.skipped_checks, "the report must show what it could not verify"


def test_context_service_serves_a_digest_with_text(services):
    model_id = "wired_ctx"
    created = services.store.create(model_id, rect_pad_ir(model_id))
    version = int(created.version)
    asyncio.run(
        run_commit(
            services, model_id=model_id, ir_version=version, message="build",
            workdir=str(REPO_ROOT), data_dir=str(services.store.data_dir),
        )
    )

    digest = services.context.digest(model_id, version)
    assert digest.text.strip(), "the digest text is what the model reads"
    assert "pad" in digest.text
    assert digest.volume == pytest.approx(GOLD_VOLUME)


def test_context_service_marks_the_unknown_when_measurements_are_absent(services):
    model_id = "wired_ctx_empty"
    services.store.create(model_id, rect_pad_ir(model_id))
    digest = services.context.digest(model_id, 0)
    assert digest.measurements_available is False
    assert "未验证几何" in digest.text


# ══════════════════════════════════════════════════════════════════════════
# 3. the renderer turns a real mesh into real PNGs
# ══════════════════════════════════════════════════════════════════════════


def test_renderer_adapter_renders_a_real_mesh_to_pngs(services):
    from tcad.core.types import BBox, Mesh

    model_id = "wired_render"
    created = services.store.create(model_id, rect_pad_ir(model_id))
    version = int(created.version)

    res = services.worker.request(
        "tessellate",
        {
            "ir": created.model_dump(),
            "out_dir": str(services.store.artifact_dir(model_id, version)),
        },
        timeout_s=60.0,
    )
    assert res["ok"] is True, res
    raw = res["result"]["mesh"]
    bb = raw.get("bbox") or {}
    mesh = Mesh(
        vertices=[tuple(v) for v in raw["vertices"]],
        facets=[tuple(f) for f in raw["facets"]],
        bbox=BBox(**{k: float(bb.get(k, 0.0)) for k in
                     ("x", "y", "z", "x_min", "y_min", "z_min")}),
        volume=float(raw.get("volume", 0.0)),
        tolerance=float(raw.get("tolerance", 0.5)),
    )

    out_dir = str(services.store.artifact_dir(model_id, version) / "views")
    images = services.renderer.render(
        mesh, out_dir=out_dir, views=["iso", "front"], style="flat_edges",
        width=320, height=240,
    )

    assert len(images) == 2
    for img in images:
        p = Path(img.path)
        assert p.is_file() and p.stat().st_size > 0
        assert (img.width, img.height) == (320, 240)
        assert img.tokens_estimate > 0, "image token cost must be accounted for"
        with open(p, "rb") as fh:
            assert fh.read(8) == b"\x89PNG\r\n\x1a\n"


# ══════════════════════════════════════════════════════════════════════════
# 4. the engine's forwarder points at the real wiring
# ══════════════════════════════════════════════════════════════════════════


def test_engine_forwarder_refuses_incomplete_config():
    """The old implementation of this method was a stub with invented import
    paths that raised ImportError. It now fails with an actionable message."""
    from tcad.loop.engine import LoopConfig, LoopEngine

    with pytest.raises(TypeError, match="Config"):
        LoopEngine.build_default_services(LoopConfig())

    with pytest.raises(TypeError, match="Config"):
        LoopEngine.build_default_services(None)


# ══════════════════════════════════════════════════════════════════════════
# 5. a broken sketch must reach the caller as a solver error, not a silent pass
# ══════════════════════════════════════════════════════════════════════════


def _over_determined_ir(model_id: str):
    """A rectangle with a deliberately over-determined constraint set.

    Dimensioning both a line's start point and its end point in absolute
    coordinates, on top of Horizontal/Vertical constraints, is a solver conflict.
    This is not a contrived input — it is the mistake a real author made, and it
    is the case that exposed the whole silent-failure chain.
    """
    from tests.contract.test_e2e_pipeline import make_ir
    from tcad.ir.schema import SketchConstraint

    ir = make_ir(model_id=model_id)
    sk = ir.bodies[0].sketches[0]
    sk.geometry = [
        g.model_copy(update={"points": [p.model_copy(update={"x": 20.0, "y": 15.0})
                                        if i == 0 else p
                                        for i, p in enumerate(g.points)]})
        for g in sk.geometry
    ]
    sk.constraints = [
        SketchConstraint(**c) for c in (
            {"type": "Coincident", "refs": [0, 2, 1, 1]},
            {"type": "Coincident", "refs": [1, 2, 2, 1]},
            {"type": "Coincident", "refs": [2, 2, 3, 1]},
            {"type": "Coincident", "refs": [3, 2, 0, 1]},
            {"type": "Horizontal", "refs": [0]},
            {"type": "Horizontal", "refs": [2]},
            {"type": "Vertical", "refs": [1]},
            {"type": "Vertical", "refs": [3]},
            # both endpoints of line 0 pinned in X and Y, plus the opposite corner:
            # over-determined against the H/V constraints.
            {"type": "DistanceX", "refs": [0, 1], "value": 20.0},
            {"type": "DistanceY", "refs": [0, 1], "value": 15.0},
            {"type": "DistanceX", "refs": [0, 2], "value": 60.0},
            {"type": "DistanceY", "refs": [1, 2], "value": 35.0},
        )
    ]
    return ir


def test_broken_sketch_is_reported_as_a_solver_error(services):
    """The chain that made a broken model look like a *successful* build.

    Three defects lined up and this test pins all three:

      1. the worker wrapped a handler's own ``ok=false`` in an outer ``ok=true``,
         so a failed compile looked like a successful call;
      2. ``run_commit`` ignored the compile result and walked on to the Gate;
      3. the Gate, having no measurements, could only say "cannot attest".

    Net effect for the model: no idea that its sketch had conflicting constraints.
    Now the solver error must reach the caller, naming the sketch.
    """
    model_id = "wired_broken"
    created = services.store.create(model_id, _over_determined_ir(model_id))
    version = int(created.version)

    result, report = asyncio.run(
        run_commit(
            services, model_id=model_id, ir_version=version, message="broken sketch",
            workdir=str(REPO_ROOT), data_dir=str(services.store.data_dir),
        )
    )

    text = result.content
    assert report is None or report.passed is False, "a broken sketch must never pass"
    assert result.ok is False, f"expected a tool error, got: {text[:400]}"
    assert result.error is not None
    assert result.error.kind.value == "solver", result.error.kind
    assert result.error.feature_id == "sk0", result.error.feature_id
    assert "conflict" in result.error.message.lower(), result.error.message


def test_worker_envelope_never_flattens_nested_failure(services):
    """Call-level check on the same defect, without the commit pipeline in between."""
    ir = _over_determined_ir("wired_envelope")
    services.store.create("wired_envelope", ir)
    env = services.worker.request(
        "compile_ir",
        {"ir": ir.model_dump(), "out_dir": str(services.store.artifact_dir("wired_envelope", 0))},
        timeout_s=120.0,
    )
    assert env["ok"] is False, "a failed compile must not be reported as a successful call"
    assert env["error"]["kind"] == "solver"
    assert env["error"]["feature_id"] == "sk0"


def test_upstream_failures_are_named_when_the_gate_cannot_attest(services):
    """When export/measure fail, the report must say so instead of leaving the
    model with an unexplained "cannot attest"."""
    model_id = "wired_upstream"
    created = services.store.create(model_id, rect_pad_ir(model_id))
    version = int(created.version)

    # Make the worker refuse to export or measure, exactly as it would if the
    # solid were not there.
    real_request = services.worker.request

    def picky(method, params=None, *, timeout_s=30.0):
        if method in ("export_artifacts", "introspect_document"):
            return {"ok": False, "error": {"kind": "compile", "message": f"{method} refused"}}
        return real_request(method, params, timeout_s=timeout_s)

    services.worker.request = picky
    try:
        result, report = asyncio.run(
            run_commit(
                services, model_id=model_id, ir_version=version, message="degraded",
                workdir=str(REPO_ROOT), data_dir=str(services.store.data_dir),
            )
        )
    finally:
        services.worker.request = real_request

    assert report is not None and report.passed is False
    assert "artefact export failed" in result.content
    assert "geometry measurement failed" in result.content
    assert "export_artifacts refused" in result.content
    assert "introspect_document refused" in result.content


# ══════════════════════════════════════════════════════════════════════════
# 5. provenance — the Gate may only grade files this attempt wrote
# ══════════════════════════════════════════════════════════════════════════


def _commit(services, model_id: str, *, message: str = "build", version: int | None = None):
    """Create (or reuse) a model and run one real commit attempt."""
    if version is None:
        created = services.store.create(model_id, rect_pad_ir(model_id))
        version = int(created.version)
    result, report = asyncio.run(
        run_commit(
            services, model_id=model_id, ir_version=version, message=message,
            workdir=str(REPO_ROOT), data_dir=str(services.store.data_dir),
        )
    )
    return version, result, report


def _provenance(report):
    return next(r for r in report.results if r.check_id == "provenance")


def test_live_build_stamps_the_artefacts_it_is_graded_on(services):
    """The stamp must exist, name this build, and predate this build's files."""
    model_id = "wired_provenance"
    version, result, report = _commit(services, model_id)
    assert report is not None, f"never reached the Gate: {result.content[:300]}"
    assert report.passed is True, report.blocking_failures

    artifact_dir = services.store.artifact_dir(model_id, version)
    stamp = read_build_stamp(artifact_dir)
    assert stamp is not None, "run_commit must stamp before it compiles"
    assert (stamp.model_id, stamp.ir_version) == (model_id, version)
    assert len(stamp.ir_sha256) == 64, "the IR handed to the compiler must be hashed"

    prov = _provenance(report)
    assert prov.status.value == "pass", prov.message
    assert report.attempt_id == stamp.attempt_id

    step = artifact_dir / f"{model_id}.step"
    assert step.stat().st_mtime >= stamp.started_at, (
        "the graded STEP predates this attempt — it is a leftover")


def test_retry_of_the_same_version_gets_its_own_attempt(services):
    """Two commits into v<N> must not share a verdict's evidence.

    The artefact directory is keyed by version, so a retry overwrites the files
    but is a different build. Each attempt has to be identifiable on disk.
    """
    model_id = "wired_retry"
    version, _, first = _commit(services, model_id, message="first")
    _, _, second = _commit(services, model_id, message="retry", version=version)
    assert first is not None and second is not None
    assert first.passed is True and second.passed is True
    assert first.attempt_id != second.attempt_id
    assert _provenance(second).status.value == "pass"
    assert read_build_stamp(
        services.store.artifact_dir(model_id, version)).attempt_id == second.attempt_id


def test_leftover_export_cannot_satisfy_a_later_grading(services):
    """An old file with the right name must not read as a delivered artefact."""
    model_id = "wired_leftover"
    version, _, report = _commit(services, model_id)
    assert report is not None and report.passed is True

    artifact_dir = services.store.artifact_dir(model_id, version)
    step = artifact_dir / f"{model_id}.step"
    stamp = read_build_stamp(artifact_dir)
    back = stamp.started_at - 3600.0
    os.utime(step, (back, back))

    regraded = services.gate.evaluate(model_id, version)
    prov = _provenance(regraded)
    assert prov.status.value == "fail", prov.message
    assert "step" in prov.message
    assert regraded.passed is False
    assert "provenance" in regraded.blocking_failures


def test_unstamped_directory_is_not_a_free_pass(services):
    """A directory nobody stamped cannot certify its own provenance."""
    model_id = "wired_unstamped"
    version, _, report = _commit(services, model_id)
    assert report is not None and report.passed is True

    stamp_path = services.store.artifact_dir(model_id, version) / "build_stamp.json"
    stamp_path.unlink()
    regraded = services.gate.evaluate(model_id, version)
    prov = _provenance(regraded)
    assert prov.status.value == "fail", prov.message
    assert "build stamp" in prov.message
    assert prov.status.value != "skip", (
        "'cannot verify' must not degrade into an ignorable SKIP")
    assert regraded.passed is False


# ══════════════════════════════════════════════════════════════════════════
# 6. staging — a build is graded in private, published only if it passed
# ══════════════════════════════════════════════════════════════════════════


def test_the_gate_grades_an_isolated_staging_directory(services):
    """The Gate must never grade the directory a failed attempt can pollute."""
    model_id = "wired_staging_target"
    created = services.store.create(model_id, rect_pad_ir(model_id))
    version = int(created.version)

    seen: dict = {}
    real_evaluate = services.gate.evaluate

    def remember(mid, ver, *, artifact_dir=None):
        seen["artifact_dir"] = artifact_dir
        return real_evaluate(mid, ver, artifact_dir=artifact_dir)

    services.gate.evaluate = remember
    try:
        _, report = asyncio.run(run_commit(
            services, model_id=model_id, ir_version=version, message="staged",
            workdir=str(REPO_ROOT), data_dir=str(services.store.data_dir),
        ))
    finally:
        services.gate.evaluate = real_evaluate

    assert report is not None and report.passed is True, report.blocking_failures
    graded = seen["artifact_dir"]
    assert graded, "the Gate was not told which directory to grade"
    assert ".staging-" in graded, graded
    canonical = services.store.artifact_dir(model_id, version)
    assert Path(graded) != canonical
    # ...and the verified attempt did become the version's artifacts.
    assert canonical.is_dir() and (canonical / f"{model_id}.step").is_file()


def test_a_failed_build_is_never_published(services):
    """No export, no digest -> the version directory must stay empty."""
    model_id = "wired_staging_fail"
    created = services.store.create(model_id, rect_pad_ir(model_id))
    version = int(created.version)
    canonical = services.store.artifact_dir(model_id, version)

    real_request = services.worker.request

    def picky(method, params=None, *, timeout_s=30.0):
        if method in ("export_artifacts", "introspect_document"):
            return {"ok": False, "error": {"kind": "compile", "message": f"{method} refused"}}
        return real_request(method, params, timeout_s=timeout_s)

    services.worker.request = picky
    try:
        _, report = asyncio.run(run_commit(
            services, model_id=model_id, ir_version=version, message="degraded",
            workdir=str(REPO_ROOT), data_dir=str(services.store.data_dir),
        ))
    finally:
        services.worker.request = real_request

    assert report is not None and report.passed is False
    assert not canonical.exists() or not any(
        p.suffix.lower() in (".step", ".stl", ".fcstd") for p in canonical.rglob("*")
    ), f"a failed build leaked artifacts: {sorted(p.name for p in canonical.rglob('*')) if canonical.exists() else []}"

    parent = canonical.parent
    leftovers = [p.name for p in parent.iterdir() if ".staging-" in p.name] if parent.exists() else []
    assert leftovers == [], f"failed attempts left staging directories behind: {leftovers}"


def test_a_published_build_carries_a_manifest(services):
    """The version directory lists its own files, hashed, bound to the attempt."""
    import hashlib as _hashlib

    model_id = "wired_manifest"
    version, _, report = _commit(services, model_id)
    assert report is not None and report.passed is True, report.blocking_failures

    canonical = services.store.artifact_dir(model_id, version)
    manifest_path = canonical / "manifest.json"
    assert manifest_path.is_file(), "a published build must carry its artifact list"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["model_id"] == model_id
    assert manifest["ir_version"] == version
    assert manifest["attempt_id"] == report.attempt_id
    for name, meta in manifest["files"].items():
        data = (canonical / name).read_bytes()
        assert meta["bytes"] == len(data), name
        assert meta["sha256"] == _hashlib.sha256(data).hexdigest(), name


def test_a_failed_retry_leaves_the_verified_build_in_place(services):
    """Recovery is "do nothing": the last green build is never damaged."""
    model_id = "wired_failed_retry"
    version, _, first = _commit(services, model_id, message="first")
    assert first is not None and first.passed is True

    canonical = services.store.artifact_dir(model_id, version)
    manifest_before = json.loads((canonical / "manifest.json").read_text(encoding="utf-8"))
    step_before = (canonical / f"{model_id}.step").read_bytes()
    assert manifest_before["attempt_id"] == first.attempt_id

    real_request = services.worker.request

    def picky(method, params=None, *, timeout_s=30.0):
        if method in ("export_artifacts", "introspect_document"):
            return {"ok": False, "error": {"kind": "compile", "message": f"{method} refused"}}
        return real_request(method, params, timeout_s=timeout_s)

    services.worker.request = picky
    try:
        _, second = asyncio.run(run_commit(
            services, model_id=model_id, ir_version=version, message="retry",
            workdir=str(REPO_ROOT), data_dir=str(services.store.data_dir),
        ))
    finally:
        services.worker.request = real_request

    assert second is not None and second.passed is False
    # The failed retry must not have overwritten the verified evidence.
    assert (canonical / f"{model_id}.step").read_bytes() == step_before
    manifest_after = json.loads((canonical / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_after["attempt_id"] == first.attempt_id
