"""Tool solids, booleans, fillets - spec §6.3, §6.4, §2."""

from __future__ import annotations

import math

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

    def test_the_footprint_stays_on_the_sketch_plane(
        self, logo_profile, top_plane, bracket_step
    ):
        """The flat decal says where the artwork lands, not where the sweep began.

        Taken after the start shift it floats most of a part diagonal above the face
        for a through cut, sits two millimetres up for a symmetric one, and lands a
        micron inside the part for a blind add, where it z-fights with the face.
        """
        from stamp.io.part_import import bounding_box

        for operation in (
            cut_op(mode=DepthMode.THROUGH_ALL),
            cut_op(4.0, DepthMode.SYMMETRIC),
            add_op(1.0),
        ):
            tool = build_tool_solid(
                logo_profile, Placement(), operation, top_plane,
                part_diagonal=bracket_step.diagonal,
            )
            box = bounding_box(tool.footprint)
            assert box[2] == pytest.approx(box[5], abs=1e-6), "the decal stays flat"
            # Just clear of the face at z = 8, on the outside of it.
            assert 8.0 < box[2] < 8.05, operation.depth_mode

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


class TestDraft:
    """A draft pivots on the sketch plane, so the mark keeps its drawn size (§6.3).

    Placed at the start of the sweep instead, the wall arrives at the face already
    tapered by the start offset times the tangent of the angle - which is half a part
    diagonal for a through cut.  A 512 mm2 logo came out 431 mm2 at half a degree and
    351 mm2 at one, and by two degrees the tool had pinched out entirely and the only
    complaint was that the cut did not touch the part.
    """

    def _area_at_the_face(self, shape, z: float = 8.0, slice_mm: float = 0.01) -> float:
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.gp import gp_Pnt

        box = BRepPrimAPI_MakeBox(
            gp_Pnt(-500, -500, z - slice_mm), gp_Pnt(500, 500, z + slice_mm)
        ).Shape()
        common = BRepAlgoAPI_Common(shape, box)
        common.Build()
        assert common.IsDone()
        return solid_ops.volume(common.Shape()) / (2 * slice_mm)

    @pytest.mark.parametrize("angle", [0.5, 1.0, 2.0])
    def test_a_through_cut_is_full_size_at_the_face(
        self, logo_profile, top_plane, bracket_step, angle
    ):
        tool = build_tool_solid(
            logo_profile, Placement(),
            Operation(kind=OperationKind.CUT, depth_mode=DepthMode.THROUGH_ALL,
                      depth=1.0, direction=Direction.INTO, draft_angle=angle),
            top_plane, part_diagonal=bracket_step.diagonal, contact_overlap=0.0,
        )
        assert self._area_at_the_face(tool.shape) == pytest.approx(PROFILE_AREA, rel=1e-3)

    @pytest.mark.parametrize("angle", [1.0, 5.0])
    def test_a_symmetric_cut_is_full_size_at_the_face(
        self, logo_profile, top_plane, bracket_step, angle
    ):
        tool = build_tool_solid(
            logo_profile, Placement(),
            Operation(kind=OperationKind.CUT, depth_mode=DepthMode.SYMMETRIC,
                      depth=4.0, direction=Direction.INTO, draft_angle=angle),
            top_plane, part_diagonal=bracket_step.diagonal, contact_overlap=0.0,
        )
        assert self._area_at_the_face(tool.shape) == pytest.approx(PROFILE_AREA, rel=1e-3)

    def test_the_taper_still_happens_below_the_face(
        self, logo_profile, top_plane, bracket_step
    ):
        """Full size just under the face, and clearly narrower two millimetres in."""
        tool = build_tool_solid(
            logo_profile, Placement(),
            Operation(kind=OperationKind.CUT, depth_mode=DepthMode.BLIND, depth=4.0,
                      direction=Direction.INTO, draft_angle=5.0),
            top_plane, part_diagonal=bracket_step.diagonal, contact_overlap=0.0,
        )
        # A blind cut stops at the face, so the slice is taken just inside it.
        assert self._area_at_the_face(tool.shape, z=7.99) == pytest.approx(
            PROFILE_AREA, rel=1e-3
        )
        # Two millimetres down, every wall has moved in by 2 mm x tan 5 degrees: the
        # rectangle shrinks by that on each side and the hole grows by it.
        inset = 2.0 * math.tan(math.radians(5.0))
        expected = (36 - 2 * inset) * (16 - 2 * inset) - (8 + 2 * inset) ** 2
        assert expected < PROFILE_AREA * 0.96, "the check has to be able to fail"
        assert self._area_at_the_face(tool.shape, z=6.0) == pytest.approx(expected, rel=1e-3)


class TestToFaceDepth:
    def test_a_target_behind_the_tool_is_refused(self, logo_profile, top_plane, bracket_step):
        """A cut set Into, with the target above the sketch plane, is a mistake.

        The distance is signed and the direction is not, so taking the absolute value
        drives the cut into the part to a depth nobody asked for and says nothing.
        """
        with pytest.raises(ToolSolidError, match="other side of the sketch plane"):
            build_tool_solid(
                logo_profile, Placement(), cut_op(1.0, DepthMode.TO_FACE), top_plane,
                part_diagonal=bracket_step.diagonal, to_face_distance=5.0,
            )

    def test_a_target_ahead_of_the_tool_is_the_depth(
        self, logo_profile, top_plane, bracket_step
    ):
        from stamp.io.part_import import bounding_box

        tool = build_tool_solid(
            logo_profile, Placement(), cut_op(1.0, DepthMode.TO_FACE), top_plane,
            part_diagonal=bracket_step.diagonal, to_face_distance=-5.0,
            contact_overlap=0.0,
        )
        box = bounding_box(tool.shape)
        assert box[2] == pytest.approx(3.0, abs=1e-6)
        assert box[5] == pytest.approx(8.0, abs=1e-6)


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

    def test_a_tool_that_will_not_tessellate_says_so(self):
        """An open shape makes an empty manifold, not an exception, unless asked.

        Left unchecked it reaches the boolean as an empty solid, and every cut then
        reports that the feature removed the whole part - which sends the user off
        checking a depth and a direction that were never the problem.
        """
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
        from OCP.gp import gp_Pln

        open_shape = BRepBuilderAPI_MakeFace(gp_Pln(), -5.0, 5.0, -5.0, 5.0).Face()
        with pytest.raises(ValueError, match="closed shape"):
            mesh_ops.shape_to_manifold(open_shape)

    def test_display_decimation_actually_reduces_the_mesh(self):
        """trimesh 5 takes a fraction first, so the count went in as a multiplier."""
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeSphere

        sphere = mesh_ops.shape_to_manifold(BRepPrimAPI_MakeSphere(10.0).Shape(), 0.02)
        before = len(mesh_ops.to_trimesh(sphere).faces)
        assert before > 2000, "the fixture has to be big enough to be worth reducing"
        reduced = mesh_ops.decimate_for_display(sphere, 1000)
        assert len(reduced.faces) <= 1000
        assert len(reduced.faces) > 0

    def test_weld_closes_a_tessellated_box(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

        box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
        verts, tris = mesh_ops.triangulate(box, 0.01)
        assert len(verts) == 8
        assert len(tris) == 12


class TestWorkingValueSearch:
    """A value too large must always name one that works (§6.4).

    The first version bisected between zero and the value asked for, in six
    steps.  That never probes below a sixty-fourth of the request, so a request
    a hundred times too large - 2 mm of fillet on 2.5 mm text - reported that
    nothing at all would work, and the automatic correction had nothing to
    apply.  The search descends first, then closes in.
    """

    def _tool(self, logo_profile, top_plane, bracket_step):
        return build_tool_solid(
            logo_profile, Placement(), add_op(0.8), top_plane,
            part_diagonal=bracket_step.diagonal,
        )

    def _suggestion(self, tool, kind, requested):
        modifier = Modifier(kind=kind, value=requested,
                            target=EdgeSelector(role=EdgeRole.TOP))
        edges = solid_ops.select_edges(tool.shape, modifier, tool.direction)
        assert edges
        return solid_ops.apply_modifier(tool.shape, modifier, edges, label="m"), edges

    @pytest.mark.parametrize("requested", [2.0, 5.0, 20.0, 200.0])
    def test_a_wildly_large_value_still_names_one_that_works(
        self, logo_profile, top_plane, bracket_step, requested
    ):
        tool = self._tool(logo_profile, top_plane, bracket_step)
        result, edges = self._suggestion(tool, ModifierKind.FILLET, requested)

        assert not result.applied, "this value cannot work, so it must be refused"
        assert result.suggested_value is not None, (
            f"a request of {requested} mm produced no usable value"
        )
        assert result.suggested_value > 0

        # The value offered has to survive a real build, not just look plausible.
        ok, _ = solid_ops._try_modifier(
            tool.shape,
            Modifier(kind=ModifierKind.FILLET, value=result.suggested_value,
                     target=EdgeSelector(role=EdgeRole.TOP)),
            edges,
            result.suggested_value,
        )
        assert ok, "the value offered must actually build"

    def test_the_answer_does_not_depend_on_how_wrong_the_request_was(
        self, logo_profile, top_plane, bracket_step
    ):
        tool = self._tool(logo_profile, top_plane, bracket_step)
        answers = [
            self._suggestion(tool, ModifierKind.FILLET, requested)[0].suggested_value
            for requested in (2.0, 5.0, 20.0)
        ]
        assert all(a is not None for a in answers)
        assert max(answers) - min(answers) < 0.1, answers

    def test_a_chamfer_is_offered_a_value_too(self, logo_profile, top_plane, bracket_step):
        tool = self._tool(logo_profile, top_plane, bracket_step)
        result, _ = self._suggestion(tool, ModifierKind.CHAMFER, 20.0)
        assert result.suggested_value is not None

    def test_a_value_that_works_is_left_alone(self, logo_profile, top_plane, bracket_step):
        tool = self._tool(logo_profile, top_plane, bracket_step)
        result, _ = self._suggestion(tool, ModifierKind.FILLET, 0.2)
        assert result.applied
        assert result.suggested_value is None


class TestMeasuringAFaceInItsSketchPlane:
    """A face's size decides what "Fit to face" scales the artwork to.

    Measuring the vertices alone got this wrong on anything bounded by arcs: a
    face bounded by a full circle has one seam vertex, so it measured nothing
    at all and the fit collapsed the profile to zero.
    """

    @staticmethod
    def _extent(face):
        from stamp.core.refs import face_center, face_extent_in_plane, plane_from_face

        plane, _ = plane_from_face(face, face_center(face))
        return face_extent_in_plane(face, plane)

    def test_a_full_circle_measures_its_diameter(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

        from stamp.core.refs import faces_of, surface_kind

        shape = BRepPrimAPI_MakeCylinder(10.0, 30.0).Shape()
        flat = [f for f in faces_of(shape) if surface_kind(f) == "plane"]
        assert flat, "a cylinder has two flat ends"
        assert self._extent(flat[0]) == pytest.approx((20.0, 20.0), abs=1e-6)

    def test_a_tilted_slab_measures_its_own_sides_not_the_world_box(self):
        """A 60 x 20 face turned 45 degrees sits in a world box of about
        56.6 x 56.6, which is the wrong number in both directions."""
        import math

        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf

        from stamp.core.refs import face_center, face_normal_at, faces_of

        box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(60, 20, 10)).Shape()
        turn = gp_Trsf()
        turn.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.radians(45))
        shape = BRepBuilderAPI_Transform(box, turn, True).Shape()

        top = next(
            f for f in faces_of(shape)
            if face_normal_at(f, face_center(f))[2] > 0.99
        )
        assert sorted(self._extent(top)) == pytest.approx([20.0, 60.0], abs=1e-6)

    def test_the_bracket_top_face_measures_the_part(self, bracket_step):
        from stamp.core.refs import face_area, face_center, face_normal_at, faces_of

        flat = [
            f for f in faces_of(bracket_step.runtime)
            if face_normal_at(f, face_center(f))[2] > 0.99
        ]
        top = max(flat, key=face_area)
        assert sorted(self._extent(top)) == pytest.approx([40.0, 80.0], abs=1e-6)
