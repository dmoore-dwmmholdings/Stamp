"""Tool solids, booleans, fillets - spec §6.3, §6.4, §2."""

from __future__ import annotations

import pytest

from stamp.core.document import (
    DepthMode,
    Direction,
    EdgeRole,
    EdgeSelector,
    Modifier,
    ModifierKind,
    Operation,
    OperationKind,
    Placement,
)
from stamp.geom import mesh_ops, solid_ops
from stamp.geom.tool_solid import ToolSolidError, build_tool_solid, contact_overlap_for

# logo.svg: a 36 x 16 rectangle with an 8 x 8 hole, plus a filled circle of radius 5
# drawn on top of it.  The circle sits inside the rectangle, so it adds no area - it
# is separate *material*, not a hole, which is what the fill rule decides (§5.3).
PROFILE_AREA = 36 * 16 - 8 * 8


def add_op(depth=0.8):
    return Operation(
        kind=OperationKind.ADD, depth_mode=DepthMode.BLIND, depth=depth,
        direction=Direction.OUT_OF,
    )


def cut_op(depth=0.5, mode=DepthMode.BLIND):
    return Operation(
        kind=OperationKind.CUT, depth_mode=mode, depth=depth, direction=Direction.INTO
    )


class TestToolSolid:
    def test_volume_is_area_times_depth(self, logo_profile, top_plane, bracket_step):
        tool = build_tool_solid(
            logo_profile, Placement(), add_op(0.8), top_plane,
            part_diagonal=bracket_step.diagonal, contact_overlap=0.0,
        )
        assert solid_ops.volume(tool.shape) == pytest.approx(PROFILE_AREA * 0.8, rel=1e-3)

    def test_overlapping_fills_are_fused_not_double_counted(
        self, logo_profile, top_plane, bracket_step
    ):
        """The circle of logo.svg lies inside the rectangle, so the tool is one solid.

        Normalization already resolved the overlap in 2D, so this is really a check
        that nothing downstream reintroduces it.
        """
        tool = build_tool_solid(
            logo_profile, Placement(), add_op(1.0), top_plane,
            part_diagonal=bracket_step.diagonal, contact_overlap=0.0,
        )
        assert solid_ops.solid_count(tool.shape) == 1
        assert solid_ops.volume(tool.shape) == pytest.approx(PROFILE_AREA, rel=1e-3)

    def test_disjoint_profile_makes_several_solids(self, fixtures, top_plane, bracket_step):
        from stamp.io.profile_import import import_profile

        profile = import_profile(fixtures / "serial.dxf").profile
        tool = build_tool_solid(
            profile, Placement(), cut_op(1.0), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        assert solid_ops.solid_count(tool.shape) == 5

    def test_uniform_scale(self, logo_profile, top_plane, bracket_step):
        tool = build_tool_solid(
            logo_profile, Placement(scale=(0.5, 0.5)), add_op(1.0), top_plane,
            part_diagonal=bracket_step.diagonal, contact_overlap=0.0,
        )
        assert solid_ops.volume(tool.shape) == pytest.approx(PROFILE_AREA * 0.25, rel=1e-3)

    def test_anisotropic_scale(self, logo_profile, top_plane, bracket_step):
        tool = build_tool_solid(
            logo_profile, Placement(scale=(2.0, 0.5), uniform_scale=False), add_op(1.0),
            top_plane, part_diagonal=bracket_step.diagonal, contact_overlap=0.0,
        )
        assert solid_ops.volume(tool.shape) == pytest.approx(PROFILE_AREA, rel=1e-3)

    def test_rotation_moves_the_bounding_box(self, logo_profile, top_plane, bracket_step):
        from stamp.io.part_import import bounding_box

        upright = build_tool_solid(
            logo_profile, Placement(), add_op(), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        turned = build_tool_solid(
            logo_profile, Placement(rotation=90.0), add_op(), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        a = bounding_box(upright.shape)
        b = bounding_box(turned.shape)
        assert (a[3] - a[0]) == pytest.approx(b[4] - b[1], abs=1e-3)
        assert (a[4] - a[1]) == pytest.approx(b[3] - b[0], abs=1e-3)

    def test_offset_moves_the_tool(self, logo_profile, top_plane, bracket_step):
        from stamp.io.part_import import bounding_box

        moved = build_tool_solid(
            logo_profile, Placement(offset_2d=(5.0, -3.0)), add_op(), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        base = build_tool_solid(
            logo_profile, Placement(), add_op(), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        assert bounding_box(moved.shape)[0] == pytest.approx(bounding_box(base.shape)[0] + 5.0, abs=1e-6)
        assert bounding_box(moved.shape)[1] == pytest.approx(bounding_box(base.shape)[1] - 3.0, abs=1e-6)

    def test_through_all_spans_the_part(self, logo_profile, top_plane, bracket_step):
        from stamp.io.part_import import bounding_box

        tool = build_tool_solid(
            logo_profile, Placement(), cut_op(mode=DepthMode.THROUGH_ALL), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        box = bounding_box(tool.shape)
        assert box[2] < 0.0 and box[5] > 14.0

    def test_symmetric_straddles_the_plane(self, logo_profile, top_plane, bracket_step):
        from stamp.io.part_import import bounding_box

        tool = build_tool_solid(
            logo_profile, Placement(), cut_op(2.0, DepthMode.SYMMETRIC), top_plane,
            part_diagonal=bracket_step.diagonal, contact_overlap=0.0,
        )
        box = bounding_box(tool.shape)
        assert box[2] == pytest.approx(7.0, abs=1e-6)
        assert box[5] == pytest.approx(9.0, abs=1e-6)

    def test_draft_narrows_a_cut(self, logo_profile, top_plane, bracket_step):
        straight = build_tool_solid(
            logo_profile, Placement(), cut_op(2.0), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        drafted = build_tool_solid(
            logo_profile, Placement(),
            Operation(kind=OperationKind.CUT, depth_mode=DepthMode.BLIND, depth=2.0,
                      direction=Direction.INTO, draft_angle=5.0),
            top_plane, part_diagonal=bracket_step.diagonal,
        )
        assert solid_ops.volume(drafted.shape) < solid_ops.volume(straight.shape)

    def test_zero_depth_is_refused(self, logo_profile, top_plane, bracket_step):
        with pytest.raises(ToolSolidError):
            build_tool_solid(
                logo_profile, Placement(), cut_op(0.0), top_plane,
                part_diagonal=bracket_step.diagonal,
            )

    def test_zero_scale_is_refused(self, logo_profile, top_plane, bracket_step):
        with pytest.raises(ToolSolidError):
            build_tool_solid(
                logo_profile, Placement(scale=(0.0, 1.0)), add_op(), top_plane,
                part_diagonal=bracket_step.diagonal,
            )


class TestContactOverlap:
    def test_applied_when_the_profile_sits_on_the_face(self):
        assert contact_overlap_for(Placement(), add_op(), 100.0) > 0

    def test_not_applied_when_lifted(self):
        assert contact_overlap_for(Placement(lift=0.5), add_op(), 100.0) == 0.0

    def test_not_applied_for_a_contrary_direction(self):
        op = Operation(kind=OperationKind.ADD, direction=Direction.INTO)
        assert contact_overlap_for(Placement(), op, 100.0) == 0.0


class TestSolidBooleans:
    def test_add_increases_volume(self, logo_profile, top_plane, bracket_step):
        tool = build_tool_solid(
            logo_profile, Placement(), add_op(0.8), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        result = solid_ops.boolean(bracket_step.runtime, tool.shape, "add",
                                   bbox_diagonal=bracket_step.diagonal)
        gained = solid_ops.volume(result.shape) - bracket_step.volume
        assert gained == pytest.approx(PROFILE_AREA * 0.8, rel=0.05)

    def test_every_solid_of_a_compound_takes_part(self, fixtures, top_plane, bracket_step):
        """A compound handed to SetTools as one entry only cuts with its first solid."""
        from stamp.io.profile_import import import_profile

        profile = import_profile(fixtures / "serial.dxf").profile
        assert len(profile.faces) == 5
        tool = build_tool_solid(
            profile, Placement(scale=(0.5, 0.5)), cut_op(0.5), top_plane,
            part_diagonal=bracket_step.diagonal, contact_overlap=0.0,
        )
        result = solid_ops.boolean(bracket_step.runtime, tool.shape, "cut",
                                   bbox_diagonal=bracket_step.diagonal)
        removed = bracket_step.volume - solid_ops.volume(result.shape)
        assert removed == pytest.approx(solid_ops.volume(tool.shape), rel=0.01)

    def test_cut_that_misses_is_reported(self, logo_profile, bracket_step):
        from stamp.core.document import Plane

        far = Plane(origin=(300.0, 300.0, 300.0), normal=(0.0, 0.0, 1.0), u_axis=(1.0, 0.0, 0.0))
        tool = build_tool_solid(
            logo_profile, Placement(), cut_op(0.5), far,
            part_diagonal=bracket_step.diagonal,
        )
        result = solid_ops.boolean(bracket_step.runtime, tool.shape, "cut",
                                   bbox_diagonal=bracket_step.diagonal)
        assert any("does not touch" in w for w in result.warnings)

    def test_disconnected_add_is_reported(self, logo_profile, bracket_step, top_plane):
        tool = build_tool_solid(
            logo_profile, Placement(lift=5.0), add_op(0.8), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        result = solid_ops.boolean(bracket_step.runtime, tool.shape, "add",
                                   bbox_diagonal=bracket_step.diagonal)
        assert any("not connected" in w for w in result.warnings)

    def test_boolean_history_gives_the_blend_edges(self, logo_profile, bracket_step, top_plane):
        tool = build_tool_solid(
            logo_profile, Placement(), add_op(0.8), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        result = solid_ops.boolean(bracket_step.runtime, tool.shape, "add",
                                   bbox_diagonal=bracket_step.diagonal)
        blends = solid_ops.find_blend_edges(
            result.shape, result.section_edges, tool.shape, tool.direction
        )
        assert len(blends) > 0


class TestEdgeSelection:
    def test_top_bottom_and_side_are_separated(self, logo_profile, top_plane, bracket_step):
        tool = build_tool_solid(
            logo_profile, Placement(), add_op(0.8), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        groups = solid_ops.classify_feature_edges(tool.shape, tool.direction)
        # The silhouette is one rectangle with one rectangular hole: four edges each,
        # top and bottom, and eight vertical seams.
        assert len(groups[EdgeRole.TOP]) == 8
        assert len(groups[EdgeRole.BOTTOM]) == 8
        assert len(groups[EdgeRole.SIDE]) == 8

    def test_classification_is_stable_across_rebuilds(self, logo_profile, top_plane, bracket_step):
        counts = []
        for _ in range(3):
            tool = build_tool_solid(
                logo_profile, Placement(), add_op(0.8), top_plane,
                part_diagonal=bracket_step.diagonal,
            )
            groups = solid_ops.classify_feature_edges(tool.shape, tool.direction)
            counts.append(tuple(len(groups[r]) for r in (EdgeRole.TOP, EdgeRole.BOTTOM, EdgeRole.SIDE)))
        assert len(set(counts)) == 1


class TestModifiers:
    def _tool(self, logo_profile, top_plane, bracket_step):
        return build_tool_solid(
            logo_profile, Placement(), add_op(0.8), top_plane,
            part_diagonal=bracket_step.diagonal,
        )

    def test_small_fillet_applies(self, logo_profile, top_plane, bracket_step):
        tool = self._tool(logo_profile, top_plane, bracket_step)
        groups = solid_ops.classify_feature_edges(tool.shape, tool.direction)
        mod = Modifier(kind=ModifierKind.FILLET, value=0.3,
                       target=EdgeSelector(role=EdgeRole.TOP))
        result = solid_ops.apply_modifier(tool.shape, mod, groups[EdgeRole.TOP])
        assert result.applied
        assert solid_ops.volume(result.shape) < solid_ops.volume(tool.shape)

    def test_oversize_fillet_keeps_the_shape_and_suggests_a_radius(
        self, logo_profile, top_plane, bracket_step
    ):
        tool = self._tool(logo_profile, top_plane, bracket_step)
        groups = solid_ops.classify_feature_edges(tool.shape, tool.direction)
        mod = Modifier(kind=ModifierKind.FILLET, value=6.0,
                       target=EdgeSelector(role=EdgeRole.TOP))
        result = solid_ops.apply_modifier(tool.shape, mod, groups[EdgeRole.TOP])
        assert not result.applied
        assert result.shape.IsSame(tool.shape)
        assert 0 < result.suggested_value < 6.0
        assert result.warnings and "largest that works" in result.warnings[0]

    def test_chamfer_applies(self, logo_profile, top_plane, bracket_step):
        tool = self._tool(logo_profile, top_plane, bracket_step)
        groups = solid_ops.classify_feature_edges(tool.shape, tool.direction)
        mod = Modifier(kind=ModifierKind.CHAMFER, value=0.2,
                       target=EdgeSelector(role=EdgeRole.TOP))
        result = solid_ops.apply_modifier(tool.shape, mod, groups[EdgeRole.TOP])
        assert result.applied

    def test_no_matching_edges_is_reported_not_silent(self, logo_profile, top_plane, bracket_step):
        tool = self._tool(logo_profile, top_plane, bracket_step)
        mod = Modifier(kind=ModifierKind.FILLET, value=0.3,
                       target=EdgeSelector(role=EdgeRole.MANUAL))
        result = solid_ops.apply_modifier(tool.shape, mod, [])
        assert not result.applied
        assert result.warnings


class TestMeshMode:
    def test_tessellated_tool_matches_the_brep_volume(
        self, logo_profile, top_plane, bracket_step
    ):
        tool = build_tool_solid(
            logo_profile, Placement(), add_op(0.8), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        manifold = mesh_ops.shape_to_manifold(tool.shape, 0.02)
        assert manifold.volume() == pytest.approx(solid_ops.volume(tool.shape), rel=0.01)

    def test_add_stays_one_body(self, logo_profile, top_plane, bracket_stl, bracket_step):
        tool = build_tool_solid(
            logo_profile, Placement(), add_op(0.8), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        manifold = mesh_ops.shape_to_manifold(tool.shape, 0.02)
        result = mesh_ops.boolean(bracket_stl.runtime, manifold, "add")
        assert not any("not connected" in w for w in result.warnings)
        assert len(result.manifold.decompose()) == 1

    def test_mesh_and_solid_agree(self, logo_profile, top_plane, bracket_stl, bracket_step):
        tool = build_tool_solid(
            logo_profile, Placement(), add_op(0.8), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        solid = solid_ops.boolean(bracket_step.runtime, tool.shape, "add",
                                  bbox_diagonal=bracket_step.diagonal)
        mesh = mesh_ops.boolean(
            bracket_stl.runtime, mesh_ops.shape_to_manifold(tool.shape, 0.02), "add"
        )
        assert mesh.manifold.volume() == pytest.approx(
            solid_ops.volume(solid.shape), rel=0.01
        )

    def test_filleted_tool_survives_into_mesh_mode(
        self, logo_profile, top_plane, bracket_stl, bracket_step
    ):
        """The whole point of §6.5: rounding the top of a logo works on an STL."""
        tool = build_tool_solid(
            logo_profile, Placement(), add_op(0.8), top_plane,
            part_diagonal=bracket_step.diagonal,
        )
        groups = solid_ops.classify_feature_edges(tool.shape, tool.direction)
        mod = Modifier(kind=ModifierKind.FILLET, value=0.3,
                       target=EdgeSelector(role=EdgeRole.TOP))
        filleted = solid_ops.apply_modifier(tool.shape, mod, groups[EdgeRole.TOP])
        assert filleted.applied

        plain = mesh_ops.boolean(
            bracket_stl.runtime, mesh_ops.shape_to_manifold(tool.shape, 0.02), "add"
        )
        rounded = mesh_ops.boolean(
            bracket_stl.runtime, mesh_ops.shape_to_manifold(filleted.shape, 0.02), "add"
        )
        assert rounded.manifold.volume() < plain.manifold.volume()

    def test_weld_closes_a_tessellated_box(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

        box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
        verts, tris = mesh_ops.triangulate(box, 0.01)
        assert len(verts) == 8
        assert len(tris) == 12
