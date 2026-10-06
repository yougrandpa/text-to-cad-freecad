"""Resolve references against source and measurements from one saved artifact."""

import math
from dataclasses import dataclass
from functools import cached_property
from tcad.render.picking import PickMapping

from tcad.artifacts.manifest import ArtifactSet, ArtifactStatus
from tcad.core.types import GeometryDigest
from tcad.inspect.artifact import ArtifactReadError, ArtifactReader
from tcad.ir.schema import IrDocument
from tcad.selection.types import ResolvedSelection, SelectionContext, SelectionError, SelectionRef


def circular_profile(body, target):
    sketch_id = target.id if target in body.sketches else getattr(target, "profile_sketch", None)
    sketch = next((s for s in body.sketches if s.id == sketch_id), None)
    consumers = [f for f in body.features if f.profile_sketch == sketch_id]
    if sketch is None or len(consumers) != 1:
        return None
    pocket = consumers[0]
    if (pocket.op != "pocket" or pocket.suppress or pocket.params.get("type") != "ThroughAll"
            or pocket != body.features[-1] or len(sketch.geometry) != 1):
        return None
    circle = sketch.geometry[0]
    dimensions = [c for c in sketch.constraints if c.type in {"Radius", "Diameter"}]
    if (circle.kind != "circle" or circle.construction or len(dimensions) != 1
            or dimensions[0].refs != [0]
            or any(c.type not in {"Radius", "Diameter", "DistanceX", "DistanceY", "Coincident"}
                   for c in sketch.constraints)):
        return None
    # A diameter recipe may not pretend that a Block-constrained or implicit
    # circle is editable. Validate the driving dimension against its source.
    dimension = dimensions[0]
    if dimension.value is None or not math.isfinite(dimension.value) or dimension.value <= 0:
        return None
    radius = dimension.value / (2 if dimension.type == "Diameter" else 1)
    if circle.radius is None or not math.isclose(radius, circle.radius, abs_tol=1e-6):
        return None
    for constraint in sketch.constraints:
        if constraint.type in {"DistanceX", "DistanceY"} and (
                constraint.refs != [0, 3] or constraint.value is None or not math.isfinite(constraint.value)):
            return None
        if constraint.type == "Coincident" and constraint.refs != [0, 3, -1, 1]:
            return None
    if target not in body.sketches and target.id != pocket.id:
        return None
    return sketch, pocket


@dataclass(frozen=True)
class SelectionSnapshot:
    manifest: ArtifactSet
    ir: IrDocument
    digest: GeometryDigest
    pick_mapping: PickMapping | None = None

    @cached_property
    def entities(self):
        return {(e.body_id, e.entity_kind, e.local_sub_id): e for e in self.pick_mapping.entities} if self.pick_mapping else {}

    def resolve(self, ref: SelectionRef) -> ResolvedSelection:
        body = next((b for b in self.ir.bodies if b.id == ref.body_id), None)
        if body is None:
            raise SelectionError("unmapped_entity", "Selected body is absent from this publication.", 422)
        if ref.entity_kind in {"face", "edge"}:
            entity = self.entities.get((ref.body_id, ref.entity_kind, ref.local_sub_id))
            if entity is None:
                raise SelectionError("unmapped_entity", "This publication has no mapping for that entity.", 422)
            return ResolvedSelection(ref=ref, label=f"{body.name} · {ref.local_sub_id}", editable=False,
                                     capabilities=["inspect"], evidence={"source": "scene.json", "mapping": self.pick_mapping.generator,
                                         "mesh_digest": self.pick_mapping.mesh_digest, "semantic_source": "unknown"})
        objects = {"body": [body], "sketch": body.sketches, "feature": body.features}[ref.entity_kind]
        matches = [obj for obj in objects if obj.id == ref.target_id]
        if len(matches) != 1:
            raise SelectionError("unmapped_entity", "Selected object is absent or ambiguous.", 422)
        target = matches[0]
        editable = body.part_ref is None
        capabilities = ["inspect", *(["ir_patch"] if editable else [])]
        evidence = {"source": "ir.json", "artifact_id": self.manifest.artifact_id}
        sketch_id = None
        # P1a deliberately proves a single static body and a single measured
        # through-hole. Multi-body/instance geometry correspondence belongs to P1b.
        hole = circular_profile(body, target) if ref.entity_kind != "body" and editable else None
        measured = self.digest.holes
        if (hole and len(self.ir.bodies) == 1 and self.ir.assembly is None
                and body.motion is None and len(measured) == 1 and measured[0].through
                and self.digest.measurements_available
                and self.digest.key_dimensions.get(f"{hole[0].id}__solve_status", 0) == 0
                and math.isclose(measured[0].diameter, hole[0].geometry[0].radius * 2, abs_tol=1e-5)):
            sketch_id = hole[0].id
            capabilities.append("set_hole_diameter")
            evidence.update({"measurements": "digest.json", "pocket_id": hole[1].id,
                             "diameter_mm": measured[0].diameter, "through": True})
        return ResolvedSelection(ref=ref, label=target.name, editable=editable,
                                 capabilities=capabilities, hole_sketch_id=sketch_id, evidence=evidence)


class SelectionResolver:
    def __init__(self, data_dir, store):
        self.reader = ArtifactReader(data_dir)
        self.store = store

    def snapshot(self, model_id: str, artifact_id: str | None = None) -> SelectionSnapshot:
        try:
            manifest, root = self.reader.resolve(model_id, artifact_id=artifact_id)
            latest, _ = self.reader.resolve(model_id)
            if manifest.artifact_id != latest.artifact_id:
                raise SelectionError("stale_selection", "Build changed; select from the current publication again.")
            if manifest.status != ArtifactStatus.VERIFIED:
                raise SelectionError("unmapped_entity", "References require a verified publication.", 422)
            ir = IrDocument.model_validate_json(self.reader.read_file(manifest, root, "ir.json"))
            if ir.model_id != model_id or ir.version != manifest.ir_version:
                raise SelectionError("unmapped_entity", "Frozen source identity does not match the publication.", 422)
            if self.store.load(model_id).model_dump() != ir.model_dump():
                raise SelectionError("stale_selection", "Source changed; commit and select again.")
            mapping = self.reader.scene(manifest, root).pick_mapping if "scene.json" in manifest.files else None
            return SelectionSnapshot(manifest, ir, self.reader.digest(manifest, root), mapping)
        except SelectionError:
            raise
        except (ArtifactReadError, FileNotFoundError, ValueError) as exc:
            raise SelectionError("unmapped_entity", "No valid saved publication for this reference.", 422) from exc

    def resolve(self, model_id: str, context: SelectionContext, *, inspection: bool = False):
        first = context.selection_refs[0]
        if first.model_id != model_id:
            raise SelectionError("forbidden", "Reference belongs to another model.", 403)
        snapshot = self.snapshot(model_id, first.artifact_id)
        if first.ir_version != snapshot.ir.version:
            raise SelectionError("stale_selection", "Reference version differs from its publication.")
        resolved = tuple(snapshot.resolve(ref) for ref in context.selection_refs)
        if not inspection and any(not r.editable for r in resolved):
            raise SelectionError("forbidden", "This geometry has no proven editable source; inspect it or reference a feature.", 403)
        return snapshot, resolved

    def catalog(self, model_id: str) -> dict:
        snapshot = self.snapshot(model_id)
        targets = []
        for body in snapshot.ir.bodies:
            for kind, objects in (("body", [body]), ("sketch", body.sketches), ("feature", body.features)):
                for obj in objects:
                    ref = SelectionRef(model_id=model_id, artifact_id=snapshot.manifest.artifact_id,
                                       ir_version=snapshot.ir.version, body_id=body.id, entity_kind=kind,
                                       **({f"{kind}_id": obj.id} if kind != "body" else {}))
                    targets.append(snapshot.resolve(ref).model_dump(mode="json"))
        if snapshot.pick_mapping:
            for entity in snapshot.pick_mapping.entities:
                ref = SelectionRef(model_id=model_id, artifact_id=snapshot.manifest.artifact_id,
                    ir_version=snapshot.ir.version, body_id=entity.body_id,
                    entity_kind=entity.entity_kind, local_sub_id=entity.local_sub_id)
                targets.append(snapshot.resolve(ref).model_dump(mode="json"))
        return {"schema_version": 1, "model_id": model_id,
                "artifact_id": snapshot.manifest.artifact_id, "ir_version": snapshot.ir.version,
                "targets": targets}
