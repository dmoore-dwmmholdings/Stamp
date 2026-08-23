"""Artwork made from a message - spec 5.3.

Qt lays out the message and gives the outline of each glyph.  Those outlines
become OCC edges, and from there the message goes through the same pipeline as
an SVG.  Thus a text feature accepts every operation, every modifier and every
export that a logo accepts.

The glyphs stay curves.  Nothing is flattened to line segments, thus a fillet on
the edge of a letter has a true curved surface to work on.
"""

from __future__ import annotations

from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
from OCP.Geom import Geom_BezierCurve
from OCP.gp import gp_Pnt
from OCP.TColgp import TColgp_Array1OfPnt
from OCP.TopoDS import TopoDS_Edge

from stamp.core.document import TextAlign, TextSpec
from stamp.io.normalize import Issue, IssueKind, Profile, normalize_groups

#: The font size used to collect the outlines.  The result is scaled to
#: millimeters afterwards.  A large value keeps the hinting out of the curves.
LAYOUT_PIXELS = 512.0


class TextProfileError(RuntimeError):
    """The message cannot become geometry.  The text is shown to the user."""


def available_families() -> list[str]:
    """Every font family on this machine, for the family list in the panel."""
    from PySide6.QtGui import QFontDatabase

    return sorted(QFontDatabase.families())


def _font(spec: TextSpec):
    from PySide6.QtGui import QFont

    font = QFont(spec.family)
    font.setPixelSize(int(LAYOUT_PIXELS))
    font.setBold(spec.bold)
    font.setItalic(spec.italic)
    # Qt applies letter spacing while it lays the glyphs out, thus the outlines
    # already have it and nothing downstream must know about it.
    if spec.letter_spacing:
        font.setLetterSpacing(
            QFont.SpacingType.AbsoluteSpacing, spec.letter_spacing * LAYOUT_PIXELS
        )
    return font


def _wrap(spec: TextSpec, metrics) -> list[list[str]]:
    """Break the message into lines, each a list of words.

    An explicit line break in the message always breaks.  A wrap width breaks in
    addition to that.  A single word longer than the width keeps its own line,
    because a break inside a word is worse than a line that is too wide.
    """
    limit = None
    if spec.wrap_mm and spec.wrap_mm > 0:
        limit = spec.wrap_mm * (LAYOUT_PIXELS / spec.size_mm)

    lines: list[list[str]] = []
    for paragraph in spec.text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        words = paragraph.split()
        if not words:
            lines.append([])
            continue
        if limit is None:
            lines.append(words)
            continue
        current: list[str] = []
        for word in words:
            candidate = (" ".join([*current, word])) if current else word
            if current and metrics.horizontalAdvance(candidate) > limit:
                lines.append(current)
                current = [word]
            else:
                current.append(word)
        if current:
            lines.append(current)
    return lines


def _line_paths(spec: TextSpec, font, metrics, lines: list[list[str]]):
    """One :class:`QPainterPath` per line, already placed and aligned."""
    from PySide6.QtGui import QPainterPath

    limit = None
    if spec.wrap_mm and spec.wrap_mm > 0:
        limit = spec.wrap_mm * (LAYOUT_PIXELS / spec.size_mm)

    widths = [metrics.horizontalAdvance(" ".join(w)) if w else 0.0 for w in lines]
    block = limit if limit is not None else (max(widths) if widths else 0.0)

    step = metrics.height() * spec.line_spacing
    space = metrics.horizontalAdvance(" ")
    paths = []
    underlines: list[tuple[float, float, float, float]] = []

    for index, (words, width) in enumerate(zip(lines, widths, strict=True)):
        baseline = index * step
        if not words:
            continue

        justified = (
            spec.align is TextAlign.JUSTIFY
            and limit is not None
            and len(words) > 1
            and index < len(lines) - 1  # the last line of a paragraph stays ragged
        )
        if justified:
            slack = block - (width - space * (len(words) - 1))
            gap = slack / (len(words) - 1)
            x = 0.0
            path = QPainterPath()
            for word in words:
                path.addText(x, baseline, font, word)
                x += metrics.horizontalAdvance(word) + gap
            line_start, line_width = 0.0, block
        else:
            if spec.align is TextAlign.CENTER:
                x = (block - width) / 2.0
            elif spec.align is TextAlign.RIGHT:
                x = block - width
            else:
                x = 0.0
            path = QPainterPath()
            path.addText(x, baseline, font, " ".join(words))
            line_start, line_width = x, width

        paths.append(path)
        if spec.underline:
            top = baseline + metrics.underlinePos()
            thickness = max(metrics.lineWidth(), LAYOUT_PIXELS * 0.02)
            underlines.append((line_start, top, line_width, thickness))

    return paths, underlines


def _subpaths(path) -> list[list[tuple[str, list[tuple[float, float]]]]]:
    """Split a painter path into closed contours of line and curve commands."""
    from PySide6.QtGui import QPainterPath

    contours: list[list[tuple[str, list[tuple[float, float]]]]] = []
    current: list[tuple[str, list[tuple[float, float]]]] = []
    cursor = (0.0, 0.0)
    index = 0
    count = path.elementCount()

    while index < count:
        element = path.elementAt(index)
        point = (element.x, element.y)
        if element.type == QPainterPath.ElementType.MoveToElement:
            if current:
                contours.append(current)
            current = []
            cursor = point
            index += 1
        elif element.type == QPainterPath.ElementType.LineToElement:
            current.append(("line", [cursor, point]))
            cursor = point
            index += 1
        elif element.type == QPainterPath.ElementType.CurveToElement:
            c1 = point
            c2 = (path.elementAt(index + 1).x, path.elementAt(index + 1).y)
            end = (path.elementAt(index + 2).x, path.elementAt(index + 2).y)
            current.append(("curve", [cursor, c1, c2, end]))
            cursor = end
            index += 3
        else:  # a stray data element - the curve branch consumed the real ones
            index += 1
    if current:
        contours.append(current)
    return contours


def _edges(contour, scale: float) -> list[TopoDS_Edge]:
    """One contour of commands becomes OCC edges, with the Y axis turned over."""

    def point(p: tuple[float, float]) -> gp_Pnt:
        # Qt measures Y downward and Stamp measures it upward.
        return gp_Pnt(p[0] * scale, -p[1] * scale, 0.0)

    edges: list[TopoDS_Edge] = []
    for kind, points in contour:
        if kind == "line":
            a, b = point(points[0]), point(points[1])
            if a.Distance(b) > 1e-9:
                edges.append(BRepBuilderAPI_MakeEdge(a, b).Edge())
        else:
            poles = [point(p) for p in points]
            array = TColgp_Array1OfPnt(1, len(poles))
            for i, pole in enumerate(poles, start=1):
                array.SetValue(i, pole)
            edges.append(BRepBuilderAPI_MakeEdge(Geom_BezierCurve(array)).Edge())
    # Close the contour if the font left it open.
    if edges:
        first, last = contour[0][1][0], contour[-1][1][-1]
        a, b = point(last), point(first)
        if a.Distance(b) > 1e-9:
            edges.append(BRepBuilderAPI_MakeEdge(a, b).Edge())
    return edges


def _rectangle_edges(x, y, width, height, scale: float) -> list[TopoDS_Edge]:
    corners = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
    pts = [gp_Pnt(cx * scale, -cy * scale, 0.0) for cx, cy in corners]
    return [
        BRepBuilderAPI_MakeEdge(pts[i], pts[(i + 1) % 4]).Edge() for i in range(4)
    ]


def build_text_profile(spec: TextSpec) -> Profile:
    """Turn a message into a normalized profile.

    Each line of the message is one group.  Containment inside that group decides
    the holes, thus the counter of an "o" becomes a hole and two letters that do
    not touch stay two separate faces.
    """
    if not spec.text.strip():
        return Profile(
            issues=[
                Issue(
                    IssueKind.EMPTY,
                    "This text feature has no message. Type the message to see it.",
                    blocking=True,
                )
            ]
        )
    if spec.size_mm <= 0:
        raise TextProfileError("The text size must be more than zero.")

    from PySide6.QtGui import QFontMetricsF

    font = _font(spec)
    metrics = QFontMetricsF(font)
    lines = _wrap(spec, metrics)
    paths, underlines = _line_paths(spec, font, metrics, lines)

    scale = spec.size_mm / LAYOUT_PIXELS
    groups: list[list[TopoDS_Edge]] = []
    for path in paths:
        # One group per line, not per contour.  Nesting inside the group is what
        # makes the counter of an "o" a hole; a separate group would make it
        # material, because each group is nested on its own.
        edges: list[TopoDS_Edge] = []
        for contour in _subpaths(path):
            edges.extend(_edges(contour, scale))
        if edges:
            groups.append(edges)
    for x, y, width, height in underlines:
        groups.append(_rectangle_edges(x, y, width, height, scale))

    if not groups:
        return Profile(
            issues=[
                Issue(
                    IssueKind.EMPTY,
                    f"The font {spec.family} gave no outlines for this message. "
                    f"Select a different font.",
                    blocking=True,
                )
            ]
        )

    return normalize_groups(groups, source_units="mm")


__all__ = [
    "TextProfileError",
    "available_families",
    "build_text_profile",
]
