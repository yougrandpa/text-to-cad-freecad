"""Compact relations: attach / align / anchor expand to ordinary coordinates."""

from __future__ import annotations

import math

import pytest

from tcad.ir.builders import BuildParts, parts_patch


def build(parts):
    request = BuildParts.model_validate({"parts": parts})
    ops, created = parts_patch(request, [])
    return [op["payload"] for op in ops if op["op"] == "add_feature"], created


def box(id, body, center, size):
    return {"id": id, "body_id": body, "shape": "box", "center": center, "size": size}


def test_box_attach_places_bottom_on_the_targets_top():
    features, _ = build([
        box("base", "b", [0, 0, 0], [10, 10, 2]),
        {"id": "lid", "body_id": "b", "shape": "box", "size": [10, 10, 2],
         "attach": {"to": "base", "where": "top"}, "anchor": "bottom"},
    ])
    base, lid = features
    assert base["placement"]["position"] == {"x": -5, "y": -5, "z": -1}
    # Lid bottom (z=-1 in its own frame) meets the base top (z=1) -> centre z=2.
    assert lid["placement"]["position"] == {"x": -5, "y": -5, "z": 1}


def test_attach_offset_nudges_in_world_millimetres():
    features, _ = build([
        box("base", "b", [10, 0, 0], [4, 4, 4]),
        {"id": "cap", "body_id": "b", "shape": "box", "size": [2, 2, 2],
         "attach": {"to": "base", "where": "top", "offset": [1, -2, 0.5]}, "anchor": "bottom"},
    ])
    cap = features[1]
    # base top centre (10,0,2) + offset (1,-2,0.5); the cap's own bottom anchor
    # sits there, so its centre is one half-height above, and its placement
    # position (the box's minimum corner) another half below that.
    assert cap["placement"]["position"] == {"x": 10, "y": -3, "z": 2.5}


def test_align_and_length_give_a_cylinder_its_axis_without_an_endpoint():
    features, _ = build([
        {"id": "post", "body_id": "b", "shape": "cylinder",
         "start": [5, 5, 1], "radius": 2, "align": "Z", "length": 8},
    ])
    post = features[0]
    assert post["params"] == {"radius": 2.0, "height": 8.0}
    assert post["placement"]["position"] == {"x": 5, "y": 5, "z": 1}
    assert [round(v, 6) for v in post["placement"]["axis"].values()] == [1, 0, 0]


def test_attach_chains_through_a_placed_part():
    features, _ = build([
        box("base", "b", [0, 0, 0], [10, 10, 2]),
        {"id": "lid", "body_id": "b", "shape": "box", "size": [10, 10, 2],
         "attach": {"to": "base", "where": "top"}, "anchor": "bottom"},
        {"id": "post", "body_id": "b", "shape": "cylinder", "radius": 1,
         "align": "Z", "length": 6, "attach": {"to": "lid", "where": "top"}},
    ])
    post = features[2]
    # lid top is z=3 (centre 2 + half 1); post anchor defaults to its start.
    assert post["placement"]["position"] == {"x": 0, "y": 0, "z": 3}


def test_attach_can_anchor_to_a_loft_section_centre():
    features, _ = build([
        {"id": "shell", "body_id": "b", "shape": "loft", "section_axis": "X",
         "sections": [{"center": [0, 0, 0], "radii": [2, 2]},
                      {"center": [10, 0, 0], "radii": [3, 3]}]},
        {"id": "cap", "body_id": "b", "shape": "box", "size": [2, 2, 2],
         "attach": {"to": "shell", "where": "end"}, "anchor": "center"},
    ])
    cap = features[-1]  # the loft's own feature comes before its cap
    loft_feature = features[-2]
    assert loft_feature["op"] == "additive_loft"
    # "end" of the loft is the last section centre (10,0,0).
    assert cap["placement"]["position"] == {"x": 9, "y": -1, "z": -1}


def test_forward_and_repeated_targets_are_refused_by_name():
    with pytest.raises(ValueError, match="EARLIER part"):
        build([
            {"id": "cap", "body_id": "b", "shape": "box", "size": [1, 1, 1],
             "attach": {"to": "base", "where": "top"}},
            box("base", "b", [0, 0, 0], [4, 4, 4]),
        ])
    with pytest.raises(ValueError, match="repeated part"):
        build([
            {**box("hub", "b", [0, 0, 0], [2, 2, 2]),
             "copies": {"count": 3, "center": [0, 0, 0], "axis": [0, 0, 1]}},
            {"id": "cap", "body_id": "b", "shape": "box", "size": [1, 1, 1],
             "attach": {"to": "hub", "where": "top"}},
        ])


def test_relation_field_misuse_is_refused_at_validation():
    with pytest.raises(ValueError):
        BuildParts(parts=[{**box("a", "b", [0, 0, 0], [1, 1, 1]), "anchor": "top"}])
    with pytest.raises(ValueError):
        BuildParts(parts=[{**box("a", "b", [0, 0, 0], [1, 1, 1]), "align": "Z", "length": 2}])
    with pytest.raises(ValueError):
        BuildParts(parts=[{"id": "a", "body_id": "b", "shape": "cylinder",
                           "start": [0, 0, 0], "radius": 1, "align": "Z"}])
    with pytest.raises(ValueError):
        BuildParts(parts=[{"id": "a", "body_id": "b", "shape": "cylinder",
                           "start": [0, 0, 0], "end": [0, 0, 2], "radius": 1,
                           "align": "Z", "length": 2}])
    with pytest.raises(ValueError):
        BuildParts(parts=[{**box("a", "b", [0, 0, 0], [1, 1, 1]),
                           "attach": {"to": "t"}, "copies": {"count": 2, "center": [0, 0, 0]}}])


def test_anchor_names_not_defined_by_the_shape_are_refused():
    with pytest.raises(ValueError, match="has no 'start' anchor"):
        build([
            box("base", "b", [0, 0, 0], [4, 4, 4]),
            {"id": "cap", "body_id": "b", "shape": "box", "size": [1, 1, 1],
             "attach": {"to": "base", "where": "start"}},
        ])


@pytest.mark.parametrize('shape,dimensions', [
    ('cylinder', {'radius': 2}),
    ('tube', {'radius': 2, 'inner_radius': 1}),
    ('beam', {'width': 2, 'depth': 3}),
])
@pytest.mark.parametrize('direction', [{'end': [0, 0, 8]}, {'align': 'Z', 'length': 8}])
@pytest.mark.parametrize('attached', [False, True])
@pytest.mark.asyncio
async def test_direction_forms_pass_the_executable_tool_schema(tmp_path, shape, dimensions, direction, attached):
    from tcad.config.loader import load_default_config
    from tcad.core.types import ToolContext
    from tcad.core.wiring import build_services
    from tcad.ir.schema import IrDocument
    from tcad.tools.authoring import build_authoring_tools
    from tcad.tools.base import execute_tool

    cfg = load_default_config()
    cfg.storage.data_dir = str(tmp_path)
    cfg.storage.sqlite_path = ''
    services = build_services(cfg, start_worker=False)
    services.store.create('m', IrDocument(model_id='m'))
    ctx = ToolContext(model_id='m', thread_id='t', turn_id='turn', data_dir=str(tmp_path))
    post = {'id': 'post', 'body_id': 'frame', 'shape': shape, **dimensions, **direction}
    if attached:
        post['attach'] = {'to': 'base', 'where': 'top'}
        parts = [box('base', 'frame', [0, 0, 0], [10, 10, 4]), post]
    else:
        post['start'] = [0, 0, 0]
        parts = [post]
    try:
        outcome = await execute_tool(build_authoring_tools(services)['cad_build_parts'], {'parts': parts}, ctx)
        assert outcome.result.ok, outcome.result.error
        feature = services.store.load('m').find_feature('post')
        assert feature.params['height'] == 8
        assert feature.placement.position.z == (2 if attached else 0)
    finally:
        services.worker.close()


@pytest.mark.parametrize('direction', [
    {}, {'align': 'Z'}, {'length': 8},
    {'end': [0, 0, 8], 'align': 'Z', 'length': 8},
])
def test_tool_schema_rejects_incomplete_or_mixed_directions(direction):
    from tcad.tools.authoring import build_authoring_tools
    from tcad.tools.schema_check import validate_tool_args

    spec = build_authoring_tools(None)['cad_build_parts']
    post = {'id': 'post', 'body_id': 'frame', 'shape': 'cylinder',
            'start': [0, 0, 0], 'radius': 2, **direction}
    assert validate_tool_args({'parts': [post]}, spec)


@pytest.mark.asyncio
async def test_handler_resolves_relations_across_the_whole_batch(tmp_path):
    from tcad.config.loader import load_default_config
    from tcad.core.types import ToolContext
    from tcad.core.wiring import build_services
    from tcad.ir.schema import IrDocument
    from tcad.tools.authoring import build_parts_handler

    cfg = load_default_config()
    cfg.storage.data_dir = str(tmp_path)
    cfg.storage.sqlite_path = ""
    services = build_services(cfg, start_worker=False)
    services.store.create("m", IrDocument(model_id="m"))
    ctx = ToolContext(model_id="m", thread_id="t", turn_id="turn", data_dir=str(tmp_path))
    try:
        result = await build_parts_handler(services, {"parts": [
            box("base", "frame", [0, 0, 0], [20, 20, 4]),
            {"id": "lid", "body_id": "frame", "shape": "box", "size": [20, 20, 2],
             "attach": {"to": "base", "where": "top"}, "anchor": "bottom"},
        ], "reason": "Stack the lid"}, ctx)
        assert result.ok, result.error
        lid = services.store.load("m").find_feature("lid")
        # The lid's minimum corner lands exactly on the base top (z=2): its
        # bottom anchor met the base's top anchor, with no coordinate math.
        assert math.isclose(lid.placement.position.z, 2, abs_tol=1e-9)
    finally:
        services.worker.close()
