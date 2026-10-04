"""Small valid on-disk artifacts for viewer and snapshot contract tests."""

import hashlib

from tcad.core.types import BuildStamp, GateReport, GeometryDigest
from tcad.ir.schema import IrDocument
from tcad.store.artifacts import ArtifactStore, write_build_stamp


def tetra_mesh(*, volume=1.0, tolerance=0.5):
    return {"vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
            "facets": [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]],
            "bbox": {"x": 1, "y": 1, "z": 1, "x_min": 0, "y_min": 0, "z_min": 0},
            "volume": volume, "tolerance": tolerance}


def publish_scene(data_dir, *, model_id="part", version=0, attempt="a1", mesh=None,
                  scene=None):
    from tcad.render.scene import SceneModel

    store = ArtifactStore(data_dir)
    root = store.staging_dir(model_id, version, attempt)
    root.mkdir(parents=True)
    raw = IrDocument(model_id=model_id, version=version).model_dump_json()
    ir_hash = hashlib.sha256(raw.encode()).hexdigest()
    (root / "ir.json").write_text(raw)
    (root / f"{model_id}.step").write_text("synthetic geometry fixture")
    scene = scene or SceneModel(mesh=mesh or tetra_mesh())
    (root / "scene.json").write_text(scene.model_dump_json())
    store.write_digest_at(root, GeometryDigest(model_id=model_id, ir_version=version,
        volume=scene.mesh.volume, bbox=scene.mesh.bbox, measurements_available=True))
    write_build_stamp(root, BuildStamp(model_id=model_id, ir_version=version,
        attempt_id=attempt, started_at=0, ir_sha256=ir_hash))
    report = GateReport(model_id=model_id, ir_version=version, attempt_id=attempt,
                        ir_sha256=ir_hash, passed=True)
    (root / "gate_report.json").write_text(report.model_dump_json())
    store.write_manifest(root, model_id=model_id, version=version, attempt_id=attempt,
                         ir_sha256=ir_hash, status="verified")
    root = store.publish(model_id, version, root)
    from tcad.inspect.artifact import ArtifactReader
    manifest, retained = ArtifactReader(data_dir).resolve(model_id, version)
    return manifest, retained
