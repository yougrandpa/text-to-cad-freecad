"""Bounded measurements of saved poses, without re-solving or executing formulas."""

import math
from collections import Counter
from typing import Sequence


def _point(matrix: list[float], point: list[float]) -> list[float]:
    return [sum(matrix[row + j] * point[j] for j in range(3)) + matrix[row + 3]
            for row in (0, 4, 8)]


def _rotation_deg(first: list[float], second: list[float]) -> float:
    # trace(R_second R_first^T), independent of translation or pivot location.
    trace = sum(first[i] * second[i] for i in (0, 1, 2, 4, 5, 6, 8, 9, 10))
    cosine = max(-1.0, min(1.0, (trace - 1) / 2))
    return 0.0 if cosine > 1 - 1e-12 else math.degrees(math.acos(cosine))


def _track(points: list[list[float]], start: float, step: float,
           sample_frames: list[int] | None) -> dict:
    low = [min(p[j] for p in points) for j in range(3)]
    high = [max(p[j] for p in points) for j in range(3)]
    distances = [math.dist(points[0], p) for p in points]
    peak = max(range(len(points)), key=distances.__getitem__)
    if sample_frames is None:
        indices = {0, len(points) - 1, peak}
        for axis in range(3):
            indices.add(min(range(len(points)), key=lambda i: points[i][axis]))
            indices.add(max(range(len(points)), key=lambda i: points[i][axis]))
        sample_frames = sorted(indices)
    return {
        "min_world_mm": [round(v, 6) for v in low],
        "max_world_mm": [round(v, 6) for v in high],
        "span_mm": [round(b - a, 6) for a, b in zip(low, high)],
        "max_displacement_from_first_mm": round(distances[peak], 6),
        "peak_displacement_frame": peak,
        "samples": [{"frame_index": i, "time_s": round(start + i * step, 9),
                     "world_mm": [round(v, 6) for v in points[i]]} for i in sample_frames],
    }


def measure_saved_motion(animation: dict, vertices: list[list[float]], *,
                         track_points: Sequence[dict] = (), sample_frames: list[int] | None = None) -> dict:
    """Use all saved frames for extrema; bound the returned point samples."""
    frames, parts = animation["frames"], animation["parts"]
    start, step = animation["start"], animation["step"]
    if sample_frames is not None:
        if (not 1 <= len(sample_frames) <= 12
                or any(type(i) is not int or not 0 <= i < len(frames) for i in sample_frames)):
            raise ValueError(f"sample_frames must contain 1..12 indices in 0..{len(frames) - 1}")
        sample_frames = sorted(set(sample_frames))
    if len(track_points) > 12:
        raise ValueError("track_points supports at most 12 reference points")
    body_ids = {p["body_id"] for p in parts}
    names = set()
    for probe in track_points:
        name, point = probe["name"], probe["point"]
        if name in names:
            raise ValueError(f"duplicate track point name: {name}")
        names.add(name)
        if probe["body_id"] not in body_ids:
            raise ValueError(f"unknown track point body: {probe['body_id']}")
        if len(point) != 3 or not all(type(v) in (int, float) and math.isfinite(v) for v in point):
            raise ValueError("track point must contain three finite world coordinates in mm")

    bodies, moving = [], []
    for part in parts:
        body = part["body_id"]
        segment = vertices[part["vertex_start"]:part["vertex_start"] + part["vertex_count"]]
        center = [(min(p[j] for p in segment) + max(p[j] for p in segment)) / 2 for j in range(3)]
        matrices = [f[body] for f in frames]
        rotations = [_rotation_deg(matrices[0], m) for m in matrices]
        peak = max(range(len(frames)), key=rotations.__getitem__)
        center_track = _track([_point(m, center) for m in matrices], start, step, sample_frames)
        if rotations[peak] > 1e-5 or center_track["max_displacement_from_first_mm"] > 1e-5:
            moving.append(body)
        bodies.append({"body_id": body, "max_rotation_from_first_deg": round(rotations[peak], 6),
                       "peak_rotation_frame": peak,
                       "sampled_rotation_path_deg": round(sum(_rotation_deg(a, b)
                           for a, b in zip(matrices, matrices[1:])), 6),
                       "preview_bbox_center": center_track})
    tracks = [{"name": p["name"], "body_id": p["body_id"], "reference_point_world_mm": p["point"],
               **_track([_point(f[p["body_id"]], p["point"]) for f in frames],
                        start, step, sample_frames)} for p in track_points]
    return {"frames_examined": len(frames), "moving_bodies": moving, "bodies": bodies,
            "point_tracks": tracks,
            "scope": "All saved poses measured relative to the first saved frame. Rotation is shortest orientation change (0..180 degrees); sampled rotation path may miss turns between frames. Centers are preview bounding-box reference points, not mass centers. Probe coordinates refer to pre-solve world geometry; attachment to solid is not verified. No ground contact, forces or continuous-path proof."}


def assembly_definition(assembly: dict) -> dict:
    if assembly.get("rotation"):
        return {"kind": "gravity_pendulum", "grounded": assembly["grounded"],
                "rotating_body_ids": assembly["rotation"]["rotating_body_ids"]}
    drivers = assembly.get("drivers", [])
    return {"kind": "native", "grounded": assembly.get("grounded", []),
            "prescribed_driver_count": len(drivers), "drivers": drivers,
            "active_joint_types": dict(Counter(j["type"] for j in assembly.get("joints", [])
                                               if not j.get("suppressed"))),
            "scope": "Saved declaration, not a measured mobility count or evidence of physical transmission geometry. Matching formulas remain separate prescribed drivers."}


def summarize_interferences(interferences: list[dict] | None) -> list[dict]:
    """Include every checked pair, even when the raw example list is truncated."""
    pairs = {}
    for item in interferences or []:
        key = tuple(sorted(item["bodies"]))
        volume = item["overlap_mm3"]
        entry = pairs.setdefault(key, {"bodies": list(key), "overlap_samples": 0,
                                      "min_overlap_mm3": volume, "max_overlap_mm3": volume,
                                      "peak_frame": item["frame"]})
        entry["overlap_samples"] += 1
        entry["min_overlap_mm3"] = min(entry["min_overlap_mm3"], volume)
        if volume > entry["max_overlap_mm3"]:
            entry.update(max_overlap_mm3=volume, peak_frame=item["frame"])
    return sorted(pairs.values(), key=lambda p: (-p["max_overlap_mm3"], p["bodies"]))
