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
    #: On the sketch plane where the artwork is, never where the sweep happened to
    #: start: the decal answers "where does this land", and a through cut starts
    #: half the part diagonal behind the face.
    footprint: TopoDS_Shape
    #: How far behind the sketch plane the sweep actually started, in mm.
    contact_overlap: float = 0.0
    #: For wrapped artwork, the axis of the face it was wrapped onto, as
    #: ``(origin, direction)``.  ``None`` for the ordinary planar sweep.  Edge
    #: classification needs it: on a wrapped tool the top and bottom of the feature
    #: are two radii, not two positions along the sweep (§6.4A).
    axis: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None


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
        # The distance is signed along the plane normal and the direction says which
        # way the sweep goes, so the two can disagree - and when they do, taking the
        # absolute value quietly extrudes the other way and to the wrong depth.  A
        # target behind the tool is a mistake in the pick or in the direction, and
        # only the user knows which, so say so instead of guessing.
        toward = -1.0 if operation.direction is Direction.INTO else 1.0
        if to_face_distance * toward < 0:
            raise ToolSolidError(
                "The target face is on the other side of the sketch plane; "
                "flip the direction."
            )
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
    swept = footprint
    if start:
        shift = gp_Trsf()
        shift.SetTranslation(sweep.Multiplied(start))
        swept = BRepBuilderAPI_Transform(footprint, shift, True).Shape()

    prism = BRepPrimAPI_MakePrism(swept, sweep.Multiplied(length), False, True)
    if not prism.IsDone():
        raise ToolSolidError("The extrude failed. Check the profile and the depth.")
    shape = prism.Shape()

    if abs(operation.draft_angle) > 1e-9:
        shape = apply_draft(shape, plane, sweep, operation.draft_angle)

    shape = fuse_overlapping(shape)

    return ToolSolid(
        shape=shape,
        transform=trsf,
        direction=(sweep.X(), sweep.Y(), sweep.Z()),
        length=length,
        footprint=_display_footprint(footprint, plane, part_diagonal),
        contact_overlap=overlap,
    )


#: How far off the face the flat decal preview floats, as a fraction of the part's
#: bounding-box diagonal.  Enough that the depth buffer keeps it in front of the
#: face it lies on, far too little to read as a gap or to measure.
DECAL_LIFT = 1e-4


def _display_footprint(
    footprint: TopoDS_Shape, plane: Plane, part_diagonal: float
) -> TopoDS_Shape:
    """Lift the flat decal a hair off the face so the two do not z-fight."""
    trsf = gp_Trsf()
    trsf.SetTranslation(
        gp_Vec(gp_Dir(*plane.normal)).Multiplied(max(part_diagonal * DECAL_LIFT, 1e-3))
    )
    return BRepBuilderAPI_Transform(footprint, trsf, True).Shape()


def component_footprints(
    profile: Profile,
    placement: Placement,
    tool: ToolSolid,
) -> dict[str, TopoDS_Shape]:
    """The placed but un-extruded outline of each artwork component.

    The flat decal of :attr:`ToolSolid.footprint`, one per component instead of
    all in one piece, so the preview can draw a two-colour logo in two colours.
    Deliberately no extrusion and no boolean: this runs every time the preview is
    redrawn, and what it has to show is where the artwork lands, which the 2D
    footprint already says.
    """
    keys = [c.key for c in profile.components]
    if len(keys) < 2:
        return {}

    sx, sy = placement.scale
    if placement.mirror_u:
        sx = -sx
    if placement.mirror_v:
        sy = -sy

    out: dict[str, TopoDS_Shape] = {}
    for key in keys:
        if not profile.faces_of(key):
            continue
        shape = _scale_shape(profile.compound_of(key), sx, sy)
        out[key] = BRepBuilderAPI_Transform(shape, tool.transform, True).Shape()
    return out


def component_prisms(
    profile: Profile,
    placement: Placement,
    tool: ToolSolid,
    *,
    reach: float,
) -> dict[str, TopoDS_Shape]:
    """A tall prism per artwork component, for dividing a built body between them.

    Not the tool solid over again.  This only has to *cover* the body, so it is
    swept far past both ends instead of to the feature's exact depth - what
    decides the split is the 2D footprint, which is the only thing the components
    differ in.  Sweeping generously means none of the depth arithmetic (start
    offset, contact overlap, draft) has to be replayed and kept in step.
    """
    keys = [c.key for c in profile.components]
    if len(keys) < 2:
        return {}

    sx, sy = placement.scale
    if placement.mirror_u:
        sx = -sx
    if placement.mirror_v:
        sy = -sy

    direction = gp_Vec(*tool.direction)
    if direction.Magnitude() < 1e-12:
        return {}
    direction.Normalize()

    prisms: dict[str, TopoDS_Shape] = {}
    for key, footprint in component_footprints(profile, placement, tool).items():
        shift = gp_Trsf()
        shift.SetTranslation(direction.Multiplied(-reach / 2.0))
        footprint = BRepBuilderAPI_Transform(footprint, shift, True).Shape()
        maker = BRepPrimAPI_MakePrism(footprint, direction.Multiplied(reach), False, True)
        if maker.IsDone():
            prisms[key] = fuse_overlapping(maker.Shape())
    return prisms


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
    build = _cylindrical_wrap_shape if kind == "cylinder" else _conical_wrap_shape
    shape, footprint, axis = build(
        profile, placement, operation, plane, target_face, depth, part_diagonal
    )
    return ToolSolid(
        shape=shape,
        transform=placement_transform(placement, plane),
        direction=(plane.normal[0] * sign, plane.normal[1] * sign, plane.normal[2] * sign),
        length=depth,
        footprint=footprint,
        axis=axis,
    )


def _open_ring(polyline: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop the repeated closing point, so the ring is n points and n segments."""
    if len(polyline) > 1 and math.dist(polyline[0], polyline[-1]) < 1e-9:
        return polyline[:-1]
    return list(polyline)


def _ring_area(ring: list[tuple[float, float]]) -> float:
    """Signed area of a closed polygon; positive counter-clockwise."""
    total = 0.0
    for i, (x0, y0) in enumerate(ring):
        x1, y1 = ring[(i + 1) % len(ring)]
        total += x0 * y1 - x1 * y0
    return total / 2.0


def _placed_rings(
    profile: Profile, placement: Placement
) -> list[list[list[tuple[float, float]]]]:
    """The placed artwork as plain polygons in the sketch plane's own (u, v) mm.

    Grouped the way ``normalize`` grouped it into faces: each entry is one material
    loop's outline followed by the holes directly inside it.  Polygons and not
    wires, because a wrap bends the outline - a straight line in the flat artwork is
    a helix once it lands on the cylinder, and no curve type survives the map, only
    points do.  The flattened polylines are the ones the whole importer already
    reasons about, so a wrap and a containment test see the same artwork.
    """
    sx, sy = placement.scale
    if placement.mirror_u:
        sx = -sx
    if placement.mirror_v:
        sy = -sy
    if abs(sx) < 1e-9 or abs(sy) < 1e-9:
        raise ToolSolidError("A scale of zero leaves nothing to extrude.")
    cos_r = math.cos(math.radians(placement.rotation))
    sin_r = math.sin(math.radians(placement.rotation))
    offset_u, offset_v = placement.offset_2d

    def place(point: tuple[float, float]) -> tuple[float, float]:
        x, y = point[0] * sx, point[1] * sy
        return (x * cos_r - y * sin_r + offset_u, x * sin_r + y * cos_r + offset_v)

    children: dict[int, list[int]] = {}
    for index, parent in enumerate(profile.parent):
        if parent >= 0:
            children.setdefault(parent, []).append(index)

    groups: list[list[list[tuple[float, float]]]] = []
    for index, loop in enumerate(profile.loops):
        if profile.depth[index] % 2 != 0 or not loop.closed or not loop.valid:
            continue  # a hole, something that never closed, or a reported crossing
        rings = [loop.polyline] + [
            profile.loops[c].polyline for c in children.get(index, []) if profile.loops[c].closed
        ]
        group = [[place(p) for p in _open_ring(r)] for r in rings]
        if len(group[0]) >= 3:
            groups.append([r for r in group if len(r) >= 3])
    if not groups:
        raise ToolSolidError("This profile has no closed area to wrap.")
    return groups


def _unwrap_map(position, radius: float, plane: Plane):
    """Return ``(axes, to_uv, outward)`` for rolling the sketch plane onto a cylinder.

    ``to_uv`` maps a point of the flat artwork, in the sketch plane's own millimetres,
    to the cylinder's ``(u, v)`` surface parameters: the in-plane offset is split into
    the part that runs around the face, which becomes an angle of arc length over
    radius, and the part that runs along the axis, which is carried across unchanged.
    That is what makes it a wrap and not a projection (§6.1) - the arc length on the
    part equals the width of the artwork, so an 18 mm word on a 20 mm tube spans the
    51.6 degrees its length actually covers rather than the 64.2 degrees a shadow
    cast onto the tube would.

    ``outward`` is +1 when the face's material is inside it, as on a boss, and -1 on
    the wall of a bore, where a cut has to travel away from the axis.
    """
    axes = gp_Ax3(position.Location(), position.Direction(), position.XDirection())
    x_dir, y_dir = gp_Vec(axes.XDirection()), gp_Vec(axes.YDirection())
    z_dir = gp_Vec(axes.Direction())
    to_origin = gp_Vec(axes.Location(), gp_Pnt(*plane.origin))
    angle = math.atan2(to_origin.Dot(y_dir), to_origin.Dot(x_dir))
    v_origin = to_origin.Dot(z_dir)

    # The frame of the unrolled surface at the point the user clicked.
    around = y_dir.Multiplied(math.cos(angle)).Subtracted(x_dir.Multiplied(math.sin(angle)))
    outward_dir = x_dir.Multiplied(math.cos(angle)).Added(y_dir.Multiplied(math.sin(angle)))

    u_axis = gp_Vec(*plane.u_axis)
    v_axis = gp_Vec(*plane.normal).Crossed(u_axis)
    u_around, v_around = u_axis.Dot(around), v_axis.Dot(around)
    u_along, v_along = u_axis.Dot(z_dir), v_axis.Dot(z_dir)

    def to_uv(point: tuple[float, float]) -> tuple[float, float]:
        a, b = point
        return (
            angle + (a * u_around + b * v_around) / radius,
            v_origin + a * u_along + b * v_along,
        )

    outward = 1.0 if gp_Vec(*plane.normal).Dot(outward_dir) >= 0 else -1.0
    return axes, to_uv, outward


def _wrap_radii(
    radius: float, depth: float, outward: float, operation: Operation, margin: float
) -> tuple[float, float]:
    """The two radii the wrapped tool spans, near side only.

    The overlap margin goes on the side the operation throws away - outside the face
    for a pocket, inside it for a boss - so the mark comes out the depth that was
    asked for.  Put on the visible side it makes every wrapped pocket one margin
    deeper and every boss one margin taller than the number in the panel.
    """
    toward = (-1.0 if operation.direction is Direction.INTO else 1.0) * outward
    far = radius + toward * depth
    near = radius - toward * margin
    low, high = min(far, near), max(far, near)
    # A through cut asks for a depth far past the axis; it stops just short of it,
    # which still clears any wall the face can have, and stays a valid solid.
    low = max(low, radius * 1e-3)
    if high - low < 1e-6:
        raise ToolSolidError("This wrapped feature has no thickness. Check the depth.")
    return low, high


def _wrap_wire(ring: list[tuple[float, float]], surface):
    """One ring of (u, v) parameters as a wire of exact curves on *surface*.

    Every segment is a straight line in the surface's own parameter space, which on
    a cylinder is a helix - so the wire lies exactly on the face, and the artwork
    keeps its arc length rather than being flattened onto a chord.
    """
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeVertex,
        BRepBuilderAPI_MakeWire,
    )
    from OCP.BRepLib import BRepLib
    from OCP.Geom2d import Geom2d_Line
    from OCP.gp import gp_Dir2d, gp_Pnt2d

    vertices = [BRepBuilderAPI_MakeVertex(surface.Value(u, v)).Vertex() for u, v in ring]
    maker = BRepBuilderAPI_MakeWire()
    count = len(ring)
    for i in range(count):
        (u0, v0), (u1, v1) = ring[i], ring[(i + 1) % count]
        du, dv = u1 - u0, v1 - v0
        length = math.hypot(du, dv)
        if length < 1e-12:
            continue
        line = Geom2d_Line(gp_Pnt2d(u0, v0), gp_Dir2d(du / length, dv / length))
        maker.Add(
            BRepBuilderAPI_MakeEdge(
                line, surface, vertices[i], vertices[(i + 1) % count], 0.0, length
            ).Edge()
        )
    if not maker.IsDone():
        raise ToolSolidError("Stamp could not lay this artwork onto the curved face.")
    wire = maker.Wire()
    BRepLib.BuildCurves3d_s(wire)
    return wire


def _wrap_face(group: list[list[tuple[float, float]]], surface):
    """One material loop and its holes as a single face on *surface*."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace

    wires = [_wrap_wire(r, surface) for r in _oriented_rings(group)]
    maker = BRepBuilderAPI_MakeFace(surface, wires[0], True)
    for hole in wires[1:]:
        maker.Add(hole)
    if not maker.IsDone():
        raise ToolSolidError("Stamp could not lay this artwork onto the curved face.")
    return maker.Face(), wires


def _oriented_rings(
    group: list[list[tuple[float, float]]]
) -> list[list[tuple[float, float]]]:
    """Outline counter-clockwise, holes clockwise, whatever the artwork wound.

    A mirror flips the winding of every loop at once, so this is decided here rather
    than trusted from the source.
    """
    out = []
    for i, ring in enumerate(group):
        wanted = 1.0 if i == 0 else -1.0
        out.append(ring if _ring_area(ring) * wanted > 0 else list(reversed(ring)))
    return out


def _wrap_solids(groups, axes, low: float, high: float) -> TopoDS_Shape:
    """Build the wrapped tool: two faces on the face's own surface, joined radially.

    The inner and outer faces are exact pieces of a cylinder, so the tool meets the
    part on the part's own surface.  Between them the walls are ruled between
    matching edges, and because the two rings differ only in radius each wall is
    radial - which is the property that makes this a wrap rather than a shadow.
    """
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepFill import BRepFill
    from OCP.BRepLib import BRepLib
    from OCP.Geom import Geom_CylindricalSurface
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Compound

    inner = Geom_CylindricalSurface(axes, low)
    outer = Geom_CylindricalSurface(axes, high)

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    for group in groups:
        inner_face, inner_wires = _wrap_face(group, inner)
        outer_face, outer_wires = _wrap_face(group, outer)
        sewing = BRepBuilderAPI_Sewing(1e-6)
        sewing.Add(inner_face)
        sewing.Add(outer_face)
        for wire_in, wire_out in zip(inner_wires, outer_wires, strict=True):
            walls = BRepFill.Shell_s(wire_in, wire_out)
            explorer = TopExp_Explorer(walls, TopAbs_ShapeEnum.TopAbs_FACE)
            while explorer.More():
                sewing.Add(TopoDS.Face_s(explorer.Current()))
                explorer.Next()
        sewing.Perform()
        maker = BRepBuilderAPI_MakeSolid()
        explorer = TopExp_Explorer(sewing.SewedShape(), TopAbs_ShapeEnum.TopAbs_SHELL)
        shells = 0
        while explorer.More():
            maker.Add(TopoDS.Shell_s(explorer.Current()))
            shells += 1
            explorer.Next()
        if not shells or not maker.IsDone():
            raise ToolSolidError("Stamp could not close the wrapped tool on this face.")
        solid = maker.Solid()
        BRepLib.OrientClosedSolid_s(solid)
        if not BRepCheck_Analyzer(solid).IsValid():
            raise ToolSolidError(
                "The wrapped artwork does not make a clean solid on this face. "
                "Simplify the artwork, or make it smaller."
            )
        builder.Add(compound, solid)
    return compound


def _cylindrical_wrap_shape(
    profile: Profile,
    placement: Placement,
    operation: Operation,
    plane: Plane,
    target_face: TopoDS_Face,
    depth: float,
    part_diagonal: float,
) -> tuple[TopoDS_Shape, TopoDS_Shape, tuple]:
    """Roll the artwork onto a cylindrical face and raise it radially (§6.1)."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    if abs(operation.draft_angle) > 1e-9:
        raise ToolSolidError(
            "A draft angle is not available on wrapped artwork: the walls of a wrap "
            "are radial. Set the draft back to zero."
        )
    cylinder = BRepAdaptor_Surface(target_face).Cylinder()
    position = cylinder.Position()
    radius = cylinder.Radius()
    axes, to_uv, outward = _unwrap_map(position, radius, plane)

    groups = [[[to_uv(p) for p in ring] for ring in group] for group in _placed_rings(profile, placement)]
    angles = [u for group in groups for ring in group for u, _v in ring]
    if max(angles) - min(angles) >= 2 * math.pi - 1e-6:
        raise ToolSolidError(
            "This artwork is wider than the way round this face, so it would wrap "
            "onto itself. Make it smaller."
        )
    _validate_wrapped_span(angles, target_face, position, axes)

    margin = max(1e-3, part_diagonal * 1e-5)
    low, high = _wrap_radii(radius, depth, outward, operation, margin)
    shape = fuse_overlapping(_wrap_solids(groups, axes, low, high))
    decal = _wrap_decal(groups, axes, radius + outward * max(part_diagonal * DECAL_LIFT, 1e-3))
    location, direction = axes.Location(), axes.Direction()
    axis = (
        (location.X(), location.Y(), location.Z()),
        (direction.X(), direction.Y(), direction.Z()),
    )
    return shape, decal, axis


def _wrap_decal(groups, axes, radius: float) -> TopoDS_Shape:
    """The wrapped artwork as bare faces on the surface, for the preview (§6.2)."""
    from OCP.BRep import BRep_Builder
    from OCP.Geom import Geom_CylindricalSurface
    from OCP.TopoDS import TopoDS_Compound

    surface = Geom_CylindricalSurface(axes, max(radius, 1e-6))
    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    for group in groups:
        face, _wires = _wrap_face(group, surface)
        builder.Add(compound, face)
    return compound


def _validate_wrapped_span(angles, target_face: TopoDS_Face, position, axes) -> None:
    """Refuse wrapped artwork that runs off the end of a trimmed face's u-range."""
    from OCP.BRepTools import BRepTools

    u0, u1, _v0, _v1 = BRepTools.UVBounds_s(target_face)
    if abs(u1 - u0) >= 2 * math.pi - 1e-6:
        return  # A complete revolution has no boundary seam to cross.
    # The face may carry a left-handed frame, in which case its u runs the other way
    # from the right-handed one the wrap was built in.
    flip = 1.0 if gp_Vec(axes.YDirection()).Dot(gp_Vec(position.YDirection())) >= 0 else -1.0
    values = sorted(flip * a for a in angles)
    low, high = values[0], values[-1]
    if any(
        u0 - 1e-6 <= low + 2 * math.pi * turns and high + 2 * math.pi * turns <= u1 + 1e-6
        for turns in (-1, 0, 1)
    ):
        return
    raise ToolSolidError(
        "The artwork crosses this face's unwrap seam. Move it away from the seam or split it."
    )


#: How far round a cone the projected wrap is allowed to reach, as a share of the
#: local radius.  A cone is still a projection along the tangent normal rather than
#: a true wrap, and the error in that grows with the angle: at 0.7 R the artwork is
#: about 5% wider round the face than it is flat, and past 1.0 R it would be clipped
#: at the silhouette without a word.
CONE_WRAP_LIMIT = 0.7


def _conical_wrap_shape(
    profile: Profile,
    placement: Placement,
    operation: Operation,
    plane: Plane,
    target_face: TopoDS_Face,
    depth: float,
    part_diagonal: float,
) -> tuple[TopoDS_Shape, TopoDS_Shape, tuple]:
    """Intersect a profile selector with a conical band cut to the artwork's sector."""
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
    inner_start = max(1e-6, radius_start - (depth if inward else margin))
    inner_end = max(1e-6, radius_end - (depth if inward else margin))
    outer_start = radius_start + (margin if inward else depth)
    outer_end = radius_end + (margin if inward else depth)
    height = (end - start) * axial_scale

    flat = _placed_footprint(profile, placement, plane)
    limit = CONE_WRAP_LIMIT * min(radius_start, radius_end)
    if _tangential_half_width(flat, plane, direction) > limit:
        raise ToolSolidError(
            f"This artwork is too wide to wrap onto this cone. On a conical face "
            f"Stamp projects the artwork rather than rolling it on, which only holds "
            f"near the middle; keep it under {2 * limit:.1f} mm across, or put it on "
            f"a cylindrical face."
        )
    _validate_wrap_seam(flat, target_face, cone.Position())
    sector, x_dir = _sector_frame(
        flat, cone.Position(), origin, direction,
        max(radius_start, radius_end), part_diagonal,
    )
    axes = gp_Ax2(origin, direction, x_dir)
    outer_solid = BRepPrimAPI_MakeCone(axes, outer_start, outer_end, height, sector).Shape()
    inner_solid = BRepPrimAPI_MakeCone(axes, inner_start, inner_end, height, sector).Shape()
    ring_op = BRepAlgoAPI_Cut(outer_solid, inner_solid)
    ring_op.Build()
    if not ring_op.IsDone() or ring_op.Shape().IsNull():
        raise ToolSolidError("Stamp could not form the conical wrap band.")

    selector = _wrap_selector(profile, placement, operation, plane, part_diagonal)
    common = BRepAlgoAPI_Common(ring_op.Shape(), selector.shape)
    common.Build()
    if not common.IsDone() or common.Shape().IsNull():
        raise ToolSolidError("The artwork does not intersect this conical face. Move it onto the face.")
    return (
        common.Shape(),
        _display_footprint(flat, plane, part_diagonal),
        ((location.X(), location.Y(), location.Z()),
         (direction.X(), direction.Y(), direction.Z())),
    )


def _wrap_selector(
    profile: Profile,
    placement: Placement,
    operation: Operation,
    plane: Plane,
    part_diagonal: float,
) -> ToolSolid:
    """A prism through the artwork, long enough to cross the band and no longer."""
    selector_placement = replace(placement, mode=PlacementMode.PLANAR)
    selector_operation = replace(operation, depth_mode=DepthMode.THROUGH_ALL)
    return build_tool_solid(
        profile, selector_placement, selector_operation, plane,
        part_diagonal=part_diagonal, contact_overlap=0.0,
    )


def _sector_frame(
    footprint: TopoDS_Shape, position, origin: gp_Pnt, direction, radius: float,
    part_diagonal: float,
) -> tuple[float, gp_Dir]:
    """The angular sector of the band the artwork actually covers.

    A full 360 degree band reaches every wall the face has, so intersecting it with a
    prism that runs right through the part keeps the artwork twice: once on the near
    wall, mirrored on the far one.  Cutting the band down to the sector the artwork
    occupies leaves the far wall out of the tool entirely.
    """
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    axes = gp_Ax3(position.Location(), position.Direction(), position.XDirection())
    x_dir, y_dir = gp_Vec(axes.XDirection()), gp_Vec(axes.YDirection())
    angles = []
    explorer = TopExp_Explorer(footprint, TopAbs_ShapeEnum.TopAbs_VERTEX)
    while explorer.More():
        point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(explorer.Current()))
        offset = gp_Vec(axes.Location(), point)
        angles.append(math.atan2(offset.Dot(y_dir), offset.Dot(x_dir)))
        explorer.Next()
    if not angles:
        raise ToolSolidError("This profile has no closed area to wrap.")
    low, high = min(angles), max(angles)
    if high - low > math.pi:
        # The artwork straddles the frame's own seam at +/- pi; measure it from there.
        shifted = [a + 2 * math.pi if a < 0 else a for a in angles]
        low, high = min(shifted), max(shifted)
    if high - low > math.pi:
        raise ToolSolidError(
            "This artwork reaches more than half way round the face. Make it smaller."
        )
    pad = max(1e-3, part_diagonal * 1e-5) / max(radius, 1e-6) + 1e-4
    low, high = low - pad, high + pad
    turn = gp_Trsf()
    turn.SetRotation(gp_Ax1(axes.Location(), axes.Direction()), low)
    return high - low, gp_Dir(gp_Vec(axes.XDirection()).Transformed(turn))


def _tangential_half_width(footprint: TopoDS_Shape, plane: Plane, axis) -> float:
    """How far the flat artwork reaches round the face, either side of the click."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    around = gp_Vec(axis).Crossed(gp_Vec(*plane.normal))
    if around.Magnitude() < 1e-9:
        return 0.0
    around.Normalize()
    origin = gp_Pnt(*plane.origin)
    widest = 0.0
    explorer = TopExp_Explorer(footprint, TopAbs_ShapeEnum.TopAbs_VERTEX)
    while explorer.More():
        point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(explorer.Current()))
        widest = max(widest, abs(gp_Vec(origin, point).Dot(around)))
        explorer.Next()
    return widest


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
) -> TopoDS_Shape:
    """Taper the side walls.  Positive flares them outward toward the opening (§6.3).

    The neutral plane - the one section a draft leaves at its drawn size - is the
    sketch plane, because that is the section the user typed the artwork's size for
    and the one the mark is measured at on the finished part.  Not where the sweep
    started: a through cut starts half a part diagonal behind the face and a
    symmetric one half its depth behind, and pivoting there would have the wall
    already tapered by that distance times the tangent of the angle by the time it
    reaches the face - a 512 mm2 logo comes out 351 mm2 at one degree.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepOffsetAPI import BRepOffsetAPI_DraftAngle
    from OCP.GeomAbs import GeomAbs_SurfaceType
    from OCP.gp import gp_Pln
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    neutral = gp_Pln(gp_Pnt(*plane.origin), gp_Dir(*plane.normal))
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
