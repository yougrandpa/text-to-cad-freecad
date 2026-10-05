"""Shared scene preparation for viewport capture and agent snapshots."""

from tcad.core.types import BBox


def prepare_scene(scene, *, angle=0, frame=0):
    mesh = scene.posed_mesh(angle=angle, frame=frame)
    lows = [min(p[j] for p in mesh.vertices) for j in range(3)]
    highs = [max(p[j] for p in mesh.vertices) for j in range(3)]
    bbox = BBox(**dict(zip(("x", "y", "z", "x_min", "y_min", "z_min"),
                         [highs[j] - lows[j] for j in range(3)] + lows)))
    return mesh.model_copy(update={"bbox": bbox})
