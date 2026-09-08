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


#: The component key used when the source draws no distinction - a DXF, or an SVG
#: in a single colour.  One component covering everything behaves exactly as the
#: profile did before components existed.
SINGLE_COMPONENT = "all"

#: A component covering at least this much of the artwork's bounding box, with
#: every other component inside it, is a background layer rather than a part of
#: the drawing.  Well clear of a border or a backing plate, which leave the
#: middle empty and so cover far less than their box.
BACKGROUND_COVERAGE = 0.95

#: A hole in a candidate backdrop counts towards its coverage when other
#: components fill at least this much of it.  A backdrop the exporter carved the
#: drawing out of has holes that *are* the drawing, so they come out near 1.0; a
#: border's empty middle, with a small drawing floating in it, is nowhere near.
FILLED_HOLE_SHARE = 0.75


@dataclass
class Component:
    """One addressable piece of the artwork - spec §5.5, §9.

    An SVG is grouped by fill colour: every black path is one component, every
    red path another.  That is how artwork is drawn and how a two-colour print is
    described, and it keeps the list short enough to pick from - a detailed logo
    has hundreds of paths and three colours.

    The key is the source colour, so it survives reopening the file and a
    re-import after the artwork is edited.  What colour it is *printed* in is not
    here: that is the user's choice, and it lives on the feature.
    """

    key: str = SINGLE_COMPONENT
    #: The colour the source drew it in, as ``#rrggbb``.  Empty when the source
    #: has no notion of colour.
    color: str = ""
    label: str = "Artwork"
    element_count: int = 0
    #: True when this was a filled backdrop the artwork sits on.  Those are
    #: dropped on import; see :func:`detect_background`.
    background: bool = False

    def to_dict(self) -> dict:
        return {
            "key": self.key, "color": self.color, "label": self.label,
            "element_count": self.element_count, "background": self.background,
        }


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
    #: The pieces the artwork divides into, in the order the source paints them.
    components: list[Component] = field(default_factory=list)
    #: ``face_component[i]`` is the key of the component face *i* belongs to.
    face_component: list[str] = field(default_factory=list)
    #: ``loop_component[i]`` is the same, for loops.  The background detector
    #: measures loops, because a component's area is its outlines, not its faces.
    loop_component: list[str] = field(default_factory=list)
    #: The backdrop the importer left out, kept here so the panel can name it and
    #: offer to put it back.  It is not in :attr:`components` - it is not part of
    #: the artwork any more.
    dropped_background: Component | None = None

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

    def component(self, key: str) -> Component | None:
        return next((c for c in self.components if c.key == key), None)

    def component_keys(self) -> list[str]:
        """The component keys, in paint order.  Never empty for a faced profile."""
        return [c.key for c in self.components] or ([SINGLE_COMPONENT] if self.faces else [])

    def faces_of(self, key: str) -> list[TopoDS_Face]:
        """The faces belonging to one component.

        An untagged profile - anything built before components, or from a source
        with no colour - answers with all of its faces for the single key, so
        callers need no special case.
        """
        if not self.face_component:
            return list(self.faces) if key in (SINGLE_COMPONENT, "") else []
        return [f for f, k in zip(self.faces, self.face_component, strict=False) if k == key]

    def loop_keys(self) -> list[str]:
        """The component key of every loop, one per loop."""
        if len(self.loop_component) == len(self.loops):
            return list(self.loop_component)
        return [SINGLE_COMPONENT] * len(self.loops)

    def compound_of(self, key: str) -> TopoDS_Compound:
        """One component's faces as a compound, for building its own tool solid."""
        comp = TopoDS_Compound()
        builder = BRep_Builder()
        builder.MakeCompound(comp)
        for f in self.faces_of(key):
            builder.Add(comp, f)
        return comp


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
    group_components: Sequence[Component] | None = None,
) -> Profile:
    """Run the §5.5 pipeline, nesting each group independently.

    Grouping matters for SVG.  A filled circle drawn on top of a filled rectangle is
    *material*, not a hole, even though it sits inside the rectangle - the fill rule
    of each element already decided that.  Every SVG element is therefore its own
    group.  DXF has no such notion, so it arrives as a single group and containment
    alone decides holes, which is exactly what §5.5 step 3 describes.

    *issues* lets an importer pass in problems it already found (live text, ignored
    gradients) so the caller gets one list.  *group_components* says which piece of
    the artwork each group belongs to, one entry per group; without it the whole
    profile is one component.
    """
    paired = list(zip(groups, group_components or [], strict=False))
    if group_components is None:
        paired = [(group, None) for group in groups]
    cleaned: list[list[TopoDS_Edge]] = []
    kept_components: list[Component | None] = []
    for group, component in paired:
        edges = [e for e in group if not e.IsNull()]
        if edges:
            cleaned.append(edges)
            kept_components.append(component)
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
        group_components=None if group_components is None else kept_components,
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
    group_components: Sequence[Component | None] | None = None,
) -> Profile:
    """The back half of the pipeline, for callers that already have closed wires.

    Two contours that touch at a single point - the two lobes of a repaired bow tie,
    for example - must not be re-joined into one wire, so this entry point skips the
    edge-connecting step entirely.
    """
    profile = Profile(issues=list(issues or []), source_units=source_units)
    loop_component: list[str] = []

    for index, wires in enumerate(groups):
        start = len(profile.loops)
        loops = _make_loops(list(wires), join_tolerance, profile, close_open_loops, start)
        _nest(profile, loops, offset=start)
        profile.loops.extend(loops)
        component = None if group_components is None else group_components[index]
        key = component.key if component is not None else SINGLE_COMPONENT
        loop_component.extend([key] * len(loops))
        if component is not None and profile.component(key) is None:
            profile.components.append(component)

    if not profile.loops:
        profile.issues.append(
            Issue(IssueKind.EMPTY, "No closed shapes could be built from this file.", blocking=True)
        )
        return profile

    profile.loop_component = list(loop_component)
    _faceify(profile, loop_component)
    # Before resolving, not after.  Resolving carves the artwork out of whatever
    # is under it, so by then a backdrop has become a thin frame and no longer
    # looks like one.
    background = detect_background(profile)
    if background is not None:
        background.background = True
    if resolve_overlaps:
        profile = _resolve_overlaps(profile, loop_component)
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


def _faceify(profile: Profile, loop_component: Sequence[str] | None = None) -> None:
    """Outer wires become faces; their direct children become holes (§5.5 step 4)."""
    loops = profile.loops
    children: dict[int, list[int]] = {i: [] for i in range(len(loops))}
    for i, p in enumerate(profile.parent):
        if p >= 0:
            children[p].append(i)

    faces: list[TopoDS_Face] = []
    face_component: list[str] = []
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
        if loop_component is not None and i < len(loop_component):
            face_component.append(loop_component[i])
    profile.faces = faces
    profile.face_component = face_component if len(face_component) == len(faces) else []


def _kept_issues(issues: Sequence[Issue], drop_crossing_issues: bool) -> list[Issue]:
    """The issues a rebuilt profile inherits.

    A self-intersection is only answered by the 2D union, so only the union drops
    those.  Merely *resolving* two other overlapping faces leaves the crossing
    loop exactly as broken as it was, and dropping its issue there is what made a
    bow tie disappear from a file with an overlap in it without a word.
    """
    if not drop_crossing_issues:
        return list(issues)
    return [i for i in issues if i.kind is not IssueKind.SELF_INTERSECTION]


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
    # The repair dialog draws these on top of the profile, so they move with it.
    for issue in profile.issues:
        issue.points = [(x + dx, y + dy) for x, y in issue.points]


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


def _contours_of(
    profile: Profile, keys: Sequence[str] | None = None, *, include_crossing: bool = False
):
    """Loop polylines wound the way the non-zero fill rule wants them.

    Outer loops counter-clockwise, holes clockwise, so a hole covered by another
    element correctly becomes material again.

    A loop that crosses itself has no meaningful winding and never becomes a
    face, so it is left out - except for :func:`union_overlapping`, whose whole
    job is to merge those, which asks for them with *include_crossing*.
    """
    contours: list[list[tuple[float, float]]] = []
    for i, loop in enumerate(profile.loops):
        if not loop.closed or len(loop.polyline) < 3:
            continue
        if not loop.valid:
            if not include_crossing:
                continue
            contours.append(list(loop.polyline))
            continue
        if keys is not None and (i >= len(keys) or keys[i] is None):
            continue
        want_ccw = profile.depth[i] % 2 == 0
        poly = list(loop.polyline)
        if (polygon_area(poly) >= 0) != want_ccw:
            poly.reverse()
        contours.append(poly)
    return contours


def _resolve_overlaps(profile: Profile, loop_component: Sequence[str] | None = None) -> Profile:
    """Resolve artwork that covers the same ground, in 2D.

    Extruding two overlapping elements separately leaves a seam where they meet -
    a flat-to-flat join with no real edge, which then turns up in the "top edges"
    fillet selection and cannot be rounded.  Resolving it here gives one clean
    silhouette per component instead.

    Within a component that is a union.  *Between* components it is the painter's
    rule: whatever the source draws later is on top, and is taken out of
    everything under it.  Doing this with a single union across the whole profile
    is what made a filled backdrop swallow the drawing - the artwork and the
    backdrop wind the same way, so the union of the two is just the backdrop, and
    a logo imported as a plain rectangle the size of its own page.
    """
    if len(profile.faces) < 2 or not _faces_overlap(profile):
        return profile

    from manifold3d import CrossSection, FillRule

    order = [c.key for c in profile.components]
    if loop_component is None or len(order) < 2:
        contours = _contours_of(profile)
        if not contours:
            return profile
        section = CrossSection(contours, FillRule.NonZero).simplify(1e-6)
        # Not centred: the crossing loops carried back in are still in source
        # coordinates, and re-centring the merged faces on their own bbox first
        # would slide them out from under those.  normalize_wire_groups centres
        # the whole thing once this returns.
        merged = _profile_from_cross_section(
            section, profile.issues, profile.source_units, center=False
        )
        if not merged.faces:
            return profile
        merged.components = list(profile.components)
        merged.face_component = [_only_key(profile)] * len(merged.faces)
        merged.loop_component = [_only_key(profile)] * len(merged.loops)
        return _carry_crossings(profile, merged)

    sections = {}
    for key in order:
        keys = [k if k == key else None for k in loop_component]
        contours = _contours_of(profile, keys)
        sections[key] = (
            CrossSection(contours, FillRule.NonZero).simplify(1e-6) if contours else None
        )

    parts: list[tuple[Component, Profile]] = []
    for index, key in enumerate(order):
        area = sections[key]
        if area is None:
            continue
        for later in order[index + 1:]:
            on_top = sections[later]
            if on_top is not None:
                area = area - on_top
        piece = _profile_from_cross_section(
            area, [], profile.source_units, center=False
        )
        if piece.faces:
            component = profile.component(key)
            parts.append((component, piece))

    if not parts:
        return profile
    return _carry_crossings(
        profile, _merge_components(parts, profile.issues, profile.source_units)
    )


def _carry_crossings(source: Profile, resolved: Profile) -> Profile:
    """Put the loops that cross themselves back into a resolved profile.

    Resolution rebuilds the profile from the contours that could be faced, so a
    crossing loop is not in the result at all.  It never was a face and it must
    not become one, but it has to still be there: the repair dialog points at it,
    and "union overlapping loops" is what finally merges it.  Without this it
    vanished from any file where two *other* elements happened to overlap.
    """
    keys = source.loop_keys()
    for index, loop in enumerate(source.loops):
        if loop.valid or not loop.closed:
            continue
        key = keys[index] if index < len(keys) else SINGLE_COMPONENT
        resolved.parent.append(-1)
        resolved.depth.append(0)
        if len(resolved.loop_component) == len(resolved.loops):
            resolved.loop_component.append(key)
        resolved.loops.append(loop)
        if resolved.components and resolved.component(key) is None:
            component = source.component(key)
            if component is not None:
                resolved.components.append(component)
    return resolved


def _only_key(profile: Profile) -> str:
    return profile.components[0].key if profile.components else SINGLE_COMPONENT


def _merge_components(
    parts: Sequence[tuple[Component, Profile]],
    issues: list[Issue],
    units: str,
    *,
    drop_crossing_issues: bool = False,
) -> Profile:
    """Stitch the separately resolved components back into one profile.

    Loop indices are local to each part, so ``parent`` is shifted as the loops
    are appended; everything downstream reads those as one flat list.
    """
    merged = Profile(
        issues=_kept_issues(issues, drop_crossing_issues),
        source_units=units,
    )
    for component, piece in parts:
        offset = len(merged.loops)
        merged.loops.extend(piece.loops)
        merged.parent.extend(p + offset if p >= 0 else -1 for p in piece.parent)
        merged.depth.extend(piece.depth)
        merged.faces.extend(piece.faces)
        merged.face_component.extend([component.key] * len(piece.faces))
        merged.loop_component.extend([component.key] * len(piece.loops))
        merged.components.append(component)
    return merged


def detect_background(profile: Profile) -> Component | None:
    """The component that is a filled backdrop rather than part of the drawing.

    Exporters put one in constantly - a white page rect under the logo - and it
    is the single most common reason an SVG arrives looking like a plain
    rectangle.  It is recognised by shape, not by colour or by position in the
    file: something that covers nearly the whole of its own bounding box, with
    every other component sitting inside it.  A border or a backing plate leaves
    the middle empty and so covers far less than its box, and survives.

    "Covers" has to allow for the backdrop already having the drawing cut out of
    it.  Plenty of exporters write the page as one path with the artwork as
    evenodd subpaths, so its material is the page *minus* the drawing - well
    under the coverage a backdrop is recognised by, and it used to survive as a
    solid slab the size of the whole image.  A hole another component fills is
    counted back in; see :func:`_knocked_out`.
    """
    if len(profile.components) < 2:
        return None

    keys = profile.loop_keys()
    boxes: dict[str, tuple[float, float, float, float]] = {}
    areas: dict[str, float] = {}
    material: dict[str, list[list[tuple[float, float]]]] = {}
    holes: dict[str, list[list[tuple[float, float]]]] = {}
    for component in profile.components:
        polys: list[tuple[list[tuple[float, float]], bool]] = []
        for index, (loop, key) in enumerate(zip(profile.loops, keys, strict=False)):
            if key != component.key or not loop.closed or len(loop.polyline) < 3:
                continue
            solid = index >= len(profile.depth) or profile.depth[index] % 2 == 0
            polys.append((loop.polyline, solid))
        if not polys:
            continue
        xs = [x for poly, _m in polys for x, _ in poly]
        ys = [y for poly, _m in polys for _, y in poly]
        boxes[component.key] = (min(xs), min(ys), max(xs), max(ys))
        # Holes come off, they do not add.  A border is an outer loop with the
        # middle cut out: its bounding box contains the whole drawing and its
        # outlines add up to more than the box, but the material it covers is a
        # thin frame.  Counting the hole as area called every border a backdrop.
        areas[component.key] = sum(
            abs(polygon_area(poly)) * (1 if solid else -1) for poly, solid in polys
        )
        material[component.key] = [poly for poly, solid in polys if solid]
        holes[component.key] = [poly for poly, solid in polys if not solid]

    for component in profile.components:
        box = boxes.get(component.key)
        if box is None:
            continue
        x0, y0, x1, y1 = box
        box_area = (x1 - x0) * (y1 - y0)
        if box_area <= 0:
            continue
        others = [k for k in boxes if k != component.key]
        if not others:
            continue
        covered = areas[component.key] + _knocked_out(
            holes.get(component.key, []), material, others
        )
        if covered < BACKGROUND_COVERAGE * box_area:
            continue
        if all(
            x0 <= boxes[k][0] + 1e-6 and y0 <= boxes[k][1] + 1e-6
            and x1 >= boxes[k][2] - 1e-6 and y1 >= boxes[k][3] - 1e-6
            for k in others
        ):
            return component
    return None


def _knocked_out(
    holes: Sequence[Sequence[tuple[float, float]]],
    material: dict[str, list[list[tuple[float, float]]]],
    others: Sequence[str],
) -> float:
    """How much of a candidate backdrop's holes are the drawing itself.

    A hole another component sits in is not really a hole in the backdrop - it
    is where the drawing was cut out of it, and the backdrop still covers the
    page.  A border's hole is an empty middle with something small floating in
    it, which is the case this has to go on telling apart, so a hole only counts
    when what sits in it very nearly fills it.
    """
    total = 0.0
    for hole in holes:
        hole_area = abs(polygon_area(hole))
        if hole_area <= 0:
            continue
        filled = sum(
            abs(polygon_area(poly))
            for key in others
            for poly in material.get(key, ())
            if point_in_polygon(representative_point(poly), hole)
        )
        if filled >= FILLED_HOLE_SHARE * hole_area:
            total += hole_area
    return total


# ------------------------------------------------------- 2D repair helpers (§10)


def union_overlapping(profile: Profile) -> Profile:
    """Merge self-intersecting / overlapping loops with a 2D boolean.

    Uses ``manifold3d.CrossSection``, which is also what the stroke-outlining path
    uses, so the two repairs share one implementation of "polygons in, polygons out".
    """
    from manifold3d import CrossSection, FillRule

    # Wound by depth, exactly as :func:`_resolve_overlaps` does it.  Fed the raw
    # polylines instead, a hole that happened to be drawn the same way round as
    # its outer loop counted as material under the non-zero rule and filled in.
    contours = _contours_of(profile, include_crossing=True)
    if not contours:
        return profile
    section = CrossSection(contours, FillRule.NonZero).simplify(1e-6)
    return _profile_from_cross_section(
        section, profile.issues, profile.source_units, drop_crossing_issues=True
    )


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


def _profile_from_cross_section(
    section,
    issues: list[Issue],
    units: str,
    *,
    center: bool = True,
    drop_crossing_issues: bool = False,
) -> Profile:
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

    kept = _kept_issues(issues, drop_crossing_issues)
    # The cross section is already a resolved region, so re-running the overlap pass
    # on it would only recurse back into this function.
    return normalize_wire_groups(
        [wires], issues=kept, source_units=units, resolve_overlaps=False, center=center
    )
