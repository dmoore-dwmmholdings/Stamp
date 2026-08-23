"""Snap targets - spec §6.2.

Everything the profile can snap to, expressed in the sketch plane's own (u, v)
coordinates so the handle overlay never has to think in 3D.

The one that earns its keep is the cylinder axis.  Centering a logo on a boss is
otherwise a matter of reading two diameters off a drawing and typing the average;
with the axis as a target it is one drag.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Face, TopoDS_Shape

from stamp.core.document import Plane

#: How far off the sketch plane a point may sit and still count as being on it.
IN_PLANE_TOLERANCE = 1e-3

#: Beyond this many edges the part-edge targets are skipped.  A snap list nobody
#: can hit is not worth the time it costs to build on every selection.
MAX_EDGES = 4000


class SnapKind(StrEnum):
    FACE_CENTER = "face center"
    FACE_CORNER = "face corner"
    FACE_EDGE_MIDPOINT = "face edge midpoint"
    PART_VERTEX = "part vertex"
    PART_EDGE_MIDPOINT = "edge midpoint"
    CYLINDER_AXIS = "cylinder axis"
    FEATURE = "feature"
    ORIGIN = "sketch origin"
    GRID = "grid"


@dataclass(frozen=True)
class SnapTarget:
    u: float
    v: float
    kind: SnapKind

    @property
    def uv(self) -> tuple[float, float]:
        return (self.u, self.v)


def v_axis(plane: Plane) -> tuple[float, float, float]:
    nx, ny, nz = plane.normal
    ux, uy, uz = plane.u_axis
    return (ny * uz - nz * uy, nz * ux - nx * uz, nx * uy - ny * ux)


def to_plane(plane: Plane, point: tuple[float, float, float]) -> tuple[float, float, float]:
    """Return ``(u, v, out_of_plane)`` for a world point."""
    ox, oy, oz = plane.origin
    rx, ry, rz = point[0] - ox, point[1] - oy, point[2] - oz
    ux, uy, uz = plane.u_axis
    vx, vy, vz = v_axis(plane)
    nx, ny, nz = plane.normal
    return (
        rx * ux + ry * uy + rz * uz,
        rx * vx + ry * vy + rz * vz,
        rx * nx + ry * ny + rz * nz,
    )


def collect(
    shape: TopoDS_Shape | None,
    plane: Plane,
    anchor_face: TopoDS_Face | None = None,
    *,
    feature_offsets: list[tuple[float, float]] | None = None,
    include_part_edges: bool = True,
) -> list[SnapTarget]:
    """Every snap target for one sketch plane, in plane coordinates."""
    targets: list[SnapTarget] = [SnapTarget(0.0, 0.0, SnapKind.ORIGIN)]

    for offset in feature_offsets or []:
        targets.append(SnapTarget(offset[0], offset[1], SnapKind.FEATURE))

    if anchor_face is not None:
        targets.extend(_face_targets(anchor_face, plane))

    if shape is not None:
        targets.extend(_cylinder_axis_targets(shape, plane))
        if include_part_edges:
            targets.extend(_edge_targets(shape, plane))

    return _dedupe(targets)


def _face_targets(face: TopoDS_Face, plane: Plane) -> list[SnapTarget]:
    """The centre, the bounding-box corners, and the edge midpoints of a face."""
    from stamp.core.refs import face_center

    out: list[SnapTarget] = []
    u, v, _ = to_plane(plane, face_center(face))
    out.append(SnapTarget(u, v, SnapKind.FACE_CENTER))

    us: list[float] = []
    vs: list[float] = []
    explorer = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        point = _edge_midpoint(edge)
        if point is not None:
            eu, ev, offset = to_plane(plane, point)
            if abs(offset) <= IN_PLANE_TOLERANCE:
                out.append(SnapTarget(eu, ev, SnapKind.FACE_EDGE_MIDPOINT))
                us.append(eu)
                vs.append(ev)
        explorer.Next()

    for vertex in _vertices(face):
        vu, vv, offset = to_plane(plane, vertex)
        if abs(offset) <= IN_PLANE_TOLERANCE:
            us.append(vu)
            vs.append(vv)

    if us and vs:
        for cu in (min(us), max(us)):
            for cv in (min(vs), max(vs)):
                out.append(SnapTarget(cu, cv, SnapKind.FACE_CORNER))
    return out


def _edge_targets(shape: TopoDS_Shape, plane: Plane) -> list[SnapTarget]:
    """Midpoints and endpoints of the part's edges that lie in the sketch plane."""
    out: list[SnapTarget] = []
    count = 0
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
    while explorer.More():
        count += 1
        if count > MAX_EDGES:
            break
        edge = TopoDS.Edge_s(explorer.Current())
        midpoint = _edge_midpoint(edge)
        if midpoint is not None:
            u, v, offset = to_plane(plane, midpoint)
            if abs(offset) <= IN_PLANE_TOLERANCE:
                out.append(SnapTarget(u, v, SnapKind.PART_EDGE_MIDPOINT))
        for vertex in _vertices(edge):
            u, v, offset = to_plane(plane, vertex)
            if abs(offset) <= IN_PLANE_TOLERANCE:
                out.append(SnapTarget(u, v, SnapKind.PART_VERTEX))
        explorer.Next()
    return out


def _cylinder_axis_targets(shape: TopoDS_Shape, plane: Plane) -> list[SnapTarget]:
    """Where each cylindrical or conical axis crosses the sketch plane.

    This is what centres a logo on a boss (§6.2).
    """
    out: list[SnapTarget] = []
    seen: set[tuple[int, int]] = set()
    nx, ny, nz = plane.normal

    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        surface = BRepAdaptor_Surface(face)
        kind = surface.GetType()
        axis = None
        if kind == GeomAbs_SurfaceType.GeomAbs_Cylinder:
            axis = surface.Cylinder().Axis()
        elif kind == GeomAbs_SurfaceType.GeomAbs_Cone:
            axis = surface.Cone().Axis()
        explorer.Next()
        if axis is None:
            continue

        direction = axis.Direction()
        denominator = direction.X() * nx + direction.Y() * ny + direction.Z() * nz
        if abs(denominator) < 1e-6:
            continue  # the axis runs along the plane, so it never crosses it

        location = axis.Location()
        ox, oy, oz = plane.origin
        t = (
            (ox - location.X()) * nx
            + (oy - location.Y()) * ny
            + (oz - location.Z()) * nz
        ) / denominator
        point = (
            location.X() + t * direction.X(),
            location.Y() + t * direction.Y(),
            location.Z() + t * direction.Z(),
        )
        u, v, _ = to_plane(plane, point)
        key = (round(u, 4), round(v, 4))
        if key in seen:
            continue
        seen.add(key)
        out.append(SnapTarget(u, v, SnapKind.CYLINDER_AXIS))
    return out


def _edge_midpoint(edge) -> tuple[float, float, float] | None:
    try:
        curve = BRepAdaptor_Curve(edge)
        first, last = BRep_Tool.Range_s(edge)
        point = curve.Value((first + last) / 2.0)
        return (point.X(), point.Y(), point.Z())
    except Exception:
        return None


def _vertices(shape: TopoDS_Shape) -> list[tuple[float, float, float]]:
    out: list[tuple[float, float, float]] = []
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_VERTEX)
    while explorer.More():
        point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(explorer.Current()))
        out.append((point.X(), point.Y(), point.Z()))
        explorer.Next()
    return out


#: Targets closer together than this collapse into one.
DEDUPE_TOLERANCE = 1e-4

#: The order targets are preferred in when two land on the same spot.
_PRIORITY = {
    SnapKind.CYLINDER_AXIS: 0,
    SnapKind.FACE_CENTER: 1,
    SnapKind.FEATURE: 2,
    SnapKind.ORIGIN: 3,
    SnapKind.FACE_CORNER: 4,
    SnapKind.PART_VERTEX: 5,
    SnapKind.FACE_EDGE_MIDPOINT: 6,
    SnapKind.PART_EDGE_MIDPOINT: 7,
    SnapKind.GRID: 8,
}


def _dedupe(targets: list[SnapTarget]) -> list[SnapTarget]:
    best: dict[tuple[int, int], SnapTarget] = {}
    for target in targets:
        if not (math.isfinite(target.u) and math.isfinite(target.v)):
            continue
        key = (
            round(target.u / DEDUPE_TOLERANCE),
            round(target.v / DEDUPE_TOLERANCE),
        )
        current = best.get(key)
        if current is None or _PRIORITY[target.kind] < _PRIORITY[current.kind]:
            best[key] = target
    return list(best.values())


def nearest(
    targets: list[SnapTarget], point: tuple[float, float], tolerance: float
) -> SnapTarget | None:
    """The closest target within *tolerance*, or None."""
    best: SnapTarget | None = None
    best_distance = tolerance
    for target in targets:
        distance = math.dist(point, target.uv)
        if distance < best_distance or (
            distance == best_distance
            and best is not None
            and _PRIORITY[target.kind] < _PRIORITY[best.kind]
        ):
            best = target
            best_distance = distance
    return best


__all__ = [
    "IN_PLANE_TOLERANCE",
    "SnapKind",
    "SnapTarget",
    "collect",
    "nearest",
    "to_plane",
    "v_axis",
]
