"""Mapping identity, completeness, body boundaries and legacy compatibility."""

import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from tcad.render.scene import SceneModel
from tcad.selection.resolve import SelectionSnapshot
from tcad.selection.types import SelectionRef, SelectionError
from tests.fixtures.artifact_scene import tetra_mesh
from tests.reference_support import MODEL, plate, protocol_services, publish


def mapped_scene():
    mesh = tetra_mesh()
    mapping = {"schema_version":1,"generator":"freecad-face-mesh-v1",
        "mesh_digest":"sha256:" + hashlib.sha256(json.dumps(
            [[list(map(float,p)) for p in mesh["vertices"]],mesh["facets"]], separators=(",", ":")).encode()).hexdigest(),
        "parts":[{"body_id":"plate","vertex_start":0,"vertex_count":4}],
        "entities":[{"body_id":"plate","entity_kind":"face","local_sub_id":"Face1",
            "triangle_start":0,"triangle_count":4,"segments":[]},
            {"body_id":"plate","entity_kind":"edge","local_sub_id":"Edge1",
                "triangle_start":0,"triangle_count":0,"segments":[[0,1]]}]}
    return SceneModel(mesh=mesh,body_ids=["plate"],pick_mapping=mapping)


@pytest.mark.parametrize("mutation", ["digest", "gap", "duplicate", "cross_body", "kind", "edge", "pose", "unknown_field"])
def test_corrupt_mapping_is_rejected(mutation):
    raw = mapped_scene().model_dump(mode="json")
    mapping = raw["pick_mapping"]
    if mutation == "digest": mapping["mesh_digest"] = "sha256:" + "0"*64
    elif mutation == "gap": mapping["entities"][0]["triangle_count"] = 3
    elif mutation == "duplicate": mapping["entities"].append(copy.deepcopy(mapping["entities"][1]))
    elif mutation == "cross_body": mapping["entities"][0]["body_id"] = "other"
    elif mutation == "kind": mapping["entities"][0]["local_sub_id"] = "Edge1"
    elif mutation == "edge": mapping["entities"][1]["segments"] = [[0,99]]
    elif mutation == "pose": raw["motion"] = [{"body_id":"plate","vertex_start":1,"vertex_count":3,
        "pivot":{"x":0,"y":0,"z":0},"axis":{"x":0,"y":0,"z":1},"ratio":1}]
    else: mapping["artifact_id"] = "self-referential"
    with pytest.raises(ValidationError): SceneModel.model_validate(raw)


def test_legacy_scene_has_no_fine_picking():
    assert SceneModel(mesh=tetra_mesh()).pick_mapping is None


def test_geometry_resolves_only_from_trusted_mapping_and_never_guesses_feature(tmp_path):
    services = protocol_services(tmp_path)
    manifest = publish(tmp_path, services.store)
    from tcad.core.types import GeometryDigest
    snapshot = SelectionSnapshot(manifest, plate(), GeometryDigest(model_id=MODEL,ir_version=0),mapped_scene().pick_mapping)
    body_id = plate().bodies[0].id
    # The real fixture body ID is deliberately used instead of a global FaceN.
    mapping = mapped_scene().pick_mapping.model_copy(deep=True)
    for entity in mapping.entities: entity.body_id = body_id
    snapshot = SelectionSnapshot(manifest, plate(), snapshot.digest, mapping)
    for kind, local in (("face","Face1"),("edge","Edge1")):
        ref = SelectionRef(model_id=MODEL,artifact_id=manifest.artifact_id,ir_version=0,
            body_id=body_id,entity_kind=kind,local_sub_id=local)
        resolved = snapshot.resolve(ref)
        assert not resolved.editable and resolved.capabilities == ["inspect"]
        assert resolved.evidence["semantic_source"] == "unknown"
        with pytest.raises(SelectionError): snapshot.resolve(ref.model_copy(update={"local_sub_id": local+"99"}))
