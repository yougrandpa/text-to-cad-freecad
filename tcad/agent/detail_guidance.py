"""Feature-oriented discovery backed by the existing native IR operations."""
from math import pi

DETAIL_ROUTES = {
    "slot": {"description": "长圆槽 / straight rounded slot: two lines + two semicircles, then pocket.",
             "topics": ["sketch", "feature"], "feature_ops": ["pocket"]},
    "arc": {"description": "圆弧轮廓 / circular arc: native arc + closing edges, then pad or pocket.",
            "topics": ["sketch", "feature"], "feature_ops": ["pad", "pocket"]},
    "fillet": {"description": "圆角 / edge fillet: round selected measured edges of the current solid.",
               "topics": ["feature"], "feature_ops": ["fillet"]},
    "chamfer": {"description": "倒角 / edge chamfer: bevel selected measured edges of the current solid.",
                "topics": ["feature"], "feature_ops": ["chamfer"]},
    "groove": {"description": "环槽 / revolved groove: revolve a closed cutting profile about an axis.",
               "topics": ["sketch", "feature"], "feature_ops": ["groove"]},
}

DETAIL_SELECTION = (
    "Choose details from the request and part function: rounded slots use arc/line sketches + pocket; "
    "curved boundaries use native arcs, ellipses or bsplines + pad/pocket; "
    "edge fillets/chamfers use measured edges after the main shape and cuts. "
    "Read ir_help(topic=detail) for choices, then select detail=slot|arc|fillet|chamfer|groove "
    "to get an example and unlock matching native edits together. "
    "Do not replace requested slots/arcs with rectangular cutouts or silently omit failed treatments. "
    "No feature quota: add only appropriate details; unspecified radii/depths are assumptions."
)


def _point(x: float, y: float, z: float = 0) -> dict[str, float]:
    return {"x": x, "y": y, "z": z}


def _line(id: str, start: dict, end: dict) -> dict:
    return {"id": id, "kind": "line", "points": [start, end]}


def _arc(id: str, center: dict, radius: float, start: float, end: float) -> dict:
    return {"id": id, "kind": "arc", "points": [center], "radius": radius,
            "theta1": start, "theta2": end}


def detail_profile_example(detail: str) -> dict:
    """Examples in mm using fully fixed geometry, not guessed Face/Edge numbers."""
    if detail == "slot":
        geometry = [
            _line("bottom", _point(30, 22), _point(50, 22)),
            _arc("right", _point(50, 25), 3, -pi / 2, pi / 2),
            _line("top", _point(50, 28), _point(30, 28)),
            _arc("left", _point(30, 25), 3, pi / 2, 3 * pi / 2),
        ]
        plane, body, op, params = "XY", "base", "pocket", {"length": 3, "reversed": True}
    elif detail == "arc":
        geometry = [_arc("boundary", _point(0, 0), 8, -pi / 2, pi / 2),
                    _line("diameter", _point(0, 8), _point(0, -8))]
        plane, body, op, params = "XY", "arc_part", "pad", {"length": 5}
    elif detail == "groove":
        points = [_point(8, 0, 6), _point(12, 0, 6), _point(12, 0, 10), _point(8, 0, 10)]
        geometry = [_line(f"side_{i}", point, points[(i + 1) % 4])
                    for i, point in enumerate(points)]
        plane, body, op, params = "XZ", "base", "groove", {"axis": "v_axis", "angle": 360}
    else:
        raise ValueError(f"No profile example for {detail!r}")
    sketch = f"{detail}_profile"
    ops = []
    if detail == "arc":
        ops.append({"op": "add_body", "payload": {"id": body, "name": body},
                    "reason": "Standalone arc-profile example; adapt ownership to the actual part"})
    ops.extend([
        {"op": "add_sketch", "payload": {"id": sketch, "name": sketch, "body_id": body,
            "plane": {"kind": "origin_plane", "plane": plane}, "geometry": geometry,
            "constraints": [{"type": "Block", "refs": [i]} for i in range(len(geometry))]},
         "reason": "Create an exact closed native profile"},
        {"op": "add_feature", "payload": {"id": f"{detail}_feature", "name": detail,
            "body_id": body, "op": op, "profile_sketch": sketch, "params": params},
         "reason": "Build the selected detail from its native profile"},
    ])
    return {"base_version": "current", "ops": ops}
