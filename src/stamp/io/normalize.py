"""Profile normalization - spec §5.5.

Every importer (SVG, DXF, DWG) converges here.  Input is a bag of planar OCC edges
in the XY plane; output is a :class:`Profile` of nested, closed, faced loops centered
on the origin, plus an honest list of everything that is wrong with it.

The rule that shapes the whole module: **never silently drop geometry**.  An open
loop, a self-intersection, or a stroke with no fill is reported as an
:class:`Issue`, not swallowed.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.gp import gp_Pnt, gp_Trsf, gp_Vec
from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Edge, TopoDS_Face, TopoDS_Wire
from OCP.TopTools import TopTools_HSequenceOfShape

#: Default endpoint-joining tolerance, in mm (spec §5.5 step 1).
DEFAULT_JOIN_TOLERANCE = 0.01

#: Deflection used when a curve is flattened to a polyline for containment tests.
FLATTEN_DEFLECTION = 0.02


class IssueKind(StrEnum):
    OPEN_LOOP = "open_loop"
    SELF_INTERSECTION = "self_intersection"
    NO_FILL = "no_fill"
    LIVE_TEXT = "live_text"
    UNSUPPORTED_ELEMENT = "unsupported_element"
    DEGENERATE_LOOP = "degenerate_loop"
    AMBIGUOUS_UNITS = "ambiguous_units"
    EMPTY = "empty"


@dataclass
class Issue:
    """One specific, nameable problem with an imported profile - spec §10."""

    kind: IssueKind
    message: str
    blocking: bool = False
    loop_index: int | None = None
    points: list[tuple[float, float]] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


@dataclass
class Loop:
    """One closed (or stubbornly open) wire in the XY plane."""

    wire: TopoDS_Wire
    closed: bool
    area: float  # signed area of the flattened polyline
    polyline: list[tuple[float, float]]
    gap: float | None = None  # endpoint distance, when the loop will not close
    index: int = 0
    #: False when the loop crosses itself.  It is kept (the repair paths need its
    #: polyline) but it never becomes a face.
    valid: bool = True

    @property
    def winding(self) -> str:
        return "ccw" if self.area >= 0 else "cw"

    @property
    def abs_area(self) -> float:
        return abs(self.area)

    def bbox(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.polyline]
        ys = [p[1] for p in self.polyline]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class Profile:
    """Normalized artwork: nested loops, faced, centered on the origin."""

    loops: list[Loop] = field(default_factory=list)
    #: ``parent[i]`` is the index of the loop that directly contains loop *i*.
    parent: list[int] = field(default_factory=list)
    #: ``depth[i]`` - even means material, odd means hole.
    depth: list[int] = field(default_factory=list)
    faces: list[TopoDS_Face] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    source_units: str = "mm"

    @property
    def blocked(self) -> bool:
        return any(i.blocking for i in self.issues)

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def is_empty(self) -> bool:
        return not self.faces

    def compound(self) -> TopoDS_Compound:
        """All faces as one compound - a five-character serial is five faces."""
        comp = TopoDS_Compound()
        builder = BRep_Builder()
        builder.MakeCompound(comp)
        for f in self.faces:
            builder.Add(comp, f)
        return comp

    def issues_of(self, kind: IssueKind) -> list[Issue]:
        return [i for i in self.issues if i.kind is kind]


# --------------------------------------------------------------------- flatten


def flatten_wire(wire: TopoDS_Wire, deflection: float = FLATTEN_DEFLECTION) -> list[tuple[float, float]]:
    """Discretize a wire into an XY polyline, following edge order and orientation."""
    points: list[tuple[float, float]] = []
    explorer = TopExp_Explorer(wire, TopAbs_ShapeEnum.TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        pts = flatten_edge(edge, deflection)
        if points and pts and _close(points[-1], pts[0], 1e-7):
            pts = pts[1:]
        points.extend(pts)
        explorer.Next()
    return points


def flatten_edge(edge: TopoDS_Edge, deflection: float = FLATTEN_DEFLECTION) -> list[tuple[float, float]]:
    from OCP.TopAbs import TopAbs_Orientation

    curve = BRepAdaptor_Curve(edge)
    try:
        sampler = GCPnts_QuasiUniformDeflection(curve, deflection)
        if not sampler.IsDone():
            raise RuntimeError
        pts = [sampler.Value(i) for i in range(1, sampler.NbPoints() + 1)]
    except Exception:  # a degenerate or tiny edge - fall back to its two ends
        first, last = BRep_Tool.Range_s(edge)
        pts = [curve.Value(first), curve.Value(last)]
    out = [(p.X(), p.Y()) for p in pts]
    if edge.Orientation() == TopAbs_Orientation.TopAbs_REVERSED:
        out.reverse()
    return out


def polygon_area(points: Sequence[tuple[float, float]]) -> float:
    """Signed area by the shoelace formula.  Positive means counter-clockwise."""
    n = len(points)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % n]
        total += x0 * y1 - x1 * y0
    return total / 2.0


def point_in_polygon(point: tuple[float, float], polygon: Sequence[tuple[float, float]]) -> bool:
    """Ray casting.  Points exactly on the boundary are not guaranteed either way,
    which is fine - callers test an interior sample point."""
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            t = (y - y0) / (y1 - y0)
            if x < x0 + t * (x1 - x0):
                inside = not inside
    return inside


def _close(a: tuple[float, float], b: tuple[float, float], tol: float) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= tol


def representative_point(polygon: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """A point that is inside *polygon*, even when it is concave."""
    if len(polygon) < 3:
        return polygon[0] if polygon else (0.0, 0.0)
    # Try the centroid first, then midpoints of vertex pairs.
    cx = sum(p[0] for p in polygon) / len(polygon)
    cy = sum(p[1] for p in polygon) / len(polygon)
    if point_in_polygon((cx, cy), polygon):
        return (cx, cy)
    for i in range(len(polygon)):
        a = polygon[i]
        b = polygon[(i + len(polygon) // 2) % len(polygon)]
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        if point_in_polygon(mid, polygon):
            return mid
    return (cx, cy)


# ------------------------------------------------------------------- pipeline


def normalize(
    edges: Iterable[TopoDS_Edge],
    *,
    join_tolerance: float = DEFAULT_JOIN_TOLERANCE,
    issues: list[Issue] | None = None,
    center: bool = True,
    close_open_loops: bool = False,
    source_units: str = "mm",
) -> Profile:
    """Run the §5.5 pipeline over a bag of planar edges, nested as one group."""
    return normalize_groups(
        [edges],
        join_tolerance=join_tolerance,
        issues=issues,
        center=center,
        close_open_loops=close_open_loops,
        source_units=source_units,
    )


def normalize_groups(
    groups: Sequence[Iterable[TopoDS_Edge]],
    *,
    join_tolerance: float = DEFAULT_JOIN_TOLERANCE,
    issues: list[Issue] | None = None,
    center: bool = True,
    close_open_loops: bool = False,
    source_units: str = "mm",
) -> Profile:
    """Run the §5.5 pipeline, nesting each group independently.

    Grouping matters for SVG.  A filled circle drawn on top of a filled rectangle is
    *material*, not a hole, even though it sits inside the rectangle - the fill rule
    of each element already decided that.  Every SVG element is therefore its own
    group.  DXF has no such notion, so it arrives as a single group and containment
    alone decides holes, which is exactly what §5.5 step 3 describes.

    *issues* lets an importer pass in problems it already found (live text, ignored
    gradients) so the caller gets one list.
    """
    cleaned = [[e for e in group if not e.IsNull()] for group in groups]
    cleaned = [g for g in cleaned if g]
    if not cleaned:
        profile = Profile(issues=list(issues or []), source_units=source_units)
        profile.issues.append(
            Issue(IssueKind.EMPTY, "No geometry was found in this file.", blocking=True)
        )
        return profile

    wire_groups = [_connect_edges_to_wires(group, join_tolerance) for group in cleaned]
    return normalize_wire_groups(
        wire_groups,
        join_tolerance=join_tolerance,
        issues=issues,
        center=center,
        close_open_loops=close_open_loops,
        source_units=source_units,
    )


def normalize_wire_groups(
    groups: Sequence[Sequence[TopoDS_Wire]],
    *,
    join_tolerance: float = DEFAULT_JOIN_TOLERANCE,
    issues: list[Issue] | None = None,
    center: bool = True,
    close_open_loops: bool = False,
    source_units: str = "mm",
    resolve_overlaps: bool = True,
) -> Profile:
    """The back half of the pipeline, for callers that already have closed wires.

    Two contours that touch at a single point - the two lobes of a repaired bow tie,
    for example - must not be re-joined into one wire, so this entry point skips the
    edge-connecting step entirely.
    """
    profile = Profile(issues=list(issues or []), source_units=source_units)

    for wires in groups:
        start = len(profile.loops)
        loops = _make_loops(list(wires), join_tolerance, profile, close_open_loops, start)
        _nest(profile, loops, offset=start)
        profile.loops.extend(loops)

    if not profile.loops:
        profile.issues.append(
            Issue(IssueKind.EMPTY, "No closed shapes could be built from this file.", blocking=True)
        )
        return profile

    _faceify(profile)
    if resolve_overlaps:
        profile = _resolve_overlaps(profile)
    if center:
        _center(profile)
    _bbox(profile)
    return profile


def _connect_edges_to_wires(edges: list[TopoDS_Edge], tol: float) -> list[TopoDS_Wire]:
    seq = TopTools_HSequenceOfShape()
    for e in edges:
        seq.Append(e)
    out = TopTools_HSequenceOfShape()
    ShapeAnalysis_FreeBounds.ConnectEdgesToWires_s(seq, tol, False, out)
    return [TopoDS.Wire_s(out.Value(i)) for i in range(1, out.Length() + 1)]


def _wire_endpoints(wire: TopoDS_Wire) -> tuple[gp_Pnt, gp_Pnt] | None:
    from OCP.TopoDS import TopoDS_Vertex

    v1 = TopoDS_Vertex()
    v2 = TopoDS_Vertex()
    TopExp.Vertices_s(wire, v1, v2)
    if v1.IsNull() or v2.IsNull():
        return None
    return BRep_Tool.Pnt_s(v1), BRep_Tool.Pnt_s(v2)


def _make_loops(
    wires: list[TopoDS_Wire],
    tol: float,
    profile: Profile,
    close_open_loops: bool,
    index_offset: int = 0,
) -> list[Loop]:
    loops: list[Loop] = []
    for wire in wires:
        polyline = flatten_wire(wire)
        if len(polyline) < 2:
            continue

        closed = bool(wire.Closed())
        gap = None
        if not closed:
            ends = _wire_endpoints(wire)
            if ends is not None:
                gap = ends[0].Distance(ends[1])
                closed = gap <= tol
            if not closed and gap is not None:
                if close_open_loops:
                    wire = _close_wire(wire)
                    closed = True
                    polyline = flatten_wire(wire)
                    profile.issues.append(
                        Issue(
                            IssueKind.OPEN_LOOP,
                            f"A loop had a {gap:.3f} mm gap. Stamp closed it with a straight line.",
                            blocking=False,
                            loop_index=index_offset + len(loops),
                            points=[polyline[0], polyline[-1]],
                            detail={"gap": gap},
                        )
                    )
                else:
                    profile.issues.append(
                        Issue(
                            IssueKind.OPEN_LOOP,
                            f"A loop does not close. The gap is {gap:.3f} mm. "
                            f"Close it, outline the stroke, or discard the loop.",
                            blocking=True,
                            loop_index=index_offset + len(loops),
                            points=[polyline[0], polyline[-1]],
                            detail={"gap": gap},
                        )
                    )

        area = polygon_area(polyline)
        if closed and abs(area) < 1e-9:
            # Zero signed area is the signature of a bow tie: the two lobes cancel.
            # Report it as what it is before dismissing it as degenerate.
            crossings = self_intersections(polyline)
            if not crossings:
                profile.issues.append(
                    Issue(
                        IssueKind.DEGENERATE_LOOP,
                        "A closed loop has no area and cannot be extruded.",
                        blocking=False,
                        loop_index=index_offset + len(loops),
                    )
                )
                continue
        else:
            crossings = self_intersections(polyline) if closed else []

        if crossings:
            profile.issues.append(
                _self_intersection_issue(index_offset + len(loops), crossings)
            )

        loops.append(
            Loop(
                wire=wire,
                closed=closed,
                area=area,
                polyline=polyline,
                gap=gap,
                index=index_offset + len(loops),
                valid=not crossings,
            )
        )
    return loops


def _self_intersection_issue(index: int, points: list[tuple[float, float]]) -> Issue:
    return Issue(
        IssueKind.SELF_INTERSECTION,
        f"This loop crosses itself at {len(points)} "
        f"point{'s' if len(points) != 1 else ''}. Repair the artwork, or choose "
        f"'Union overlapping loops' to merge it.",
        blocking=True,
        loop_index=index,
        points=points[:32],
    )


#: Above this many polyline points the O(n^2) self-intersection test is skipped.
#: Traced artwork that large is checked by BRepCheck_Analyzer on the face instead.
SELF_INTERSECT_POINT_LIMIT = 2000


def self_intersections(polyline: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Return the points where a closed polyline crosses itself.

    Neighbouring segments share an endpoint and are skipped, so only genuine
    crossings are reported.
    """
    n = len(polyline)
    if n < 4 or n > SELF_INTERSECT_POINT_LIMIT:
        return []
    hits: list[tuple[float, float]] = []
    for i in range(n):
        a0 = polyline[i]
        a1 = polyline[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue  # the two segments that meet at the seam
            b0 = polyline[j]
            b1 = polyline[(j + 1) % n]
            hit = _segment_intersection(a0, a1, b0, b1)
            if hit is not None:
                hits.append(hit)
    return hits


def _segment_intersection(p0, p1, q0, q1, eps: float = 1e-9):
    rx, ry = p1[0] - p0[0], p1[1] - p0[1]
    sx, sy = q1[0] - q0[0], q1[1] - q0[1]
    denom = rx * sy - ry * sx
    if abs(denom) < eps:
        return None
    tx, ty = q0[0] - p0[0], q0[1] - p0[1]
    t = (tx * sy - ty * sx) / denom
    u = (tx * ry - ty * rx) / denom
    if eps < t < 1.0 - eps and eps < u < 1.0 - eps:
        return (p0[0] + t * rx, p0[1] + t * ry)
    return None


def _close_wire(wire: TopoDS_Wire) -> TopoDS_Wire:
    ends = _wire_endpoints(wire)
    if ends is None:
        return wire
    p0, p1 = ends
    if p0.Distance(p1) < 1e-9:
        return wire
    bridge = BRepBuilderAPI_MakeEdge(p1, p0).Edge()
    maker = BRepBuilderAPI_MakeWire()
    maker.Add(wire)
    maker.Add(bridge)
    return maker.Wire() if maker.IsDone() else wire


def _nest(profile: Profile, loops: list[Loop], offset: int) -> None:
    """Even depth = material, odd depth = hole (spec §5.5 step 3).

    Containment is tested only inside one group, so *parent* indices are always
    within the group.  They are then shifted by *offset* into profile-wide indices.
    """
    n = len(loops)
    parent = [-1] * n
    depth = [0] * n

    # A loop is contained by the smallest loop of its group that encloses it.
    samples = [representative_point(loop.polyline) for loop in loops]
    for i in range(n):
        best = -1
        best_area = float("inf")
        for j in range(n):
            if i == j or not loops[j].closed:
                continue
            if loops[j].abs_area <= loops[i].abs_area:
                continue
            if point_in_polygon(samples[i], loops[j].polyline):
                if loops[j].abs_area < best_area:
                    best = j
                    best_area = loops[j].abs_area
        parent[i] = best

    for i in range(n):
        d = 0
        p = parent[i]
        guard = 0
        while p >= 0 and guard < n + 1:
            d += 1
            p = parent[p]
            guard += 1
        depth[i] = d

    profile.parent.extend([(p + offset) if p >= 0 else -1 for p in parent])
    profile.depth.extend(depth)


def _faceify(profile: Profile) -> None:
    """Outer wires become faces; their direct children become holes (§5.5 step 4)."""
    loops = profile.loops
    children: dict[int, list[int]] = {i: [] for i in range(len(loops))}
    for i, p in enumerate(profile.parent):
        if p >= 0:
            children[p].append(i)

    faces: list[TopoDS_Face] = []
    for i, loop in enumerate(loops):
        if profile.depth[i] % 2 != 0 or not loop.closed or not loop.valid:
            continue  # a hole, something that never closed, or a reported crossing
        maker = BRepBuilderAPI_MakeFace(_oriented(loop.wire, ccw=True))
        if not maker.IsDone():
            profile.issues.append(
                Issue(
                    IssueKind.DEGENERATE_LOOP,
                    "A loop could not be turned into a face.",
                    blocking=False,
                    loop_index=i,
                )
            )
            continue
        for c in children[i]:
            if loops[c].closed:
                maker.Add(_oriented(loops[c].wire, ccw=False))
        face = maker.Face()
        if not BRepCheck_Analyzer(face).IsValid():
            already = any(
                iss.kind is IssueKind.SELF_INTERSECTION and iss.loop_index == i
                for iss in profile.issues
            )
            if not already:
                profile.issues.append(
                    Issue(
                        IssueKind.SELF_INTERSECTION,
                        "This loop crosses itself. Repair the artwork, or choose "
                        "'Union overlapping loops' to merge it.",
                        blocking=True,
                        loop_index=i,
                        points=_flatten_sample(loops[i].polyline),
                    )
                )
            continue
        faces.append(face)
    profile.faces = faces


def _oriented(wire: TopoDS_Wire, *, ccw: bool) -> TopoDS_Wire:
    """Return the wire with the winding a face outer/inner boundary expects."""

    area = polygon_area(flatten_wire(wire))
    want_positive = ccw
    if (area >= 0) == want_positive:
        return wire
    reversed_wire = TopoDS.Wire_s(wire.Reversed())
    return reversed_wire


def _flatten_sample(polyline: list[tuple[float, float]], limit: int = 16) -> list[tuple[float, float]]:
    if len(polyline) <= limit:
        return list(polyline)
    step = len(polyline) // limit
    return polyline[::step][:limit]


def _center(profile: Profile) -> None:
    """Translate so the bounding-box center sits at the origin (§5.5 step 6)."""
    _bbox(profile)
    x0, y0, x1, y1 = profile.bbox
    dx = -(x0 + x1) / 2.0
    dy = -(y0 + y1) / 2.0
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return

    trsf = gp_Trsf()
    trsf.SetTranslation(gp_Vec(dx, dy, 0.0))
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform

    profile.faces = [
        TopoDS.Face_s(BRepBuilderAPI_Transform(f, trsf, True).Shape()) for f in profile.faces
    ]
    for loop in profile.loops:
        loop.wire = TopoDS.Wire_s(BRepBuilderAPI_Transform(loop.wire, trsf, True).Shape())
        loop.polyline = [(x + dx, y + dy) for x, y in loop.polyline]


def _bbox(profile: Profile) -> None:
    """Set the profile bounding box, exactly where possible.

    The faces give the true extent; the flattened polylines only approximate a curve
    to within the flattening deflection.  The size fields in the properties panel are
    what the user types exact numbers into, so they must not inherit that error.
    """
    if profile.faces:
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib

        box = Bnd_Box()
        box.SetGap(0.0)
        for face in profile.faces:
            BRepBndLib.AddOptimal_s(face, box, True, False)
        if not box.IsVoid():
            x0, y0, _z0, x1, y1, _z1 = box.Get()
            profile.bbox = (x0, y0, x1, y1)
            return

    pts = [p for loop in profile.loops for p in loop.polyline]
    if not pts:
        profile.bbox = (0.0, 0.0, 0.0, 0.0)
        return
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    profile.bbox = (min(xs), min(ys), max(xs), max(ys))


def _faces_overlap(profile: Profile) -> bool:
    """Cheap bounding-box test for faces that cover the same ground."""
    boxes = []
    for face in profile.faces:
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib

        box = Bnd_Box()
        BRepBndLib.Add_s(face, box)
        boxes.append(box)
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if not boxes[i].IsOut(boxes[j]):
                return True
    return False


def _resolve_overlaps(profile: Profile) -> Profile:
    """Union faces that cover the same ground, in 2D.

    Artwork overlaps all the time: a filled circle drawn on top of a filled
    rectangle covers ground the rectangle already covers.  Extruding the two
    separately would leave a seam where the circle meets the rectangle - a flat to
    flat join with no real edge, which then shows up in the "top edges" fillet
    selection and cannot be rounded.

    Resolving it here, in 2D, gives one clean silhouette instead.  The non-zero fill
    rule does the work: outer loops wind counter-clockwise and holes clockwise, so a
    hole covered by another element correctly becomes material again.
    """
    if len(profile.faces) < 2 or not _faces_overlap(profile):
        return profile

    from manifold3d import CrossSection, FillRule

    contours: list[list[tuple[float, float]]] = []
    for i, loop in enumerate(profile.loops):
        if not loop.closed or not loop.valid or len(loop.polyline) < 3:
            continue
        want_ccw = profile.depth[i] % 2 == 0
        poly = list(loop.polyline)
        if (polygon_area(poly) >= 0) != want_ccw:
            poly.reverse()
        contours.append(poly)
    if not contours:
        return profile

    section = CrossSection(contours, FillRule.NonZero).simplify(1e-6)
    merged = _profile_from_cross_section(section, profile.issues, profile.source_units)
    if not merged.faces:
        return profile
    return merged


# ------------------------------------------------------- 2D repair helpers (§10)


def union_overlapping(profile: Profile) -> Profile:
    """Merge self-intersecting / overlapping loops with a 2D boolean.

    Uses ``manifold3d.CrossSection``, which is also what the stroke-outlining path
    uses, so the two repairs share one implementation of "polygons in, polygons out".
    """
    from manifold3d import CrossSection, FillRule

    polys = [loop.polyline for loop in profile.loops if loop.closed and len(loop.polyline) >= 3]
    polys = [p for p in polys if abs(polygon_area(p)) > 0.0 or self_intersections(p)]
    if not polys:
        return profile
    section = CrossSection(polys, FillRule.NonZero).simplify(1e-6)
    return _profile_from_cross_section(section, profile.issues, profile.source_units)


#: Number of segments used to draw a round join or cap when outlining a stroke.
STROKE_CAP_SEGMENTS = 24


def outline_strokes(
    polylines: Sequence[Sequence[tuple[float, float]]],
    width_mm: float,
    *,
    closed: Sequence[bool] | None = None,
) -> Profile:
    """Give area to stroke-only artwork by thickening each path to *width_mm*.

    ``CrossSection.offset`` cannot do this: an open path is a zero-area contour and
    Clipper discards it.  So the ribbon is built directly - one rectangle per
    segment, one disc per vertex for the round joins and caps - and the pieces are
    unioned with the non-zero fill rule.
    """
    from manifold3d import CrossSection, FillRule

    if width_mm <= 0:
        raise ValueError("Stroke width must be greater than zero.")

    half = width_mm / 2.0
    pieces: list[list[tuple[float, float]]] = []
    for poly in polylines:
        pts = _dedupe(list(poly))
        if len(pts) < 2:
            if pts:
                pieces.append(_disc(pts[0], half))
            continue
        for a, b in zip(pts, pts[1:], strict=False):
            rect = _segment_rectangle(a, b, half)
            if rect:
                pieces.append(rect)
        for p in pts:
            pieces.append(_disc(p, half))

    if not pieces:
        return Profile(
            issues=[Issue(IssueKind.EMPTY, "There are no strokes to outline.", blocking=True)]
        )

    section = CrossSection(pieces, FillRule.NonZero).simplify(1e-5)
    return _profile_from_cross_section(section, [], "mm")


def _dedupe(pts: list[tuple[float, float]], tol: float = 1e-9) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for p in pts:
        if not out or not _close(out[-1], p, tol):
            out.append(p)
    return out


def _segment_rectangle(a, b, half: float) -> list[tuple[float, float]] | None:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return None
    nx, ny = -dy / length * half, dx / length * half
    # Counter-clockwise, to match the discs.  Mixed winding would cancel under the
    # non-zero fill rule and punch holes where the pieces overlap.
    return [
        (a[0] - nx, a[1] - ny),
        (b[0] - nx, b[1] - ny),
        (b[0] + nx, b[1] + ny),
        (a[0] + nx, a[1] + ny),
    ]


def _disc(center, radius: float, segments: int = STROKE_CAP_SEGMENTS) -> list[tuple[float, float]]:
    cx, cy = center
    return [
        (
            cx + radius * math.cos(2.0 * math.pi * i / segments),
            cy + radius * math.sin(2.0 * math.pi * i / segments),
        )
        for i in range(segments)
    ]


def _profile_from_cross_section(section, issues: list[Issue], units: str) -> Profile:
    """Convert a manifold3d CrossSection back into OCC wires and faces.

    Every contour becomes one wire and they are nested as a single group, so an
    outer contour and its holes still resolve correctly.
    """
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon

    wires: list[TopoDS_Wire] = []
    for contour in section.to_polygons():
        pts = [(float(p[0]), float(p[1])) for p in contour]
        if len(pts) < 3:
            continue
        maker = BRepBuilderAPI_MakePolygon()
        for x, y in pts:
            maker.Add(gp_Pnt(x, y, 0.0))
        maker.Close()
        if maker.IsDone():
            wires.append(maker.Wire())

    kept = [i for i in issues if i.kind is not IssueKind.SELF_INTERSECTION]
    # The cross section is already a resolved region, so re-running the overlap pass
    # on it would only recurse back into this function.
    return normalize_wire_groups(
        [wires], issues=kept, source_units=units, resolve_overlaps=False
    )
