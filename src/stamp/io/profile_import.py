"""SVG / DXF / DWG -> Profile - spec §5.3, §5.4.

Each importer does only the format-specific work: find the geometry, work out the
unit scale, and surface the questions only the user can answer.  Everything after that is
:mod:`stamp.io.normalize`.
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import quoteattr

from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_Transform
from OCP.Geom import Geom_BezierCurve
from OCP.gp import gp_Pnt, gp_Trsf
from OCP.TColgp import TColgp_Array1OfPnt
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Edge, TopoDS_Shape

from stamp.io.normalize import (
    DEFAULT_JOIN_TOLERANCE,
    Component,
    Issue,
    IssueKind,
    Profile,
    normalize,
    normalize_groups,
    outline_strokes,
    union_overlapping,
)
from stamp.units import DXF_INSUNITS_TO_MM, MM_PER_INCH, TO_MM

SVG_EXTS = {".svg"}
DXF_EXTS = {".dxf"}
DWG_EXTS = {".dwg"}
PROFILE_EXTS = SVG_EXTS | DXF_EXTS | DWG_EXTS

_LENGTH_RE = re.compile(r"^\s*([+-]?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)\s*([a-zA-Z%]*)\s*$")

#: SVG features that carry no extrudable geometry.  Warn once, then ignore (§5.3).
_IGNORED_SVG_TAGS = {
    "linearGradient": "gradients",
    "radialGradient": "gradients",
    "mask": "masks",
    "clipPath": "clip paths",
    "filter": "filters",
    "animate": "animation",
    "animateTransform": "animation",
    "pattern": "patterns",
}


@dataclass
class ImportOptions:
    """Everything the user can change at import time without touching the file."""

    join_tolerance: float = DEFAULT_JOIN_TOLERANCE
    close_open_loops: bool = False
    union_overlapping: bool = False
    outline_stroke_width: float | None = None  # mm
    layers: list[str] | None = None  # DXF only
    unit_override: str | None = None  # "mm" | "in" | ... when the file does not say
    extra_scale: float = 1.0
    #: Keep a filled backdrop that would otherwise be dropped.  Stamp drops one by
    #: default because it is nearly always an exporter's white page rect and it
    #: covers the artwork; this is how the user says it was deliberate.
    keep_background: bool = False


@dataclass
class ImportResult:
    profile: Profile
    native_units: str
    unit_scale: float  # file units -> mm
    source_hash: str
    available_layers: list[str] = field(default_factory=list)
    #: Set when the file gives no reliable unit and the UI must prompt (§10).
    units_ambiguous: bool = False
    #: Raw stroke polylines, kept so "outline strokes" can run without a re-read.
    stroke_polylines: list[list[tuple[float, float]]] = field(default_factory=list)
    #: The backdrop that was dropped, when one was.  The UI offers to keep it.
    dropped_background: Component | None = None

    @property
    def issues(self) -> list[Issue]:
        return self.profile.issues


def file_hash(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:32]


def import_profile(path: str | Path, options: ImportOptions | None = None) -> ImportResult:
    """Dispatch on extension.  Raises ValueError for anything unsupported."""
    path = Path(path)
    options = options or ImportOptions()
    suffix = path.suffix.lower()
    if suffix in SVG_EXTS:
        return import_svg(path, options)
    if suffix in DXF_EXTS:
        return import_dxf(path, options)
    if suffix in DWG_EXTS:
        return import_dwg(path, options)
    raise ValueError(
        f"Stamp cannot read {suffix or 'this file'}. Use SVG, DXF, or DWG for profiles."
    )


# ------------------------------------------------------------------------- SVG


def parse_length(text: str | None) -> tuple[float, str] | None:
    """Split an SVG length into (value, unit).  Unitless means px.

    CSS unit identifiers are case-insensitive, so ``2IN`` is two inches, and
    the unit comes back lowercased.  Matched case-sensitively it was not a
    length at all, and a drawing declared in capitals imported at 96 dpi.
    """
    if not text:
        return None
    m = _LENGTH_RE.match(text)
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "px").lower()
    if unit == "%":
        return None
    return value, unit


@dataclass(frozen=True)
class SvgScale:
    """The two different scales an SVG needs, which are not the same number.

    *geometry* multiplies what ocpsvg hands back.  ocpsvg parses at 25.4 ppi and
    applies the viewBox-to-viewport transform itself - but only where
    svgelements does, which is only when both width and height are absolute
    lengths.  So its shapes are already millimetres for a document with a
    viewBox and a physical width *and* height, and are still in the file's own
    user units for anything else.  Multiplying the first kind by the viewBox
    ratio as well scaled the file a second time; not multiplying the second kind
    left a width-only document at whatever its viewBox said.  *geometry* is what
    is left over: the millimetres one user unit measures, divided by the scale
    ocpsvg already applied.

    *user_unit_mm* is what one viewBox unit measures, which is what a
    ``stroke-width`` written in the file is expressed in.
    """

    geometry: float
    user_unit_mm: float
    unit: str
    ambiguous: bool


#: The root attributes that decide how big an SVG is.  Nothing else in the
#: document has any bearing on it.
_SIZING_ATTRS = ("width", "height", "viewBox", "preserveAspectRatio")


def _sizing_root(root):
    """svgelements' own reading of the root's width, height and viewBox.

    Sizing has to agree with ocpsvg, which hands the file to svgelements at
    25.4 ppi, so the numbers come from svgelements rather than being worked out
    a second time here where the two can drift apart.  Only the root's four
    sizing attributes are handed over: re-parsing the whole document just to
    read the top of it is what makes a large logo slow to open.
    """
    import svgelements

    attrs = " ".join(
        f"{name}={quoteattr(root.get(name))}" for name in _SIZING_ATTRS if root.get(name)
    )
    stub = f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}/>'
    try:
        return svgelements.SVG.parse(io.StringIO(stub), ppi=MM_PER_INCH)
    except Exception:  # noqa: BLE001 - a root nobody can read is sized as pixels
        return None


def _view_box_size(svg) -> tuple[float, float] | None:
    """The viewBox's width and height, or None where there is no usable one.

    A viewBox that is missing, has the wrong number of values in it, or measures
    zero is no viewBox: svgelements leaves the geometry in user units and so
    does the sizing here.
    """
    try:
        box = svg.viewbox
        width = float(box.width)
        height = float(box.height)
    except (AttributeError, TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def _viewport_scale(svg, *, vertical: bool) -> float:
    """The scale svgelements has already applied to the geometry.

    svgelements maps the viewBox onto the viewport only when *both* width and
    height resolve to absolute lengths.  With either one missing or given as a
    percentage it substitutes the viewBox's own dimension, and the viewport
    transform comes out as a centring translation with no scale in it at all -
    which is what made a ``width="200mm" viewBox="0 0 100 50"`` document, with
    no height, import at 100 mm instead of 200.  Asking svgelements for the
    transform it would apply is the only way the two can never disagree.

    The two axes differ only under ``preserveAspectRatio="none"``, which
    stretches the drawing; the axis the declared size came from is the one that
    can still be made right.
    """
    from svgelements import Matrix

    try:
        matrix = Matrix(svg.viewbox.transform(svg))
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return 1.0
    scale = matrix.d if vertical else matrix.a
    return scale if scale > 0 else 1.0


def svg_scale(path: Path) -> SvgScale:
    """Work out how to size an SVG.  See :class:`SvgScale`."""
    root = ET.parse(path).getroot()
    svg = _sizing_root(root)
    view_box = _view_box_size(svg)
    width = parse_length(root.get("width"))
    height = parse_length(root.get("height"))

    if view_box is None:
        # Without a viewBox one user unit *is* one CSS pixel, whatever the
        # viewport is declared in, so the width says nothing about the artwork.
        # That is what the spec says and what every browser draws, and it still
        # surprises people who wrote width="100mm" and get 26.5, so the size is
        # offered for correction rather than committed to (§5.3).
        return SvgScale(TO_MM["px"], TO_MM["px"], "px", True)

    vb_width, vb_height = view_box
    if width is not None:
        value, unit = width
        user_unit_mm = value * TO_MM.get(unit, TO_MM["px"]) / vb_width
    elif height is not None:
        # Against the viewBox *height*: dividing a height by the viewBox width
        # sized a non-square document by the ratio between two unrelated numbers.
        value, unit = height
        user_unit_mm = value * TO_MM.get(unit, TO_MM["px"]) / vb_height
    else:
        # No usable size - a percentage counts as none.  User units are read as
        # CSS pixels at 96 dpi, and that reading is a guess the UI must offer to
        # correct (§5.3).
        return SvgScale(TO_MM["px"], TO_MM["px"], "px", True)

    applied = _viewport_scale(svg, vertical=width is None)
    return SvgScale(user_unit_mm / applied, user_unit_mm, unit, unit == "px")


def svg_user_unit_mm(path: Path) -> tuple[float, str, bool]:
    """How many millimetres one SVG user unit is, plus the declared unit.

    ``ambiguous`` is True when the document gives no physical size, so the caller
    must show the 96 dpi guess as an editable field rather than commit to it.
    """
    scale = svg_scale(path)
    return scale.user_unit_mm, scale.unit, scale.ambiguous


def _scan_svg_extras(path: Path) -> list[Issue]:
    """Name the things Stamp will not guess about: live text, gradients, masks."""
    issues: list[Issue] = []
    root = ET.parse(path).getroot()
    seen: set[str] = set()
    text_count = 0
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag in ("text", "tspan", "textPath"):
            if tag == "text":
                text_count += 1
        elif tag in _IGNORED_SVG_TAGS:
            seen.add(_IGNORED_SVG_TAGS[tag])

    if text_count:
        issues.append(
            Issue(
                IssueKind.LIVE_TEXT,
                f"This SVG contains {text_count} live text element"
                f"{'s' if text_count != 1 else ''}. In Illustrator or Inkscape, "
                f"convert text to outlines and export the file again.",
                blocking=True,
            )
        )
    if seen:
        names = ", ".join(sorted(seen))
        issues.append(
            Issue(
                IssueKind.UNSUPPORTED_ELEMENT,
                f"Stamp ignores {names} in SVG. Only filled paths become geometry.",
                blocking=False,
            )
        )
    return issues


def import_svg(path: Path, options: ImportOptions) -> ImportResult:
    from ocpsvg import import_svg_document

    issues = _scan_svg_extras(path)
    sizing = svg_scale(path)
    declared_unit, ambiguous = sizing.unit, sizing.ambiguous
    scale, user_unit_mm = sizing.geometry, sizing.user_unit_mm
    if options.unit_override:
        # The override says what one user unit is, so it replaces the reading of
        # the document, not the viewport transform ocpsvg already applied.
        override = TO_MM[options.unit_override.lower()]
        scale *= override / user_unit_mm if user_unit_mm else override
        user_unit_mm = override
        ambiguous = False
    scale *= options.extra_scale
    user_unit_mm *= options.extra_scale

    from ocpsvg import ColorAndLabel

    items = list(import_svg_document(path, flip_y=True, metadata=ColorAndLabel))
    faces = [it for it, _meta in items if it.ShapeType() == TopAbs_ShapeEnum.TopAbs_FACE]
    face_colors = [
        _hex_color(meta.color_for(it))
        for it, meta in items
        if it.ShapeType() == TopAbs_ShapeEnum.TopAbs_FACE
    ]
    wires = [it for it, _meta in items if it.ShapeType() == TopAbs_ShapeEnum.TopAbs_WIRE]

    stroke_polylines = _polylines_of(wires, scale)

    if not faces and wires:
        width = _guess_stroke_width(path) or 0.5
        if options.outline_stroke_width:
            profile = outline_strokes(stroke_polylines, options.outline_stroke_width)
            profile.issues[:0] = issues
        else:
            issues.append(
                Issue(
                    IssueKind.NO_FILL,
                    "Every path in this SVG has a stroke but no fill, so it has no "
                    f"area to extrude. Outline the strokes at {width:g} mm to give "
                    "them area.",
                    blocking=True,
                    detail={"suggested_width_mm": width * user_unit_mm},
                )
            )
            profile = Profile(issues=issues)
        return ImportResult(
            profile=profile,
            native_units=declared_unit,
            unit_scale=scale,
            source_hash=file_hash(path),
            units_ambiguous=ambiguous,
            stroke_polylines=stroke_polylines,
        )

    if ambiguous and not options.unit_override:
        issues.append(
            Issue(
                IssueKind.AMBIGUOUS_UNITS,
                "This SVG gives no physical size, so Stamp read it at 96 dpi. "
                "Confirm the size before you place it.",
                blocking=False,
            )
        )

    # One group per SVG element: its own fill rule already decided its holes, so a
    # filled circle sitting inside a filled rectangle stays material (§5.3).
    groups = [_edges_of([f], scale) for f in faces]
    components = _components_by_color(face_colors)
    profile = normalize_groups(
        groups,
        join_tolerance=options.join_tolerance,
        issues=issues,
        close_open_loops=options.close_open_loops,
        source_units=declared_unit,
        group_components=[components[c] for c in face_colors],
    )

    dropped = None
    background = next((c for c in profile.components if c.background), None)
    if background is not None:
        if options.keep_background:
            profile.issues.append(
                Issue(
                    IssueKind.UNSUPPORTED_ELEMENT,
                    f"The {background.label.lower()} backdrop in this file is being "
                    f"kept, so the artwork sits on a filled panel.",
                    blocking=False,
                    detail={"component": background.key},
                )
            )
        else:
            dropped = background
            profile = _without_component(
                profile, background, groups, face_colors, components, options,
                issues, declared_unit,
            )

    if options.union_overlapping and profile.issues_of(IssueKind.SELF_INTERSECTION):
        profile = union_overlapping(profile)

    return ImportResult(
        profile=profile,
        native_units=declared_unit,
        unit_scale=scale,
        source_hash=file_hash(path),
        units_ambiguous=ambiguous,
        stroke_polylines=stroke_polylines,
        dropped_background=dropped,
    )


def _hex_color(rgba) -> str:
    """An ocpsvg colour tuple as ``#rrggbb``.  Alpha is not a component key."""
    if not rgba:
        return "#000000"
    r, g, b = (max(0, min(255, int(round(v * 255)))) for v in rgba[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


#: Colours close enough to these get the name rather than the hex, because
#: "Black" and "Red" are what the user calls them.
_COLOR_NAMES = {
    "#000000": "Black", "#ffffff": "White", "#ff0000": "Red", "#00ff00": "Green",
    "#0000ff": "Blue", "#ffff00": "Yellow", "#00ffff": "Cyan", "#ff00ff": "Magenta",
    "#808080": "Grey", "#c0c0c0": "Silver", "#ffa500": "Orange",
}


def _components_by_color(face_colors: Sequence[str]) -> dict[str, Component]:
    """One component per fill colour, in the order the document paints them.

    Grouping by colour rather than by element is what keeps this usable: a
    detailed logo has hundreds of paths and three colours, and it is the three
    the user wants to pick from.
    """
    components: dict[str, Component] = {}
    for color in face_colors:
        component = components.get(color)
        if component is None:
            components[color] = component = Component(
                key=color,
                color=color,
                label=_COLOR_NAMES.get(color.lower(), color.upper()),
            )
        component.element_count += 1
    return components


def _without_component(
    profile: Profile,
    background: Component,
    groups, face_colors, components, options, issues, declared_unit,
) -> Profile:
    """Re-normalize with the backdrop left out, and say so.

    Re-run rather than subtracted: the remaining components have to be nested and
    resolved against each other as though the backdrop had never been in the
    file, and centring and the bounding box have to follow the artwork rather
    than the page it was drawn on.
    """
    keep = [
        (group, components[color])
        for group, color in zip(groups, face_colors, strict=False)
        if color != background.key
    ]
    if not keep:
        return profile  # the backdrop was all there was; better a rectangle than nothing
    reduced = normalize_groups(
        [group for group, _c in keep],
        join_tolerance=options.join_tolerance,
        issues=list(issues),
        close_open_loops=options.close_open_loops,
        source_units=declared_unit,
        group_components=[component for _g, component in keep],
    )
    reduced.issues.append(
        Issue(
            IssueKind.UNSUPPORTED_ELEMENT,
            f"This file has a filled {background.label.lower()} backdrop behind the "
            f"artwork, covering all of it. Stamp left it out - kept, it would be "
            f"the only thing that stamped. Turn on \"Keep the background layer\" "
            f"if it was meant to be there.",
            blocking=False,
            detail={"component": background.key, "background": True},
        )
    )
    reduced.dropped_background = background
    return reduced


def _guess_stroke_width(path: Path) -> float | None:
    root = ET.parse(path).getroot()
    for el in root.iter():
        w = el.get("stroke-width")
        if w:
            parsed = parse_length(w)
            if parsed:
                return parsed[0]
        style = el.get("style") or ""
        m = re.search(r"stroke-width\s*:\s*([0-9.]+)", style)
        if m:
            return float(m.group(1))
    return None


def _scaled(shape: TopoDS_Shape, scale: float) -> TopoDS_Shape:
    if abs(scale - 1.0) < 1e-12:
        return shape
    trsf = gp_Trsf()
    trsf.SetScale(gp_Pnt(0, 0, 0), scale)
    return BRepBuilderAPI_Transform(shape, trsf, True).Shape()


def _edges_of(shapes: Sequence[TopoDS_Shape], scale: float) -> list[TopoDS_Edge]:
    edges: list[TopoDS_Edge] = []
    for shape in shapes:
        s = _scaled(shape, scale)
        explorer = TopExp_Explorer(s, TopAbs_ShapeEnum.TopAbs_EDGE)
        while explorer.More():
            edges.append(TopoDS.Edge_s(explorer.Current()))
            explorer.Next()
    return edges


def _polylines_of(shapes: Sequence[TopoDS_Shape], scale: float) -> list[list[tuple[float, float]]]:
    from stamp.io.normalize import flatten_wire

    out = []
    for shape in shapes:
        s = _scaled(shape, scale)
        if s.ShapeType() == TopAbs_ShapeEnum.TopAbs_WIRE:
            out.append(flatten_wire(TopoDS.Wire_s(s)))
    return out


# ------------------------------------------------------------------------- DXF

#: Entity types that carry profile geometry (§5.4).
_DXF_GEOMETRY_TYPES = {
    "LINE",
    "ARC",
    "CIRCLE",
    "ELLIPSE",
    "SPLINE",
    "LWPOLYLINE",
    "POLYLINE",
    "HATCH",
}
_DXF_TEXT_TYPES = {"TEXT", "MTEXT", "ATTDEF", "ATTRIB"}
_DXF_SKIP_LAYERS = {"defpoints"}

#: How far to follow block references into other block references.  Deep enough
#: for any real drawing; a guard against a block that somehow inserts itself.
_MAX_INSERT_DEPTH = 16


def _exploded(entity, layer: str | None = None, depth: int = 0):
    """Yield ``(entity, layer)`` for everything *entity* actually draws.

    A block reference draws nothing itself - its geometry lives in the block
    definition - so an INSERT is replaced by its transformed contents, and a
    nested INSERT by its own.  Entities drawn on layer 0 inside a block take the
    layer the block was inserted on, which is how AutoCAD has always resolved
    them; anything on a named layer keeps it.
    """
    kind = entity.dxftype()
    own = getattr(entity.dxf, "layer", "0")
    resolved = layer if (own == "0" and layer is not None) else own
    if kind != "INSERT":
        yield entity, resolved
        return
    if depth >= _MAX_INSERT_DEPTH:
        return
    try:
        # A MINSERT is one INSERT that draws its block on a grid.
        # ``virtual_entities`` gives the contents of a single cell, so a 2x3
        # array of pads imported as one pad; ``multi_insert`` gives one INSERT
        # per cell, already placed, and each of those explodes normally.
        if getattr(entity, "mcount", 1) > 1:
            virtual = list(entity.multi_insert())
        else:
            virtual = list(entity.virtual_entities())
    except Exception:
        return  # a block Stamp cannot expand is skipped, not fatal
    for sub in virtual:
        yield from _exploded(sub, resolved, depth + 1)
    for attrib in getattr(entity, "attribs", ()):
        yield attrib, resolved


def dxf_layers(path: Path) -> list[str]:
    """The layers that actually carry geometry, for the layer filter UI."""
    import ezdxf

    doc = ezdxf.readfile(path)
    layers: dict[str, int] = {}
    for e in doc.modelspace():
        for entity, layer in _exploded(e):
            if entity.dxftype() in _DXF_GEOMETRY_TYPES:
                layers[layer] = layers.get(layer, 0) + 1
    return sorted(layers)


def default_dxf_layers(path: Path) -> list[str]:
    """All visible, non-defpoints layers - the default selection (§5.4)."""
    import ezdxf

    doc = ezdxf.readfile(path)
    out = []
    for name in dxf_layers(path):
        if name.lower() in _DXF_SKIP_LAYERS:
            continue
        layer = doc.layers.get(name) if doc.layers.has_entry(name) else None
        if layer is not None and layer.is_off():
            continue
        out.append(name)
    return out


def import_dxf(path: Path, options: ImportOptions) -> ImportResult:
    import ezdxf
    from ezdxf import path as ezpath

    doc = ezdxf.readfile(path)
    insunits = int(doc.header.get("$INSUNITS", 0) or 0)
    ambiguous = insunits == 0
    if options.unit_override:
        scale = TO_MM[options.unit_override.lower()]
        ambiguous = False
        native = options.unit_override
    elif insunits in DXF_INSUNITS_TO_MM:
        scale = DXF_INSUNITS_TO_MM[insunits]
        native = "mm" if scale == 1.0 else f"insunits {insunits}"
    else:
        scale = 1.0
        native = "unknown"
    scale *= options.extra_scale

    available = dxf_layers(path)
    wanted = options.layers if options.layers is not None else default_dxf_layers(path)
    wanted_set = {w.lower() for w in wanted}

    issues: list[Issue] = []
    if ambiguous:
        issues.append(
            Issue(
                IssueKind.AMBIGUOUS_UNITS,
                "This DXF does not declare its units ($INSUNITS is 0). "
                "Confirm the size before you place it.",
                blocking=False,
            )
        )

    text_on_selected = 0
    text_elsewhere = 0
    edges: list[TopoDS_Edge] = []
    msp = doc.modelspace()
    for top in msp:
        for entity, layer in _exploded(top):
            kind = entity.dxftype()
            selected = layer.lower() in wanted_set
            if kind in _DXF_TEXT_TYPES:
                if selected:
                    text_on_selected += 1
                else:
                    text_elsewhere += 1
                continue
            if not selected or kind not in _DXF_GEOMETRY_TYPES:
                continue

            try:
                if kind == "HATCH":
                    paths = list(ezpath.from_hatch(entity))
                else:
                    paths = [ezpath.make_path(entity)]
            except Exception:
                continue
            for p in paths:
                edges.extend(_edges_from_ezdxf_path(p, scale))

    if text_on_selected:
        issues.append(
            Issue(
                IssueKind.LIVE_TEXT,
                f"The selected layers contain {text_on_selected} text "
                f"entit{'ies' if text_on_selected != 1 else 'y'}. Explode the text "
                f"to geometry in your CAD program and save the file again.",
                blocking=True,
            )
        )
    elif text_elsewhere:
        issues.append(
            Issue(
                IssueKind.LIVE_TEXT,
                f"This DXF contains {text_elsewhere} text "
                f"entit{'ies' if text_elsewhere != 1 else 'y'} on layers that are "
                f"not selected. Stamp ignores them.",
                blocking=False,
            )
        )

    profile = normalize(
        edges,
        join_tolerance=options.join_tolerance,
        issues=issues,
        close_open_loops=options.close_open_loops,
        source_units=native,
    )
    if options.union_overlapping and profile.issues_of(IssueKind.SELF_INTERSECTION):
        profile = union_overlapping(profile)

    return ImportResult(
        profile=profile,
        native_units=native,
        unit_scale=scale,
        source_hash=file_hash(path),
        available_layers=available,
        units_ambiguous=ambiguous,
    )


def _edges_from_ezdxf_path(p, scale: float) -> list[TopoDS_Edge]:
    """Convert one ezdxf Path (lines + quadratic/cubic Beziers) to OCC edges.

    The curves stay curves - no flattening - so a spline imported from DXF is still
    a spline when it reaches the extrude.
    """
    from ezdxf.path import Command

    edges: list[TopoDS_Edge] = []
    current = _pt(p.start, scale)
    for cmd in p.commands():
        if cmd.type == Command.LINE_TO:
            end = _pt(cmd.end, scale)
            if current.Distance(end) > 1e-9:
                edges.append(BRepBuilderAPI_MakeEdge(current, end).Edge())
            current = end
        elif cmd.type == Command.CURVE3_TO:
            end = _pt(cmd.end, scale)
            edges.append(_bezier_edge([current, _pt(cmd.ctrl, scale), end]))
            current = end
        elif cmd.type == Command.CURVE4_TO:
            end = _pt(cmd.end, scale)
            edges.append(
                _bezier_edge([current, _pt(cmd.ctrl1, scale), _pt(cmd.ctrl2, scale), end])
            )
            current = end
        elif cmd.type == Command.MOVE_TO:
            current = _pt(cmd.end, scale)
    return edges


def _pt(v, scale: float) -> gp_Pnt:
    return gp_Pnt(float(v.x) * scale, float(v.y) * scale, 0.0)


def _bezier_edge(poles: list[gp_Pnt]) -> TopoDS_Edge:
    array = TColgp_Array1OfPnt(1, len(poles))
    for i, p in enumerate(poles, start=1):
        array.SetValue(i, p)
    return BRepBuilderAPI_MakeEdge(Geom_BezierCurve(array)).Edge()


# ------------------------------------------------------------------------- DWG


class DwgUnavailable(RuntimeError):
    """Raised when no DWG reader is present.  Never fatal to the app (§5.4)."""


#: Set by the interface when the user points Stamp at a converter (§5.4).
ODA_CONVERTER_PATH: str | None = None


def set_oda_converter(path: str | None) -> None:
    """Remember where the ODA File Converter is, for the rest of this session."""
    global ODA_CONVERTER_PATH
    ODA_CONVERTER_PATH = path
    if path:
        try:
            from ezdxf.addons import odafc

            odafc.win_exec_path = path
        except Exception:
            pass


def dwg_backend() -> str | None:
    """Which DWG path is available: ``"ezdwg"``, ``"odafc"``, or None."""
    if ODA_CONVERTER_PATH and Path(ODA_CONVERTER_PATH).exists():
        return "odafc"
    try:
        import ezdwg  # noqa: F401

        return "ezdwg"
    except ImportError:
        pass
    if shutil.which("ODAFileConverter") or shutil.which("ODAFileConverter.exe"):
        return "odafc"
    try:
        from ezdxf.addons import odafc

        if odafc.win_exec_path and Path(odafc.win_exec_path).exists():
            return "odafc"
    except Exception:
        pass
    return None


def import_dwg(path: Path, options: ImportOptions) -> ImportResult:
    backend = dwg_backend()
    if backend is None:
        raise DwgUnavailable(
            "Stamp cannot read DWG on this machine. Save the drawing as DXF from "
            "your CAD program and try again, or install the ODA File Converter."
        )

    if backend == "ezdwg":
        try:
            return _import_dwg_ezdwg(path, options)
        except Exception:
            pass  # fall through to the converter

    from ezdxf.addons import odafc

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / (path.stem + ".dxf")
        odafc.convert(str(path), str(out), version="R2013")
        if not out.exists():
            raise DwgUnavailable(
                "The ODA File Converter did not produce a DXF. Save the drawing as "
                "DXF from your CAD program and try again."
            )
        result = import_dxf(out, options)
    result.source_hash = file_hash(path)
    return result


def _import_dwg_ezdwg(path: Path, options: ImportOptions) -> ImportResult:
    import ezdwg

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / (path.stem + ".dxf")
        doc = ezdwg.readfile(str(path))
        doc.saveas(out)
        result = import_dxf(out, options)
    result.source_hash = file_hash(path)
    return result


__all__ = [
    "DWG_EXTS",
    "DXF_EXTS",
    "DwgUnavailable",
    "ImportOptions",
    "ImportResult",
    "PROFILE_EXTS",
    "SVG_EXTS",
    "default_dxf_layers",
    "dwg_backend",
    "set_oda_converter",
    "dxf_layers",
    "file_hash",
    "import_dxf",
    "import_dwg",
    "import_profile",
    "import_svg",
    "parse_length",
    "svg_scale",
    "svg_user_unit_mm",
]
