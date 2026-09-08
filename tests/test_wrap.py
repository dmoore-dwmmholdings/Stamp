"""Wrapped artwork on cylindrical and conical faces - spec §6.1, §6.3.

A wrap is not a projection.  The artwork is rolled onto the surface, so the arc it
covers on the part is as long as the artwork is wide, the walls come straight out of
the axis, and the tool exists only on the wall the user clicked.  These tests measure
all three, because every one of them was wrong in a way that still built a solid.
"""

from __future__ import annotations

import math

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from stamp.core.document import (
    DepthMode,
    Direction,
    EdgeRole,
    Operation,
    OperationKind,
    Placement,
    PlacementMode,
    Plane,
)
from stamp.geom import solid_ops
from stamp.geom.tool_solid import ToolSolidError, build_tool_solid
from stamp.io.normalize import normalize

#: The test part: a plain Ø20 x 40 tube standing on the origin, on the z axis.
RADIUS = 10.0
HEIGHT = 40.0
DIAGONAL = math.hypot(2 * RADIUS, HEIGHT)


@pytest.fixture(scope="module")
def tube():
    return BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), RADIUS, HEIGHT
    ).Shape()


@pytest.fixture(scope="module")
def tube_face(tube):
    from stamp.core.refs import faces_of, surface_kind

    return next(f for f in faces_of(tube) if surface_kind(f) == "cylinder")


def rectangle(width: float, height: float):
    """A plain rectangular profile, centered on the origin like any import."""
    corners = [
        (-width / 2, -height / 2),
        (width / 2, -height / 2),
        (width / 2, height / 2),
        (-width / 2, height / 2),
    ]
    edges = [
        BRepBuilderAPI_MakeEdge(
            gp_Pnt(corners[i][0], corners[i][1], 0.0),
            gp_Pnt(corners[(i + 1) % 4][0], corners[(i + 1) % 4][1], 0.0),
        ).Edge()
        for i in range(4)
    ]
    return normalize(edges)


def side_plane(z: float = 20.0) -> Plane:
    """The sketch plane on the tube's wall at +x, u running round the tube."""
    return Plane(origin=(RADIUS, 0.0, z), normal=(1.0, 0.0, 0.0), u_axis=(0.0, 1.0, 0.0))


def wrap(profile, face, operation, plane=None, placement=None):
    return build_tool_solid(
        profile,
        placement or Placement(mode=PlacementMode.WRAP),
        operation,
        plane or side_plane(),
        part_diagonal=DIAGONAL,
        target_face=face,
    )


def cut(depth: float = 1.0) -> Operation:
    return Operation(
        kind=OperationKind.CUT, depth_mode=DepthMode.BLIND, depth=depth,
        direction=Direction.INTO,
    )


def add(depth: float = 1.0) -> Operation:
    return Operation(
        kind=OperationKind.ADD, depth_mode=DepthMode.BLIND, depth=depth,
        direction=Direction.OUT_OF,
    )


def volume_beyond(shape, x: float) -> float:
    """How much of *shape* lies past ``x``, on the far side of the tube."""
    box = BRepPrimAPI_MakeBox(gp_Pnt(-500, -500, -500), gp_Pnt(x, 500, 500)).Shape()
    common = BRepAlgoAPI_Common(shape, box)
    common.Build()
    return solid_ops.volume(common.Shape()) if common.IsDone() else float("nan")


def angular_span(shape) -> float:
    """The angle about the z axis the shape covers, in radians."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    angles = []
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_VERTEX)
    while explorer.More():
        point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(explorer.Current()))
        angles.append(math.atan2(point.Y(), point.X()))
        explorer.Next()
    return max(angles) - min(angles)


def radial_range(shape) -> tuple[float, float]:
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    radii = []
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_VERTEX)
    while explorer.More():
        point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(explorer.Current()))
        radii.append(math.hypot(point.X(), point.Y()))
        explorer.Next()
    return min(radii), max(radii)


class TestSingleWall:
    def test_the_tool_is_only_on_the_wall_that_was_clicked(self, tube_face):
        """A 360° band through the whole tube stamps the far wall too, mirrored."""
        tool = wrap(rectangle(9.0, 4.0), tube_face, cut(1.0))
        assert volume_beyond(tool.shape, 0.0) == pytest.approx(0.0, abs=1e-9)
        assert solid_ops.volume(tool.shape) > 0

    def test_a_wrapped_cut_removes_material_from_one_side_only(self, tube, tube_face):
        tool = wrap(rectangle(9.0, 4.0), tube_face, cut(1.0))
        result = solid_ops.boolean(tube, tool.shape, "cut", bbox_diagonal=DIAGONAL)
        before = solid_ops.volume(tube)
        removed = before - solid_ops.volume(result.shape)
        # One pocket, not two: the annulus sector of a 9 x 4 mark, 1 mm deep.
        expected = 0.5 * (RADIUS**2 - (RADIUS - 1.0) ** 2) * (9.0 / RADIUS) * 4.0
        assert removed == pytest.approx(expected, rel=1e-3)


class TestArcLength:
    def test_the_artwork_covers_the_arc_it_is_wide(self, tube_face):
        """18 mm of artwork on a Ø20 tube is 1.8 radians of tube, not 2.24."""
        tool = wrap(rectangle(18.0, 6.0), tube_face, cut(1.0))
        assert angular_span(tool.shape) == pytest.approx(18.0 / RADIUS, rel=1e-6)

    def test_a_projection_would_have_been_wider(self, tube_face):
        """The shadow of an 18 mm mark reaches ±64.2°; the wrap reaches ±51.6°."""
        tool = wrap(rectangle(18.0, 6.0), tube_face, cut(1.0))
        projected = 2 * math.asin(9.0 / RADIUS)
        assert angular_span(tool.shape) < projected - 0.2

    def test_the_walls_come_out_of_the_axis(self, tube_face):
        """The rim and the floor of the pocket cover the same angle.

        That is what a radial wall means, and it is the thing a projection cannot
        do: cast a shadow into the tube and the floor of the pocket is narrower in
        angle than the rim, because the straight wall cuts the arc as a chord.
        """
        tool = wrap(rectangle(12.0, 5.0), tube_face, cut(2.0))
        rim = angular_span(_vertices_near_radius(tool.shape, RADIUS + 1e-3))
        floor = angular_span(_vertices_near_radius(tool.shape, RADIUS - 2.0))
        assert rim == pytest.approx(12.0 / RADIUS, rel=1e-6)
        assert floor == pytest.approx(rim, rel=1e-9)

    def test_artwork_wider_than_the_face_is_refused(self, tube_face):
        with pytest.raises(ToolSolidError, match="wider than the way round"):
            wrap(rectangle(70.0, 4.0), tube_face, cut(0.5))


class TestDepth:
    def test_a_pocket_is_exactly_as_deep_as_asked(self, tube_face):
        """The overlap margin belongs on the discarded side, outside the face."""
        tool = wrap(rectangle(8.0, 4.0), tube_face, cut(1.5))
        low, high = radial_range(tool.shape)
        assert low == pytest.approx(RADIUS - 1.5, abs=1e-9)
        assert high > RADIUS  # the margin, sitting proud of the face

    def test_a_boss_is_exactly_as_tall_as_asked(self, tube_face):
        tool = wrap(rectangle(8.0, 4.0), tube_face, add(0.8))
        low, high = radial_range(tool.shape)
        assert high == pytest.approx(RADIUS + 0.8, abs=1e-9)
        assert low < RADIUS  # the margin, buried in the part

    def test_a_draft_angle_is_refused_rather_than_ignored(self, tube_face):
        operation = Operation(
            kind=OperationKind.CUT, depth_mode=DepthMode.BLIND, depth=1.0,
            direction=Direction.INTO, draft_angle=3.0,
        )
        with pytest.raises(ToolSolidError, match="draft"):
            wrap(rectangle(8.0, 4.0), tube_face, operation)


class TestWrappedEdges:
    def test_edges_are_classified_by_radius_not_by_height(self, tube_face):
        """A four-sided mark has four edges top, four bottom and four walls.

        Measured along the sketch-plane normal instead, the far corners of the floor
        sit further from the plane than the near corners of the rim, and the counts
        come out three and five.
        """
        tool = wrap(rectangle(12.0, 5.0), tube_face, cut(1.0))
        assert tool.axis is not None
        groups = solid_ops.classify_feature_edges(tool.shape, tool.direction, tool.axis)
        assert len(groups[EdgeRole.TOP]) == 4
        assert len(groups[EdgeRole.BOTTOM]) == 4
        assert len(groups[EdgeRole.SIDE]) == 4

    def test_the_flat_classifier_gets_a_wrapped_tool_wrong(self, tube_face):
        """The reason the axis has to be carried at all - kept so it stays fixed."""
        tool = wrap(rectangle(12.0, 5.0), tube_face, cut(1.0))
        flat = solid_ops.classify_feature_edges(tool.shape, tool.direction)
        radial = solid_ops.classify_feature_edges(tool.shape, tool.direction, tool.axis)
        assert len(flat[EdgeRole.TOP]) != len(radial[EdgeRole.TOP])

    def test_a_planar_tool_is_unchanged_by_the_new_argument(self, logo_profile, top_plane, bracket_step):
        tool = build_tool_solid(
            logo_profile, Placement(),
            Operation(kind=OperationKind.ADD, depth_mode=DepthMode.BLIND, depth=0.8,
                      direction=Direction.OUT_OF),
            top_plane, part_diagonal=bracket_step.diagonal,
        )
        assert tool.axis is None
        groups = solid_ops.classify_feature_edges(tool.shape, tool.direction, tool.axis)
        assert len(groups[EdgeRole.TOP]) == 8
        assert len(groups[EdgeRole.BOTTOM]) == 8
        assert len(groups[EdgeRole.SIDE]) == 8


class TestHolesAndPlacement:
    def test_a_hole_in_the_artwork_survives_the_wrap(self, tube_face):
        """The logo is a rectangle with a rectangular hole; the wrap keeps the hole."""
        outer = rectangle(20.0, 10.0)
        pierced = _rectangle_with_hole(20.0, 10.0, 6.0, 4.0)
        solid = wrap(outer, tube_face, cut(1.0))
        holed = wrap(pierced, tube_face, cut(1.0))
        assert solid_ops.volume(holed.shape) < solid_ops.volume(solid.shape)
        # The hole is 6 x 4 of a 20 x 10 mark, and the wrap is area-preserving.
        band = 0.5 * ((RADIUS + 1e-3) ** 2 - (RADIUS - 1.0) ** 2) / RADIUS
        missing = solid_ops.volume(solid.shape) - solid_ops.volume(holed.shape)
        assert missing == pytest.approx(band * 6.0 * 4.0, rel=1e-3)

    def test_rotating_the_placement_turns_the_mark_on_the_face(self, tube_face):
        """Rotated a quarter turn, a wide mark becomes a tall one."""
        flat = wrap(rectangle(12.0, 4.0), tube_face, cut(0.5))
        turned = wrap(
            rectangle(12.0, 4.0), tube_face, cut(0.5),
            placement=Placement(mode=PlacementMode.WRAP, rotation=90.0),
        )
        assert angular_span(flat.shape) == pytest.approx(12.0 / RADIUS, rel=1e-6)
        assert angular_span(turned.shape) == pytest.approx(4.0 / RADIUS, rel=1e-6)

    def test_the_decal_preview_sits_on_the_face(self, tube_face):
        tool = wrap(rectangle(9.0, 4.0), tube_face, cut(1.0))
        low, high = radial_range(tool.footprint)
        assert low == pytest.approx(RADIUS, abs=0.01)
        assert high == pytest.approx(RADIUS, abs=0.01)


class TestCone:
    """A cone is still a projection onto the tangent plane, so it is kept small."""

    @pytest.fixture(scope="class")
    @classmethod
    def cone_face(cls):
        from stamp.core.refs import faces_of, surface_kind

        shape = BRepPrimAPI_MakeCone(8.0, 4.0, 20.0).Shape()
        return next(f for f in faces_of(shape) if surface_kind(f) == "cone")

    def _plane(self, cone_face):
        """A plane from a click halfway up the +x side of the cone.

        Not from the face centre: the centroid of a whole cone's lateral surface
        lies on its axis, and projecting a point on the axis back onto the surface
        can land anywhere round it.
        """
        from stamp.core.refs import plane_from_face

        plane, _warnings = plane_from_face(cone_face, (6.0, 0.0, 10.0))
        return plane

    def test_a_conical_wrap_stamps_one_wall(self, cone_face):
        plane = self._plane(cone_face)
        tool = build_tool_solid(
            rectangle(3.0, 2.0), Placement(mode=PlacementMode.WRAP), cut(0.4), plane,
            part_diagonal=25.0, target_face=cone_face,
        )
        # The click is on the +x side of the cone, so nothing may reach -x.
        assert volume_beyond(tool.shape, 0.0) == pytest.approx(0.0, abs=1e-9)
        assert solid_ops.volume(tool.shape) > 0

    def test_artwork_too_wide_for_a_projection_is_refused(self, cone_face):
        plane = self._plane(cone_face)
        with pytest.raises(ToolSolidError, match="too wide to wrap onto this cone"):
            build_tool_solid(
                rectangle(12.0, 12.0), Placement(mode=PlacementMode.WRAP), cut(0.4), plane,
                part_diagonal=25.0, target_face=cone_face,
            )


def _rectangle_with_hole(width, height, hole_w, hole_h):
    def ring(w, h):
        pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
        return [
            BRepBuilderAPI_MakeEdge(
                gp_Pnt(pts[i][0], pts[i][1], 0.0),
                gp_Pnt(pts[(i + 1) % 4][0], pts[(i + 1) % 4][1], 0.0),
            ).Edge()
            for i in range(4)
        ]

    return normalize(ring(width, height) + ring(hole_w, hole_h))


def _vertices_near_radius(shape, radius: float, tolerance: float = 1e-6):
    """The shape's vertices that sit at *radius* from the z axis, as a compound."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_VERTEX)
    while explorer.More():
        vertex = TopoDS.Vertex_s(explorer.Current())
        point = BRep_Tool.Pnt_s(vertex)
        if abs(math.hypot(point.X(), point.Y()) - radius) < tolerance:
            builder.Add(compound, vertex)
        explorer.Next()
    return compound
