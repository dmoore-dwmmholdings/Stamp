"""Reference stability - spec §8.

The rule: never store an index.  Store geometry plus intent, and re-resolve on every
rebuild.  When resolution is ambiguous the feature is marked broken with a message
and a re-pick button, and the previous good result is kept - the user never stares
at a vanished part.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepGProp import BRepGProp
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.gp import gp_Pnt, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Face, TopoDS_Shape

from stamp.core.document import Anchor, AnchorKind, FaceRef, Plane

#: A candidate face must score at least this well to be accepted (§8.2).
ACCEPT_THRESHOLD = 0.55

#: Two candidates within this much of each other are called ambiguous, not guessed.
AMBIGUITY_MARGIN = 0.05

SURFACE_NAMES = {
    GeomAbs_SurfaceType.GeomAbs_Plane: "plane",
    GeomAbs_SurfaceType.GeomAbs_Cylinder: "cylinder",
    GeomAbs_SurfaceType.GeomAbs_Cone: "cone",
    GeomAbs_SurfaceType.GeomAbs_Sphere: "sphere",
    GeomAbs_SurfaceType.GeomAbs_Torus: "torus",
    GeomAbs_SurfaceType.GeomAbs_BezierSurface: "bezier",
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface: "bspline",
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution: "revolution",
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion: "extrusion",
    GeomAbs_SurfaceType.GeomAbs_OffsetSurface: "offset",
    GeomAbs_SurfaceType.GeomAbs_OtherSurface: "other",
}


class ReferenceError(RuntimeError):
    """A stored reference could not be resolved.  The message names the problem."""


@dataclass
class ResolvedFace:
    face: TopoDS_Face
    score: float
    ambiguous: bool = False


# ------------------------------------------------------------------ inspection


def faces_of(shape: TopoDS_Shape) -> list[TopoDS_Face]:
    out: list[TopoDS_Face] = []
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    while explorer.More():
        out.append(TopoDS.Face_s(explorer.Current()))
        explorer.Next()
    return out


def face_area(face: TopoDS_Face) -> float:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    return props.Mass()


def face_center(face: TopoDS_Face) -> tuple[float, float, float]:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    p = props.CentreOfMass()
    return (p.X(), p.Y(), p.Z())


def surface_kind(face: TopoDS_Face) -> str:
    return SURFACE_NAMES.get(BRepAdaptor_Surface(face).GetType(), "other")


def face_normal_at(face: TopoDS_Face, point: tuple[float, float, float]) -> tuple[float, float, float]:
    """Outward normal at the parameters nearest *point*, corrected for orientation."""
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.GeomLProp import GeomLProp_SLProps

    surface = BRep_Tool.Surface_s(face)
    projector = GeomAPI_ProjectPointOnSurf(gp_Pnt(*point), surface)
    if projector.NbPoints() > 0:
        u, v = projector.LowerDistanceParameters()
    else:
        u0, u1, v0, v1 = BRep_Tool.Surface_s(face).Bounds()  # pragma: no cover
        u, v = (u0 + u1) / 2.0, (v0 + v1) / 2.0

    props = GeomLProp_SLProps(surface, u, v, 1, 1e-6)
    if not props.IsNormalDefined():
        return (0.0, 0.0, 1.0)
    n = props.Normal()
    if face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED:
        n.Reverse()
    return (n.X(), n.Y(), n.Z())


def surface_parameters(face: TopoDS_Face) -> dict:
    """Enough of the surface definition to tell two faces apart on rebuild."""
    adaptor = BRepAdaptor_Surface(face)
    kind = adaptor.GetType()
    if kind == GeomAbs_SurfaceType.GeomAbs_Plane:
        pln = adaptor.Plane()
        a = pln.Axis()
        return {
            "origin": [a.Location().X(), a.Location().Y(), a.Location().Z()],
            "axis": [a.Direction().X(), a.Direction().Y(), a.Direction().Z()],
        }
    if kind == GeomAbs_SurfaceType.GeomAbs_Cylinder:
        cyl = adaptor.Cylinder()
        a = cyl.Axis()
        return {
            "origin": [a.Location().X(), a.Location().Y(), a.Location().Z()],
            "axis": [a.Direction().X(), a.Direction().Y(), a.Direction().Z()],
            "radius": cyl.Radius(),
        }
    if kind == GeomAbs_SurfaceType.GeomAbs_Cone:
        cone = adaptor.Cone()
        a = cone.Axis()
        return {
            "origin": [a.Location().X(), a.Location().Y(), a.Location().Z()],
            "axis": [a.Direction().X(), a.Direction().Y(), a.Direction().Z()],
            "radius": cone.RefRadius(),
            "half_angle": cone.SemiAngle(),
        }
    if kind == GeomAbs_SurfaceType.GeomAbs_Sphere:
        sph = adaptor.Sphere()
        c = sph.Location()
        return {"origin": [c.X(), c.Y(), c.Z()], "radius": sph.Radius()}
    return {}


# ------------------------------------------------------------------ make a ref


def make_face_ref(
    face: TopoDS_Face,
    point: tuple[float, float, float],
    *,
    origin_feature_id: str | None = None,
) -> FaceRef:
    """Capture everything §8.2 asks for at the moment the user clicks a face."""
    return FaceRef(
        point=tuple(point),
        normal=face_normal_at(face, point),
        surface_type=surface_kind(face),
        surface_params=surface_parameters(face),
        area=face_area(face),
        bbox_center=face_center(face),
        origin_feature_id=origin_feature_id,
    )


# --------------------------------------------------------------------- resolve


def resolve_face_ref(ref: FaceRef, shape: TopoDS_Shape) -> ResolvedFace:
    """Score every candidate face and take the best, or refuse to guess.

    Surface type must match.  After that the score weighs distance from the stored
    point, agreement of the normal, and the ratio of areas.
    """
    candidates = [f for f in faces_of(shape) if surface_kind(f) == ref.surface_type]
    if not candidates:
        raise ReferenceError(
            f"No {ref.surface_type} face is left on this part. Pick the face again."
        )

    diagonal = _shape_diagonal(shape) or 1.0
    scored: list[tuple[float, TopoDS_Face]] = []
    for face in candidates:
        scored.append((_score(ref, face, diagonal), face))
    scored.sort(key=lambda item: item[0], reverse=True)

    best_score, best_face = scored[0]
    if best_score < ACCEPT_THRESHOLD:
        raise ReferenceError(
            "The face this feature was placed on no longer matches anything on the "
            "part. Pick the face again."
        )
    ambiguous = len(scored) > 1 and (best_score - scored[1][0]) < AMBIGUITY_MARGIN
    return ResolvedFace(face=best_face, score=best_score, ambiguous=ambiguous)


def _score(ref: FaceRef, face: TopoDS_Face, diagonal: float) -> float:
    point = _closest_point_on_face(face, ref.point)
    distance = math.dist(point, ref.point)
    distance_score = max(0.0, 1.0 - distance / (diagonal * 0.25))

    normal = face_normal_at(face, ref.point)
    dot = sum(a * b for a, b in zip(normal, ref.normal, strict=True))
    normal_score = max(0.0, dot)

    area = face_area(face)
    if ref.area > 0 and area > 0:
        ratio = min(area, ref.area) / max(area, ref.area)
    else:
        ratio = 0.0

    return 0.5 * distance_score + 0.35 * normal_score + 0.15 * ratio


def _closest_point_on_face(face: TopoDS_Face, point) -> tuple[float, float, float]:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
    dist = BRepExtrema_DistShapeShape(face, vertex)
    if dist.IsDone() and dist.NbSolution() > 0:
        p = dist.PointOnShape1(1)
        return (p.X(), p.Y(), p.Z())
    return face_center(face)


def _shape_diagonal(shape: TopoDS_Shape) -> float:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return 0.0
    x0, y0, z0, x1, y1, z1 = box.Get()
    return math.dist((x0, y0, z0), (x1, y1, z1))


# ---------------------------------------------------------------- sketch plane


def longest_edge_direction(face: TopoDS_Face, normal: tuple[float, float, float]):
    """The direction of the longest straight edge, projected into the face plane.

    This is stable across rebuilds and usually matches what the user thinks of as
    "along" the face (§6.1).  Falls back to global +X projected into the plane.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GCPnts import GCPnts_AbscissaPoint
    from OCP.GeomAbs import GeomAbs_CurveType

    best_len = 0.0
    best_dir: tuple[float, float, float] | None = None

    explorer = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        curve = BRepAdaptor_Curve(edge)
        if curve.GetType() == GeomAbs_CurveType.GeomAbs_Line:
            try:
                length = GCPnts_AbscissaPoint.Length_s(curve)
            except Exception:
                length = 0.0
            if length > best_len:
                d = curve.Line().Direction()
                best_len = length
                best_dir = (d.X(), d.Y(), d.Z())
        explorer.Next()

    if best_dir is None:
        best_dir = (1.0, 0.0, 0.0)
    return _orthogonalize(best_dir, normal)


def _orthogonalize(direction, normal) -> tuple[float, float, float]:
    """Project *direction* into the plane with the given normal, and normalize."""
    d = gp_Vec(*direction)
    n = gp_Vec(*normal)
    if n.Magnitude() < 1e-12:
        return (1.0, 0.0, 0.0)
    n.Normalize()
    d = d.Subtracted(n.Multiplied(d.Dot(n)))
    if d.Magnitude() < 1e-9:
        # The chosen direction was parallel to the normal.  Take any perpendicular.
        alt = gp_Vec(0.0, 0.0, 1.0) if abs(n.Z()) < 0.9 else gp_Vec(1.0, 0.0, 0.0)
        d = alt.Subtracted(n.Multiplied(alt.Dot(n)))
    d.Normalize()
    return (d.X(), d.Y(), d.Z())


DATUM_PLANES = {
    "XY": ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
    "XZ": ((0.0, 0.0, 0.0), (0.0, -1.0, 0.0), (1.0, 0.0, 0.0)),
    "YZ": ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
}


def plane_from_face(
    face: TopoDS_Face, point: tuple[float, float, float]
) -> tuple[Plane, list[str]]:
    """Build the sketch plane for a picked face and point (§6.1)."""
    warnings: list[str] = []
    kind = surface_kind(face)
    normal = face_normal_at(face, point)
    origin = _closest_point_on_face(face, point)

    if kind != "plane":
        warnings.append(
            f"This is a {kind} face, not a flat one. Stamp uses the flat plane that "
            f"touches it where you clicked, so the walls of the feature will be "
            f"straight, not radial."
        )
    u_axis = longest_edge_direction(face, normal)
    return Plane(origin=origin, normal=normal, u_axis=u_axis), warnings


def resolve_anchor(anchor: Anchor, shape: TopoDS_Shape) -> tuple[Plane, list[str]]:
    """Turn a stored anchor into a live sketch plane."""
    if anchor.kind is AnchorKind.MESH_REGION:
        # The base mesh is immutable, so the plane fitted when the user clicked is
        # still the right answer.  There is no topology here to re-resolve against;
        # the seed point and the tolerance are kept so the region can be grown
        # again when the user changes the tolerance, which the interface does.
        if anchor.plane is None:
            raise ReferenceError(
                "This feature has no plane on the mesh. Pick the surface again."
            )
        return anchor.plane, []

    if anchor.kind is AnchorKind.DATUM:
        name = (anchor.datum or "XY").upper()
        if name not in DATUM_PLANES:
            raise ReferenceError(f"Unknown datum plane {name!r}.")
        origin, normal, u_axis = DATUM_PLANES[name]
        if anchor.datum_offset:
            origin = tuple(o + n * anchor.datum_offset for o, n in zip(origin, normal, strict=True))
        return Plane(origin=origin, normal=normal, u_axis=u_axis), []

    if anchor.face_ref is None:
        raise ReferenceError("This feature has no face to sit on. Pick a face.")

    resolved = resolve_face_ref(anchor.face_ref, shape)
    plane, warnings = plane_from_face(resolved.face, anchor.face_ref.point)
    if resolved.ambiguous:
        warnings.append(
            "Two faces on this part match this feature's anchor equally well. "
            "Check that it landed where you expect, or pick the face again."
        )
    return plane, warnings


__all__ = [
    "ACCEPT_THRESHOLD",
    "DATUM_PLANES",
    "ReferenceError",
    "ResolvedFace",
    "face_area",
    "face_center",
    "face_normal_at",
    "faces_of",
    "longest_edge_direction",
    "make_face_ref",
    "plane_from_face",
    "resolve_anchor",
    "resolve_face_ref",
    "surface_kind",
    "surface_parameters",
]
