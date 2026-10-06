"""Patch payloads must not smuggle wrong-typed values into the IR (task §5-A).

The merge helpers assign fields one at a time, and pydantic v2 does **not**
validate on assignment by default. Two consequences, both real:

  * ``update_sketch {"reversed": "false"}`` stored the *string* ``"false"`` in a
    ``bool`` field. ``"false"`` is truthy, so the compiler did the opposite of
    what was asked, with no error anywhere on the way.
  * ``update_feature {"refs": "ft_pad"}`` went through ``list(v)`` and became
    ``['f','t','_','p','a','d']`` — the payload was not rejected, it was
    reinterpreted.

These tests are deterministic and need no FreeCAD.
"""

from __future__ import annotations

import pytest

from tcad.core.types import ToolErrorKind
from tcad.ir.patch import PatchError, apply_patch
from tcad.ir.schema import (
    BodySpec,
    FeatureSpec,
    IrDocument,
    IrPatch,
    IrPatchOp,
    PlaneRef,
    SketchGeom,
    SketchSpec,
    Vec3,
)
from tcad.store.ir_store import IrStore


def _doc(version: int = 1) -> IrDocument:
    return IrDocument(
        model_id="m", version=version,
        bodies=[BodySpec(
            id="b1", name="b",
            sketches=[SketchSpec(
                id="sk1", name="sk",
                plane=PlaneRef(kind="origin_plane", plane="XY"),
                geometry=[SketchGeom(id="g0", kind="line", points=[
                    Vec3(x=0.0, y=0.0, z=0.0), Vec3(x=10.0, y=0.0, z=0.0)])],
            )],
            features=[FeatureSpec(id="f1", name="pad", op="pad",
                                  profile_sketch="sk1", params={"length": 5.0})],
        )],
    )


def _patch(op: str, target: str, payload: dict, *, base: int = 1) -> IrPatch:
    return IrPatch(base_version=base,
                   ops=[IrPatchOp(op=op, target_id=target, payload=payload, reason="test")])


def _sketch(out) -> SketchSpec:
    return out.ir.bodies[0].sketches[0]


def _feature(out) -> FeatureSpec:
    return out.ir.bodies[0].features[0]


def test_invalid_added_ellipse_identifies_batch_operation_and_sketch_without_mutation():
    ir = _doc()
    before = ir.model_dump()
    patch = IrPatch(base_version=1, ops=[
        IrPatchOp(op='add_body', payload={'id': 'cockpit', 'name': 'cockpit'}, reason='New part'),
        IrPatchOp(op='add_sketch', payload={
            'id': 'cockpit_section', 'name': 'cockpit_section', 'body_id': 'cockpit',
            'plane': {'kind': 'origin_plane', 'plane': 'YZ'},
            'geometry': [{'id': 'ellipse', 'kind': 'ellipse',
                          'points': [{'x': 0, 'y': 0, 'z': 0}],
                          'major_radius': 3, 'minor_radius': 5}],
        }, reason='Cabin profile'),
    ])
    with pytest.raises(PatchError) as failure:
        apply_patch(ir, patch)
    error = failure.value.error
    assert error.kind is ToolErrorKind.SCHEMA
    assert error.feature_id == 'cockpit_section'
    assert all(term in error.message for term in ('ops[1]', 'add_sketch', 'cockpit_section',
                                                  'major_radius >= minor_radius'))
    assert ir.model_dump() == before


# ══════════════════════════════════════════════════════════════════════════
# 1. the silent one: a wrong-typed scalar must be coerced or refused
# ══════════════════════════════════════════════════════════════════════════


def test_a_string_bool_is_never_stored_as_a_truthy_string():
    """``"false"`` must become ``False`` — not stay a non-empty string."""
    out = apply_patch(_doc(), _patch("update_sketch", "sk1", {"reversed": "false"}))
    stored = _sketch(out).reversed
    assert stored is False, f"stored {stored!r}; a truthy string flips the sketch"
    dumped = out.ir.model_dump()["bodies"][0]["sketches"][0]["reversed"]
    assert dumped is False
    assert isinstance(out.ir.bodies[0].sketches[0].reversed, bool)


def test_a_nonsense_bool_is_refused_rather_than_guessed():
    with pytest.raises(PatchError) as exc:
        apply_patch(_doc(), _patch("update_sketch", "sk1",
                                   {"require_fully_constrained": "perhaps"}))
    err = exc.value.error
    assert err.kind == ToolErrorKind.SCHEMA
    assert "require_fully_constrained" in err.message


def test_every_field_of_the_document_is_a_declared_type_after_a_patch():
    out = apply_patch(_doc(), _patch("update_sketch", "sk1", {"reversed": "true"}))
    # Re-parsing the dump must be a no-op: nothing unvalidated is left inside.
    re_parsed = IrDocument.model_validate(out.ir.model_dump())
    assert re_parsed == out.ir
    assert _sketch(out).reversed is True


# ══════════════════════════════════════════════════════════════════════════
# 2. list fields must be lists, not strings that explode into characters
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("payload,needle", [
    ({"refs": "ft_pad"}, "refs"),
    ({"refs_append": "ft_pad"}, "refs_append"),
])
def test_a_string_ref_list_is_refused(payload, needle):
    with pytest.raises(PatchError) as exc:
        apply_patch(_doc(), _patch("update_feature", "f1", payload))
    err = exc.value.error
    assert err.kind == ToolErrorKind.SCHEMA
    assert needle in err.message
    assert "array" in err.message


@pytest.mark.parametrize("payload,needle", [
    ({"geometry": "abc"}, "geometry"),
    ({"geometry_append": "abc"}, "geometry_append"),
    ({"constraints": "abc"}, "constraints"),
    ({"constraints_append": "abc"}, "constraints_append"),
])
def test_a_string_geometry_or_constraint_list_is_refused(payload, needle):
    with pytest.raises(PatchError) as exc:
        apply_patch(_doc(), _patch("update_sketch", "sk1", payload))
    err = exc.value.error
    assert err.kind == ToolErrorKind.SCHEMA
    assert needle in err.message


def test_refs_are_still_set_normally_from_a_real_list():
    out = apply_patch(_doc(), _patch("update_feature", "f1",
                                     {"refs": [], "params": {"length": 8.0}}))
    assert _feature(out).refs == []
    assert _feature(out).params["length"] == 8.0


def test_a_real_ref_list_is_accepted_and_kept_typeless():
    """A genuine list of ids still works end to end."""
    doc = _doc()
    doc.bodies[0].features.append(
        FeatureSpec(id="f0", name="base", op="pad", params={"length": 3.0}))
    out = apply_patch(doc, _patch("update_feature", "f1", {"refs": ["f0"]}))
    assert _feature(out).refs == ["f0"]
    assert _feature(out).name == "pad"          # untouched by the patch


@pytest.mark.parametrize("payload,needle", [
    ({"params": "nope"}, "params"),
    ({"plane": "XY"}, "plane"),
])
def test_non_object_structured_fields_are_refused(payload, needle):
    op = "update_feature" if needle == "params" else "update_sketch"
    target = "f1" if needle == "params" else "sk1"
    with pytest.raises(PatchError) as exc:
        apply_patch(_doc(), _patch(op, target, payload))
    err = exc.value.error
    assert err.kind == ToolErrorKind.SCHEMA
    assert needle in err.message


# ══════════════════════════════════════════════════════════════════════════
# 3. a bad payload is a readable error, not a pydantic dump
# ══════════════════════════════════════════════════════════════════════════


def test_an_invalid_entity_payload_names_the_field():
    patch = IrPatch(base_version=1, ops=[IrPatchOp(
        op="add_feature", target_id=None,
        payload={"id": "f2", "name": "bad", "op": "pad", "params": "not-an-object"},
        reason="test")])
    with pytest.raises(PatchError) as exc:
        apply_patch(_doc(), patch)
    msg = exc.value.error.message
    assert "params" in msg, msg
    assert "pydantic.dev" not in msg, "the message must not be a raw pydantic dump"
    assert "validation error" not in msg.lower()


def test_an_unknown_op_is_still_a_semantic_error():
    patch = IrPatch(base_version=1, ops=[IrPatchOp(
        op="add_feature", payload={"op": "pad", "params": 5}, reason="t")])
    with pytest.raises(PatchError) as exc:
        apply_patch(_doc(), patch)
    assert exc.value.error.kind in (ToolErrorKind.SCHEMA, ToolErrorKind.SEMANTIC)


# ══════════════════════════════════════════════════════════════════════════
# 4. no half-updated state: the document and the store are untouched
# ══════════════════════════════════════════════════════════════════════════


def test_a_failing_op_rolls_back_the_whole_patch():
    """Op 1 would succeed; op 2 is invalid. Neither may land."""
    before = _doc()
    patch = IrPatch(base_version=1, ops=[
        IrPatchOp(op="update_sketch", target_id="sk1",
                  payload={"reversed": True}, reason="ok"),
        IrPatchOp(op="update_feature", target_id="f1",
                  payload={"refs": "ft_pad"}, reason="invalid"),
    ])
    with pytest.raises(PatchError):
        apply_patch(before, patch)

    # The caller's document is exactly as it was.
    assert before == _doc()
    assert before.bodies[0].sketches[0].reversed is False


def test_the_store_writes_nothing_for_a_rejected_patch(tmp_path):
    store = IrStore(tmp_path / "data")
    store.create("m", _doc(version=0))
    log = tmp_path / "data" / "models" / "m" / "events.jsonl"
    events_before = log.read_text(encoding="utf-8")

    with pytest.raises(PatchError):
        store.apply_patch("m", _patch("update_feature", "f1", {"refs": "ft_pad"}, base=0))

    assert store.load("m").version == 0
    assert log.read_text(encoding="utf-8") == events_before, "a rejected patch was logged"
    assert not (tmp_path / "data" / "models" / "m" / "v1.json").exists()


# ══════════════════════════════════════════════════════════════════════════
# 5. placement: a dict payload for a typed field, on both write paths
# ══════════════════════════════════════════════════════════════════════════
#
# ``placement`` is how an origin-placed primitive (a pin, a boss, a drilled
# hole) says where it goes. It is a typed model, but it arrives inside a patch
# payload — and both patch paths are gaps with their own failure mode:
#
#   * ``add_feature`` reads its payload key by key, so a field it does not name
#     is dropped without complaint: the model asks for a pin at (10,10), the
#     patch succeeds, and the pin is built at the origin.
#   * ``update_feature`` goes through ``_merge_feature``, which assigns whatever
#     the key maps to — a raw dict would sit in a typed field until something
#     downstream trips over it.
#
# The last test is the reason the whitelist exists: an op that positions itself
# from its sketch must not also accept a second, contradictory position.


def _add_primitive(op: str = "additive_box", **extra) -> IrPatch:
    payload = {"id": "f2", "name": "pin", "op": op,
               "params": {"length": 10.0, "width": 10.0, "height": 4.0},
               "refs": ["f1"]}
    payload.update(extra)
    return IrPatch(base_version=1, ops=[IrPatchOp(
        op="add_feature", payload=payload, reason="test")])


def _added(out) -> FeatureSpec:
    return out.ir.bodies[0].features[1]


def test_a_placement_on_an_added_primitive_arrives_typed():
    out = apply_patch(_doc(), _add_primitive(
        placement={"position": {"x": 10.0, "y": 10.0, "z": 0.0}}))
    placed = _added(out).placement
    assert placed is not None, "the patch reported success but dropped the placement"
    assert (placed.position.x, placed.position.y, placed.position.z) == (10.0, 10.0, 0.0)
    assert placed.angle == 0.0 and placed.axis is None
    # Nothing unvalidated rides along into the worker.
    assert isinstance(out.ir.model_dump()["bodies"][0]["features"][1]["placement"], dict)
    assert IrDocument.model_validate(out.ir.model_dump()) == out.ir


def test_a_placement_carries_its_axis_and_angle():
    out = apply_patch(_doc(), _add_primitive(
        op="additive_cylinder",
        params={"radius": 6.0, "height": 20.0},
        placement={"position": {"x": 40.0, "y": 25.0, "z": 8.0},
                   "axis": {"x": 0.0, "y": 1.0, "z": 0.0}, "angle": 90.0}))
    placed = _added(out).placement
    assert placed.axis is not None and (placed.axis.x, placed.axis.y, placed.axis.z) == (0.0, 1.0, 0.0)
    assert placed.angle == 90.0


def test_update_feature_moves_a_primitive():
    """The edit "move that pin to the other corner" must reach the IR typed."""
    doc = apply_patch(_doc(), _add_primitive(
        placement={"position": {"x": 10.0, "y": 10.0, "z": 0.0}})).ir
    out = apply_patch(doc, _patch("update_feature", "f2",
                                  {"placement": {"position": {"x": 70.0, "y": 40.0, "z": 0.0}}},
                                  base=doc.version))
    moved = out.ir.find_feature("f2").placement
    assert (moved.position.x, moved.position.y) == (70.0, 40.0)
    assert out.ir.find_feature("f2").params["height"] == 4.0, "the rest of the feature moved too"


def test_a_null_placement_sends_the_feature_back_to_the_origin():
    doc = apply_patch(_doc(), _add_primitive(
        placement={"position": {"x": 10.0, "y": 10.0, "z": 0.0}})).ir
    out = apply_patch(doc, _patch("update_feature", "f2", {"placement": None},
                                  base=doc.version))
    assert out.ir.find_feature("f2").placement is None


@pytest.mark.parametrize("payload", [
    {"placement": "at 10,10"},                      # a string is not a position
    {"placement": {"position": {"x": 1.0}}},        # half a position
    {"placement": {"position": {"x": 1.0, "y": 2.0, "z": 0.0}, "angle": "ninety"}},  # not a number
])
def test_a_malformed_placement_is_refused_by_name(payload):
    with pytest.raises(PatchError) as exc:
        apply_patch(_doc(), _add_primitive(**payload))
    err = exc.value.error
    assert err.kind == ToolErrorKind.SCHEMA
    assert "placement" in err.message, err.message
    assert "pydantic.dev" not in err.message, "the message must not be a raw pydantic dump"


def test_a_rotation_without_an_axis_is_refused_by_the_semantic_gate():
    """The schema cannot say "required if angle != 0"; the validator can, and
    FreeCAD would otherwise treat the rotation as a no-op."""
    with pytest.raises(PatchError) as exc:
        apply_patch(_doc(), _add_primitive(placement={
            "position": {"x": 1.0, "y": 2.0, "z": 0.0}, "angle": 90.0}))
    assert "placement_axis_missing" in exc.value.error.message
    assert exc.value.error.kind == ToolErrorKind.SEMANTIC


def test_a_placement_on_an_op_that_positions_itself_is_refused():
    """``pad`` takes its position from its sketch; a placement would be a
    second answer to the same question, so the patch must not land."""
    with pytest.raises(PatchError) as exc:
        apply_patch(_doc(), _patch("update_feature", "f1",
                                   {"placement": {"position": {"x": 1.0, "y": 2.0, "z": 0.0}}},
                                   base=1))
    assert "placement_unused" in exc.value.error.message
    assert exc.value.error.kind == ToolErrorKind.SEMANTIC


# ══════════════════════════════════════════════════════════════════════════
# 4. an unknown payload key must be refused, and `add_feature` must accept the
#    reference fields (task §5-A)
# ══════════════════════════════════════════════════════════════════════════
#
# The payload is declared to the model as an untyped object, so the patch layer
# is the only place that can catch a key the op does not read. It did not catch
# it: `_op_add_feature` read the keys it knew and dropped the rest, so
# `{"op": "pad", "parms": {...}}` produced a pad with no parameters — a
# type-valid document that passes `validate_ir` and is not what was asked for.
# And the same dropping made `base_feature` / `sub_elements` / `plane`
# unusable on `add_feature`, so `fillet`, `chamfer`, `mirrored`, `draft` and
# `thickness` could not be created in one op — with an error message telling the
# model to set the very fields it had just set.


def _add(payload: dict):
    return apply_patch(_doc(), _patch("add_feature", None, payload, base=1))


@pytest.mark.parametrize("payload, expected_key", [
    ({"op": "pad", "parms": {"length": 10}}, "parms"),
    ({"op": "pad", "params": {"length": 10}, "profile": "sk1"}, "profile"),
    ({"op": "pad", "params": {"length": 10}, "foo": "bar"}, "foo"),
])
def test_an_unknown_payload_key_is_refused_not_ignored(payload, expected_key):
    with pytest.raises(PatchError) as exc:
        _add(payload)
    err = exc.value.error
    assert err.kind == ToolErrorKind.SCHEMA
    assert expected_key in err.message
    # The message has to be actionable: what to write instead, and what is legal.
    assert "does not accept payload key" in err.message
    assert "params" in err.message


def test_a_near_miss_names_the_field_that_was_meant():
    with pytest.raises(PatchError) as exc:
        _add({"op": "pad", "parms": {"length": 10}})
    assert "did you mean 'params'" in exc.value.error.message


def test_the_same_rule_applies_to_every_op():
    """A whitelist covering only `add_feature` would leave the same hole open
    one op over."""
    cases = [
        ("update_feature", "f1", {"parms": {}}),
        ("update_sketch", "sk1", {"geometries": []}),
        ("rename", "f1", {"name": "x", "id": "y"}),
        ("update_requirement", None, {"constraint": []}),
        ("remove_feature", "f1", {"cascades": True}),
    ]
    for op, target, payload in cases:
        with pytest.raises(PatchError) as exc:
            apply_patch(_doc(), _patch(op, target, payload, base=1))
        assert exc.value.error.kind == ToolErrorKind.SCHEMA, (op, payload)
        assert "does not accept payload key" in exc.value.error.message, op


def test_a_reference_list_that_is_a_string_is_refused():
    """``sub_elements`` has the same trap ``refs`` had: a bare string is
    iterable, so `list("Edge1")` used to become five names that exist nowhere."""
    with pytest.raises(PatchError) as exc:
        _add({"op": "fillet", "base_feature": "f1", "sub_elements": "Edge1"})
    assert exc.value.error.kind == ToolErrorKind.SCHEMA
    assert "sub_elements" in exc.value.error.message

    with pytest.raises(PatchError) as exc2:
        _add({"op": "pad", "refs": "f1"})
    assert exc2.value.error.kind == ToolErrorKind.SCHEMA


def test_add_feature_now_carries_the_reference_fields():
    """The headline of this section: `fillet` in ONE op, with its edges."""
    out = _add({"op": "fillet", "params": {"radius": 3.0},
                "base_feature": "f1", "sub_elements": ["Edge1", "Edge4"]})
    feat = out.ir.all_features()[-1]
    assert feat.op == "fillet"
    assert feat.base_feature == "f1"
    assert feat.sub_elements == ["Edge1", "Edge4"]
    assert out.ir.model_dump()["bodies"][0]["features"][-1]["sub_elements"] == ["Edge1", "Edge4"]


def test_add_feature_plane_is_a_typed_plane_ref_not_a_raw_dict():
    out = _add({"op": "mirrored", "refs": ["f1"],
                "plane": {"kind": "origin_plane", "plane": "XY"}})
    feat = out.ir.all_features()[-1]
    assert isinstance(feat.plane, PlaneRef)
    assert feat.plane.plane == "XY"


def test_add_feature_and_update_feature_agree_on_which_keys_exist():
    """One mapping, two ops: they must not drift. A key accepted by
    `update_feature` cannot be unknown to `add_feature`."""
    from tcad.ir.patch import _PAYLOAD_FIELDS

    assert _PAYLOAD_FIELDS["add_feature"] <= _PAYLOAD_FIELDS["update_feature"] | {"id", "body_id", "after_feature"}
    for key in sorted(_PAYLOAD_FIELDS["add_feature"]):
        if key in ("id", "body_id", "after_feature"):
            continue
        assert key in _PAYLOAD_FIELDS["update_feature"], key


def test_the_whitelist_is_derived_from_the_model():
    """A field added to a spec must be accepted immediately — a handwritten list
    starts refusing valid payloads the day a field is added, which is how
    whitelists get deleted."""
    from tcad.ir.patch import _PAYLOAD_FIELDS

    for key in FeatureSpec.model_fields:
        assert key in _PAYLOAD_FIELDS["add_feature"], key
    for key in SketchSpec.model_fields:
        assert key in _PAYLOAD_FIELDS["add_sketch"], key
