"""Build the downloadable acceptance bundle (review/acceptance/).

Drives the mandatory samples A/B/C through a REAL FreeCAD worker — the same IR
builders the contract tests use:

  turn 1: compile → export (STEP/STL/FCStd) → digest → analytic verification
          → multi-view previews from the worker's own tessellation
  turn 2: parametric edit via reopen_edit_measure on the delivered FCStd
          (sample B across a worker restart, exactly like the contract test)

Every number is measured by the kernel; any mismatch aborts the run BEFORE the
manifest is written, so a manifest on disk always describes a fully verified
bundle. Artifacts, IR, requirement contract, and manifest are bound together by
model_id + ir_version + attempt_id + sha256.

Usage:
    .venv/bin/python tools/build_acceptance_artifacts.py
    TCAD_FREECAD_CMD=/path/to/FreeCADCmd .venv/bin/python tools/build_acceptance_artifacts.py
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tcad.core.worker_client import WorkerHandle  # noqa: E402
from tcad.core.types import Mesh, ViewName  # noqa: E402
from tcad.render.png import write_png  # noqa: E402
from tcad.render.raster import render_views  # noqa: E402
from tcad.worker.protocol import (  # noqa: E402
    M_COMPILE_IR,
    M_EXPORT,
    M_IMPORT_ASSET,
    M_INTROSPECT,
    M_REOPEN_EDIT,
    M_TESSELLATE,
)

from tests.contract.test_samples_acceptance import holes_ir, slot_ir, tube_ir  # noqa: E402
from tests.contract.test_revolution import stepped_shaft_ir  # noqa: E402
from tests.contract.test_groove import grooved_cylinder_ir  # noqa: E402

FREECAD_CMD = Path(os.environ.get(
    "TCAD_FREECAD_CMD",
    str(REPO_ROOT / "free-cad" / "FreeCAD" / "build" / "debug" / "bin" / "FreeCADCmd"),
))
OUT_ROOT = REPO_ROOT / "review" / "acceptance"

PI = math.pi
VOL_REL = 1e-6  # same tolerances as tests/contract/test_samples_acceptance.py
ABS = 1e-6

ATTEMPT_ID = f"acc-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


class AcceptanceError(RuntimeError):
    pass


def check(name: str, got: float, expect: float, rel: float = VOL_REL, abs_tol: float = ABS) -> None:
    if abs(got - expect) > max(rel * abs(expect), abs_tol):
        raise AcceptanceError(f"{name}: measured {got!r}, expected {expect!r}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def freecad_version() -> str:
    try:
        out = subprocess.run(
            [str(FREECAD_CMD), "--version"], capture_output=True, text=True, timeout=60,
        )
        return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else "unknown"
    except Exception:  # noqa: BLE001 — version is informative, not gating
        return "unknown"


def source_digest() -> dict:
    """Which code built this bundle — the one fact ``git`` normally supplies.

    The bundle is only evidence if someone can tell what produced it, and this
    workspace has no readable git metadata (``git status`` is refused by an
    unaccepted Xcode licence; the report's header says so). So the bundle
    carries its own answer: a digest over every ``*.py`` the build depends on —
    the product (``tcad/``), the producers (``tools/``) and the IR builders the
    script imports out of ``tests/``. Same digest => same inputs; different
    digest => rebuild before trusting these numbers.
    """
    roots = ("tcad", "tools", "tests")
    entries: list[tuple[str, str]] = []
    for root in roots:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            entries.append((str(path.relative_to(REPO_ROOT)), sha256(path)))
    lines = "".join(f"{name}\0{digest}\n" for name, digest in entries)
    return {
        "roots": list(roots),
        "files": len(entries),
        "sha256": hashlib.sha256(lines.encode("utf-8")).hexdigest(),
        "method": r"sha256 over sorted '<path>\0<sha256>\n' lines",
    }


def check_source_tree(manifest_path: Path) -> tuple[bool, str]:
    """Is the bundle on disk still the one this working tree would produce?

    The bundle is evidence about a specific code state, not a permanent verdict:
    after any change under ``tcad/``/``tools/``/``tests/`` the numbers in it were
    produced by code that no longer exists. This is the check that says so —
    ``tools/build_acceptance_artifacts.py --check`` — instead of leaving a reader
    to assume the manifest is fresh.
    """
    if not manifest_path.exists():
        return False, f"no manifest at {manifest_path} (build the bundle first)"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"manifest unreadable: {type(exc).__name__}: {exc}"
    recorded = manifest.get("source_tree")
    if not isinstance(recorded, dict) or "sha256" not in recorded:
        return False, "the manifest records no source_tree (bundle predates that field)"
    current = source_digest()
    if recorded["sha256"] == current["sha256"]:
        return True, (f"bundle matches the working tree "
                      f"({current['files']} py files, sha256 {current['sha256'][:16]}…)")
    return False, (f"STALE: the bundle was built from sha256 {recorded['sha256'][:16]}… "
                   f"but the tree is now {current['sha256'][:16]}…; "
                   f"rebuild with tools/build_acceptance_artifacts.py")


def start_worker(worker_id: str) -> WorkerHandle:
    handle = WorkerHandle(
        str(FREECAD_CMD), REPO_ROOT, worker_id=worker_id,
        startup_timeout_s=180.0, request_timeout_s=180.0,
    )
    handle.start()
    return handle


def _check_holes(model_id: str, digest: dict,
                 expect: list[tuple[float, bool]]) -> None:
    """Compare the holes MEASURED ON THE BREP against the requirement contract.

    ``expect`` is the required multiset of (diameter mm, through) pairs. The
    measured side comes from the worker's concave-cylindrical-face scan, never
    from the IR's declared parameters — otherwise the bundle would certify the
    model's own numbers back at it.
    """
    got = sorted((round(float(h["diameter"]), 6), bool(h["through"]))
                 for h in digest.get("holes", []))
    want = sorted((round(float(d), 6), bool(t)) for d, t in expect)
    if got != want:
        raise AcceptanceError(
            f"{model_id} BRep-measured holes {got} != required {want}")


PREVIEW_VIEWS: list[ViewName] = ["iso", "front", "top", "right"]
PREVIEW_SIZE = (768, 576)


def render_previews(worker: WorkerHandle, ir: dict, out_dir: Path) -> list[str]:
    """Write one PNG per standard view of the shape the kernel just built.

    The triangles come from the worker tessellating *this* IR, so a preview
    cannot belong to another model or version. And a preview that shows nothing
    is not a deliverable, so each frame is checked for drawn pixels — an empty
    white image would otherwise satisfy "a file exists" while proving nothing.
    """
    model_id = ir["model_id"]
    res = worker.request_sync(
        M_TESSELLATE, {"ir": ir, "out_dir": str(out_dir)}, timeout_s=180.0)
    if not res.get("ok"):
        raise AcceptanceError(f"{model_id} tessellation failed: {res.get('error')}")
    data = res.get("mesh")
    if not data:
        raise AcceptanceError(f"{model_id}: worker returned no mesh to preview")
    mesh = Mesh.model_validate(data)
    if not mesh.facets:
        raise AcceptanceError(f"{model_id}: preview mesh has no facets")

    width, height = PREVIEW_SIZE
    buffers = render_views(mesh, PREVIEW_VIEWS, width, height)
    written: list[str] = []
    for view in PREVIEW_VIEWS:
        arr = buffers.get(view)
        if arr is None:
            raise AcceptanceError(f"{model_id}: no {view} buffer rendered")
        path = out_dir / f"preview_{view}.png"
        w, h = write_png(arr, str(path))
        if (w, h) != (width, height):
            raise AcceptanceError(f"{model_id}: preview_{view}.png is {w}x{h}")
        if int((arr != 255).any(axis=-1).sum()) == 0:
            path.unlink(missing_ok=True)
            raise AcceptanceError(f"{model_id}: preview_{view}.png is blank")
        written.append(path.name)
    return written


def build_turn1(worker: WorkerHandle, ir: dict, out_dir: Path,
                *, expect_volume: float, expect_bbox: dict,
                expect_holes: list[tuple[float, bool]] | None = None) -> dict:
    """Compile, export, introspect, verify analytically, lay artifacts on disk."""
    out_dir.mkdir(parents=True, exist_ok=True)
    model_id = ir["model_id"]

    (out_dir / "ir.json").write_text(json.dumps(ir, indent=2, ensure_ascii=False), encoding="utf-8")

    compile_res = worker.request_sync(
        M_COMPILE_IR, {"ir": ir, "out_dir": str(out_dir)}, timeout_s=180.0)
    if not compile_res.get("ok"):
        raise AcceptanceError(f"{model_id} compile failed: {compile_res.get('errors')}")
    m = compile_res["measurements"]
    if not (m["is_valid"] and m["solids"] == 1):
        raise AcceptanceError(f"{model_id} invalid geometry: {m}")
    check(f"{model_id} volume", m["volume"], expect_volume)
    for axis, extent in expect_bbox.items():
        check(f"{model_id} bbox.{axis}", m["bbox"][axis], extent, abs_tol=ABS)

    export_res = worker.request_sync(
        M_EXPORT, {"ir": ir, "out_dir": str(out_dir), "exports": ["step", "stl", "fcstd"]},
        timeout_s=180.0)
    if not export_res.get("ok"):
        raise AcceptanceError(f"{model_id} export failed: {export_res.get('errors')}")
    for fmt in ("step", "stl", "fcstd"):
        p = out_dir / f"{model_id}.{fmt}"
        if not (p.is_file() and p.stat().st_size > 0):
            raise AcceptanceError(f"{model_id}: missing or empty {p}")

    digest = worker.request_sync(
        M_INTROSPECT, {"ir": ir, "out_dir": str(out_dir)}, timeout_s=180.0)
    if not digest.get("ok"):
        raise AcceptanceError(f"{model_id} introspect failed: {digest}")
    (out_dir / "digest.json").write_text(
        json.dumps({k: v for k, v in digest.items() if k != "ok"}, indent=2),
        encoding="utf-8")
    if expect_holes is not None:
        _check_holes(model_id, digest, expect_holes)

    # STEP readback: the bytes on disk re-imported and measured by the kernel.
    rt = worker.request_sync(
        M_IMPORT_ASSET, {"path": str(out_dir / f"{model_id}.step")}, timeout_s=180.0)
    if not rt.get("ok") or not rt.get("shape_summary", {}).get("is_valid"):
        raise AcceptanceError(f"{model_id} STEP readback failed: {rt}")
    step_volume = float(rt["shape_summary"]["volume"])
    check(f"{model_id} STEP readback volume", step_volume, expect_volume)

    previews = render_previews(worker, ir, out_dir)

    report = {
        "compile_measurements": m,
        "step_readback_volume": step_volume,
        "expected_volume": expect_volume,
        "measured_holes": digest.get("holes", []),
        "previews": previews,
        "verified": True,
    }
    (out_dir / "build_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return report


def build_turn2_reopen(worker: WorkerHandle, turn1_dir: Path, model_id: str,
                       edits: list[dict], out_dir: Path, *, expect_volume: float,
                       expect_bbox: dict | None = None) -> dict:
    """Reopen the DELIVERED turn-1 FCStd, apply parametric edits, verify.

    This is the acceptance evidence for '重启服务后继续修改': the file on disk
    carries a live Body/Sketch/Feature history and recomputes to the new dims.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    fcstd = turn1_dir / f"{model_id}.FCStd"
    if not fcstd.is_file():
        raise AcceptanceError(f"missing delivered FCStd: {fcstd}")

    res = worker.request_sync(
        M_REOPEN_EDIT,
        {"fcstd_path": str(fcstd), "edits": edits, "out_dir": str(out_dir)},
        timeout_s=180.0)
    (out_dir / "edit_report.json").write_text(
        json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    if not res.get("ok"):
        raise AcceptanceError(f"{model_id} reopen+edit failed: {res.get('errors')}")
    m = res["measurements"]
    if not (m["is_valid"] and m["solids"] == 1):
        raise AcceptanceError(f"{model_id} edited geometry invalid: {m}")
    check(f"{model_id} edited volume", m["volume"], expect_volume)
    if expect_bbox:
        for axis, extent in expect_bbox.items():
            check(f"{model_id} edited bbox.{axis}", m["bbox"][axis], extent, abs_tol=ABS)
    invalid = [n for n, s in res.get("feature_states", {}).items() if "Invalid" in s.get("state", [])]
    if invalid:
        raise AcceptanceError(f"{model_id} features Invalid after edit: {invalid}")
    return res


def export_edited_ir(worker: WorkerHandle, ir: dict, out_dir: Path, *,
                     expect_volume: float,
                     expect_holes: list[tuple[float, bool]] | None = None) -> None:
    """Turn-2 downloadables AND their own measurement evidence.

    The edited IR is rebuilt and exported fresh, then introspected and read
    back from STEP exactly like turn 1: a delivered turn whose geometry was
    never re-measured would ask the reader to trust the edit.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    model_id = ir["model_id"]
    (out_dir / "ir.json").write_text(json.dumps(ir, indent=2, ensure_ascii=False), encoding="utf-8")
    res = worker.request_sync(
        M_EXPORT, {"ir": ir, "out_dir": str(out_dir), "exports": ["step", "stl", "fcstd"]},
        timeout_s=180.0)
    if not res.get("ok"):
        raise AcceptanceError(f"{model_id} turn-2 export failed: {res.get('errors')}")
    for fmt in ("step", "stl", "fcstd"):
        p = out_dir / f"{model_id}.{fmt}"
        if not (p.is_file() and p.stat().st_size > 0):
            raise AcceptanceError(f"{model_id}: missing or empty {p}")

    digest = worker.request_sync(
        M_INTROSPECT, {"ir": ir, "out_dir": str(out_dir)}, timeout_s=180.0)
    if not digest.get("ok"):
        raise AcceptanceError(f"{model_id} turn-2 introspect failed: {digest}")
    (out_dir / "digest.json").write_text(
        json.dumps({k: v for k, v in digest.items() if k != "ok"}, indent=2),
        encoding="utf-8")
    if expect_holes is not None:
        _check_holes(model_id, digest, expect_holes)

    rt = worker.request_sync(
        M_IMPORT_ASSET, {"path": str(out_dir / f"{model_id}.step")}, timeout_s=180.0)
    if not rt.get("ok") or not rt.get("shape_summary", {}).get("is_valid"):
        raise AcceptanceError(f"{model_id} turn-2 STEP readback failed: {rt}")
    step_volume = float(rt["shape_summary"]["volume"])
    check(f"{model_id} turn-2 STEP readback volume", step_volume, expect_volume)

    previews = render_previews(worker, ir, out_dir)
    (out_dir / "build_report.json").write_text(json.dumps({
        "expected_volume": expect_volume,
        "step_readback_volume": step_volume,
        "measured_holes": digest.get("holes", []),
        "previews": previews,
        "verified": True,
    }, indent=2), encoding="utf-8")


def write_requirements(out_dir: Path, contract: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    contract = dict(contract)
    contract["attempt_id"] = ATTEMPT_ID
    (out_dir / "requirements.json").write_text(
        json.dumps(contract, indent=2, ensure_ascii=False), encoding="utf-8")


def requirement_contract(sample: str, version: int, raw_text: str,
                         user_stated: list[dict], system_inferred: list[dict],
                         later_modifications: list[dict]) -> dict:
    """The §4 contract: original text + unit + frame + stated vs inferred vs
    later-modified dimensions, each carrying its text evidence. Independent of
    whatever geometry was generated — nothing here is derived from the IR."""
    return {
        "sample": sample,
        "schema_version": 1,
        "version": version,
        "units": "mm",
        "coordinate_system": "right-handed; plate occupies x∈[0,w], y∈[0,h], z∈[0,t]",
        "raw_text": raw_text,
        "user_stated_requirements": user_stated,
        "system_inferred": system_inferred,
        "later_modifications": later_modifications,
        "generated_from": None,
    }


def main() -> int:
    if "--check" in sys.argv:
        ok, message = check_source_tree(OUT_ROOT / "manifest.json")
        print(("OK:   " if ok else "FAIL: ") + message)
        return 0 if ok else 1

    if not FREECAD_CMD.exists():
        print(f"BLOCKED: FreeCADCmd not found at {FREECAD_CMD}", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="acceptance_"))
    root = work / "acceptance"
    root.mkdir(parents=True)
    version = freecad_version()
    print(f"FreeCADCmd: {FREECAD_CMD}")
    print(f"version:    {version}")
    print(f"attempt:    {ATTEMPT_ID}")
    src = source_digest()
    print(f"source:     {src['files']} py files, sha256 {src['sha256'][:16]}…")

    try:
        # ── sample A ────────────────────────────────────────────────────────
        ir_a = slot_ir("accept_a", 80, 50, 8, 20, 15, 40, 20,
                       "80×50×8 的矩形板，中心有 40×20 的矩形贯穿开口")
        write_requirements(root / "sample_a" / "turn1", requirement_contract(
            "A", 1, ir_a["requirements"]["raw_text"],
            user_stated=[
                {"kind": "bbox", "value": {"x": 80.0, "y": 50.0, "z": 8.0},
                 "evidence": "80×50×8 的矩形板", "confirmed": True},
                {"kind": "through_slot", "value": {"x_range": [20.0, 60.0], "y_range": [15.0, 35.0]},
                 "evidence": "中心有 40×20 的矩形贯穿开口", "confirmed": True},
            ],
            system_inferred=[
                {"kind": "single_solid", "value": 1,
                 "reason": "贯穿开口在单板内切除，不构成第二实体"},
            ],
            later_modifications=[],
        ))
        worker = start_worker("accept_t1")
        try:
            build_turn1(worker, ir_a, root / "sample_a" / "turn1",
                        expect_volume=80 * 50 * 8 - 40 * 20 * 8,   # 25600
                        expect_bbox={"x": 80.0, "y": 50.0, "z": 8.0},
                        expect_holes=[])          # a rectangular slot is not a hole
        finally:
            worker.close()
        print("sample A turn1: OK (V=25600, bbox 80×50×8, STEP readback exact)")

        # ── sample B ────────────────────────────────────────────────────────
        holes_t1 = [(10.0, 10.0, 3.0), (70.0, 10.0, 3.0), (10.0, 40.0, 3.0), (70.0, 40.0, 3.0)]
        ir_b = holes_ir("accept_b", 80, 50, 8, holes_t1,
                        "80×50×8 的矩形板，四个直径 6 的 Z 向通孔，孔位 (10,10)、(70,10)、(10,40)、(70,40)")
        write_requirements(root / "sample_b" / "turn1", requirement_contract(
            "B", 1, ir_b["requirements"]["raw_text"],
            user_stated=[
                {"kind": "bbox", "value": {"x": 80.0, "y": 50.0, "z": 8.0},
                 "evidence": "80×50×8 的矩形板", "confirmed": True},
                {"kind": "through_holes", "value": {
                    "count": 4, "diameter": 6.0, "axis": "Z",
                    "centers": [[10.0, 10.0], [70.0, 10.0], [10.0, 40.0], [70.0, 40.0]]},
                 "evidence": "四个直径 6 的 Z 向通孔，孔位 …", "confirmed": True},
            ],
            system_inferred=[],
            later_modifications=[],
        ))

        worker1 = start_worker("accept_b_t1")
        try:
            build_turn1(worker1, ir_b, root / "sample_b" / "turn1",
                        expect_volume=32000 - 4 * PI * 3.0**2 * 8,   # 32000−288π
                        expect_bbox={"x": 80.0, "y": 50.0, "z": 8.0},
                        expect_holes=[(6.0, True)] * 4)
        finally:
            worker1.close()
        print("sample B turn1: OK (V=32000−288π, 4 holes r=3, STEP readback exact)")

        # turn 2 across a worker restart: the delivered FCStd is reopened by a
        # brand-new process and the Ø6 → Ø8 edit recomputes.
        worker2 = start_worker("accept_b_t2")
        try:
            b_t2_raw = "将这四个孔的直径改为 8，其余不变。"
            write_requirements(root / "sample_b" / "turn2", requirement_contract(
                "B", 2, b_t2_raw,
                user_stated=[
                    {"kind": "bbox", "value": {"x": 80.0, "y": 50.0, "z": 8.0},
                     "evidence": "80×50×8 的矩形板", "confirmed": True},
                    {"kind": "through_holes", "value": {
                        "count": 4, "diameter": 6.0, "axis": "Z",
                        "centers": [[10.0, 10.0], [70.0, 10.0], [10.0, 40.0], [70.0, 40.0]]},
                     "evidence": "四个直径 6 的 Z 向通孔，孔位 …", "confirmed": True},
                ],
                system_inferred=[],
                later_modifications=[
                    {"kind": "through_holes", "field": "diameter", "from": 6.0, "to": 8.0,
                     "evidence": b_t2_raw,
                     "preserved": "count=4, centers, plate dims, thickness, solid count"},
                ],
            ))
            res = build_turn2_reopen(
                worker2, root / "sample_b" / "turn1", "accept_b",
                [{"object": "sk_holes", "constraint": "Radius", "value": 4.0}],
                root / "sample_b" / "turn2",
                expect_volume=32000 - 4 * PI * 4.0**2 * 8,          # 32000−512π
                expect_bbox={"x": 80.0, "y": 50.0, "z": 8.0})

            circles = sorted(
                (c for c in res["sketches"]["sk_holes"]["circles"]),
                key=lambda c: (c["x"], c["y"]))
            want = sorted([(x, y) for x, y, _r in holes_t1])
            got = [(c["x"], c["y"]) for c in circles]
            if got != [tuple(w) for w in want]:
                raise AcceptanceError(f"hole centres moved: {got} != {want}")
            if any(abs(c["radius"] - 4.0) > ABS for c in circles):
                raise AcceptanceError(f"edited radii wrong: {[c['radius'] for c in circles]}")

            export_edited_ir(worker2, holes_ir(
                "accept_b_v2", 80, 50, 8,
                [(10.0, 10.0, 4.0), (70.0, 10.0, 4.0), (10.0, 40.0, 4.0), (70.0, 40.0, 4.0)],
                b_t2_raw), root / "sample_b" / "turn2",
                expect_volume=32000 - 4 * PI * 4.0**2 * 8,          # 32000−512π
                expect_holes=[(8.0, True)] * 4)
        finally:
            worker2.close()
        print("sample B turn2: OK (restart + Ø6→Ø8, centres preserved, V=32000−512π)")

        # ── sample C ────────────────────────────────────────────────────────
        ir_c = tube_ir("accept_c", 30, 40, 10,
                       "轴线沿 Z，外径 30、高度 40、同轴通孔直径 10 的圆筒")
        write_requirements(root / "sample_c" / "turn1", requirement_contract(
            "C", 1, ir_c["requirements"]["raw_text"],
            user_stated=[
                {"kind": "tube", "value": {"outer_diameter": 30.0, "height": 40.0,
                                           "bore_diameter": 10.0, "axis": "Z"},
                 "evidence": "轴线沿 Z，外径 30、高度 40、同轴通孔直径 10 的圆筒",
                 "confirmed": True},
            ],
            system_inferred=[],
            later_modifications=[],
        ))
        worker3 = start_worker("accept_c_t1")
        try:
            build_turn1(worker3, ir_c, root / "sample_c" / "turn1",
                        expect_volume=PI * (15.0**2 - 5.0**2) * 40.0,   # 8000π
                        expect_bbox={"x": 30.0, "y": 30.0, "z": 40.0},
                        expect_holes=[(10.0, True)])

            c_t2_raw = "把高度改为 50，其他保持不变。"
            write_requirements(root / "sample_c" / "turn2", requirement_contract(
                "C", 2, c_t2_raw,
                user_stated=[
                    {"kind": "tube", "value": {"outer_diameter": 30.0, "height": 40.0,
                                               "bore_diameter": 10.0, "axis": "Z"},
                     "evidence": "轴线沿 Z，外径 30、高度 40、同轴通孔直径 10 的圆筒",
                     "confirmed": True},
                ],
                system_inferred=[],
                later_modifications=[
                    {"kind": "tube", "field": "height", "from": 40.0, "to": 50.0,
                     "evidence": c_t2_raw,
                     "preserved": "outer diameter, bore diameter, axis, solid count"},
                ],
            ))
            res = build_turn2_reopen(
                worker3, root / "sample_c" / "turn1", "accept_c",
                [{"object": "ft_tube", "property": "Length", "value": 50.0}],
                root / "sample_c" / "turn2",
                expect_volume=PI * (15.0**2 - 5.0**2) * 50.0,          # 10000π
                expect_bbox={"x": 30.0, "y": 30.0, "z": 50.0})
            bore = res["sketches"]["sk_bore"]["circles"]
            if len(bore) != 1 or abs(bore[0]["radius"] - 5.0) > ABS:
                raise AcceptanceError(f"bore changed: {bore}")
            export_edited_ir(worker3, tube_ir("accept_c_v2", 30, 50, 10, c_t2_raw),
                              root / "sample_c" / "turn2",
                              expect_volume=PI * (15.0**2 - 5.0**2) * 50.0,   # 10000π
                              expect_holes=[(10.0, True)])
        finally:
            worker3.close()
        print("sample C turn1+turn2: OK (8000π → h=50 → 10000π, bore preserved)")

        # ── sample D: a stepped shaft by REVOLUTION ─────────────────────────
        # The first op promoted out of EXPERIMENTAL: its proof lives in
        # tests/contract/test_revolution.py, and this bundle is the downloadable
        # counterpart. Turn 2 edits the Revolution's Angle — a parametric edit
        # that only works if the FCStd really kept the feature.
        d_r1, d_h1, d_r2, d_h2 = 10.0, 20.0, 6.0, 20.0
        d_vol = PI * (d_r1**2 * d_h1 + d_r2**2 * d_h2)          # 2720π
        ir_d = stepped_shaft_ir("accept_d", r1=d_r1, h1=d_h1, r2=d_r2, h2=d_h2)
        write_requirements(root / "sample_d" / "turn1", requirement_contract(
            "D", 1, "轴线沿 Z 的阶梯轴：粗段 Ø20、高 20；细段 Ø12、高 20。用旋转（revolve）成型",
            user_stated=[
                {"kind": "stepped_shaft",
                 "value": {"axis": "Z", "coarse_diameter": 20.0, "coarse_height": 20.0,
                           "fine_diameter": 12.0, "fine_height": 20.0,
                           "total_height": 40.0, "feature": "revolution"},
                 "evidence": "粗段 Ø20、高 20；细段 Ø12、高 20",
                 "confirmed": True},
            ],
            system_inferred=[],
            later_modifications=[],
        ))
        worker4 = start_worker("accept_d_t1")
        try:
            build_turn1(worker4, ir_d, root / "sample_d" / "turn1",
                        expect_volume=d_vol,
                        expect_bbox={"x": 2 * d_r1, "y": 2 * d_r1, "z": d_h1 + d_h2})

            d_t2_raw = "把回转角从整圈改成半圈（180°），其余不变。"
            write_requirements(root / "sample_d" / "turn2", requirement_contract(
                "D", 2, d_t2_raw,
                user_stated=[
                    {"kind": "stepped_shaft",
                     "value": {"axis": "Z", "coarse_diameter": 20.0, "coarse_height": 20.0,
                               "fine_diameter": 12.0, "fine_height": 20.0,
                               "feature": "revolution"},
                     "evidence": "粗段 Ø20、高 20；细段 Ø12、高 20",
                     "confirmed": True},
                ],
                system_inferred=[],
                later_modifications=[
                    {"kind": "revolution", "field": "angle", "from": 360.0, "to": 180.0,
                     "evidence": d_t2_raw,
                     "preserved": "profile dimensions, axis, solid count, feature tree"},
                ],
            ))
            res_d = build_turn2_reopen(
                worker4, root / "sample_d" / "turn1", "accept_d",
                [{"object": "ft_shaft", "property": "Angle", "value": 180.0}],
                root / "sample_d" / "turn2",
                expect_volume=d_vol / 2.0)                          # 1360π
            if "Invalid" in res_d["feature_states"]["ft_shaft"]["state"]:
                raise AcceptanceError("the Revolution went Invalid after the Angle edit")
            export_edited_ir(
                worker4,
                stepped_shaft_ir("accept_d_v2", r1=d_r1, h1=d_h1, r2=d_r2, h2=d_h2, angle=180.0),
                root / "sample_d" / "turn2",
                expect_volume=d_vol / 2.0)
        finally:
            worker4.close()
        print("sample D turn1+turn2: OK (revolution 2720π → Angle 180° → 1360π, feature tree kept)")

        # ── sample E: a circumferential groove on a cylinder ────────────────
        # The second op promoted out of EXPERIMENTAL. Its proof is
        # tests/contract/test_groove.py; this is the downloadable counterpart.
        e_r, e_h, e_in, e_z, e_w = 15.0, 40.0, 10.0, 18.0, 4.0
        e_solid = PI * e_r**2 * e_h                                   # 9000π
        e_ring = PI * (e_r**2 - e_in**2) * e_w                        # 500π
        ir_e = grooved_cylinder_ir("accept_e", radius=e_r, height=e_h,
                                   groove_inner=e_in, groove_z=e_z, groove_width=e_w)
        write_requirements(root / "sample_e" / "turn1", requirement_contract(
            "E", 1, "轴线沿 Z、外径 30、高 40 的圆柱，在外壁 z=18..22 处开一圈深 5 的周向环槽",
            user_stated=[
                {"kind": "cylinder_with_groove",
                 "value": {"axis": "Z", "diameter": 30.0, "height": 40.0,
                           "groove_inner_radius": 10.0, "groove_z": [18.0, 22.0],
                           "groove_depth": 5.0, "feature": "groove"},
                 "evidence": "外径 30、高 40；在外壁 z=18..22 处开一圈深 5 的周向环槽",
                 "confirmed": True},
            ],
            system_inferred=[],
            later_modifications=[],
        ))
        worker5 = start_worker("accept_e_t1")
        try:
            build_turn1(worker5, ir_e, root / "sample_e" / "turn1",
                        expect_volume=e_solid - e_ring,
                        expect_bbox={"x": 2 * e_r, "y": 2 * e_r, "z": e_h})

            e_t2_raw = "把环槽的回转角从整圈改成半圈（180°），其余不变。"
            write_requirements(root / "sample_e" / "turn2", requirement_contract(
                "E", 2, e_t2_raw,
                user_stated=[
                    {"kind": "cylinder_with_groove",
                     "value": {"axis": "Z", "diameter": 30.0, "height": 40.0,
                               "groove_inner_radius": 10.0, "groove_z": [18.0, 22.0],
                               "feature": "groove"},
                     "evidence": "外径 30、高 40；在外壁 z=18..22 处开一圈深 5 的周向环槽",
                     "confirmed": True},
                ],
                system_inferred=[],
                later_modifications=[
                    {"kind": "groove", "field": "angle", "from": 360.0, "to": 180.0,
                     "evidence": e_t2_raw,
                     "preserved": "cylinder diameter/height, groove position/depth, solid count"},
                ],
            ))
            res_e = build_turn2_reopen(
                worker5, root / "sample_e" / "turn1", "accept_e",
                [{"object": "ft_groove", "property": "Angle", "value": 180.0}],
                root / "sample_e" / "turn2",
                expect_volume=e_solid - e_ring / 2.0)
            if "Invalid" in res_e["feature_states"]["ft_groove"]["state"]:
                raise AcceptanceError("the Groove went Invalid after the Angle edit")
            export_edited_ir(
                worker5,
                grooved_cylinder_ir("accept_e_v2", radius=e_r, height=e_h, groove_inner=e_in,
                                    groove_z=e_z, groove_width=e_w, angle=180.0),
                root / "sample_e" / "turn2",
                expect_volume=e_solid - e_ring / 2.0)
        finally:
            worker5.close()
        print("sample E turn1+turn2: OK (groove 8500π → Angle 180° → 8750π, feature tree kept)")

        # ── manifest: bind everything by hash ───────────────────────────────
        # *.FCBak are FreeCAD's automatic save backups from the compile step —
        # build noise, not deliverables, so they stay out of the bundle.
        for bak in root.rglob("*.FCBak"):
            bak.unlink()
        files = {}
        for p in sorted(root.rglob("*")):
            if p.is_file():
                files[str(p.relative_to(root))] = {
                    "sha256": sha256(p),
                    "bytes": p.stat().st_size,
                }
        manifest = {
            "attempt_id": ATTEMPT_ID,
            "built_at_utc": datetime.now(timezone.utc).isoformat(),
            "platform": platform.platform(),
            "freecad_cmd": str(FREECAD_CMD),
            "freecad_version": version,
            "source_tree": source_digest(),
            "tolerances": {
                "volume_rel": VOL_REL,
                "bbox_abs": ABS,
                "step_readback_rel": VOL_REL,
                "rationale": "tests/contract/test_samples_acceptance.py module docstring",
            },
            "previews": {
                "views": list(PREVIEW_VIEWS),
                "size_px": list(PREVIEW_SIZE),
                "source": "M_TESSELLATE of the same IR the artifacts were built from",
            },
            "samples": {
                "sample_a": {"turns": ["turn1"],
                             "expected_volume_turn1": 25600.0},
                "sample_b": {"turns": ["turn1", "turn2"],
                             "expected_volume_turn1": 32000 - 288 * PI,
                             "expected_volume_turn2": 32000 - 512 * PI,
                             "restart_between_turns": True},
                "sample_c": {"turns": ["turn1", "turn2"],
                             "expected_volume_turn1": 8000 * PI,
                             "expected_volume_turn2": 10000 * PI},
                "sample_d": {"turns": ["turn1", "turn2"],
                             "expected_volume_turn1": 2720 * PI,
                             "expected_volume_turn2": 1360 * PI,
                             "op": "revolution",
                             "turn2_edit": "Revolution.Angle 360 -> 180"},
                "sample_e": {"turns": ["turn1", "turn2"],
                             "expected_volume_turn1": 9000 * PI - 500 * PI,
                             "expected_volume_turn2": 9000 * PI - 250 * PI,
                             "op": "groove",
                             "turn2_edit": "Groove.Angle 360 -> 180"},
            },
            "files": files,
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        if OUT_ROOT.exists():
            shutil.rmtree(OUT_ROOT)
        shutil.copytree(root, OUT_ROOT)
        print(f"\nwrote {len(files) + 1} files under {OUT_ROOT}")
        return 0
    except AcceptanceError as exc:
        print(f"ACCEPTANCE FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
