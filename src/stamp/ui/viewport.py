"""The OpenCascade AIS/V3d viewport, embedded in a PySide6 widget.

Ported from CQ-editor's occt_widget.py (PyQt5) to PySide6.  The only coupling
between Qt and OpenCascade is a single native window handle, which OCC wraps in a
platform window object (WNT_Window / Xw_Window / Cocoa_Window).
"""

from __future__ import annotations

import ctypes
import platform

from OCP.AIS import AIS_DisplayMode, AIS_InteractiveContext, AIS_Shaded, AIS_Shape
from OCP.Aspect import (
    Aspect_DisplayConnection,
    Aspect_GradientFillMethod,
    Aspect_PolygonOffsetMode,
    Aspect_TypeOfLine,
    Aspect_TypeOfTriedronPosition,
)
from OCP.gp import gp_Dir
from OCP.Graphic3d import (
    Graphic3d_MaterialAspect,
    Graphic3d_NameOfMaterial_Aluminum,
    Graphic3d_NameOfMaterial_Plastered,
    Graphic3d_TypeOfShadingModel,
)
from OCP.OpenGl import OpenGl_GraphicDriver
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB, Quantity_TOC_sRGB
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS_Shape
from OCP.V3d import (
    V3d_AmbientLight,
    V3d_DirectionalLight,
    V3d_TypeOfOrientation,
    V3d_Viewer,
)
from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent, QResizeEvent, QWheelEvent
from PySide6.QtWidgets import QWidget

from stamp import diagnostics

#: Face boundary edges.  They are what makes a contour readable on a shaded solid.
#: This one is sRGB, not the linear RGB the other colors here use: Quantity_TOC_RGB
#: takes linear values, so a "dark" triple given that way comes out mid-gray.
BOUNDARY_COLOR = (0.13, 0.15, 0.19)
BOUNDARY_WIDTH = 1.6

#: Named preset views, bound to the 1-6 keys.
PRESET_VIEWS: dict[str, V3d_TypeOfOrientation] = {
    "front": V3d_TypeOfOrientation.V3d_TypeOfOrientation_Zup_Front,
    "back": V3d_TypeOfOrientation.V3d_TypeOfOrientation_Zup_Back,
    "left": V3d_TypeOfOrientation.V3d_TypeOfOrientation_Zup_Left,
    "right": V3d_TypeOfOrientation.V3d_TypeOfOrientation_Zup_Right,
    "top": V3d_TypeOfOrientation.V3d_TypeOfOrientation_Zup_Top,
    "bottom": V3d_TypeOfOrientation.V3d_TypeOfOrientation_Zup_Bottom,
    "iso": V3d_TypeOfOrientation.V3d_TypeOfOrientation_Zup_AxoRight,
}


def _pointer_handle(handle: int):
    """Wrap a native window handle in a PyCapsule.

    OCP 7.9 binds OCCT's window handle as a raw pointer on Windows (an ``HWND``)
    and on macOS (an ``NSView*``), and pybind11 accepts only a PyCapsule for a
    pointer argument - an int raises ``TypeError``.  X11 is the exception: an
    ``XID`` really is an integer there, so the Linux path passes the int through
    unchanged (see :meth:`Viewport._make_window`).
    """
    new_capsule = ctypes.pythonapi.PyCapsule_New
    new_capsule.restype = ctypes.py_object
    new_capsule.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
    return new_capsule(ctypes.c_void_p(handle), None, None)


SELECTION_MODES: dict[str, TopAbs_ShapeEnum] = {
    "shape": TopAbs_ShapeEnum.TopAbs_SHAPE,
    "face": TopAbs_ShapeEnum.TopAbs_FACE,
    "edge": TopAbs_ShapeEnum.TopAbs_EDGE,
    "vertex": TopAbs_ShapeEnum.TopAbs_VERTEX,
}


class Viewport(QWidget):
    """A 3D view with native OCC face and edge selection.

    Signals
    -------
    ready
        Emitted once, after the OpenGL context and viewer exist.
    picked
        (TopoDS_Shape, gp_Pnt) - a sub-shape was clicked, with the 3D point under
        the cursor.  The sub-shape kind follows :meth:`set_selection_mode`.
    nothing_picked
        The user clicked empty space, which clears the selection.
    init_failed
        The viewer could not start.  Sent once, with the reason.
    """

    ready = Signal()
    #: The viewer could not start; the argument says why.  Emitted at most once.
    init_failed = Signal(str)
    picked = Signal(object, object)
    nothing_picked = Signal()
    view_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # These three attributes plus the paintEngine() override below are what stop
        # Qt from drawing into the window underneath OpenGL.
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_PaintOnScreen)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(320, 240)

        self._initialised = False
        self._init_failed = False
        self._selection_mode = "face"
        self._drag_start: QPoint | None = None
        self._drag_kind: str | None = None
        self._displayed: dict[str, AIS_Shape] = {}
        #: Where the last pick happened, so a caller can cast its own ray.
        self.last_pick_position: QPoint | None = None

        self._startup_fitted = False
        self.viewer: V3d_Viewer | None = None
        self.view = None
        self.context: AIS_InteractiveContext | None = None

    # ------------------------------------------------------------------ Qt glue

    def paintEngine(self):  # noqa: N802 - Qt naming
        """Return None so Qt never paints over the OpenGL surface."""
        return None

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._initialised:
            self._init_viewer()

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._initialised:
            self._init_viewer()
        elif self.view is not None:
            self.view.Redraw()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_window_size()

    def _sync_window_size(self, refit: bool = False) -> None:
        """Make the GL surface match the widget.

        Qt sizes the native window one event-loop turn after it reports the new
        widget geometry, so at ``showEvent`` time the handle is still 640x480.
        ``DoResize`` re-reads the handle, and the deferred call in
        :meth:`_init_viewer` is the one that catches the real size.
        """
        if self.view is None or self._window is None:
            return
        self._window.DoResize()
        self.view.MustBeResized()
        if refit and self._displayed and not self._startup_fitted:
            # The startup fit ran while the view still had the stale 640x480
            # viewport, so its framing is wrong.  Do it again, once.
            self._startup_fitted = True
            self.fit_all()

    # -------------------------------------------------------------- viewer setup

    def _init_viewer(self) -> None:
        """Start the viewer once, and never retry a start that has failed.

        Every repaint calls this until the viewer exists, so a viewer that cannot
        start raises again on every frame - and the error dialog Stamp shows in
        answer repaints the widget under it, which raises again.  That is how one
        bad window handle became an endless stack of error boxes at launch.  A
        failure now disables the viewport and reports itself exactly once.
        """
        if self._initialised or self._init_failed:
            return
        try:
            self._start_viewer()
        except Exception as exc:  # noqa: BLE001 - no OCC failure may loop
            self._init_failed = True
            self.viewer = None
            self.view = None
            self.context = None
            self._window = None
            diagnostics.note_exception("viewport startup", exc)
            self.init_failed.emit(f"{type(exc).__name__}: {exc}")
            return

        self._initialised = True
        self._sync_window_size()
        QTimer.singleShot(0, lambda: self._sync_window_size(refit=True))
        QTimer.singleShot(60, lambda: self._sync_window_size(refit=True))
        self.ready.emit()

    def _start_viewer(self) -> None:
        self._display_connection = Aspect_DisplayConnection()
        self._driver = OpenGl_GraphicDriver(self._display_connection)

        self.viewer = V3d_Viewer(self._driver)
        self._setup_lights()

        self.view = self.viewer.CreateView()
        self._window = self._make_window()
        self.view.SetWindow(self._window)
        if not self._window.IsMapped():
            self._window.Map()

        self.context = AIS_InteractiveContext(self.viewer)
        self.context.SetDisplayMode(AIS_DisplayMode.AIS_Shaded, True)
        self.context.SetAutomaticHilight(True)

        params = self.view.ChangeRenderingParams()
        params.NbMsaaSamples = 8
        params.IsAntialiasingEnabled = True
        params.ShadingModel = Graphic3d_TypeOfShadingModel.Graphic3d_TypeOfShadingModel_Phong
        # No cast shadows.  See _setup_lights.
        params.IsShadowEnabled = False

        self.set_background(0.20, 0.22, 0.26, 0.08, 0.09, 0.11)
        self.view.TriedronDisplay(
            Aspect_TypeOfTriedronPosition.Aspect_TOTP_RIGHT_LOWER,
            Quantity_Color(0.8, 0.8, 0.8, Quantity_TOC_RGB),
            0.08,
        )
        self.view.SetProj(PRESET_VIEWS["iso"])

    def _setup_lights(self) -> None:
        """Even light from every side, the way a CAD viewer lights a part.

        No light casts a shadow.  A cast shadow hides the very geometry the user
        is trying to look at, and on a part the form is read from the shading of
        each face, not from the shadow it throws.  Four directional lights and a
        strong ambient give every face a different value with nothing in the dark.
        """
        assert self.viewer is not None

        #: (direction, intensity, is a headlight)
        rig = (
            ((-0.35, -0.65, -0.70), 1.35, True),   # key, over the shoulder
            ((0.60, 0.35, -0.45), 0.75, True),     # fill, the other side
            ((0.10, 0.75, 0.35), 0.55, True),      # from behind and below
            ((-0.55, 0.30, 0.60), 0.45, True),     # a touch on the far top
        )
        for direction, intensity, headlight in rig:
            light = V3d_DirectionalLight(
                gp_Dir(*direction),
                Quantity_Color(1.0, 0.99, 0.97, Quantity_TOC_RGB),
                headlight,
            )
            light.SetIntensity(intensity)
            self.viewer.AddLight(light)
            self.viewer.SetLightOn(light)

        ambient = V3d_AmbientLight(Quantity_Color(0.55, 0.56, 0.60, Quantity_TOC_RGB))
        self.viewer.AddLight(ambient)
        self.viewer.SetLightOn(ambient)

    def _make_window(self):
        # PySide may return a Shiboken wrapper for winId(), hence the conversion.
        # What OCP wants from there differs by platform: a pointer capsule on
        # Windows and macOS, a plain XID integer on X11.  See _pointer_handle.
        handle = int(self.winId())
        system = platform.system()
        if system == "Windows":
            from OCP.WNT import WNT_Window

            return WNT_Window(_pointer_handle(handle))
        if system == "Darwin":
            from OCP.Cocoa import Cocoa_Window

            return Cocoa_Window(_pointer_handle(handle))
        # Linux/BSD: X11 or XWayland.  Native Wayland is not supported - see spec 3.1.
        from OCP.Xw import Xw_Window

        return Xw_Window(self._display_connection, handle)

    # ------------------------------------------------------------------- display

    def display_shape(
        self,
        key: str,
        shape: TopoDS_Shape,
        *,
        color: tuple[float, float, float] | None = None,
        transparency: float = 0.0,
        material: bool = True,
        matte: bool = False,
        selectable: bool = True,
        update: bool = True,
    ) -> AIS_Shape | None:
        """Display *shape* under *key*, replacing anything already shown there.

        Returns ``None`` when the viewer could not start, so a dead viewport makes
        the 3D view empty rather than making every later call raise.
        """
        if self.context is None:
            self._init_viewer()
        if self.context is None:
            return None

        self.erase(key, update=False)
        ais = AIS_Shape(shape)
        if color is not None:
            ais.SetColor(Quantity_Color(*color, Quantity_TOC_RGB))
        if material:
            ais.SetMaterial(Graphic3d_MaterialAspect(Graphic3d_NameOfMaterial_Aluminum))
            # A shaded face against a shaded face of the same color has no visible
            # border.  The boundary edges give the eye the contour.
            drawer = ais.Attributes()
            drawer.SetFaceBoundaryDraw(True)
            drawer.SetupOwnFaceBoundaryAspect()
            boundary = drawer.FaceBoundaryAspect()
            boundary.SetColor(Quantity_Color(*BOUNDARY_COLOR, Quantity_TOC_sRGB))
            boundary.SetTypeOfLine(Aspect_TypeOfLine.Aspect_TOL_SOLID)
            boundary.SetWidth(BOUNDARY_WIDTH)
            # Push the shaded faces back in depth.  Without this the boundary
            # lines sit at the same depth as the surface and the surface wins.
            ais.SetPolygonOffsets(
                int(Aspect_PolygonOffsetMode.Aspect_POM_Fill), 1.0, 1.0
            )
        elif matte:
            # OCC's implicit material is noticeably glossy.  A plaster finish
            # gives imported triangulations readable, neutral shading.
            ais.SetMaterial(
                Graphic3d_MaterialAspect(Graphic3d_NameOfMaterial_Plastered)
            )
        if transparency:
            ais.SetTransparency(transparency)
        self.context.Display(ais, AIS_Shaded, 0, False)
        self.context.Deactivate(ais)
        if selectable:
            self.context.Activate(
                ais, AIS_Shape.SelectionMode_s(SELECTION_MODES[self._selection_mode])
            )
        self._displayed[key] = ais
        if update:
            self.context.UpdateCurrentViewer()
        return ais

    def erase(self, key: str, *, update: bool = True) -> None:
        ais = self._displayed.pop(key, None)
        if ais is not None and self.context is not None:
            self.context.Remove(ais, False)
            if update:
                self.context.UpdateCurrentViewer()

    def clear(self) -> None:
        if self.context is None:
            return
        for key in list(self._displayed):
            self.erase(key, update=False)
        self.context.UpdateCurrentViewer()

    def has(self, key: str) -> bool:
        return key in self._displayed

    def set_transparency(self, key: str, value: float) -> None:
        ais = self._displayed.get(key)
        if ais is not None and self.context is not None:
            self.context.SetTransparency(ais, value, True)

    # ------------------------------------------------------------------ camera

    def fit_all(self) -> None:
        if self.view is not None:
            self.view.FitAll(0.1, False)
            self.view.ZFitAll()
            self.view.Redraw()

    def set_preset_view(self, name: str) -> None:
        if self.view is None:
            return
        self.view.SetProj(PRESET_VIEWS[name])
        self.fit_all()

    def set_background(self, r1, g1, b1, r2, g2, b2) -> None:
        """Set the vertical gradient behind the part, from sRGB triples.

        sRGB, not the linear RGB the lights and materials use.  The background
        sits next to the Qt panels, so it has to be the colour a stylesheet would
        name: given to Quantity_TOC_RGB, a dark slate came out mid-grey and the
        3D view read as a light panel in a dark window.  Same trap as
        :data:`BOUNDARY_COLOR`.
        """
        if self.view is None:
            return
        self.view.SetBgGradientColors(
            Quantity_Color(r1, g1, b1, Quantity_TOC_sRGB),
            Quantity_Color(r2, g2, b2, Quantity_TOC_sRGB),
            Aspect_GradientFillMethod.Aspect_GradientFillMethod_Vertical,
            True,
        )

    # ---------------------------------------------------------------- selection

    def set_selection_mode(self, mode: str) -> None:
        """Switch between face, edge, vertex and whole-shape picking."""
        if mode not in SELECTION_MODES:
            raise ValueError(f"Unknown selection mode {mode!r}")
        self._selection_mode = mode
        if self.context is None:
            return
        for ais in self._displayed.values():
            self.context.Deactivate(ais)
            self.context.Activate(ais, AIS_Shape.SelectionMode_s(SELECTION_MODES[mode]))
        self.context.UpdateCurrentViewer()

    @property
    def selection_mode(self) -> str:
        return self._selection_mode

    def clear_selection(self) -> None:
        if self.context is not None:
            self.context.ClearSelected(True)

    # -------------------------------------------------------- pixel conversion

    def device_px(self, value: float) -> int:
        """Qt reports positions in logical pixels; the OCC view is in device ones.

        On a Windows display at 150% those differ by half: OCC sizes its window
        from the HWND, which is physical, while every Qt mouse position arrives
        scaled down.  Handing one to the other put every click a third of the way
        up and to the left of where the user aimed.  Everything crossing into the
        view goes through here, and :meth:`logical_px` brings it back.
        """
        return int(round(value * self.devicePixelRatioF()))

    def logical_px(self, value: float) -> float:
        """Device pixels from the view, back in the units Qt positions use."""
        ratio = self.devicePixelRatioF()
        return value / ratio if ratio else value

    # ------------------------------------------------------------ mouse handling

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.view is None:
            return
        pos = event.position().toPoint()
        self._drag_start = pos
        buttons = event.buttons()
        mods = event.modifiers()

        if buttons & Qt.MouseButton.MiddleButton:
            if mods & Qt.KeyboardModifier.ShiftModifier:
                self._drag_kind = "pan"
            else:
                self._drag_kind = "orbit"
                self.view.StartRotation(self.device_px(pos.x()), self.device_px(pos.y()))
        elif buttons & Qt.MouseButton.RightButton:
            self._drag_kind = "pan"
        else:
            self._drag_kind = "select"

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.view is None or self.context is None:
            return
        pos = event.position().toPoint()

        if self._drag_kind == "orbit":
            self.view.Rotation(self.device_px(pos.x()), self.device_px(pos.y()))
            self.view_changed.emit()
        elif self._drag_kind == "pan" and self._drag_start is not None:
            delta = pos - self._drag_start
            self.view.Pan(self.device_px(delta.x()), -self.device_px(delta.y()))
            self._drag_start = pos
            self.view_changed.emit()
        elif self._drag_kind is None:
            self.context.MoveTo(
                self.device_px(pos.x()), self.device_px(pos.y()), self.view, True
            )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.view is None or self.context is None:
            return
        pos = event.position().toPoint()
        moved = self._drag_start is not None and (pos - self._drag_start).manhattanLength() > 3

        if self._drag_kind == "select" and not moved:
            self._do_pick(pos, additive=bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))

        self._drag_kind = None
        self._drag_start = None

    def _do_pick(self, pos: QPoint, *, additive: bool) -> None:
        if self.context is None or self.view is None:
            return
        self.last_pick_position = QPoint(pos)
        self.context.MoveTo(
            self.device_px(pos.x()), self.device_px(pos.y()), self.view, True
        )
        if additive:
            self.context.ShiftSelect(True)
        else:
            self.context.Select(True)

        if not self.context.HasDetected():
            self.nothing_picked.emit()
            return

        shape = self.context.DetectedShape()
        point = self._point_at(pos)
        self.picked.emit(shape, point)

    def _point_at(self, pos: QPoint):
        """Return the 3D point under the cursor, from the selector's pick depth."""
        from OCP.gp import gp_Pnt

        assert self.view is not None and self.context is not None  # _do_pick checked
        if self.context.HasDetected():
            selector = self.context.MainSelector()
            if selector.NbPicked() > 0:
                p = selector.PickedPoint(1)
                return gp_Pnt(p.X(), p.Y(), p.Z())
        x, y, z = self.view.Convert(self.device_px(pos.x()), self.device_px(pos.y()))
        return gp_Pnt(x, y, z)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if self.view is None:
            return
        pos = event.position().toPoint()
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = 1.12 if delta > 0 else 1.0 / 1.12
        self.view.StartZoomAtPoint(self.device_px(pos.x()), self.device_px(pos.y()))
        self.view.SetZoom(factor, True)
        self.view.Redraw()
        self.view_changed.emit()

    def ray_at(self, x: int, y: int):
        """The eye ray through a window position, as ``(origin, direction)``.

        Mesh mode needs this: OCC selection sees an STL as one large face, so the
        triangle under the cursor has to be found by casting the ray directly.
        """
        if self.view is None:
            return None
        try:
            px, py, pz, dx, dy, dz = self.view.ConvertWithProj(
                self.device_px(x), self.device_px(y)
            )
        except Exception:
            return None
        return (px, py, pz), (dx, dy, dz)

    def pick_at(self, x: int, y: int):
        """Return ``(sub_shape, gp_Pnt)`` under a window position, or None.

        Used by the drop gesture: an SVG dropped on a face has to know which face
        it landed on before the mouse has ever been pressed.
        """
        if self.context is None or self.view is None:
            return None
        self.context.MoveTo(self.device_px(x), self.device_px(y), self.view, True)
        if not self.context.HasDetected():
            return None
        shape = self.context.DetectedShape()
        if shape is None or shape.IsNull():
            return None
        return shape, self._point_at(QPoint(x, y))

    def set_display_quality(self, draft: bool) -> None:
        """Coarsen the tessellation used for display only (§10).

        The geometry is unchanged; this only affects how finely it is drawn, which
        is the lever worth pulling when a rebuild has become slow.
        """
        if self.context is None:
            return
        drawer = self.context.DefaultDrawer()
        drawer.SetDeviationCoefficient(0.01 if draft else 0.001)
        drawer.SetDeviationAngle(0.5 if draft else 0.2)
        for key, ais in list(self._displayed.items()):
            if key in ("handles", "handle_frame", "snap_marker"):
                continue
            self.context.Redisplay(ais, False)
        self.context.UpdateCurrentViewer()

    # --------------------------------------------------------------- screenshot

    def screenshot(self, path: str) -> bool:
        """Dump the current view to a PNG.  Used for project thumbnails."""
        if self.view is None:
            return False
        self.view.Redraw()
        return bool(self.view.Dump(path))
