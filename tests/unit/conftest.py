"""Shared fixtures/factories for the IR + store unit tests."""

from __future__ import annotations

from tcad.ir.schema import (
    BodySpec,
    FeatureSpec,
    IrDocument,
    PlaneRef,
    SketchGeom,
    SketchSpec,
    Vec3,
)


def make_minimal_ir(model_id: str = "m1", version: int = 0) -> IrDocument:
    """A valid IR: one body, one rectangle sketch on XY, one pad from it."""
    body = BodySpec(
        id="body_1",
        name="body_1",
        sketches=[
            SketchSpec(
                id="sk_outline",
                name="outline",
                plane=PlaneRef(kind="origin_plane", plane="XY"),
                geometry=[
                    SketchGeom(id="g0", kind="line",
                               points=[Vec3(x=0, y=0, z=0), Vec3(x=10, y=0, z=0)]),
                    SketchGeom(id="g1", kind="line",
                               points=[Vec3(x=10, y=0, z=0), Vec3(x=10, y=10, z=0)]),
                    SketchGeom(id="g2", kind="line",
                               points=[Vec3(x=10, y=10, z=0), Vec3(x=0, y=10, z=0)]),
                    SketchGeom(id="g3", kind="line",
                               points=[Vec3(x=0, y=10, z=0), Vec3(x=0, y=0, z=0)]),
                ],
            )
        ],
        features=[
            FeatureSpec(id="ft_pad", name="pad", op="pad",
                        profile_sketch="sk_outline", params={"length": 5}),
        ],
    )
    return IrDocument(model_id=model_id, version=version, bodies=[body])


def make_two_feature_ir(model_id: str = "m1") -> IrDocument:
    """Body with feature 'a' (pad) and 'b' (hole) where b refs a.

    Used to exercise dependent-removal refusal.
    """
    body = BodySpec(
        id="body_1",
        name="body_1",
        features=[
            FeatureSpec(id="a", name="base", op="pad",
                        profile_sketch=None, params={"length": 1}),
            FeatureSpec(id="b", name="hole", op="hole",
                        profile_sketch=None, refs=["a"], params={"diameter": 2}),
        ],
    )
    return IrDocument(model_id=model_id, version=0, bodies=[body])
