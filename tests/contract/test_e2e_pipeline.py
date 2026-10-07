"""END-TO-END integration: real FreeCADCmd → real disk artefacts → real Gate.

This is the test that proves the harness is actually wired together, as opposed
to each layer passing its own unit tests in isolation. It deliberately crosses
every process/format boundary in the design:

    supervisor (this process, pydantic+numpy)
        │  WorkerHandle  ── JSONL over stdio ──▶  FreeCADCmd (stdlib+FreeCAD only)
        │                                              │  IR -> PartDesign/Sketcher
        │  ◀── GeometryDigest dict ────────────────────┘  real BRep measurements
        │
        ├─ writes ir.json + digest.json + *.step/*.stl to disk
        │
        └─ build_check_context(disk only) -> Gate.evaluate() -> GateReport

Nothing here is faked: the geometry is real OCC geometry, the STEP file is a real
file, and the digest the Gate consumes is re-read from disk, not handed over in
memory (design §4.6 "the generator must not grade its own paper").

Run:
    cd /Users/slg/workspace/text_to_cad && \
    .venv/bin/python -m pytest tests/contract/test_e2e_pipeline.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from tcad.core.types import BuildStamp, CheckStatus, GeometryDigest, Severity
from tcad.core.worker_client import WorkerHandle
from tcad.ir.schema import ConstraintExpr, IrDocument, RequirementSpec
from tcad.store.artifacts import write_build_stamp
from tcad.verify.context import build_check_context
from tcad.verify.gate import Gate

REPO_ROOT = Path(__file__).resolve().parents[2]

FREECAD_CMD = os.environ.get(
    "TCAD_FREECAD_CMD",
    str(REPO_ROOT / "free-cad" / "FreeCAD" / "build" / "debug" / "bin" / "FreeCADCmd"),
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(
        not Path(FREECAD_CMD).exists(),
        reason="FreeCADCmd build not found (free-cad/FreeCAD/build/debug/bin/FreeCADCmd)",
    ),
]

# Golden numbers for a 60 x 40 rectangle padded 10 mm.
GOLD_FACES = 6
GOLD_VOLUME = 24000.0
GOLD_AREA = 6800.0
GOLD_EDGES = 12
GOLD_VERTEXES = 8


def make_ir(
    *, model_id: str = "e2e_rect_pad", requirements: RequirementSpec | None = None
) -> IrDocument:
    """A fully-constrained 60x40 rectangle on the XY plane, padded 10 mm.

    The constraint order matters and is not arbitrary: the sketch is bound to the
    origin FIRST (`Coincident(0,1,-1,1)`), and only free endpoints are dimensioned
    afterwards. Dimensioning a point already tied to the origin makes the solver
    conflict, and FreeCAD reports that with a misleading
    "Invalid constraint index" message (design doc 附录 B-2).
    """
    return IrDocument.model_validate(
        {
            "schema_version": 1,
            "model_id": model_id,
            "version": 1,
            "units": "mm",
            "bodies": [
                {
                    "id": "body0",
                    "name": "Body",
                    "sketches": [
                        {
                            "id": "sk0",
                            "name": "base_sketch",
                            "plane": {"kind": "origin_plane", "plane": "XY"},
                            "map_mode": "FlatFace",
                            "require_fully_constrained": True,
                            "geometry": [
                                {"id": "g0", "kind": "line",
                                 "points": [{"x": 0, "y": 0, "z": 0}, {"x": 60, "y": 0, "z": 0}]},
                                {"id": "g1", "kind": "line",
                                 "points": [{"x": 60, "y": 0, "z": 0}, {"x": 60, "y": 40, "z": 0}]},
                                {"id": "g2", "kind": "line",
                                 "points": [{"x": 60, "y": 40, "z": 0}, {"x": 0, "y": 40, "z": 0}]},
                                {"id": "g3", "kind": "line",
                                 "points": [{"x": 0, "y": 40, "z": 0}, {"x": 0, "y": 0, "z": 0}]},
                            ],
                            "constraints": [
                                {"type": "Coincident", "refs": [0, 1, -1, 1]},
                                {"type": "Coincident", "refs": [0, 2, 1, 1]},
                                {"type": "Coincident", "refs": [1, 2, 2, 1]},
                                {"type": "Coincident", "refs": [2, 2, 3, 1]},
                                {"type": "Coincident", "refs": [3, 2, 0, 1]},
                                {"type": "Horizontal", "refs": [0]},
                                {"type": "Horizontal", "refs": [2]},
                                {"type": "Vertical", "refs": [1]},
                                {"type": "Vertical", "refs": [3]},
                                {"type": "DistanceX", "refs": [0, 2], "value": 60.0},
                                {"type": "DistanceY", "refs": [1, 2], "value": 40.0},
                            ],
                        }
                    ],
                    "features": [
                        {"id": "pad0", "name": "pad", "op": "pad",
                         "profile_sketch": "sk0",
                         "params": {"length": 10.0, "type": "Length"}},
                    ],
                }
            ],
            "requirements": (requirements or RequirementSpec()).model_dump(),
            "notes": [],
        }
    )


# ── one real worker for the whole module; it is cheap to keep alive ────────


@pytest.fixture(scope="module")
def worker():
    handle = WorkerHandle(
        FREECAD_CMD, REPO_ROOT, worker_id="e2e",
        startup_timeout_s=180.0, request_timeout_s=180.0,
    )
    handle.start()
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture(scope="module")
def built(worker, tmp_path_factory):
    """Compile + export + introspect once, and lay the artefacts out on disk.

    Stamped before the worker runs, exactly like ``run_commit`` does — the Gate
    refuses to certify the provenance of an unstamped directory.
    """
    artifact_dir = tmp_path_factory.mktemp("e2e_artifacts")
    ir = make_ir()
    ir_dict = json.loads(ir.model_dump_json())
    stamp = BuildStamp(
        attempt_id="e2e-build",
        model_id=ir.model_id,
        ir_version=ir.version,
        started_at=time.time(),
        ir_sha256=hashlib.sha256(ir.model_dump_json().encode("utf-8")).hexdigest(),
    )
    write_build_stamp(artifact_dir, stamp)

    compile_res = worker.request_sync("compile_ir", {"ir": ir_dict, "out_dir": str(artifact_dir), "round_trip": True})
    assert compile_res.get("ok") is True, f"compile failed: {compile_res}"
    assert compile_res.get("errors") == [], f"compile errors: {compile_res.get('errors')}"

    export_res = worker.request_sync(
        "export_artifacts",
        {"ir": ir_dict, "out_dir": str(artifact_dir), "exports": ["step", "stl"]},
    )
    assert export_res.get("ok") is True, f"export failed: {export_res}"
    assert export_res.get("errors") == [], f"export errors: {export_res.get('errors')}"

    digest_dict = worker.request_sync(
        "introspect_document", {"ir": ir_dict, "out_dir": str(artifact_dir)}
    )
    assert digest_dict.get("ok") is True, f"introspect failed: {digest_dict}"

    # Persist exactly what the Gate will read back — nothing passed in memory.
    ir_path = artifact_dir / "ir.json"
    ir_path.write_text(ir.model_dump_json(), encoding="utf-8")
    (artifact_dir / "digest.json").write_text(
        json.dumps({k: v for k, v in digest_dict.items() if k != "ok"}), encoding="utf-8"
    )
    return {
        "artifact_dir": artifact_dir,
        "ir": ir,
        "ir_path": ir_path,
        "stamp": stamp,
        "compile": compile_res,
        "export": export_res,
        "digest": digest_dict,
    }


# ══════════════════════════════════════════════════════════════════════════
# 1. the worker really produced golden geometry
# ══════════════════════════════════════════════════════════════════════════


def test_worker_compiles_golden_geometry(built):
    m = built["compile"]["measurements"]
    assert m["faces"] == GOLD_FACES, m
    assert m["edges"] == GOLD_EDGES, m
    assert m["vertexes"] == GOLD_VERTEXES, m
    assert m["solids"] == 1, m
    assert m["shells"] == 1, m
    assert m["shape_type"] == "Solid", m
    assert m["is_valid"] is True, m
    assert m["volume"] == pytest.approx(GOLD_VOLUME, rel=1e-9), m
    assert m["area"] == pytest.approx(GOLD_AREA, rel=1e-9), m
    assert m["bbox"]["x"] == pytest.approx(60.0)
    assert m["bbox"]["y"] == pytest.approx(40.0)
    assert m["bbox"]["z"] == pytest.approx(10.0)


def test_step_round_trip_is_exact(built):
    """The golden reproducibility standard: export → re-import → identical volume."""
    rt = built["compile"]["round_trip"]
    assert rt["ok"] is True, rt
    assert rt["rel_err"] < 1e-6, f"STEP round-trip relative volume error = {rt['rel_err']}"
    assert rt["rel_err"] == 0.0, f"expected bit-exact, got {rt['rel_err']}"


def test_exports_landed_on_disk(built):
    adir = built["artifact_dir"]
    assert (adir / "e2e_rect_pad.step").is_file()
    assert (adir / "e2e_rect_pad.stl").is_file()
    assert (adir / "e2e_rect_pad.step").stat().st_size > 0
    assert (adir / "e2e_rect_pad.stl").stat().st_size > 0


# ══════════════════════════════════════════════════════════════════════════
# 2. the digest is a valid GeometryDigest, and carries the reserved keys
# ══════════════════════════════════════════════════════════════════════════


def test_digest_validates_against_the_frozen_contract(built):
    d = GeometryDigest.model_validate(
        {k: v for k, v in built["digest"].items() if k != "ok"}
    )
    assert d.model_id == "e2e_rect_pad"
    assert d.measurements_available is True
    assert d.is_valid is True
    assert d.topology.faces == GOLD_FACES
    assert d.volume == pytest.approx(GOLD_VOLUME)
    assert d.text.strip(), "digest text must not be empty"


def test_digest_carries_sketch_constraint_state(built):
    """The worker↔Gate convention for per-sketch constraint state.

    These reserved keys are the ONLY carrier for "every sketch must be fully
    constrained". If the worker stops emitting them the check silently degrades
    to SKIP, which is how this invariant could disappear without any test going
    red — so it gets its own assertion.
    """
    kd = built["digest"]["key_dimensions"]
    assert kd.get("sk0__fully_constrained") == 1.0, kd
    assert kd.get("sk0__dof") == 0.0, kd


# ══════════════════════════════════════════════════════════════════════════
# 3. the real Gate, reading real files, judges the real model
# ══════════════════════════════════════════════════════════════════════════


def test_gate_passes_on_the_real_model(built, worker):
    ctx = build_check_context(
        model_id="e2e_rect_pad",
        ir_version=1,
        artifact_dir=str(built["artifact_dir"]),
        ir_path=str(built["ir_path"]),
        worker=worker,
    )
    report = Gate(lambda m, v: ctx).evaluate("e2e_rect_pad", 1)

    details = {r.check_id: (r.status.value, r.message) for r in report.results}
    assert report.passed is True, f"Gate failed: {report.blocking_failures}\n{details}"
    assert report.blocking_failures == [], f"{report.blocking_failures}\n{details}"

    # Every BLOCKING check must have genuinely verified something — or say that it
    # did not. `bbox_spec` / `mass_spec` skip when no such requirement was
    # recorded, which is honest reporting rather than a silent pass; anything else
    # skipping here would mean an invariant quietly left the Gate.
    blocking = {r.check_id for r in report.results if r.severity.value == "blocking"}
    allowed_skips = {"bbox_spec", "mass_spec"}
    blocking_skips = [c for c in report.skipped_checks if c in blocking]
    assert set(blocking_skips) <= allowed_skips, f"blocking checks skipped: {blocking_skips}"

    # `wall_thickness` is advisory (design §4.6 item 4) when the user recorded no
    # wall requirement, so it can never affect `passed` here. It used to be
    # EXPECTED to skip, because the worker computed no `min_wall_thickness` at
    # all; it now measures one off the BRep, so it must actually run. The
    # invariant that matters either way: it is *reported*, never silent.
    by_id_wall = {r.check_id: r for r in report.results}.get("wall_thickness")
    assert by_id_wall is not None or "wall_thickness" in report.skipped_checks, (
        "the wall check is absent from both results and skips — silently dropped"
    )
    if by_id_wall is not None:
        assert by_id_wall.severity is Severity.ADVISORY, (
            "with no wall requirement recorded this check must stay advisory; "
            "making it blocking would fail builds for a shop default"
        )

    by_id = {r.check_id: r for r in report.results}
    # Geometry self-consistency: these must have genuinely run and passed.
    for check_id in (
        "solid_validity", "solid_count",
        "sketch_fully_constrained", "round_trip", "exportability", "provenance",
    ):
        assert by_id[check_id].status is CheckStatus.PASS, (check_id, by_id[check_id].message)
    # The verdict says which build it graded, and that build is the one the
    # fixture just ran: the STEP the Gate read was written after the stamp.
    assert report.attempt_id == built["stamp"].attempt_id
    assert report.ir_sha256 == built["stamp"].ir_sha256
    # The spec checks have no requirement to judge against in this fixture, so
    # they must say so by skipping. Reporting a pass for work they did not do is
    # precisely the failure mode being guarded.
    for check_id in ("bbox_spec", "mass_spec"):
        assert by_id[check_id].status is CheckStatus.SKIP, (check_id, by_id[check_id].status)

    # round_trip read the STEP back from disk, through the worker's import path.
    rt = by_id["round_trip"]
    assert rt.measurements.get("step_volume") == pytest.approx(GOLD_VOLUME), rt.measurements
    assert rt.evidence, "round_trip must cite the file it read"


def test_gate_blocks_a_wrong_requirement(built, worker, tmp_path):
    """A confirmed requirement the model violates must fail the Gate, and the
    failure must name what to change."""
    req = RequirementSpec(constraints=[
        ConstraintExpr(
            kind="bbox", value={"x": 100.0, "y": 40.0, "z": 10.0}, tol=0.05,
            source_text="width must be 100mm", confirmed=True,
        ),
    ])
    ir = make_ir(model_id="e2e_rect_pad", requirements=req)
    ir_path = built["artifact_dir"] / "ir_negative.json"
    ir_path.write_text(ir.model_dump_json(), encoding="utf-8")

    ctx = build_check_context(
        model_id="e2e_rect_pad",
        ir_version=1,
        artifact_dir=str(built["artifact_dir"]),
        ir_path=str(ir_path),
        worker=worker,
    )
    report = Gate(lambda m, v: ctx).evaluate("e2e_rect_pad", 1)

    assert report.passed is False
    assert "bbox_spec" in report.blocking_failures, report.blocking_failures
    r = next(r for r in report.results if r.check_id == "bbox_spec")
    assert r.status is CheckStatus.FAIL
    assert r.feature_id, "an attributable failure must name the feature/body"
    assert r.expected, "the report must show what was expected"
    assert os.path.isfile(ctx.exports["step"]), "Gate must have read the real export"


def test_unconfirmed_requirement_cannot_block_the_real_pipeline(built, worker):
    """Same wrong requirement, but unconfirmed -> advisory only, build still passes.

    This is the load-bearing rule from design §4.6 item 5 and it is worth proving
    end-to-end, not just against a fixture.
    """
    req = RequirementSpec(constraints=[
        ConstraintExpr(
            kind="bbox", value={"x": 100.0, "y": 40.0, "z": 10.0}, tol=0.05,
            source_text="maybe 100mm wide?", confirmed=False,
        ),
    ])
    ir = make_ir(model_id="e2e_rect_pad", requirements=req)
    ir_path = built["artifact_dir"] / "ir_unconfirmed.json"
    ir_path.write_text(ir.model_dump_json(), encoding="utf-8")

    ctx = build_check_context(
        model_id="e2e_rect_pad",
        ir_version=1,
        artifact_dir=str(built["artifact_dir"]),
        ir_path=str(ir_path),
        worker=worker,
    )
    report = Gate(lambda m, v: ctx).evaluate("e2e_rect_pad", 1)
    assert report.passed is True, report.blocking_failures


# ══════════════════════════════════════════════════════════════════════════
# 4. the worker's own API self-test, run against the live build
# ══════════════════════════════════════════════════════════════════════════


def test_worker_api_selftest_passes(worker):
    st = worker.request_sync("api_selftest", {})
    assert st.get("ok") is True, f"documented FreeCAD APIs changed: missing={st.get('missing')}"
    assert st.get("missing") == [], st.get("missing")


@pytest.mark.parametrize("method", ["compile_ir", "build_artifacts"])
def test_default_build_creates_no_download_conversions(worker, tmp_path, method):
    ir = make_ir().model_dump(mode="json")
    response = worker.request_sync(method, {"ir": ir, "out_dir": str(tmp_path), "exports": []}, timeout_s=180)
    assert response.get("ok"), response
    assert (tmp_path / (ir["model_id"] + ".FCStd")).is_file()
    assert not any(p.suffix.lower() in {".step", ".stl", ".brep"} for p in tmp_path.rglob("*"))
    if method == "compile_ir":
        assert response["round_trip"] is None
    else:
        assert set(response["files"]) == {"fcstd"}
        assert response["scene"]["ok"]
