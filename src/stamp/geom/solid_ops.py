"""The OpenCascade B-rep path: booleans, fillets, chamfers - spec §6.3, §6.4.

Two rules run through this module:

* A boolean that fails is retried once with a fuzzy tolerance before it is reported.
* A fillet that fails never silently disappears.  The un-filleted result is kept, the
  failing edges are named, and a working radius is found by bisection so the message
  can suggest one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Edge, TopoDS_Shape
from OCP.TopTools import TopTools_ListOfShape

from stamp.core.document import EdgeRole, Modifier, ModifierKind

#: Number of bisection steps used to suggest a working fillet radius (§6.4).
BISECTION_STEPS = 6


class GeometryError(RuntimeError):
    """A modeling operation failed in a way the user must be told about."""


@dataclass
class BooleanResult:
    shape: TopoDS_Shape
    #: Edges the boolean created where the tool met the base - the blend targets (§6.4B).
    section_edges: list[TopoDS_Edge] = field(default_factory=list)
    used_fuzzy: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class ModifierResult:
    shape: TopoDS_Shape
    warnings: list[str] = field(default_factory=list)
    #: Edges that refused the requested radius, for highlighting in the viewport.
    failed_edges: list[TopoDS_Edge] = field(default_factory=list)
    #: The largest value that did work, found by bisection.  None if nothing worked.
    suggested_value: float | None = None
    applied: bool = True


# --------------------------------------------------------------------- helpers


def explore(shape: TopoDS_Shape, kind: TopAbs_ShapeEnum) -> list[TopoDS_Shape]:
    out: list[TopoDS_Shape] = []
    explorer = TopExp_Explorer(shape, kind)
    while explorer.More():
        out.append(explorer.Current())
        explorer.Next()
    return out


def edges_of(shape: TopoDS_Shape) -> list[TopoDS_Edge]:
    """Every edge, de-duplicated - TopExp_Explorer visits shared edges twice."""
    seen: list[TopoDS_Edge] = []
    for e in explore(shape, TopAbs_ShapeEnum.TopAbs_EDGE):
        edge = TopoDS.Edge_s(e)
        if not any(edge.IsSame(s) for s in seen):
            seen.append(edge)
    return seen


def volume(shape: TopoDS_Shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()


def solid_count(shape: TopoDS_Shape) -> int:
    return len(explore(shape, TopAbs_ShapeEnum.TopAbs_SOLID))


def edge_midpoint(edge: TopoDS_Edge):
    from OCP.BRep import BRep_Tool

    curve = BRepAdaptor_Curve(edge)
    first, last = BRep_Tool.Range_s(edge)
    return curve.Value((first + last) / 2.0)


def edge_length(edge: TopoDS_Edge) -> float:
    from OCP.GCPnts import GCPnts_AbscissaPoint

    try:
        return GCPnts_AbscissaPoint.Length_s(BRepAdaptor_Curve(edge))
    except Exception:
        return 0.0


def edge_tangent(edge: TopoDS_Edge):
    from OCP.BRep import BRep_Tool
    from OCP.gp import gp_Pnt, gp_Vec

    curve = BRepAdaptor_Curve(edge)
    first, last = BRep_Tool.Range_s(edge)
    p = gp_Pnt()
    v = gp_Vec()
    curve.D1((first + last) / 2.0, p, v)
    if v.Magnitude() < 1e-12:
        return (0.0, 0.0, 0.0)
    v.Normalize()
    return (v.X(), v.Y(), v.Z())


# -------------------------------------------------------------------- booleans


def boolean(
    base: TopoDS_Shape,
    tool: TopoDS_Shape,
    kind: str,
    *,
    bbox_diagonal: float = 100.0,
    collect_history: bool = True,
) -> BooleanResult:
    """Fuse or cut, with one fuzzy retry.  ``kind`` is ``"add"`` or ``"cut"``."""
    warnings: list[str] = []
    before = volume(base)

    result, section = _run_boolean(base, tool, kind, fuzzy=0.0, collect_history=collect_history)
    used_fuzzy = False
    if result is None:
        fuzzy = 1e-4 * max(bbox_diagonal, 1.0)
        result, section = _run_boolean(
            base, tool, kind, fuzzy=fuzzy, collect_history=collect_history
        )
        used_fuzzy = True
        if result is None:
            raise GeometryError(
                "The boolean failed, even with a fuzzy tolerance. The previous "
                "result was kept. Move the profile slightly, or simplify it."
            )
        warnings.append(
            f"The boolean needed a fuzzy tolerance of {fuzzy:.5f} mm to succeed."
        )

    after = volume(result)
    if kind == "cut" and abs(after - before) < 1e-9:
        warnings.append(
            "This cut does not touch the part, so nothing was removed. "
            "Check the depth and the direction."
        )
    elif kind == "add" and abs(after - before) < 1e-9:
        warnings.append(
            "This feature adds nothing. It is already inside the part."
        )

    if kind == "add" and solid_count(result) > 1:
        warnings.append(
            "This feature is not connected to the part. The result is more than one "
            "body, which will not print or machine as one piece."
        )

    return BooleanResult(
        shape=result, section_edges=section, used_fuzzy=used_fuzzy, warnings=warnings
    )


def _split_compound(shape: TopoDS_Shape) -> list[TopoDS_Shape]:
    """Expand a compound into its solids.

    A compound handed to ``SetTools`` as one entry is not fully processed - only the
    first solid takes part in the boolean.  A five-character serial number is five
    solids, so they have to go in as five list entries.
    """
    solids = explore(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    return list(solids) if solids else [shape]


def _run_boolean(base, tool, kind: str, fuzzy: float, collect_history: bool):
    op = BRepAlgoAPI_Fuse() if kind == "add" else BRepAlgoAPI_Cut()
    args = TopTools_ListOfShape()
    args.Append(base)
    tools = TopTools_ListOfShape()
    for piece in _split_compound(tool):
        tools.Append(piece)
    op.SetArguments(args)
    op.SetTools(tools)
    if fuzzy:
        op.SetFuzzyValue(fuzzy)
    # History is collected by default; SetToFillHistory only ever disables it (§3.1).
    op.SetToFillHistory(collect_history)
    try:
        op.Build()
    except Exception:
        return None, []
    if not op.IsDone():
        return None, []
    shape = op.Shape()
    if shape.IsNull() or not BRepCheck_Analyzer(shape).IsValid():
        return None, []

    section: list[TopoDS_Edge] = []
    if collect_history:
        try:
            section = [TopoDS.Edge_s(e) for e in op.SectionEdges()]
        except Exception:
            section = []
    return shape, section


# --------------------------------------------------------- edge classification


def classify_feature_edges(
    tool: TopoDS_Shape,
    direction: tuple[float, float, float],
) -> dict[EdgeRole, list[TopoDS_Edge]]:
    """Sort a tool solid's edges into top, bottom and side sets.

    Classification is by where the edge sits along the sweep, measured against the
    tool's own extent rather than against the sketch plane.  That keeps it correct
    whatever start offset the symmetric mode or the contact overlap introduced, and
    it is derived only from the profile and the placement - so it comes out
    identical on every rebuild, which is the property §8.3 asks for.
    """
    dx, dy, dz = direction
    edges = edges_of(tool)
    if not edges:
        return {EdgeRole.TOP: [], EdgeRole.BOTTOM: [], EdgeRole.SIDE: []}

    positions = []
    for edge in edges:
        p = edge_midpoint(edge)
        positions.append(p.X() * dx + p.Y() * dy + p.Z() * dz)
    low, high = min(positions), max(positions)
    span = high - low
    tol = max(span * 0.05, 1e-6)

    groups: dict[EdgeRole, list[TopoDS_Edge]] = {
        EdgeRole.TOP: [],
        EdgeRole.BOTTOM: [],
        EdgeRole.SIDE: [],
    }
    for edge, t in zip(edges, positions, strict=True):
        tangent = edge_tangent(edge)
        along = abs(tangent[0] * dx + tangent[1] * dy + tangent[2] * dz)
        if along > 0.9:
            groups[EdgeRole.SIDE].append(edge)
        elif t > high - tol:
            groups[EdgeRole.TOP].append(edge)
        elif t < low + tol:
            groups[EdgeRole.BOTTOM].append(edge)
        else:
            groups[EdgeRole.SIDE].append(edge)
    return groups



def edges_reach_shape(
    edges: list[TopoDS_Edge], reference: TopoDS_Shape, margin: float
) -> bool:
    """Is any edge near enough to *reference* to change it?

    A cut tool is extended past the part, thus the edges of its end caps sit far
    outside the material.  A fillet there succeeds on the tool and changes nothing
    in the result.  This reports that condition so the caller can say so.

    The test is box against box, not point against box: a side edge of a through
    cut is longer than the part and crosses it, but its midpoint is outside.
    """
    if not edges:
        return False
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    part_box = Bnd_Box()
    BRepBndLib.Add_s(reference, part_box)
    if part_box.IsVoid():
        return True
    part_box.Enlarge(margin)
    for edge in edges:
        edge_box = Bnd_Box()
        BRepBndLib.Add_s(edge, edge_box)
        if edge_box.IsVoid():
            continue
        if not part_box.IsOut(edge_box):
            return True
    return False


def select_edges(
    tool: TopoDS_Shape,
    modifier: Modifier,
    direction: tuple[float, float, float],
) -> list[TopoDS_Edge]:
    """Resolve a modifier's :class:`EdgeSelector` against a tool solid."""
    role = modifier.target.role
    groups = classify_feature_edges(tool, direction)

    if role is EdgeRole.ALL:
        return edges_of(tool)
    if role in (EdgeRole.TOP, EdgeRole.BOTTOM, EdgeRole.SIDE):
        return groups[role]
    if role is EdgeRole.MANUAL:
        return _resolve_picks(tool, modifier.target.picks)
    return []


def _resolve_picks(shape: TopoDS_Shape, picks: list[dict]) -> list[TopoDS_Edge]:
    """Nearest-match resolution for manually picked edges (§8.3, geometric kind)."""
    candidates = edges_of(shape)
    out: list[TopoDS_Edge] = []
    for pick in picks:
        mid = pick.get("midpoint")
        if not mid:
            continue
        want_len = pick.get("length", 0.0)
        best = None
        best_score = float("inf")
        for edge in candidates:
            p = edge_midpoint(edge)
            d = math.dist((p.X(), p.Y(), p.Z()), tuple(mid))
            score = d + 0.1 * abs(edge_length(edge) - want_len)
            if score < best_score:
                best_score = score
                best = edge
        if best is not None and best_score < max(want_len * 0.5, 1.0):
            out.append(best)
    return out


# --------------------------------------------------------------- blend edges


def find_blend_edges(
    result: TopoDS_Shape,
    section_edges: list[TopoDS_Edge],
    tool: TopoDS_Shape | None = None,
    direction: tuple[float, float, float] | None = None,
    *,
    tolerance: float = 1e-4,
) -> list[TopoDS_Edge]:
    """Find the edges where the feature meets the base surface - the §6.4B targets.

    The primary source is the boolean's own history: ``SectionEdges()`` returns the
    intersection curves, which are exactly the blend edges.  It only returns them
    when the tool genuinely crosses the base surface, which is why the tool starts a
    hair behind the sketch plane (see ``tool_solid.contact_overlap_for``) - with an
    exactly coplanar contact the list comes back empty.

    Section edges are filtered to those that survive into the result.  When the list
    is empty anyway, the fallback derives them from the tool's own bottom edges,
    which come straight from the profile and so are deterministic across rebuilds.
    """
    result_edges = edges_of(result)
    kept = [e for e in section_edges if any(e.IsSame(r) for r in result_edges)]
    if kept:
        return kept
    if tool is None or direction is None:
        return []

    bottom = classify_feature_edges(tool, direction)[EdgeRole.BOTTOM]
    if not bottom:
        return []
    wanted = []
    for edge in bottom:
        p = edge_midpoint(edge)
        wanted.append((p.X(), p.Y(), p.Z(), edge_length(edge)))

    out: list[TopoDS_Edge] = []
    for edge in result_edges:
        p = edge_midpoint(edge)
        length = edge_length(edge)
        for wx, wy, wz, wlen in wanted:
            if (
                math.dist((p.X(), p.Y(), p.Z()), (wx, wy, wz)) <= tolerance
                and abs(length - wlen) <= max(tolerance, wlen * 1e-3)
            ):
                out.append(edge)
                break
    return out


# ------------------------------------------------------------ fillet / chamfer


def apply_modifier(
    shape: TopoDS_Shape,
    modifier: Modifier,
    edges: list[TopoDS_Edge],
    *,
    label: str = "",
) -> ModifierResult:
    """Fillet or chamfer *edges*, and degrade visibly when it will not work."""
    if not modifier.enabled:
        return ModifierResult(shape=shape, applied=False)
    if not edges:
        return ModifierResult(
            shape=shape,
            applied=False,
            warnings=[
                f"{label or modifier.label}: no edges matched this selection, so "
                f"nothing was rounded."
            ],
        )
    if modifier.value <= 0:
        return ModifierResult(shape=shape, applied=False)

    ok, result = _try_modifier(shape, modifier, edges, modifier.value)
    if ok:
        return ModifierResult(shape=result, applied=True)

    # It failed.  Find out which edges are to blame, and a value that would work.
    failed = _failing_edges(shape, modifier, edges)
    suggested, exact = _largest_working_value(shape, modifier, edges)

    word = "radius" if modifier.kind is ModifierKind.FILLET else "distance"
    detail = smallest_detail(edges)
    name = label or modifier.label
    if suggested is not None and exact:
        message = (
            f"{name}: a {word} of {modifier.value:g} mm is too large for this "
            f"artwork. The largest that works is about {suggested:.3f} mm. The "
            f"smallest detail here is {detail:.3f} mm. The part is shown without it."
        )
    elif suggested is not None:
        message = (
            f"{name}: a {word} of {modifier.value:g} mm is too large for this "
            f"artwork. A {word} of {suggested:.3f} mm works on every edge. The "
            f"smallest detail here is {detail:.3f} mm. The part is shown without it."
        )
    else:
        message = (
            f"{name}: this artwork is too fine for a {word}. The smallest detail "
            f"here is {detail:.3f} mm, and every edge must accept the same {word}. "
            f"Make the artwork larger, or make it simpler. The part is shown "
            f"without it."
        )
    return ModifierResult(
        shape=shape,
        applied=False,
        warnings=[message],
        failed_edges=failed,
        suggested_value=suggested,
    )


def _try_modifier(
    shape: TopoDS_Shape, modifier: Modifier, edges: list[TopoDS_Edge], value: float
) -> tuple[bool, TopoDS_Shape]:
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet

    if value <= 0:
        return False, shape
    try:
        if modifier.kind is ModifierKind.FILLET:
            maker = BRepFilletAPI_MakeFillet(shape)
            for edge in edges:
                maker.Add(value, edge)
        else:
            maker = BRepFilletAPI_MakeChamfer(shape)
            for edge in edges:
                maker.Add(value, edge)
        maker.Build()
        if not maker.IsDone():
            return False, shape
        result = maker.Shape()
        if result.IsNull() or not BRepCheck_Analyzer(result).IsValid():
            return False, shape
        return True, result
    except Exception:
        return False, shape


#: Above this many edges, the per-edge blame loop costs more than it is worth.
MAX_BLAMED_EDGES = 24

#: Below this, a fillet is not worth putting on a drawing, in millimeters.
MIN_USEFUL_VALUE = 0.01


def _failing_edges(
    shape: TopoDS_Shape, modifier: Modifier, edges: list[TopoDS_Edge]
) -> list[TopoDS_Edge]:
    """Which edges individually refuse the requested value.

    Each test builds a complete fillet, thus a profile with many edges makes this
    the slowest step of a rebuild.  Above :data:`MAX_BLAMED_EDGES` the highlight is
    not worth the wait, and the message alone has to be sufficient.
    """
    if len(edges) > MAX_BLAMED_EDGES:
        return []
    failed = []
    for edge in edges:
        ok, _ = _try_modifier(shape, modifier, [edge], modifier.value)
        if not ok:
            failed.append(edge)
    return failed


#: A fillet is built for every edge at once.  One edge that cannot take the value
#: fails the whole build, thus the value has to suit the smallest detail present.
#: The result is uniform either way: every edge is rounded, or none is.

#: Up to this many edges a build is under about two seconds, so the exact answer
#: is worth searching for.  Above it, one build alone is ten seconds and a user
#: is waiting, thus the answer is estimated instead.
MAX_SEARCHED_EDGES = 250

#: Measured against text and logo artwork, the largest value a set accepts ran
#: between 0.57 and 1.05 of its shortest edge.  Half of the shortest edge is
#: therefore a value that works, and it costs no search to find.
SAFE_EDGE_FRACTION = 0.5

#: Builds the estimate may spend.
MAX_ESTIMATE_BUILDS = 3


def smallest_detail(edges: list[TopoDS_Edge]) -> float:
    """The shortest edge, which is what limits the value."""
    return min((edge_length(e) for e in edges), default=0.0)


def _bisect_working_value(
    shape: TopoDS_Shape, modifier: Modifier, edges: list[TopoDS_Edge]
) -> float | None:
    """The largest value that succeeds, to within the steps allowed."""
    low, high = 0.0, modifier.value
    best: float | None = None
    for _ in range(BISECTION_STEPS):
        mid = (low + high) / 2.0
        if mid <= 1e-6:
            break
        ok, _ = _try_modifier(shape, modifier, edges, mid)
        if ok:
            best = mid
            low = mid
        else:
            high = mid
    return best


def _estimate_working_value(
    shape: TopoDS_Shape, modifier: Modifier, edges: list[TopoDS_Edge]
) -> float | None:
    """A value that works, found from the shortest edge rather than by search."""
    shortest = smallest_detail(edges)
    if shortest <= 0.0:
        return None
    candidate = min(SAFE_EDGE_FRACTION * shortest, modifier.value * 0.9)
    for _ in range(MAX_ESTIMATE_BUILDS):
        if candidate < MIN_USEFUL_VALUE:
            return None
        ok, _ = _try_modifier(shape, modifier, edges, candidate)
        if ok:
            return candidate
        candidate /= 2.0
    return None


def _largest_working_value(
    shape: TopoDS_Shape, modifier: Modifier, edges: list[TopoDS_Edge]
) -> tuple[float | None, bool]:
    """A value the whole set accepts, and whether it is the exact largest.

    A search costs one complete fillet for each step.  That is affordable on a
    few hundred edges and not affordable on a few thousand, thus the method
    changes with the count and the caller is told which answer it received.
    """
    if len(edges) <= MAX_SEARCHED_EDGES:
        return _bisect_working_value(shape, modifier, edges), True
    return _estimate_working_value(shape, modifier, edges), False


# ------------------------------------------------------------------- validity


def check_valid(shape: TopoDS_Shape) -> bool:
    return not shape.IsNull() and BRepCheck_Analyzer(shape).IsValid()


def unify_same_domain(shape: TopoDS_Shape) -> TopoDS_Shape:
    """Merge co-planar faces.  Export only - it invalidates face references (§3.1)."""
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    upgrader = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
    upgrader.Build()
    return upgrader.Shape()


def tessellate(shape: TopoDS_Shape, deflection: float = 0.02, angle: float = 0.3):
    """Tessellate for display or for handing to the mesh engine."""
    from OCP.BRepMesh import BRepMesh_IncrementalMesh

    BRepMesh_IncrementalMesh(shape, deflection, False, angle, True)
    return shape


__all__ = [
    "BooleanResult",
    "GeometryError",
    "ModifierResult",
    "apply_modifier",
    "boolean",
    "check_valid",
    "classify_feature_edges",
    "edge_length",
    "edge_midpoint",
    "edge_tangent",
    "edges_of",
    "explore",
    "find_blend_edges",
    "select_edges",
    "solid_count",
    "tessellate",
    "unify_same_domain",
    "volume",
]
