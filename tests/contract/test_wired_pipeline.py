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
