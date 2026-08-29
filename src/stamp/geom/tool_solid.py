"""Profile + placement + operation -> the tool solid (spec §6.3).

The tool solid is **always B-rep**, in both solid and mesh mode.  That is what makes
rounding the top edge of a raised logo work on an STL part: the feature is built and
filleted in OpenCascade, and only tessellated at the last moment for the mesh
boolean (§2, §6.5).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeCone,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakePrism,
)
from OCP.gp import gp_Ax1, gp_Ax2, gp_Ax3, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.TopoDS import TopoDS_Face, TopoDS_Shape

from stamp.core.document import (
    DepthMode,
    Direction,
    Operation,
    OperationKind,
    Placement,
    PlacementMode,
    Plane,
)
from stamp.io.normalize import Profile


class ToolSolidError(RuntimeError):
    """The tool solid could not be built.  The message is shown to the user."""


@dataclass
class ToolSolid:
    shape: TopoDS_Shape
    #: The transform that took the profile from the XY plane to its final position.
    transform: gp_Trsf
    #: Unit vector the profile was swept along.
    direction: tuple[float, float, float]
    length: float
    #: The placed but un-extruded profile, for the flat decal preview (§6.2).
    footprint: TopoDS_Shape
    #: How far behind the sketch plane the sweep actually started, in mm.
    contact_overlap: float = 0.0


def placement_transform(placement: Placement, plane: Plane) -> gp_Trsf:
    """Build the full profile-to-world transform.

    Order matters and is fixed: mirror, then scale, then rotate about the plane
    normal, then translate in-plane by (u, v), then lift along the normal, and
    finally map the XY plane onto the sketch plane.
    """
    sx, sy = placement.scale
    if placement.mirror_u:
        sx = -sx
    if placement.mirror_v:
        sy = -sy
    if abs(sx) < 1e-9 or abs(sy) < 1e-9:
        raise ToolSolidError("A scale of zero leaves nothing to extrude.")

    # gp_Trsf cannot hold a non-uniform scale, so anisotropic scaling is applied
    # separately by the caller through _scale_shape.  Everything else composes here.
    rotate = gp_Trsf()
    rotate.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.radians(placement.rotation))

    translate = gp_Trsf()
    translate.SetTranslation(gp_Vec(placement.offset_2d[0], placement.offset_2d[1], placement.lift))

    to_plane = gp_Trsf()
    to_plane.SetTransformation(
        gp_Ax3(
            gp_Pnt(*plane.origin),
            gp_Dir(*plane.normal),
            gp_Dir(*plane.u_axis),
        ),
        gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), gp_Dir(1, 0, 0)),
    )
    return to_plane * translate * rotate


def _scale_shape(shape: TopoDS_Shape, sx: float, sy: float) -> TopoDS_Shape:
    """Apply an in-plane scale.  Uniform goes through gp_Trsf, anisotropic through
    a general transform (gp_GTrsf), which gp_Trsf itself cannot represent."""
    if abs(sx - 1.0) < 1e-12 and abs(sy - 1.0) < 1e-12:
        return shape
    if abs(sx - sy) < 1e-12:
        trsf = gp_Trsf()
        trsf.SetScale(gp_Pnt(0, 0, 0), sx)
        return BRepBuilderAPI_Transform(shape, trsf, True).Shape()

    from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform
    from OCP.gp import gp_GTrsf, gp_Mat

    gtrsf = gp_GTrsf()
    gtrsf.SetVectorialPart(gp_Mat(sx, 0, 0, 0, sy, 0, 0, 0, 1.0))
    return BRepBuilderAPI_GTransform(shape, gtrsf, True).Shape()


def extrusion_length(
    operation: Operation,
    plane: Plane,
    part_diagonal: float,
    to_face_distance: float | None = None,
) -> tuple[float, float]:
    """Return ``(start_offset, length)`` along the sweep direction, both positive.

    ``start_offset`` is how far from the sketch plane the extrusion begins, which is
    non-zero only for the symmetric mode.
    """
    mode = operation.depth_mode
    if operation.kind is OperationKind.COLOR and mode is not DepthMode.BLIND:
        # A colour stamp is a thin layer sitting at the face, and the 3MF export
        # fills it back in from the face down.  Every other depth mode either has
        # no floor to fill to or puts the mark somewhere the face is not.
        raise ToolSolidError(
            "A color stamp is a thin layer at the face, so it takes a blind depth."
        )
    if mode is DepthMode.BLIND:
        if operation.depth <= 0:
            raise ToolSolidError("The depth must be greater than zero.")
        return 0.0, operation.depth
    if mode is DepthMode.SYMMETRIC:
        if operation.depth <= 0:
            raise ToolSolidError("The depth must be greater than zero.")
        return -operation.depth / 2.0, operation.depth
    if mode is DepthMode.THROUGH_ALL:
        # 1.5x the bounding-box diagonal is cheap and always enough (§6.3).  Start
        # behind the plane too, so a cut that begins outside the part still reaches it.
        reach = max(part_diagonal * 1.5, 1.0)
        return -reach / 2.0, reach * 1.5
    if mode is DepthMode.TO_FACE:
        if to_face_distance is None:
            raise ToolSolidError(
                "The target face for this feature is missing. Pick the face again."
            )
        if abs(to_face_distance) < 1e-9:
            raise ToolSolidError("The target face is in the sketch plane, so the depth is zero.")
        return 0.0, abs(to_face_distance)
    raise ToolSolidError(f"Unknown depth mode {mode!r}.")


def contact_overlap_for(
    placement: Placement, operation: Operation, part_diagonal: float
) -> float:
    """How far to start the sweep *behind* the sketch plane, in mm.

    A tool whose end cap is exactly coplanar with the face it sits on is the worst
    case for both boolean engines: OpenCascade has to intersect two coincident
    planes, and manifold3d ends up with two components that touch over a
    measure-zero patch, which then reads as a disconnected body.

    Starting a hair behind the plane removes that case.  The extension is always
    into material that the operation discards - inside the part for an *add*, above
    the surface for a *cut* - so the result is identical to the exact version.  When
    the profile is lifted off the face, or the direction is the contrary one, the
    extension would be visible, so there is none.
    """
    if abs(placement.lift) > 1e-9:
        return 0.0
    growing_outward = operation.direction is Direction.OUT_OF
    if not operation.removes_material and not growing_outward:
        return 0.0
    if operation.removes_material and growing_outward:
        return 0.0
    return max(1e-3, part_diagonal * 1e-5)


def build_tool_solid(
    profile: Profile,
    placement: Placement,
    operation: Operation,
    plane: Plane,
    *,
    part_diagonal: float,
    to_face_distance: float | None = None,
    contact_overlap: float | None = None,
    target_face: TopoDS_Face | None = None,
) -> ToolSolid:
    """Place the profile on the sketch plane and sweep it into a solid."""
    if not profile.faces:
        raise ToolSolidError("This profile has no closed area to extrude.")

    if placement.mode is PlacementMode.WRAP:
        if target_face is None:
            raise ToolSolidError("Wrapped artwork needs a cylindrical or conical face. Pick the face again.")
        return _build_wrapped_tool(
            profile, placement, operation, plane, target_face, part_diagonal=part_diagonal
        )

    sx, sy = placement.scale
    if placement.mirror_u:
        sx = -sx
    if placement.mirror_v:
        sy = -sy

    footprint = _scale_shape(profile.compound(), sx, sy)
    trsf = placement_transform(placement, plane)
    footprint = BRepBuilderAPI_Transform(footprint, trsf, True).Shape()

    normal = gp_Dir(*plane.normal)
    sign = -1.0 if operation.direction is Direction.INTO else 1.0
    sweep = gp_Vec(normal).Multiplied(sign)

    start, length = extrusion_length(operation, plane, part_diagonal, to_face_distance)
    overlap = (
        contact_overlap_for(placement, operation, part_diagonal)
        if contact_overlap is None
        else contact_overlap
    )
    if overlap and operation.depth_mode is not DepthMode.THROUGH_ALL:
        start -= overlap
        length += overlap
    if start:
        shift = gp_Trsf()
        shift.SetTranslation(sweep.Multiplied(start))
        footprint = BRepBuilderAPI_Transform(footprint, shift, True).Shape()

    prism = BRepPrimAPI_MakePrism(footprint, sweep.Multiplied(length), False, True)
    if not prism.IsDone():
        raise ToolSolidError("The extrude failed. Check the profile and the depth.")
    shape = prism.Shape()

    if abs(operation.draft_angle) > 1e-9:
        shape = apply_draft(shape, plane, sweep, operation.draft_angle, start)

    shape = fuse_overlapping(shape)

    return ToolSolid(
        shape=shape,
        transform=trsf,
        direction=(sweep.X(), sweep.Y(), sweep.Z()),
        length=length,
        footprint=footprint,
        contact_overlap=overlap,
    )


def _placed_footprint(profile: Profile, placement: Placement, plane: Plane) -> TopoDS_Shape:
    """Apply the shared 2D placement transform without constructing an extrusion."""
    sx, sy = placement.scale
    if placement.mirror_u:
        sx = -sx
    if placement.mirror_v:
        sy = -sy
    footprint = _scale_shape(profile.compound(), sx, sy)
    return BRepBuilderAPI_Transform(footprint, placement_transform(placement, plane), True).Shape()


def _build_wrapped_tool(
    profile: Profile,
    placement: Placement,
    operation: Operation,
    plane: Plane,
    target_face: TopoDS_Face,
    *,
    part_diagonal: float,
) -> ToolSolid:
    """Create a normal-thickness tool bounded by a cylinder or cone."""
    from stamp.core.refs import surface_kind

    kind = surface_kind(target_face)
    if kind not in {"cylinder", "cone"}:
        raise ToolSolidError("Wrap is available only on cylindrical and conical faces.")
    if placement.lift:
        raise ToolSolidError("Wrapped artwork cannot be lifted off its face.")
    if operation.depth_mode is DepthMode.TO_FACE:
        raise ToolSolidError("To-face depth is not available for wrapped artwork.")
    if operation.depth_mode is DepthMode.SYMMETRIC:
        raise ToolSolidError("Symmetric depth is not available for wrapped artwork.")
    depth = operation.depth if operation.depth_mode is DepthMode.BLIND else max(part_diagonal * 1.5, 1.0)
    if depth <= 0:
        raise ToolSolidError("The depth must be greater than zero.")
    sign = -1.0 if operation.direction is Direction.INTO else 1.0
    if kind == "cylinder":
        shape, footprint = _cylindrical_wrap_shape(
            profile, placement, operation, plane, target_face, depth, part_diagonal
        )
        return ToolSolid(
            shape=shape, transform=placement_transform(placement, plane),
            direction=(plane.normal[0] * sign, plane.normal[1] * sign, plane.normal[2] * sign),
            length=depth, footprint=footprint,
        )

    shape, footprint = _conical_wrap_shape(
        profile, placement, operation, plane, target_face, depth, part_diagonal
    )
    return ToolSolid(
        shape=shape, transform=placement_transform(placement, plane),
        direction=(plane.normal[0] * sign, plane.normal[1] * sign, plane.normal[2] * sign),
        length=depth, footprint=footprint,
    )


def _cylindrical_wrap_shape(
    profile: Profile,
    placement: Placement,
    operation: Operation,
    plane: Plane,
    target_face: TopoDS_Face,
    depth: float,
    part_diagonal: float,
) -> tuple[TopoDS_Shape, TopoDS_Shape]:
    """Intersect a profile selector with an exact cylindrical annular band."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
    from OCP.BRepTools import BRepTools

    cylinder = BRepAdaptor_Surface(target_face).Cylinder()
    axis = cylinder.Axis()
    direction = axis.Direction()
    _u0, _u1, v0, v1 = BRepTools.UVBounds_s(target_face)
    margin = max(1e-3, part_diagonal * 1e-5)
    start = min(v0, v1) - margin
    height = abs(v1 - v0) + 2 * margin
    location = axis.Location()
    origin = gp_Pnt(
        location.X() + direction.X() * start,
        location.Y() + direction.Y() * start,
        location.Z() + direction.Z() * start,
    )
    radius = cylinder.Radius()
    inward = operation.direction is Direction.INTO
    inner = max(1e-6, radius - (depth + margin if inward else margin))
    outer = radius + (margin if inward else depth + margin)
    axes = gp_Ax2(origin, direction)
    outer_solid = BRepPrimAPI_MakeCylinder(axes, outer, height).Shape()
    inner_solid = BRepPrimAPI_MakeCylinder(axes, inner, height).Shape()
    ring_op = BRepAlgoAPI_Cut(outer_solid, inner_solid)
    ring_op.Build()
    if not ring_op.IsDone() or ring_op.Shape().IsNull():
        raise ToolSolidError("Stamp could not form the cylindrical wrap band.")

    selector_placement = replace(placement, mode=PlacementMode.PLANAR)
    selector_operation = replace(operation, depth_mode=DepthMode.THROUGH_ALL)
    selector = build_tool_solid(
        profile, selector_placement, selector_operation, plane,
        part_diagonal=part_diagonal, contact_overlap=0.0,
    )
    _validate_wrap_seam(selector.footprint, target_face, cylinder.Position())
    common = BRepAlgoAPI_Common(ring_op.Shape(), selector.shape)
    common.Build()
    if not common.IsDone() or common.Shape().IsNull():
        raise ToolSolidError("The artwork does not intersect this cylindrical face. Move it onto the face.")
    return common.Shape(), selector.footprint


def _conical_wrap_shape(
    profile: Profile,
    placement: Placement,
    operation: Operation,
    plane: Plane,
    target_face: TopoDS_Face,
    depth: float,
    part_diagonal: float,
) -> tuple[TopoDS_Shape, TopoDS_Shape]:
    """Intersect a profile selector with a conical annular band."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
    from OCP.BRepTools import BRepTools

    cone = BRepAdaptor_Surface(target_face).Cone()
    axis = cone.Axis()
    direction = axis.Direction()
    _u0, _u1, v0, v1 = BRepTools.UVBounds_s(target_face)
    start, end = min(v0, v1), max(v0, v1)
    angle = cone.SemiAngle()
    axial_scale = abs(math.cos(angle))
    radial_scale = math.sin(angle)
    margin = max(1e-3, part_diagonal * 1e-5)
    start -= margin
    end += margin
    radius_start = cone.RefRadius() + start * radial_scale
    radius_end = cone.RefRadius() + end * radial_scale
    if min(radius_start, radius_end) <= margin:
        raise ToolSolidError("This cone is too close to its tip for wrapped artwork.")
    location = axis.Location()
    origin = gp_Pnt(
        location.X() + direction.X() * start * axial_scale,
        location.Y() + direction.Y() * start * axial_scale,
        location.Z() + direction.Z() * start * axial_scale,
    )
    inward = operation.direction is Direction.INTO
    inner_start = max(1e-6, radius_start - (depth + margin if inward else margin))
    inner_end = max(1e-6, radius_end - (depth + margin if inward else margin))
    outer_start = radius_start + (margin if inward else depth + margin)
    outer_end = radius_end + (margin if inward else depth + margin)
    height = (end - start) * axial_scale
    axes = gp_Ax2(origin, direction)
    outer_solid = BRepPrimAPI_MakeCone(axes, outer_start, outer_end, height).Shape()
    inner_solid = BRepPrimAPI_MakeCone(axes, inner_start, inner_end, height).Shape()
    ring_op = BRepAlgoAPI_Cut(outer_solid, inner_solid)
    ring_op.Build()
    if not ring_op.IsDone() or ring_op.Shape().IsNull():
        raise ToolSolidError("Stamp could not form the conical wrap band.")

    selector_placement = replace(placement, mode=PlacementMode.PLANAR)
    selector_operation = replace(operation, depth_mode=DepthMode.THROUGH_ALL)
    selector = build_tool_solid(
        profile, selector_placement, selector_operation, plane,
        part_diagonal=part_diagonal, contact_overlap=0.0,
    )
    _validate_wrap_seam(selector.footprint, target_face, cone.Position())
    common = BRepAlgoAPI_Common(ring_op.Shape(), selector.shape)
    common.Build()
    if not common.IsDone() or common.Shape().IsNull():
        raise ToolSolidError("The artwork does not intersect this conical face. Move it onto the face.")
    return common.Shape(), selector.footprint


def _validate_wrap_seam(footprint: TopoDS_Shape, target_face: TopoDS_Face, position) -> None:
    """Refuse artwork that crosses a trimmed cylinder/cone face's unwrap seam."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    u0, u1, _v0, _v1 = BRepTools.UVBounds_s(target_face)
    if abs(u1 - u0) >= 2 * math.pi - 1e-6:
        return  # A complete revolution has no boundary seam to cross.
    origin = position.Location()
    axis = position.Direction()
    x_axis = position.XDirection()
    y_axis = position.YDirection()
    explorer = TopExp_Explorer(footprint, TopAbs_ShapeEnum.TopAbs_VERTEX)
    while explorer.More():
        point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(explorer.Current()))
        dx, dy, dz = point.X() - origin.X(), point.Y() - origin.Y(), point.Z() - origin.Z()
        axial = dx * axis.X() + dy * axis.Y() + dz * axis.Z()
        rx, ry, rz = dx - axial * axis.X(), dy - axial * axis.Y(), dz - axial * axis.Z()
        angle = math.atan2(
            rx * y_axis.X() + ry * y_axis.Y() + rz * y_axis.Z(),
            rx * x_axis.X() + ry * x_axis.Y() + rz * x_axis.Z(),
        )
        if not any(u0 - 1e-6 <= angle + 2 * math.pi * turns <= u1 + 1e-6 for turns in (-1, 0, 1)):
            raise ToolSolidError(
                "The artwork crosses this face's unwrap seam. Move it away from the seam or split it."
            )
        explorer.Next()


def fuse_overlapping(shape: TopoDS_Shape) -> TopoDS_Shape:
    """Fuse the solids of a tool that overlap each other.

    Overlapping fills are ordinary in artwork - a filled circle drawn on top of a
    filled rectangle is two elements covering the same ground.  Extruded separately
    they become two solids sharing a volume, which double-counts in every
    measurement taken of the tool.  Boxes are tested first, so a serial number of
    five disjoint glyphs costs nothing.
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepBndLib import BRepBndLib
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_ListOfShape

    solids = []
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    while explorer.More():
        solids.append(TopoDS.Solid_s(explorer.Current()))
        explorer.Next()
    if len(solids) < 2:
        return shape

    boxes = []
    for solid in solids:
        box = Bnd_Box()
        BRepBndLib.Add_s(solid, box)
        boxes.append(box)
    overlapping = any(
        not boxes[i].IsOut(boxes[j])
        for i in range(len(boxes))
        for j in range(i + 1, len(boxes))
    )
    if not overlapping:
        return shape

    args = TopTools_ListOfShape()
    args.Append(solids[0])
    tools = TopTools_ListOfShape()
    for solid in solids[1:]:
        tools.Append(solid)
    op = BRepAlgoAPI_Fuse()
    op.SetArguments(args)
    op.SetTools(tools)
    op.SetToFillHistory(False)
    try:
        op.Build()
    except Exception:
        return shape
    if not op.IsDone() or op.Shape().IsNull():
        return shape
    return op.Shape()


def apply_draft(
    shape: TopoDS_Shape,
    plane: Plane,
    sweep: gp_Vec,
    angle_deg: float,
    start_offset: float,
) -> TopoDS_Shape:
    """Taper the side walls.  Positive flares them outward toward the opening (§6.3)."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepOffsetAPI import BRepOffsetAPI_DraftAngle
    from OCP.GeomAbs import GeomAbs_SurfaceType
    from OCP.gp import gp_Pln
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    origin = gp_Pnt(*plane.origin)
    if start_offset:
        origin.Translate(sweep.Multiplied(start_offset))
    neutral = gp_Pln(origin, gp_Dir(*plane.normal))
    pull = gp_Dir(sweep)

    drafter = BRepOffsetAPI_DraftAngle(shape)
    added = 0
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        surface = BRepAdaptor_Surface(face)
        # Skip the two caps: their normal is parallel to the sweep.
        if surface.GetType() == GeomAbs_SurfaceType.GeomAbs_Plane:
            face_normal = surface.Plane().Axis().Direction()
            if abs(face_normal.Dot(gp_Dir(sweep))) > 0.999:
                explorer.Next()
                continue
        try:
            drafter.Add(face, pull, math.radians(angle_deg), neutral)
            if not drafter.AddDone():
                drafter.Remove(face)
            else:
                added += 1
        except Exception:
            pass
        explorer.Next()

    if not added:
        raise ToolSolidError(
            "No side wall could take a draft angle. Set the draft back to zero."
        )
    drafter.Build()
    if not drafter.IsDone():
        raise ToolSolidError(
            f"A draft of {angle_deg:g} degrees is too large for this profile. "
            f"Use a smaller angle."
        )
    return drafter.Shape()


def distance_to_face(plane: Plane, target_point: tuple[float, float, float]) -> float:
    """Signed distance from the sketch plane to a point, along the plane normal."""
    ox, oy, oz = plane.origin
    nx, ny, nz = plane.normal
    px, py, pz = target_point
    return (px - ox) * nx + (py - oy) * ny + (pz - oz) * nz


__all__ = [
    "ToolSolid",
    "contact_overlap_for",
    "ToolSolidError",
    "apply_draft",
    "build_tool_solid",
    "distance_to_face",
    "extrusion_length",
    "fuse_overlapping",
    "placement_transform",
]
