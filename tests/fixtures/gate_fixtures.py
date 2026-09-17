"""Reusable golden fixtures for the V+C layer tests.

A small in-memory ``IrDocument`` plus a matching on-disk ``digest.json`` written
to a real tmpdir, so every test exercises the genuine disk-read path the Gate
depends on (design §4.6 / §9 "V → S").
"""

from __future__ import annotations

from pathlib import Path

from tcad.core.types import BBox, GeometryDigest, Topology
from tcad.core.types import FeatureDigest
from tcad.ir.schema import (
    BodySpec,
    ConstraintExpr,
    FeatureSpec,
    IrDocument,
    PlaneRef,
    RequirementSpec,
    SketchSpec,
)
from tcad.worker.protocol import M_IMPORT_ASSET


def make_ir(model_id: str = "m1", version: int = 1,
            requirements: RequirementSpec | None = None) -> IrDocument:
    sketch = SketchSpec(
        id="sk_base", name="base", plane=PlaneRef(kind="origin_plane", plane="XY"),
        require_fully_constrained=True,
    )
    feature = FeatureSpec(
        id="ft_pad", name="pad", op="pad", profile_sketch="sk_base",
        params={"length": 10.0},
    )
    body = BodySpec(id="body1", name="body1", sketches=[sketch], features=[feature])
    return IrDocument(
        model_id=model_id, version=version, bodies=[body],
        requirements=requirements or RequirementSpec(),
    )


def make_digest(model_id: str = "m1", version: int = 1, *,
                measurements_available: bool = True,
                bbox: tuple[float, float, float] = (60.0, 40.0, 10.0),
                volume: float = 24000.0,
                solids: int = 1, faces: int = 6, edges: int = 12, vertexes: int = 8,
                min_wall: float = 2.0,
                sketch_id: str = "sk_base") -> GeometryDigest:
    return GeometryDigest(
        model_id=model_id, ir_version=version,
        feature_chain=[FeatureDigest(id="ft_pad", name="pad", op="pad",
                                     params={"length": 10.0})],
        topology=Topology(solids=solids, faces=faces, edges=edges,
                          vertexes=vertexes, shells=1),
        bbox=BBox(x=bbox[0], y=bbox[1], z=bbox[2]),
        volume=volume, area=6800.0, shape_type="Solid", is_valid=True,
        key_dimensions={
            f"{sketch_id}__fully_constrained": 1.0,
            f"{sketch_id}__dof": 0.0,
            "min_wall_thickness": min_wall,
        },
        measurements_available=measurements_available,
    )


class FakeWorker:
    """Stand-in for the FreeCAD worker. Its ``request`` mimics a successful
    STEP read-back returning the in-process numbers, so round_trip passes."""

    def request(self, method: str, params: dict) -> dict:
        assert method == M_IMPORT_ASSET
        return {"shape_summary": {"volume": 24000.0, "faces": 6, "edges": 12}}


class FakeWorkerWrong:
    """Returns numbers that disagree with the digest -> round_trip FAILS."""

    def request(self, method: str, params: dict) -> dict:
        assert method == M_IMPORT_ASSET
        return {"shape_summary": {"volume": 999.0, "faces": 3, "edges": 4}}


def write_artefacts(tmp_path: Path, *,
                    ir: IrDocument | None = None,
                    digest: GeometryDigest | None = None,
                    with_exports: bool = True) -> tuple[Path, Path]:
    """Write ir.json + digest.json (+ dummy exports) and return (ir_path, artifact_dir)."""
    ir = ir or make_ir()
    digest = digest or make_digest()
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    ir_path = tmp_path / "ir.json"
    ir_path.write_text(ir.model_dump_json(), encoding="utf-8")
    (artifact_dir / "digest.json").write_text(digest.model_dump_json(), encoding="utf-8")
    if with_exports:
        (artifact_dir / "m1.step").write_text("STEPDATA", encoding="utf-8")
        (artifact_dir / "m1.stl").write_text("STLDATA", encoding="utf-8")
    return ir_path, artifact_dir
