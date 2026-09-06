"""The icons on the ribbon.

Stamp ships no icon set, and the ribbon used to stand in emoji for one.  That is
the wrong tool for a toolbar: some emoji render in full colour, some as a box
with a hex number in it, and none of them share a stroke weight or an optical
size - so a row of them reads as a ransom note rather than as one set.  Worse,
the fallback painted them in a fixed light grey, which on a light desktop theme
meant white on white: the old ribbon's icons were, quite literally, not there.

These are drawn instead.  One 24-unit grid, one stroke weight, two colours taken
from the palette - the outline in the window's text colour, and a single accent
for the part of the icon that says what the command does to the thing.  It
follows a light or a dark theme with no second bitmap, and it stays sharp at any
scaling because it is redrawn rather than resampled.
"""

from __future__ import annotations

from math import cos, radians, sin

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

#: Every icon is drawn on this square and scaled to whatever the button asks for.
GRID = 24.0

#: Stroke weight, in grid units.  One weight throughout is most of what makes a
#: hand-drawn set look like a set.
STROKE = 1.7

_CACHE: dict[tuple, QIcon] = {}


# --------------------------------------------------------------------------
# Drawing primitives.  All coordinates are in grid units.
# --------------------------------------------------------------------------

class _Ink:
    """The pens an icon draws with: the outline colour and the accent."""

    def __init__(self, painter: QPainter, color: QColor, accent: QColor) -> None:
        self.painter = painter
        self.color = color
        self.accent = accent
        self.line()

    def _pen(self, color: QColor, weight: float) -> None:
        pen = QPen(color)
        pen.setWidthF(weight)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self.painter.setPen(pen)
        self.painter.setBrush(Qt.BrushStyle.NoBrush)

    def line(self, weight: float = STROKE) -> None:
        self._pen(self.color, weight)

    def mark(self, weight: float = STROKE) -> None:
        """The accent pen: the part of the icon that carries the verb."""
        self._pen(self.accent, weight)

    def fill(self, color: QColor | None = None) -> None:
        self.painter.setPen(Qt.PenStyle.NoPen)
        self.painter.setBrush(color or self.accent)

    def wash(self, color: QColor | None = None, alpha: int = 70) -> None:
        """A translucent fill, for one face of a solid rather than a whole shape."""
        tint = QColor(color or self.accent)
        tint.setAlpha(alpha)
        self.painter.setPen(Qt.PenStyle.NoPen)
        self.painter.setBrush(tint)


def _poly(painter: QPainter, points, close: bool = False) -> None:
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    if close:
        path.closeSubpath()
    painter.drawPath(path)


def _line(painter: QPainter, x1, y1, x2, y2) -> None:
    painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))


def _rect(painter: QPainter, x, y, w, h, radius: float = 2.0) -> None:
    painter.drawRoundedRect(QRectF(x, y, w, h), radius, radius)


def _circle(painter: QPainter, cx, cy, r) -> None:
    painter.drawEllipse(QPointF(cx, cy), r, r)


def _arc(painter: QPainter, cx, cy, r, start, span) -> None:
    rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
    path = QPainterPath()
    path.arcMoveTo(rect, start)
    path.arcTo(rect, start, span)
    painter.drawPath(path)


def _head(painter: QPainter, x, y, dx, dy, size: float = 3.6) -> None:
    """An arrowhead at *x, y* pointing along the unit vector *dx, dy*."""
    for angle in (150.0, -150.0):
        turn = radians(angle)
        bx = dx * cos(turn) - dy * sin(turn)
        by = dx * sin(turn) + dy * cos(turn)
        _line(painter, x, y, x + size * bx, y + size * by)


def _arrow(painter: QPainter, x1, y1, x2, y2, size: float = 3.6) -> None:
    _line(painter, x1, y1, x2, y2)
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 or 1.0
    _head(painter, x2, y2, (x2 - x1) / length, (y2 - y1) / length, size)


def _turn(painter: QPainter, cx, cy, r, start, span, size: float = 3.4) -> None:
    """A circular arrow: an arc with a head on the end it finishes at."""
    _arc(painter, cx, cy, r, start, span)
    end = radians(start + span)
    ex, ey = cx + r * cos(end), cy - r * sin(end)
    tangent = radians(start + span + (90 if span > 0 else -90))
    _head(painter, ex, ey, cos(tangent), -sin(tangent), size)


def _cube_points(cx, cy, r):
    """An isometric cube projects to a regular hexagon, so the maths is short."""
    return [
        (cx + r * cos(radians(a)), cy - r * sin(radians(a)))
        for a in (90, 30, -30, -90, -150, 150)
    ]


def _cube(painter: QPainter, cx, cy, r) -> None:
    corners = _cube_points(cx, cy, r)
    _poly(painter, corners, close=True)
    for corner in (corners[1], corners[3], corners[5]):
        _line(painter, cx, cy, *corner)


def _cube_face(painter: QPainter, cx, cy, r, which: str) -> None:
    """Fill one visible face of the cube - top, right or left."""
    p = _cube_points(cx, cy, r)
    faces = {
        "top": [p[0], p[1], (cx, cy), p[5]],
        "right": [p[1], p[2], p[3], (cx, cy)],
        "left": [(cx, cy), p[3], p[4], p[5]],
    }
    _poly(painter, faces[which], close=True)


def _doc(painter: QPainter, x=5.0, y=3.0, w=13.0, h=18.0, fold=5.0) -> None:
    _poly(
        painter,
        [(x, y), (x + w - fold, y), (x + w, y + fold), (x + w, y + h), (x, y + h)],
        close=True,
    )
    _poly(painter, [(x + w - fold, y), (x + w - fold, y + fold), (x + w, y + fold)])


def _star(painter: QPainter, cx, cy, outer, inner) -> None:
    points = []
    for step in range(10):
        angle = radians(90 + step * 36)
        radius = outer if step % 2 == 0 else inner
        points.append((cx + radius * cos(angle), cy - radius * sin(angle)))
    _poly(painter, points, close=True)


def _badge(ink: _Ink, kind: str, cx: float = 17.8, cy: float = 17.8, r: float = 5.2) -> None:
    """A filled accent disc in the corner, carrying the verb of the command.

    Every "add this" and every "write this out" wears the same badge, so the
    commands read as families rather than as thirty unrelated pictures - which is
    most of what makes a real icon set feel designed.

    The ring around it and the mark inside it are erased rather than painted in
    a background colour: the button behind changes colour on hover, on press and
    when checked, and a hole is right against all four of them.
    """
    painter = ink.painter
    painter.save()
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    ink.fill(QColor(0, 0, 0))
    _circle(painter, cx, cy, r + 1.5)
    painter.restore()

    ink.fill()
    _circle(painter, cx, cy, r)

    painter.save()
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    ink._pen(QColor(0, 0, 0), 1.9)
    if kind == "plus":
        _line(painter, cx, cy - 2.6, cx, cy + 2.6)
        _line(painter, cx - 2.6, cy, cx + 2.6, cy)
    else:
        _arrow(painter, cx, cy - 2.8, cx, cy + 1.6, 2.6)
    painter.restore()
    ink.line()


# --------------------------------------------------------------------------
# The icons.
# --------------------------------------------------------------------------

def _open_part(ink: _Ink) -> None:
    _doc(ink.painter)
    ink.mark(1.5)
    _cube(ink.painter, 11.5, 15.0, 4.0)


def _open_project(ink: _Ink) -> None:
    _poly(
        ink.painter,
        [(3.5, 20), (3.5, 6.5), (9.5, 6.5), (11.8, 9.2), (20.5, 9.2), (20.5, 20)],
        close=True,
    )


def _save(ink: _Ink) -> None:
    _poly(
        ink.painter,
        [(3.5, 3.5), (16, 3.5), (20.5, 8), (20.5, 20.5), (3.5, 20.5)],
        close=True,
    )
    _rect(ink.painter, 7.5, 3.5, 7.5, 6.0, 0.8)
    ink.mark(1.5)
    _rect(ink.painter, 7.0, 13.0, 10.0, 7.5, 0.8)


def _add_profile(ink: _Ink) -> None:
    path = QPainterPath(QPointF(4, 16))
    path.cubicTo(QPointF(7, 5), QPointF(12, 18), QPointF(16.5, 6.5))
    ink.painter.drawPath(path)
    ink.fill(ink.color)
    _rect(ink.painter, 2.6, 14.6, 2.9, 2.9, 0.4)
    _rect(ink.painter, 15.1, 5.1, 2.9, 2.9, 0.4)
    ink.line()
    _badge(ink, "plus")


def _add_text(ink: _Ink) -> None:
    _line(ink.painter, 3.5, 5.5, 15.5, 5.5)
    _line(ink.painter, 9.5, 5.5, 9.5, 19.0)
    _badge(ink, "plus")


def _add_code(ink: _Ink) -> None:
    for x, y in ((3.0, 3.0), (13.5, 3.0), (3.0, 13.5)):
        _rect(ink.painter, x, y, 7.5, 7.5, 1.3)
        ink.fill(ink.color)
        _rect(ink.painter, x + 2.5, y + 2.5, 2.5, 2.5, 0.4)
        ink.line()
    _badge(ink, "plus")


def _undo(ink: _Ink) -> None:
    _turn(ink.painter, 12, 14, 7, 0, 180)


def _redo(ink: _Ink) -> None:
    _turn(ink.painter, 12, 14, 7, 180, -180)


def _save_preset(ink: _Ink) -> None:
    _star(ink.painter, 12, 12.5, 8.5, 3.9)


def _insert_preset(ink: _Ink) -> None:
    ink.fill()
    _star(ink.painter, 12, 12.5, 8.5, 3.9)


def _align_edge(ink: _Ink) -> None:
    _rect(ink.painter, 9.5, 6.5, 11.0, 11.0, 1.5)
    ink.mark(2.6)
    _line(ink.painter, 3.6, 4.0, 3.6, 20.0)
    ink.mark()
    _arrow(ink.painter, 8.2, 12.0, 5.4, 12.0, 2.8)


def _origin_vertex(ink: _Ink) -> None:
    _poly(ink.painter, [(5, 4), (5, 19), (20, 19)])
    ink.fill()
    _circle(ink.painter, 5, 19, 2.6)


def _origin_hole(ink: _Ink) -> None:
    _circle(ink.painter, 12, 12, 6.0)
    ink.mark(1.4)
    for x1, y1, x2, y2 in (
        (12, 3.5, 12, 8.0), (12, 16.0, 12, 20.5),
        (3.5, 12, 8.0, 12), (16.0, 12, 20.5, 12),
    ):
        _line(ink.painter, x1, y1, x2, y2)


def _datum_new(ink: _Ink) -> None:
    _poly(ink.painter, [(11, 5.5), (19.5, 11), (11, 16.5), (2.5, 11)], close=True)
    _badge(ink, "plus")


def _datum_place(ink: _Ink) -> None:
    """A stamp already sitting on the datum, rather than an arrow towards one -
    otherwise this and "normal to face" are the same picture."""
    _poly(ink.painter, [(12, 8.5), (21, 14.0), (12, 19.5), (3, 14.0)], close=True)
    ink.fill()
    _poly(ink.painter, [(12, 10.6), (16.4, 13.4), (12, 16.2), (7.6, 13.4)], close=True)
    ink.line()


def _replace_part(ink: _Ink) -> None:
    _turn(ink.painter, 12, 12, 7.0, 55, 145)
    _turn(ink.painter, 12, 12, 7.0, 235, 145)


def _relink(ink: _Ink) -> None:
    painter = ink.painter
    painter.save()
    painter.translate(12, 12)
    painter.rotate(-45)
    _rect(painter, -8.6, -3.7, 8.2, 7.4, 3.7)
    _rect(painter, 0.4, -3.7, 8.2, 7.4, 3.7)
    ink.mark()
    _line(painter, -2.2, 0, 2.2, 0)
    painter.restore()
    ink.line()


def _limits(ink: _Ink) -> None:
    _line(ink.painter, 4.0, 5.5, 4.0, 18.5)
    _line(ink.painter, 20.0, 5.5, 20.0, 18.5)
    ink.mark()
    _line(ink.painter, 4.0, 12, 20.0, 12)
    _head(ink.painter, 4.6, 12, -1, 0, 3.0)
    _head(ink.painter, 19.4, 12, 1, 0, 3.0)


def _export_step(ink: _Ink) -> None:
    _cube(ink.painter, 10.6, 10.6, 7.4)
    _badge(ink, "down")


def _export_stl(ink: _Ink) -> None:
    _poly(ink.painter, [(10.6, 3.2), (19.4, 17.8), (1.8, 17.8)], close=True)
    _poly(ink.painter, [(6.2, 10.5), (15.0, 10.5), (10.6, 17.8), (6.2, 10.5)])
    _badge(ink, "down")


def _export_3mf(ink: _Ink) -> None:
    """One cube, three faces in three tones - 3MF is the format that carries
    which filament each body prints in."""
    ink.wash(alpha=30)
    _cube_face(ink.painter, 10.6, 10.6, 7.4, "top")
    ink.wash(alpha=95)
    _cube_face(ink.painter, 10.6, 10.6, 7.4, "left")
    ink.wash(alpha=190)
    _cube_face(ink.painter, 10.6, 10.6, 7.4, "right")
    ink.line()
    _cube(ink.painter, 10.6, 10.6, 7.4)
    _badge(ink, "down")


def _export_quote(ink: _Ink) -> None:
    _poly(
        ink.painter,
        [(4, 4.5), (12, 4.5), (20, 12.5), (12, 20.5), (4, 12.5)],
        close=True,
    )
    ink.fill()
    _circle(ink.painter, 8.0, 8.5, 1.9)


def _export_proof(ink: _Ink) -> None:
    _doc(ink.painter)
    ink.mark(2.0)
    _poly(ink.painter, [(7.5, 14.0), (10.5, 17.0), (16.0, 10.0)])


def _export_package(ink: _Ink) -> None:
    """A stack of files, because that is what a job package is: the part, the
    drawing and the read-me, written out together for the shop."""
    _doc(ink.painter, x=8.5, y=2.5, w=12.0, h=15.5, fold=4.2)
    ink.painter.save()
    clear = QPainterPath()
    clear.addRect(QRectF(0.0, 4.9, 16.3, 19.5))
    ink.painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    ink.painter.fillPath(clear, QColor(0, 0, 0))
    ink.painter.restore()
    ink.line()
    _doc(ink.painter, x=3.5, y=6.0, w=12.0, h=15.5, fold=4.2)
    ink.mark(1.6)
    _line(ink.painter, 6.5, 14.0, 12.5, 14.0)
    _line(ink.painter, 6.5, 17.5, 12.5, 17.5)
    ink.line()


def _batch(ink: _Ink) -> None:
    for y in (4.6, 10.6, 16.6):
        _rect(ink.painter, 2.8, y, 11.0, 2.8, 1.2)
    ink.mark()
    _arrow(ink.painter, 16.6, 11.9, 21.0, 11.9, 3.2)


def _views(ink: _Ink) -> None:
    ink.wash()
    _cube_face(ink.painter, 12, 12, 7.6, "right")
    ink.line()
    _cube(ink.painter, 12, 12, 7.6)


def _view_normal(ink: _Ink) -> None:
    _poly(ink.painter, [(12, 12.5), (20.5, 17), (12, 21.5), (3.5, 17)], close=True)
    ink.mark()
    _arrow(ink.painter, 12, 3.0, 12, 10.5, 3.4)


def _fit(ink: _Ink) -> None:
    for points in (
        [(4, 9), (4, 4), (9, 4)],
        [(15, 4), (20, 4), (20, 9)],
        [(20, 15), (20, 20), (15, 20)],
        [(9, 20), (4, 20), (4, 15)],
    ):
        _poly(ink.painter, points)
    ink.mark(1.4)
    _rect(ink.painter, 9.0, 9.0, 6.0, 6.0, 1.0)


def _roll_left(ink: _Ink) -> None:
    _turn(ink.painter, 12, 12, 7.6, 60, 285)


def _roll_right(ink: _Ink) -> None:
    _turn(ink.painter, 12, 12, 7.6, 120, -285)


def _preview(ink: _Ink) -> None:
    path = QPainterPath(QPointF(2.8, 12))
    path.quadTo(QPointF(12, 3.4), QPointF(21.2, 12))
    path.quadTo(QPointF(12, 20.6), QPointF(2.8, 12))
    ink.painter.drawPath(path)
    ink.fill()
    _circle(ink.painter, 12, 12, 2.8)


def _draft(ink: _Ink) -> None:
    _rect(ink.painter, 3.5, 5.0, 17.0, 14.0, 1.6)
    frame = QPainterPath()
    frame.addRoundedRect(QRectF(4.4, 5.9, 15.2, 12.2), 1.2, 1.2)
    ink.painter.save()
    ink.painter.setClipPath(frame)
    ink.mark(1.3)
    for offset in (-3.0, 2.0, 7.0):
        _line(ink.painter, 5.0 + offset, 18.6, 10.0 + offset, 5.4)
    ink.painter.restore()
    ink.line()


def _inspect(ink: _Ink) -> None:
    _circle(ink.painter, 10.5, 10.5, 6.2)
    _line(ink.painter, 15.2, 15.2, 20.5, 20.5)
    ink.mark(1.4)
    _line(ink.painter, 10.5, 7.2, 10.5, 13.8)
    _line(ink.painter, 7.2, 10.5, 13.8, 10.5)


def _chevron_up(ink: _Ink) -> None:
    _poly(ink.painter, [(6.5, 14.5), (12, 9.0), (17.5, 14.5)])


def _chevron_down(ink: _Ink) -> None:
    _poly(ink.painter, [(6.5, 9.5), (12, 15.0), (17.5, 9.5)])


DRAWINGS = {
    "open-part": _open_part,
    "open-project": _open_project,
    "save": _save,
    "add-profile": _add_profile,
    "add-text": _add_text,
    "add-code": _add_code,
    "undo": _undo,
    "redo": _redo,
    "save-preset": _save_preset,
    "insert-preset": _insert_preset,
    "align-edge": _align_edge,
    "origin-vertex": _origin_vertex,
    "origin-hole": _origin_hole,
    "datum-new": _datum_new,
    "datum-place": _datum_place,
    "replace-part": _replace_part,
    "relink": _relink,
    "limits": _limits,
    "export-step": _export_step,
    "export-stl": _export_stl,
    "export-3mf": _export_3mf,
    "export-quote": _export_quote,
    "export-proof": _export_proof,
    "export-package": _export_package,
    "batch": _batch,
    "views": _views,
    "view-normal": _view_normal,
    "fit": _fit,
    "roll-left": _roll_left,
    "roll-right": _roll_right,
    "preview": _preview,
    "draft": _draft,
    "inspect": _inspect,
    "chevron-up": _chevron_up,
    "chevron-down": _chevron_down,
}


def pixmap(name: str, size: int, color: QColor, accent: QColor, ratio: float = 3.0) -> QPixmap:
    """Draw *name* at *size* logical pixels.

    Drawn at *ratio* times the logical size and told to describe itself as such,
    so one icon is sharp at 100% and at 300% without a second file.
    """
    device = max(1, int(round(size * ratio)))
    out = QPixmap(device, device)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(device / GRID, device / GRID)
    draw = DRAWINGS.get(name)
    if draw is not None:
        draw(_Ink(painter, color, accent))
    painter.end()
    out.setDevicePixelRatio(ratio)
    return out


def icon(name: str, color: QColor, accent: QColor, size: int = 22) -> QIcon:
    """The cached icon for *name*, in the colours the theme is using now."""
    key = (name, color.rgba(), accent.rgba(), size)
    hit = _CACHE.get(key)
    if hit is None:
        hit = QIcon(pixmap(name, size, color, accent))
        _CACHE[key] = hit
    return hit


def names() -> list[str]:
    return sorted(DRAWINGS)


__all__ = ["DRAWINGS", "GRID", "icon", "names", "pixmap"]
