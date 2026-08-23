"""Screen-space drag handles - spec §6.2, milestone M3.

Placement should feel like moving a sticker, not like typing coordinates.  The
mouse gets you close; the numeric panel is still where exact values are set.

Two constraints shape the implementation:

* The viewport is a native OpenGL window with ``WA_PaintOnScreen``, so a Qt widget
  cannot paint an overlay on top of it.  The handles are therefore OCC presentations
  drawn in the scene, using point markers whose size is specified in *pixels* - so
  they stay the same size on screen at any zoom, which is what §6.2 asks for.
* Hit testing does not go through OCC selection.  Handle positions are projected to
  window coordinates and compared against the cursor there, which is exact and cheap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum, auto

from OCP.Aspect import Aspect_TypeOfMarker
from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeVertex
from OCP.gp import gp_Pnt
from OCP.Prs3d import Prs3d_LineAspect, Prs3d_PointAspect
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.TopoDS import TopoDS_Compound
from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent

from stamp.core.document import Feature, Plane
from stamp.core.snapping import SnapKind, SnapTarget, nearest

HANDLE_KEY = "handles"
FRAME_KEY = "handle_frame"
SNAP_KEY = "snap_marker"

#: Pixel radius within which a click counts as hitting a handle.
HIT_RADIUS_PX = 9.0

#: Marker size in pixels.  Constant on screen at any zoom.
MARKER_PX = 8.0

#: How far above the top edge the rotation handle sits, in pixels.
ROTATION_STEM_PX = 34.0

HANDLE_COLOR = (0.98, 0.75, 0.20)
FRAME_COLOR = (0.98, 0.75, 0.20)
SNAP_COLOR = (0.94, 0.25, 0.80)  # magenta, per §6.2

#: Arrow-key nudge distances in mm: plain, with Shift, with Ctrl.
NUDGE = 0.1
NUDGE_COARSE = 1.0
NUDGE_FINE = 0.01

#: Default snapping grid pitch, in mm.
GRID_PITCH = 1.0

#: How close, in pixels, a snap target must be to take effect.
SNAP_RADIUS_PX = 12.0


class Mode(Enum):
    NONE = auto()
    TRANSLATE = auto()
    SCALE_CORNER = auto()
    SCALE_EDGE = auto()
    ROTATE = auto()


@dataclass
class _Drag:
    mode: Mode
    handle: int
    start_screen: QPoint
    start_offset: tuple[float, float]
    start_scale: tuple[float, float]
    start_rotation: float
    start_uv: tuple[float, float]


class HandleOverlay(QObject):
    """Draws the manipulation frame and turns drags into placement edits.

    Signals
    -------
    placement_changed
        Emitted continuously during a drag, so the preview can follow.
    placement_committed
        Emitted once on release, with a label for the undo stack.
    """

    placement_changed = Signal(str)
    placement_committed = Signal(str)

    def __init__(self, viewport, parent=None) -> None:
        super().__init__(parent or viewport)
        self.viewport = viewport
        self.feature: Feature | None = None
        self.native_size = (1.0, 1.0)
        self.snapping = True
        self.grid_pitch = GRID_PITCH
        #: Everything the profile can snap to, in plane coordinates.  The window
        #: recomputes it whenever the selection or the geometry changes.
        self.snap_targets: list[SnapTarget] = []
        #: What the last snap landed on, for the status line.
        self.last_snap: SnapTarget | None = None

        self._drag: _Drag | None = None
        self._handles: list[tuple[Mode, int, tuple[float, float]]] = []
        self._snap_uv: tuple[float, float] | None = None

        viewport.installEventFilter(self)

    # ------------------------------------------------------------------ public

    def set_feature(self, feature: Feature | None, native_size: tuple[float, float] | None) -> None:
        self.feature = feature
        if native_size:
            self.native_size = (max(native_size[0], 1e-6), max(native_size[1], 1e-6))
        self.refresh()

    def cancel_drag(self) -> None:
        if self._drag is None:
            return
        drag, self._drag = self._drag, None
        placement = self.feature.placement if self.feature else None
        if placement is not None:
            placement.offset_2d = drag.start_offset
            placement.scale = drag.start_scale
            placement.rotation = drag.start_rotation
            self.placement_changed.emit("cancel")
        self._clear_snap()
        self.refresh()

    def refresh(self) -> None:
        """Recompute the frame and redraw it."""
        self._handles = []
        if self.feature is None or self._plane() is None or self.viewport.view is None:
            self.viewport.erase(FRAME_KEY, update=False)
            self.viewport.erase(HANDLE_KEY, update=False)
            self._clear_snap()
            return

        corners, edges, rotation_point = self._frame_points()
        self._handles = [(Mode.SCALE_CORNER, i, p) for i, p in enumerate(corners)]
        if not self.feature.placement.uniform_scale:
            self._handles += [(Mode.SCALE_EDGE, i, p) for i, p in enumerate(edges)]
        self._handles.append((Mode.ROTATE, 0, rotation_point))

        self._draw_frame(corners, rotation_point)
        self._draw_handles([p for _m, _i, p in self._handles])

    # ------------------------------------------------------------------ frame

    def _plane(self) -> Plane | None:
        if self.feature is None:
            return None
        return self.feature.placement.anchor.plane

    def _half_extents(self) -> tuple[float, float]:
        sx, sy = self.feature.placement.scale
        return (self.native_size[0] * abs(sx) / 2.0, self.native_size[1] * abs(sy) / 2.0)

    def _frame_points(self):
        """Corner, edge-midpoint and rotation handle positions, in plane (u, v)."""
        placement = self.feature.placement
        hw, hh = self._half_extents()
        cu, cv = placement.offset_2d
        angle = math.radians(placement.rotation)
        cos_a, sin_a = math.cos(angle), math.sin(angle)

        def place(x: float, y: float) -> tuple[float, float]:
            return (cu + x * cos_a - y * sin_a, cv + x * sin_a + y * cos_a)

        corners = [place(-hw, -hh), place(hw, -hh), place(hw, hh), place(-hw, hh)]
        edges = [place(0, -hh), place(hw, 0), place(0, hh), place(-hw, 0)]

        stem_mm = self._pixels_to_mm(ROTATION_STEM_PX)
        rotation_point = place(0, hh + stem_mm)
        return corners, edges, rotation_point

    def _pixels_to_mm(self, pixels: float) -> float:
        """Convert a pixel distance to millimetres at the current zoom."""
        view = self.viewport.view
        if view is None:
            return pixels * 0.1
        try:
            return abs(view.Convert(int(pixels)))
        except Exception:
            return pixels * 0.1

    # ------------------------------------------------------------------ drawing

    def _draw_frame(self, corners, rotation_point) -> None:
        maker = BRepBuilderAPI_MakePolygon()
        for u, v in corners:
            maker.Add(gp_Pnt(*self._world(u, v)))
        maker.Close()
        frame = maker.Wire()

        stem = BRepBuilderAPI_MakePolygon()
        top_u = (corners[2][0] + corners[3][0]) / 2.0
        top_v = (corners[2][1] + corners[3][1]) / 2.0
        stem.Add(gp_Pnt(*self._world(top_u, top_v)))
        stem.Add(gp_Pnt(*self._world(*rotation_point)))

        compound = TopoDS_Compound()
        builder = BRep_Builder()
        builder.MakeCompound(compound)
        builder.Add(compound, frame)
        if stem.IsDone():
            builder.Add(compound, stem.Wire())

        ais = self.viewport.display_shape(
            FRAME_KEY, compound, color=FRAME_COLOR, material=False,
            selectable=False, update=False,
        )
        from OCP.Aspect import Aspect_TypeOfLine

        aspect = Prs3d_LineAspect(
            Quantity_Color(*FRAME_COLOR, Quantity_TOC_RGB),
            Aspect_TypeOfLine.Aspect_TOL_SOLID,
            1.6,
        )
        ais.Attributes().SetWireAspect(aspect)
        ais.SetWidth(1.6)

    def _draw_handles(self, points) -> None:
        compound = TopoDS_Compound()
        builder = BRep_Builder()
        builder.MakeCompound(compound)
        for u, v in points:
            builder.Add(compound, BRepBuilderAPI_MakeVertex(gp_Pnt(*self._world(u, v))).Vertex())

        ais = self.viewport.display_shape(
            HANDLE_KEY, compound, color=HANDLE_COLOR, material=False,
            selectable=False, update=True,
        )
        aspect = Prs3d_PointAspect(
            Aspect_TypeOfMarker.Aspect_TOM_O_POINT,
            Quantity_Color(*HANDLE_COLOR, Quantity_TOC_RGB),
            MARKER_PX / 4.0,
        )
        ais.Attributes().SetPointAspect(aspect)
        if self.viewport.context is not None:
            self.viewport.context.Redisplay(ais, True)

    def _show_snap(self, uv: tuple[float, float]) -> None:
        if self._snap_uv == uv:
            return
        self._snap_uv = uv
        vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*self._world(*uv))).Vertex()
        ais = self.viewport.display_shape(
            SNAP_KEY, vertex, color=SNAP_COLOR, material=False,
            selectable=False, update=True,
        )
        aspect = Prs3d_PointAspect(
            Aspect_TypeOfMarker.Aspect_TOM_STAR,
            Quantity_Color(*SNAP_COLOR, Quantity_TOC_RGB),
            MARKER_PX / 3.0,
        )
        ais.Attributes().SetPointAspect(aspect)
        if self.viewport.context is not None:
            self.viewport.context.Redisplay(ais, True)

    def _clear_snap(self) -> None:
        self._snap_uv = None
        self.viewport.erase(SNAP_KEY, update=True)

    # ------------------------------------------------------------- coordinates

    def _world(self, u: float, v: float) -> tuple[float, float, float]:
        plane = self._plane()
        ox, oy, oz = plane.origin
        ux, uy, uz = plane.u_axis
        vx, vy, vz = self._v_axis()
        return (ox + u * ux + v * vx, oy + u * uy + v * vy, oz + u * uz + v * vz)

    def _v_axis(self) -> tuple[float, float, float]:
        plane = self._plane()
        nx, ny, nz = plane.normal
        ux, uy, uz = plane.u_axis
        return (ny * uz - nz * uy, nz * ux - nx * uz, nx * uy - ny * ux)

    def _screen(self, u: float, v: float) -> QPoint | None:
        view = self.viewport.view
        if view is None:
            return None
        x, y, z = self._world(u, v)
        try:
            vx, vy = view.Project(x, y, z)
            px, py = view.Convert(vx, vy)
        except Exception:
            return None
        return QPoint(int(px), int(py))

    def _uv_at(self, position: QPoint) -> tuple[float, float] | None:
        """Intersect the cursor ray with the sketch plane."""
        view = self.viewport.view
        plane = self._plane()
        if view is None or plane is None:
            return None
        try:
            px, py, pz, dx, dy, dz = view.ConvertWithProj(position.x(), position.y())
        except Exception:
            return None

        nx, ny, nz = plane.normal
        denominator = dx * nx + dy * ny + dz * nz
        if abs(denominator) < 1e-9:
            return None
        ox, oy, oz = plane.origin
        t = ((ox - px) * nx + (oy - py) * ny + (oz - pz) * nz) / denominator
        hx, hy, hz = px + t * dx, py + t * dy, pz + t * dz

        ux, uy, uz = plane.u_axis
        vx, vy, vz = self._v_axis()
        rx, ry, rz = hx - ox, hy - oy, hz - oz
        return (rx * ux + ry * uy + rz * uz, rx * vx + ry * vy + rz * vz)

    # ---------------------------------------------------------------- hit test

    def _handle_at(self, position: QPoint):
        best = None
        best_distance = HIT_RADIUS_PX
        for mode, index, uv in self._handles:
            screen = self._screen(*uv)
            if screen is None:
                continue
            distance = math.hypot(screen.x() - position.x(), screen.y() - position.y())
            if distance <= best_distance:
                best = (mode, index, uv)
                best_distance = distance
        return best

    def _inside_frame(self, position: QPoint) -> bool:
        uv = self._uv_at(position)
        if uv is None or self.feature is None:
            return False
        placement = self.feature.placement
        angle = math.radians(placement.rotation)
        du = uv[0] - placement.offset_2d[0]
        dv = uv[1] - placement.offset_2d[1]
        local_u = du * math.cos(-angle) - dv * math.sin(-angle)
        local_v = du * math.sin(-angle) + dv * math.cos(-angle)
        hw, hh = self._half_extents()
        return abs(local_u) <= hw and abs(local_v) <= hh

    # ------------------------------------------------------------ event filter

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is not self.viewport or self.feature is None:
            return False
        kind = event.type()

        if kind == event.Type.MouseButtonPress and isinstance(event, QMouseEvent):
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            return self._begin_drag(event)
        if kind == event.Type.MouseMove and isinstance(event, QMouseEvent):
            if self._drag is None:
                return False
            self._update_drag(event)
            return True
        if kind == event.Type.MouseButtonRelease and isinstance(event, QMouseEvent):
            if self._drag is None:
                return False
            self._end_drag()
            return True
        if kind == event.Type.KeyPress and isinstance(event, QKeyEvent):
            return self._nudge(event)
        return False

    def _begin_drag(self, event: QMouseEvent) -> bool:
        position = event.position().toPoint()
        uv = self._uv_at(position)
        if uv is None:
            return False

        placement = self.feature.placement
        hit = self._handle_at(position)
        if hit is not None:
            mode, index, _ = hit
        elif self._inside_frame(position):
            mode, index = Mode.TRANSLATE, 0
        else:
            return False

        self._drag = _Drag(
            mode=mode,
            handle=index,
            start_screen=position,
            start_offset=placement.offset_2d,
            start_scale=placement.scale,
            start_rotation=placement.rotation,
            start_uv=uv,
        )
        return True

    def _update_drag(self, event: QMouseEvent) -> None:
        drag = self._drag
        uv = self._uv_at(event.position().toPoint())
        if drag is None or uv is None:
            return
        modifiers = event.modifiers()
        placement = self.feature.placement

        if drag.mode is Mode.TRANSLATE:
            du = uv[0] - drag.start_uv[0]
            dv = uv[1] - drag.start_uv[1]
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                if abs(du) >= abs(dv):
                    dv = 0.0
                else:
                    du = 0.0
            target = (drag.start_offset[0] + du, drag.start_offset[1] + dv)
            if self.snapping and not (modifiers & Qt.KeyboardModifier.AltModifier):
                target = self._snap(target)
            placement.offset_2d = target
            self.placement_changed.emit("move")

        elif drag.mode in (Mode.SCALE_CORNER, Mode.SCALE_EDGE):
            self._scale_to(uv, drag, uniform=placement.uniform_scale)
            self.placement_changed.emit("resize")

        elif drag.mode is Mode.ROTATE:
            cu, cv = placement.offset_2d
            angle = math.degrees(math.atan2(uv[1] - cv, uv[0] - cu)) - 90.0
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                angle = round(angle / 15.0) * 15.0
            placement.rotation = angle % 360.0
            self.placement_changed.emit("rotate")

        self.refresh()

    def _scale_to(self, uv, drag: _Drag, *, uniform: bool) -> None:
        placement = self.feature.placement
        cu, cv = placement.offset_2d
        angle = math.radians(drag.start_rotation)
        du, dv = uv[0] - cu, uv[1] - cv
        local_u = abs(du * math.cos(-angle) - dv * math.sin(-angle))
        local_v = abs(du * math.sin(-angle) + dv * math.cos(-angle))

        half_w = self.native_size[0] / 2.0
        half_h = self.native_size[1] / 2.0
        sx = max(local_u / half_w, 1e-4)
        sy = max(local_v / half_h, 1e-4)

        if uniform or drag.mode is Mode.SCALE_CORNER and uniform:
            factor = max(sx, sy)
            placement.scale = (factor, factor)
        elif drag.mode is Mode.SCALE_EDGE:
            # Edge handles 0 and 2 are the v axis; 1 and 3 are the u axis.
            if drag.handle in (0, 2):
                placement.scale = (abs(drag.start_scale[0]), sy)
            else:
                placement.scale = (sx, abs(drag.start_scale[1]))
        else:
            placement.scale = (sx, sy)

    def _end_drag(self) -> None:
        drag, self._drag = self._drag, None
        self._clear_snap()
        if drag is None:
            return
        label = {
            Mode.TRANSLATE: "move",
            Mode.SCALE_CORNER: "resize",
            Mode.SCALE_EDGE: "resize",
            Mode.ROTATE: "rotate",
        }[drag.mode]
        self.placement_committed.emit(label)

    def _nudge(self, event: QKeyEvent) -> bool:
        keys = {
            Qt.Key.Key_Left: (-1.0, 0.0),
            Qt.Key.Key_Right: (1.0, 0.0),
            Qt.Key.Key_Up: (0.0, 1.0),
            Qt.Key.Key_Down: (0.0, -1.0),
        }
        direction = keys.get(event.key())
        if direction is None:
            return False

        step = NUDGE
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            step = NUDGE_COARSE
        elif event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            step = NUDGE_FINE

        placement = self.feature.placement
        placement.offset_2d = (
            placement.offset_2d[0] + direction[0] * step,
            placement.offset_2d[1] + direction[1] * step,
        )
        self.refresh()
        self.placement_committed.emit("nudge")
        return True

    # ---------------------------------------------------------------- snapping

    @property
    def alignment_targets(self) -> list[tuple[float, float]]:
        """The other features on this plane, as plain points."""
        return [t.uv for t in self.snap_targets if t.kind is SnapKind.FEATURE]

    @alignment_targets.setter
    def alignment_targets(self, values: list[tuple[float, float]]) -> None:
        keep = [t for t in self.snap_targets if t.kind is not SnapKind.FEATURE]
        self.snap_targets = keep + [
            SnapTarget(u, v, SnapKind.FEATURE) for u, v in values
        ]

    def _snap(self, target: tuple[float, float]) -> tuple[float, float]:
        """Snap to a target, then to the grid, then to a single shared axis (§6.2).

        The order matters.  A point target is what the user aimed at, the grid is a
        fallback, and lining up on one axis alone is the last resort - it is how two
        labels sit on the same line without sitting on the same spot.
        """
        tolerance = self._pixels_to_mm(SNAP_RADIUS_PX)

        hit = nearest(self.snap_targets, target, tolerance)
        if hit is not None:
            self.last_snap = hit
            self._show_snap(hit.uv)
            return hit.uv

        if self.grid_pitch > 0:
            grid = (
                round(target[0] / self.grid_pitch) * self.grid_pitch,
                round(target[1] / self.grid_pitch) * self.grid_pitch,
            )
            if math.dist(target, grid) < tolerance:
                self.last_snap = SnapTarget(grid[0], grid[1], SnapKind.GRID)
                self._show_snap(grid)
                return grid

        for candidate in self.snap_targets:
            if abs(target[0] - candidate.u) < tolerance:
                self.last_snap = candidate
                snapped = (candidate.u, target[1])
                self._show_snap(snapped)
                return snapped
            if abs(target[1] - candidate.v) < tolerance:
                self.last_snap = candidate
                snapped = (target[0], candidate.v)
                self._show_snap(snapped)
                return snapped

        self.last_snap = None
        self._clear_snap()
        return target


__all__ = ["HandleOverlay", "Mode"]
