"""Patterns - repeating a seed feature without duplicating it (§6.5).

The mirror is the one that has real geometry in it.  A linear or circular copy
only moves the artwork; a mirror *reflects* it, and a reflection is not a flip of
the artwork plus the rotation it already had.  Reflecting a placement turned by
theta across a line at alpha has to come back turned by 2*alpha - theta, and the
only way to see that is to push a point of the artwork through the transform the
tool solid actually uses.
"""

from __future__ import annotations

import pytest
from OCP.gp import gp_Pnt

from stamp.core.document import (
    Feature,
    PatternKind,
    PatternSpec,
    Placement,
    Plane,
)
from stamp.geom.tool_solid import placement_transform

#: The sketch plane is the world XY plane, so an (x, y) in the artwork reads
#: straight off the answer and the expected numbers stay legible.
FLAT = Plane(origin=(0.0, 0.0, 0.0), normal=(0.0, 0.0, 1.0), u_axis=(1.0, 0.0, 0.0))


def where(placement: Placement, point: tuple[float, float]) -> tuple[float, float]:
    """Where *point* in the artwork lands, exactly as the tool solid puts it.

    The mirror and the scale ride on the shape rather than on the transform -
    a ``gp_Trsf`` cannot hold a reflection - so both halves are applied here in
    the order ``build_tool_solid`` applies them.
    """
    sx, sy = placement.scale
    if placement.mirror_u:
        sx = -sx
    if placement.mirror_v:
        sy = -sy
    placed = gp_Pnt(point[0] * sx, point[1] * sy, 0.0)
    placed.Transform(placement_transform(placement, FLAT))
    return (placed.X(), placed.Y())


def mirrored(rotation: float, axis_angle: float, offset=(3.0, 4.0)) -> Feature:
    """A seed at *offset*, turned by *rotation*, mirrored about *axis_angle*."""
    return Feature(
        name="Logo",
        placement=Placement(offset_2d=offset, rotation=rotation),
        pattern=PatternSpec(kind=PatternKind.MIRROR, axis_angle=axis_angle),
    )


def reflect(point: tuple[float, float], axis_angle: float) -> tuple[float, float]:
    """*point* reflected across the line through the origin at *axis_angle*."""
    import math

    radians = math.radians(2.0 * axis_angle)
    x, y = point
    return (x * math.cos(radians) + y * math.sin(radians),
            x * math.sin(radians) - y * math.cos(radians))


class TestMirrorPattern:
    def test_it_makes_the_seed_and_one_copy(self):
        seed, copy = mirrored(0.0, 0.0).pattern_instances()
        assert seed.placement.offset_2d == (3.0, 4.0)
        assert copy.placement.offset_2d == pytest.approx((3.0, -4.0))
        assert copy.pattern is None

    def test_a_turned_seed_comes_back_turned_the_other_way(self):
        """The bug: the flip was toggled and the rotation was left alone."""
        seed, copy = mirrored(30.0, 0.0).pattern_instances()
        probe = (2.0, 1.0)

        assert where(seed.placement, probe) == pytest.approx((4.232, 5.866), abs=1e-3)
        assert where(copy.placement, probe) == pytest.approx((4.232, -5.866), abs=1e-3)

    def test_the_copy_is_the_reflection_of_the_seed(self):
        """Every corner of the artwork, not just the one the numbers were read off."""
        for rotation, axis in ((30.0, 0.0), (0.0, 90.0), (-20.0, 35.0), (110.0, -15.0)):
            seed, copy = mirrored(rotation, axis).pattern_instances()
            for probe in ((2.0, 1.0), (-3.0, 0.5), (0.0, 0.0), (4.0, -2.5)):
                assert where(copy.placement, probe) == pytest.approx(
                    reflect(where(seed.placement, probe), axis), abs=1e-6
                ), f"rotation {rotation} about {axis} at {probe}"

    def test_an_axis_at_ninety_degrees_reflects_left_to_right(self):
        seed, copy = mirrored(0.0, 90.0).pattern_instances()
        probe = (2.0, 1.0)

        assert where(seed.placement, probe) == pytest.approx((5.0, 5.0), abs=1e-6)
        assert where(copy.placement, probe) == pytest.approx((-5.0, 5.0), abs=1e-6)

    def test_the_copy_still_reads_as_mirrored_artwork(self):
        """A reflection has to flip the artwork as well as move it."""
        _seed, copy = mirrored(30.0, 0.0).pattern_instances()
        assert copy.placement.mirror_v is True
        assert copy.placement.rotation == pytest.approx(-30.0)

    def test_mirroring_twice_is_the_identity(self):
        """Reflecting the reflection has to land back on the seed."""
        seed, copy = mirrored(42.0, 17.0).pattern_instances()
        again = Feature(
            name="again",
            placement=Placement.from_dict(copy.placement.to_dict()),
            pattern=PatternSpec(kind=PatternKind.MIRROR, axis_angle=17.0),
        )
        _first, back = again.pattern_instances()
        for probe in ((2.0, 1.0), (-1.0, 3.0)):
            assert where(back.placement, probe) == pytest.approx(
                where(seed.placement, probe), abs=1e-6
            )

    def test_the_seed_itself_is_untouched(self):
        feature = mirrored(30.0, 25.0)
        feature.pattern_instances()
        assert feature.placement.rotation == 30.0
        assert feature.placement.mirror_v is False
