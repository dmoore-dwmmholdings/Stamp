"""The single window - spec §7.

No modes, no ribbon, no floating palettes.  Feature tree on the left, viewport in
the middle, properties on the right, a status line underneath the viewport, and one
toolbar along the bottom.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from stamp import __version__, diagnostics, reporting
from stamp.core import replace_part as replace_part_io
from stamp.core import snapping
from stamp.core.document import (
    Anchor,
    AnchorKind,
    Document,
    EdgeRole,
    EdgeSelector,
    Feature,
    Modifier,
    ModifierKind,
    Operation,
    OperationKind,
    Placement,
    ProfileRef,
    TextSpec,
    UndoStack,
)
from stamp.core.profiles import ProfileCache
from stamp.core.rebuild import RebuildEngine, RebuildResult
from stamp.core.refs import make_face_ref, plane_from_face, resolve_face_ref
from stamp.geom import mesh_regions
from stamp.geom.mesh_regions import DEFAULT_TOLERANCE_DEG
from stamp.io import export as export_io
from stamp.io import project as project_io
from stamp.io.normalize import IssueKind
from stamp.io.part_import import (
    DECIMATE_THRESHOLD,
    PART_EXTS,
    PartImportError,
    import_part,
    manifold_display_shape,
    solids_intersect,
    trimesh_display_shape,
)
from stamp.io.profile_import import (
    PROFILE_EXTS,
    DwgUnavailable,
    ImportOptions,
    default_dxf_layers,
    dxf_layers,
    import_profile,
    set_oda_converter,
)
from stamp.ui import dialogs
from stamp.ui.feature_tree import FeatureTree
from stamp.ui.handles import HandleOverlay
from stamp.ui.properties import PropertiesPanel
from stamp.ui.rebuild_worker import PROGRESS_AFTER_MS, RebuildController
from stamp.ui.viewport import Viewport

BASE_KEY = "base"
RESULT_KEY = "result"
PREVIEW_KEY = "preview"
FOOTPRINT_KEY = "footprint"
REGION_KEY = "mesh_region"

#: A rebuild slower than this offers to draw the view more coarsely (§10).
SLOW_REBUILD_MS = 10_000.0

ADD_COLOR = (0.36, 0.72, 0.42)
CUT_COLOR = (0.82, 0.36, 0.32)
PART_COLOR = (0.62, 0.66, 0.72)
REGION_COLOR = (0.36, 0.62, 0.92)


class MainWindow(QMainWindow):
    """Owns the document and drives everything else."""

    document_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Stamp {__version__}")
        self.resize(1400, 880)
        self.setAcceptDrops(True)

        self.document = Document()
        self.profiles = ProfileCache()
        self.engine = RebuildEngine(self.profiles.get)
        self.undo_stack = UndoStack()
        self.settings = QSettings("Stamp", "Stamp")

        diagnostics.error_reporter = self._report_uncaught

        self._project_path: Path | None = None
        self._last_result: RebuildResult | None = None
        self._pending_profile: ProfileRef | None = None
        self._pending_profile_size: tuple[float, float] = (0.0, 0.0)
        self._picking_to_face = False
        self._dirty = False
        self._draft_display = False
        self._slow_offer_declined = False
        self._busy_since = 0.0
        self._busy_step = ""
        self._mesh_pick_cache: dict | None = None
        self._auto_value_attempts: dict[str, int] = {}
        self._auto_value_note = ""
        self._mesh_region = None
        self._undo_baseline = self.document.snapshot()

        #: When False, every dialog answers itself with its default and every
        #: notice goes to the status line instead of a message box.  Tests and
        #: scripted runs set this; nothing in the interactive path touches it.
        self.interactive = True

        remembered = self.settings.value("dwg/converter", "")
        if remembered:
            set_oda_converter(str(remembered))

        self._build_ui()
        self._build_actions()
        self._wire()
        self._update_enabled_state()

    # ------------------------------------------------------------------ layout

    def _build_ui(self) -> None:
        self.viewport = Viewport()
        self.tree = FeatureTree()
        self.properties = PropertiesPanel()
        self.handles = HandleOverlay(self.viewport)

        self._viewport_page = QWidget()
        center_layout = QVBoxLayout(self._viewport_page)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        center_layout.addWidget(self.viewport, 1)
        center_layout.addWidget(self._build_status_strip())

        self._center = QStackedWidget()
        self._center.addWidget(self._build_welcome_page())
        self._center.addWidget(self._viewport_page)
        center = self._center

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.tree)
        splitter.addWidget(center)
        splitter.addWidget(self.properties)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([230, 810, 400])
        self.setCentralWidget(splitter)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Open a part to begin.")

        for caption, slot, tip in (
            ("Report a bug", self.report_bug,
             "Write an email about a problem, with the log already in it."),
            ("Report a crash", self.report_crash,
             "Write an email about Stamp stopping without warning."),
        ):
            button = QPushButton(caption)
            button.setFlat(True)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            self.statusBar().addPermanentWidget(button)
            if slot is self.report_crash:
                self._crash_button = button

    def _build_welcome_page(self) -> QWidget:
        """The first-run screen: two large buttons, and nothing else (§7.1)."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(2)

        title = QLabel("Stamp")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = title.font()
        font.setPointSize(max(font.pointSize() + 18, 30))
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        subtitle = QLabel("Put 2D artwork onto a 3D part as geometry.")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("color: #8a8f98;")
        layout.addWidget(subtitle)
        layout.addSpacing(28)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        for caption, slot in (
            ("Open a part", self.open_part_dialog),
            ("Open a project", self.open_project_dialog),
        ):
            button = QPushButton(caption)
            button.setMinimumSize(220, 68)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addSpacing(20)

        hint = QLabel("Or drop a STEP, STL, or .stamp file anywhere on this window.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: #8a8f98;")
        layout.addWidget(hint)
        layout.addStretch(3)
        return page

    def _show_viewport(self) -> None:
        self._center.setCurrentWidget(self._viewport_page)

    def _build_status_strip(self) -> QWidget:
        strip = QWidget()
        strip.setFixedHeight(30)
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(10, 2, 10, 2)

        self.status_label = QLabel("")
        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet("color: #c58a2a;")
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(160)
        self.progress.setVisible(False)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)

        self.action_preview = QAction("Preview", self)
        self.action_preview.setCheckable(True)
        self.action_preview.setChecked(True)
        self.action_preview.setToolTip(
            "Show the tool solid in color over the part. Turn it off to see the "
            "result on its own."
        )
        self.action_preview.toggled.connect(self.set_preview_visible)

        self.action_draft = QAction("Draft view", self)
        self.action_draft.setCheckable(True)
        self.action_draft.setToolTip(
            "Draw the part more coarsely. The geometry and the exports do not change."
        )
        self.action_draft.toggled.connect(self.set_draft_display)

        layout.addWidget(self.status_label)
        layout.addSpacing(16)
        layout.addWidget(self.warning_label, 1)
        layout.addWidget(self.progress)
        layout.addWidget(self.cancel_button)
        for action in (self.action_preview, self.action_draft):
            button = QToolButton()
            button.setDefaultAction(action)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setAutoRaise(True)
            layout.addWidget(button)
        return strip

    def _build_actions(self) -> None:
        bar = QToolBar("Main")
        bar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.BottomToolBarArea, bar)

        def add(text: str, slot, shortcut: str | None = None) -> QAction:
            action = QAction(text, self)
            action.triggered.connect(slot)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            bar.addAction(action)
            self.addAction(action)
            return action

        self.action_open_part = add("Open part", self.open_part_dialog, "Ctrl+O")
        self.action_open_project = add("Open project", self.open_project_dialog)
        self.action_save = add("Save", self.save_project, "Ctrl+S")
        self.action_replace_part = add("Replace part", self.replace_part_dialog)
        self.action_relink = add("Relink", self.relink_sources)
        bar.addSeparator()
        self.action_add_profile = add("+ Add profile", self.add_profile_dialog, "Ctrl+I")
        self.action_add_text = add("+ Add text", self.add_text_dialog, "Ctrl+T")
        bar.addSeparator()
        self.action_export_step = add("Export STEP", self.export_step)
        self.action_export_stl = add("Export STL", self.export_stl)
        self.action_export_3mf = add("Export 3MF", self.export_3mf)
        self.action_export_quote = add("Export for quote", self.export_for_quote)
        bar.addSeparator()

        self.units_box = QComboBox()
        self.units_box.addItem("Units: mm", "mm")
        self.units_box.addItem("Units: in", "in")
        self.units_box.currentIndexChanged.connect(self._on_units_changed)
        bar.addWidget(self.units_box)

        self.view_box = QComboBox()
        for caption, key in (
            ("View: iso", "iso"), ("View: front", "front"), ("View: back", "back"),
            ("View: left", "left"), ("View: right", "right"), ("View: top", "top"),
            ("View: bottom", "bottom"),
        ):
            self.view_box.addItem(caption, key)
        self.view_box.currentIndexChanged.connect(
            lambda: self.viewport.set_preset_view(self.view_box.currentData())
        )
        bar.addWidget(self.view_box)

        self.region_tolerance = QDoubleSpinBox()
        self.region_tolerance.setPrefix("Flat within: ")
        self.region_tolerance.setSuffix("°")
        self.region_tolerance.setDecimals(1)
        self.region_tolerance.setRange(0.1, 89.0)
        self.region_tolerance.setValue(DEFAULT_TOLERANCE_DEG)
        self.region_tolerance.setToolTip(
            "How far a triangle normal may differ and still count as the same flat "
            "surface. Raise it on a coarse mesh."
        )
        self.region_tolerance.valueChanged.connect(self._on_region_tolerance_changed)
        # A toolbar wraps a widget in a QWidgetAction and re-shows it on every
        # layout pass, so visibility has to be set on the action, not the widget.
        self._region_tolerance_action = bar.addWidget(self.region_tolerance)
        self._region_tolerance_action.setVisible(False)

        self.density_field = QDoubleSpinBox()
        self.density_field.setPrefix("Density: ")
        self.density_field.setSuffix(" g/cm3")
        self.density_field.setDecimals(2)
        self.density_field.setRange(0.0, 25.0)
        self.density_field.setSpecialValueText("Density: none")
        self.density_field.setValue(0.0)
        self.density_field.setToolTip(
            "Set a density to show the mass in the status line. Aluminium is 2.70."
        )
        self.density_field.valueChanged.connect(lambda _v: self._refresh_status())
        bar.addWidget(self.density_field)

        self.selection_box = QComboBox()
        self.selection_box.addItem("Select: faces", "face")
        self.selection_box.addItem("Select: edges", "edge")
        self.selection_box.currentIndexChanged.connect(
            lambda: self.viewport.set_selection_mode(self.selection_box.currentData())
        )
        bar.addWidget(self.selection_box)

        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding,
                             spacer.sizePolicy().verticalPolicy().Preferred)
        bar.addWidget(spacer)

        # The two view toggles live under the viewport, in the status strip.  The
        # toolbar is full, and what does not fit there goes into an overflow menu
        # that shuts again at each layout pass.
        self.action_preview.setShortcut(QKeySequence("Space"))
        self.addAction(self.action_preview)
        self.addAction(self.action_draft)

        self.action_undo = add("Undo", self.undo, "Ctrl+Z")
        self.action_redo = add("Redo", self.redo, "Ctrl+Y")

        # The report commands do NOT go on this toolbar.  The bar runs out of room
        # and moves whatever is last into an overflow menu, and that menu shuts
        # again at each layout pass.  The status bar has room and never moves.
        self.action_report_bug = QAction("Report a bug", self)
        self.action_report_bug.setToolTip(
            "Write an email about a problem, with the log already in it."
        )
        self.action_report_bug.triggered.connect(self.report_bug)
        self.addAction(self.action_report_bug)

        self.action_report_crash = QAction("Report a crash", self)
        self.action_report_crash.setToolTip(
            "Write an email about Stamp stopping without warning."
        )
        self.action_report_crash.triggered.connect(self.report_crash)
        self.addAction(self.action_report_crash)

        # Shortcuts with no toolbar button (§7).
        self._hidden_action("Delete feature", self.delete_selected_feature, "Del")
        self._hidden_action("Duplicate feature", self.duplicate_selected_feature, "Ctrl+D")
        self._hidden_action("Frame selection", self.viewport.fit_all, "F")
        for index, name in enumerate(
            ["front", "back", "left", "right", "top", "bottom"], start=1
        ):
            self._hidden_action(
                f"View {name}", lambda _=False, n=name: self.viewport.set_preset_view(n), str(index)
            )
        self._hidden_action("Cancel", self._cancel_pending, "Esc")

    def _hidden_action(self, text: str, slot, shortcut: str) -> QAction:
        action = QAction(text, self)
        action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(slot)
        action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.addAction(action)
        return action

    def _wire(self) -> None:
        self.rebuilder = RebuildController(self.engine, self)
        self.rebuilder.finished.connect(self._on_rebuild_finished)
        self.rebuilder.failed.connect(self._on_rebuild_failed)
        # A fillet on a thousand edges is one call that reports nothing, thus the
        # seconds have to come from here.
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(500)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

        self.rebuilder.busy_changed.connect(self._on_busy_changed)
        self.rebuilder.progress.connect(self._on_progress)
        self.cancel_button.clicked.connect(self.rebuilder.cancel)

        self.viewport.picked.connect(self._on_picked)
        self.viewport.nothing_picked.connect(self._on_nothing_picked)
        self.viewport.view_changed.connect(self.handles.refresh)

        self.tree.feature_selected.connect(self._on_feature_selected)
        self.tree.modifier_selected.connect(lambda fid, _mid: self._on_feature_selected(fid))
        self.tree.enabled_toggled.connect(self._on_enabled_toggled)
        self.tree.renamed.connect(self._on_renamed)
        self.tree.reordered.connect(self._on_reordered)
        self.tree.duplicate_requested.connect(self._duplicate_feature)
        self.tree.delete_requested.connect(self._delete_feature)
        self.tree.mirror_requested.connect(self._mirror_feature)
        self.tree.delete_modifier_requested.connect(self._delete_modifier)

        self.properties.changed.connect(self._on_property_changed)
        self.properties.center_on_face_requested.connect(self._center_on_face)
        self.properties.fit_to_face_requested.connect(self._fit_to_face)
        self.properties.add_modifier_requested.connect(self._add_modifier)
        self.properties.pick_to_face_requested.connect(self._start_to_face_pick)
        self.properties.repick_face_requested.connect(self._start_repick)

        self.handles.placement_changed.connect(self._on_handle_moved)
        self.handles.placement_committed.connect(self._on_handle_committed)

    # -------------------------------------------------------------- dialog seam

    def _ask(self, dialog) -> bool:
        """Show a dialog and report whether it was accepted.

        With :attr:`interactive` off the dialog is never shown and its defaults
        stand, so a scripted run never stops on a prompt no one can answer.
        """
        if not self.interactive:
            return True
        from PySide6.QtWidgets import QDialog

        return dialog.exec() == QDialog.DialogCode.Accepted

    def _report_uncaught(self, exc: BaseException) -> None:
        """Show an error that no handler caught, and say where the log is."""
        where = diagnostics.log_path()
        tail = f"\n\nThe log is at {where}." if where else ""
        QTimer.singleShot(
            0,
            lambda: self._notify(
                "Stamp had an internal error",
                f"{type(exc).__name__}: {exc}{tail}",
            ),
        )

    # ------------------------------------------------------------- reports

    def report_bug(self) -> None:
        """Collect what the user wants to say, then draft the email."""
        self._send_report("bug")

    def report_crash(self) -> None:
        self._send_report("crash")

    def _send_report(self, kind: str) -> None:
        dialog = dialogs.ReportDialog(kind, self)
        if not self._ask(dialog):
            return
        report = dialog.report() if hasattr(dialog, "report") else reporting.Report(kind=kind)
        result = reporting.send(report)

        if not result.opened:
            lines = [f"Send the report to {reporting.SUPPORT_EMAIL} by hand.", ""]
            if result.path is not None:
                lines.append(f"The report is here: {result.path}")
            self._notify("Stamp cannot open your mail application", "\n".join(lines))
            return

        self.statusBar().showMessage(
            f"An email to {reporting.SUPPORT_EMAIL} is ready. Look at it, then send it."
        )

    def offer_crash_report(self) -> None:
        """Say that the last run stopped, without a dialog in the way.

        A modal question at start stops the user from doing anything until it is
        answered, and a tester who crashes frequently sees it frequently.  The
        status line says it instead, and the button says it again.
        """
        if not diagnostics.previous_run_crashed():
            return
        self.statusBar().showMessage(
            "The last run of Stamp stopped without warning. "
            'Push "Report a crash" to send the log.'
        )
        if hasattr(self, "_crash_button"):
            self._crash_button.setStyleSheet(
                "QPushButton { color: #e0a33a; font-weight: bold; }"
            )
            self._crash_button.setToolTip(
                "The last run stopped without warning. Send the log."
            )


    def _notify(self, title: str, message: str) -> None:
        if self.interactive:
            dialogs.warn(self, title, message)
        else:
            self.statusBar().showMessage(f"{title}: {message}")

    def _confirm(self, title: str, message: str) -> bool:
        if not self.interactive:
            return True
        return dialogs.confirm(self, title, message)

    # ---------------------------------------------------------------- document

    @property
    def selected_feature(self) -> Feature | None:
        return self.document.feature_by_id(self.tree.selected_feature_id())

    def _push_undo(self, label: str) -> None:
        """Record the state as it was *before* this change.

        Handlers differ in when they call this: the properties panel edits the
        feature and then reports it, while the tree reports first and edits after.
        So the snapshot cannot be taken here - it is the baseline captured at the end
        of the previous change that belongs on the stack.  The new baseline is taken
        once the current event has finished, which is what the zero-delay timer does.
        """
        self.undo_stack.push(label, self._undo_baseline)
        self._dirty = True
        self._update_title()
        QTimer.singleShot(0, self._capture_baseline)

    def _capture_baseline(self) -> None:
        self._undo_baseline = self.document.snapshot()
        self._update_enabled_state()

    def _update_title(self) -> None:
        name = self._project_path.name if self._project_path else "Untitled"
        mark = " •" if self._dirty else ""
        self.setWindowTitle(f"Stamp {__version__} — {name}{mark}")

    def _update_enabled_state(self) -> None:
        has_part = self.document.base is not None
        solid = has_part and self.document.base.mode == "solid"
        self.action_add_profile.setEnabled(has_part)
        self.action_add_text.setEnabled(has_part)
        self.action_save.setEnabled(has_part)
        self.action_replace_part.setEnabled(has_part)
        self.action_export_step.setEnabled(solid)
        self.action_export_stl.setEnabled(has_part)
        self.action_export_3mf.setEnabled(has_part)
        self.action_export_quote.setEnabled(has_part)
        self.action_undo.setEnabled(self.undo_stack.can_undo())
        self.action_redo.setEnabled(self.undo_stack.can_redo())
        self.action_export_step.setToolTip(
            "" if solid else export_io.MESH_MODE_NO_STEP
        )
        mesh = has_part and self.document.base.mode == "mesh"
        self._region_tolerance_action.setVisible(mesh)

    def _refresh_tree(self) -> None:
        results = {}
        if self._last_result is not None:
            results = {r.feature_id: r for r in self._last_result.features}
        self.tree.set_document(self.document, results)

    def _refresh_properties(self) -> None:
        feature = self.selected_feature
        if feature is None:
            self.properties.show_base(self.document.base, self.document.units)
            self.handles.set_feature(None, None)
            return
        size = self._native_size(feature)
        self.properties.show_feature(
            self.document, feature, size,
            mesh_mode=self.document.base is not None and self.document.base.mode == "mesh",
        )
        self._refresh_snap_targets(feature)
        self.handles.set_feature(feature, size)

    def _native_size(self, feature: Feature) -> tuple[float, float]:
        if feature.profile.native_size_mm != (0.0, 0.0):
            return feature.profile.native_size_mm
        try:
            profile = self.profiles.get(feature.profile)
        except Exception:
            return (1.0, 1.0)
        return (profile.width or 1.0, profile.height or 1.0)

    def _alignment_targets(self, feature: Feature) -> list[tuple[float, float]]:
        """Where other features sit, so two labels can be lined up (§6.2).

        Only features on the same sketch plane count.  A feature on the opposite
        face of the part shares no useful axis with this one.
        """
        plane = feature.placement.anchor.plane
        if plane is None:
            return []
        targets: list[tuple[float, float]] = []
        for other in self.document.features:
            if other.id == feature.id:
                continue
            other_plane = other.placement.anchor.plane
            if other_plane is None:
                continue
            if not self._same_plane(plane, other_plane):
                continue
            # Each feature holds its offset in its own plane frame, and two features
            # on one geometric plane can have different origins - the origin is where
            # the user clicked.  So go out to world coordinates and back in again.
            world = self._plane_point_to_world(other_plane, other.placement.offset_2d)
            u, v, _ = snapping.to_plane(plane, world)
            targets.append((u, v))
        return targets

    @staticmethod
    def _plane_point_to_world(plane, uv: tuple[float, float]) -> tuple[float, float, float]:
        ox, oy, oz = plane.origin
        ux, uy, uz = plane.u_axis
        vx, vy, vz = snapping.v_axis(plane)
        u, v = uv
        return (ox + u * ux + v * vx, oy + u * uy + v * vy, oz + u * uz + v * vz)

    def _refresh_snap_targets(self, feature: Feature | None) -> None:
        """Rebuild the snap list for the selected feature (§6.2).

        Only the base part contributes.  Snapping to an edge that a later feature
        created would move the profile every time that feature changed.
        """
        if feature is None:
            self.handles.snap_targets = []
            return
        plane = feature.placement.anchor.plane
        if plane is None:
            self.handles.snap_targets = []
            return

        shape = None
        anchor_face = None
        if self.document.base is not None and self.document.base.mode == "solid":
            shape = self.document.base.runtime
            ref = feature.placement.anchor.face_ref
            if ref is not None:
                try:
                    anchor_face = resolve_face_ref(ref, shape).face
                except Exception:
                    anchor_face = None

        try:
            self.handles.snap_targets = snapping.collect(
                shape, plane, anchor_face,
                feature_offsets=self._alignment_targets(feature),
            )
        except Exception:
            self.handles.snap_targets = []

    @staticmethod
    def _same_plane(a, b, *, tolerance: float = 1e-6) -> bool:
        if any(abs(x - y) > 1e-3 for x, y in zip(a.normal, b.normal, strict=True)):
            return False
        offset = sum((x - y) * n for x, y, n in zip(a.origin, b.origin, a.normal, strict=True))
        return abs(offset) <= 1e-3

    def relink_sources(self) -> None:
        """Point a feature at a source file that moved, and rebuild (§10)."""
        if self.document.base is None:
            return
        missing = [
            f.profile.source_path
            for f in self.document.features
            if f.profile.source_path and not Path(
                self.profiles.relinks.get(f.profile.source_path, f.profile.source_path)
            ).exists()
        ]
        if not missing:
            self.statusBar().showMessage("Every source file is where it should be.", 5000)
            return

        for original in dict.fromkeys(missing):
            chosen, _ = QFileDialog.getOpenFileName(
                self,
                f"Find {Path(original).name}",
                self._last_dir("profile"),
                f"Profiles (*{Path(original).suffix})",
            )
            if not chosen:
                continue
            self.profiles.relink(original, chosen)
            for feature in self.document.features:
                if feature.profile.source_path == original:
                    feature.profile.source_path = chosen
        self.engine.invalidate()
        self.request_rebuild(immediate=True)

    def _warn_profile_larger_than_face(self, feature: Feature) -> None:
        """A cut hanging off an edge is legitimate, so this warns and allows it (§10)."""
        size = self._native_size(feature)
        scale = feature.placement.scale
        width, height = size[0] * abs(scale[0]), size[1] * abs(scale[1])
        face_size = self._anchor_face_size(feature)
        if face_size is None:
            return
        if width > face_size[0] or height > face_size[1]:
            self.warning_label.setStyleSheet("color: #c58a2a;")
            self.warning_label.setText(
                f"{feature.name} is {width:.1f} x {height:.1f} mm, which is larger than "
                f"the face it sits on ({face_size[0]:.1f} x {face_size[1]:.1f} mm). "
                f"That is allowed - part of it will hang over the edge."
            )

    def request_rebuild(self, *, immediate: bool = False) -> None:
        if self.document.base is None:
            return
        self.rebuilder.request(self.document, immediate=immediate)

    # ------------------------------------------------------------ part loading

    def open_part_dialog(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(PART_EXTS))
        path, _ = QFileDialog.getOpenFileName(
            self, "Open a part", self._last_dir("part"), f"3D parts ({patterns})"
        )
        if path:
            self.open_part(Path(path))

    def open_part(self, path: Path) -> None:
        try:
            result = import_part(path)
        except PartImportError as exc:
            self._notify("Stamp cannot open this part", str(exc))
            return

        if result.units_ambiguous:
            size = result.part.size
            dialog = dialogs.UnitPromptDialog(
                size,
                title="What unit is this file in?",
                note=(
                    f"{path.name} does not record a unit. Its bounding box is "
                    f"{size[0]:.2f} × {size[1]:.2f} × {size[2]:.2f} in file numbers."
                ),
            )
            if not self._ask(dialog):
                return
            if dialog.scale() != 1.0:
                result = import_part(path, unit_scale=dialog.scale())

        if result.solids:
            choice = dialogs.SolidChoiceDialog(
                len(result.solids), disjoint=not solids_intersect(result.solids), parent=self
            )
            if not self._ask(choice):
                return
            index = choice.solid_index()
            if index is not None:
                result = import_part(path, solid_index=index)

        self.document = Document(base=result.part, name=path.stem)
        self.profiles.clear()
        self.engine.invalidate()
        self.undo_stack.clear()
        self._mesh_pick_cache = None
        self._mesh_region = None
        self._project_path = None
        self._last_result = None
        self._dirty = False
        self._undo_baseline = self.document.snapshot()
        self._remember_dir("part", path)

        if result.part.warnings:
            self._notify("Note about this part", "\n\n".join(result.part.warnings))

        self._show_viewport()
        self.viewport.clear()
        self._display_geometry(result.part.runtime, result.part.mode)
        self.viewport.fit_all()
        self._refresh_tree()
        self._refresh_properties()
        self._update_enabled_state()
        self._update_title()
        # Run the (empty) rebuild so the status line has a volume from the start.
        self.request_rebuild(immediate=True)
        self.statusBar().showMessage(f"Opened {path.name}. Add a profile to place artwork on it.")

    def replace_part_dialog(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(PART_EXTS))
        path, _ = QFileDialog.getOpenFileName(
            self, "Replace the part with a newer file",
            self._last_dir("part"), f"Parts ({patterns})",
        )
        if path:
            self.replace_part(Path(path))

    def replace_part(self, path: Path) -> None:
        """Swap the part for a newer file and keep the artwork on it (§8.2).

        The features are never touched on failure: one that cannot be matched
        keeps the anchor it had, shows as broken, and waits for a face to be
        picked again.  That is recoverable; deleting it would not be.
        """
        if self.document.base is None:
            self._notify("There is no part to replace", "Open a part first.")
            return

        try:
            result = import_part(path)
        except PartImportError as exc:
            self._notify("Stamp cannot open this part", str(exc))
            return

        if result.units_ambiguous:
            size = result.part.size
            dialog = dialogs.UnitPromptDialog(
                size,
                title="What unit is this file in?",
                note=(
                    f"{path.name} does not record a unit. Its bounding box is "
                    f"{size[0]:.2f} × {size[1]:.2f} × {size[2]:.2f} in file numbers."
                ),
            )
            if not self._ask(dialog):
                return
            if dialog.scale() != 1.0:
                result = import_part(path, unit_scale=dialog.scale())

        if result.part.mode != self.document.base.mode:
            if not self._confirm(
                "That is a different kind of part",
                f"The project is in {self.document.base.mode} mode and "
                f"{path.name} is a {result.part.mode}. Replacing it changes what "
                f"Stamp can export and how the artwork is anchored.\n\nReplace anyway?",
            ):
                return

        # Say what will happen before anything changes, when it is not all good.
        report = replace_part_io.plan_replacement(self.document, result.part)
        if report.lost and self.interactive:
            names = ", ".join(m.name for m in report.lost)
            if not self._confirm(
                "Some artwork will not match",
                f"{len(report.lost)} of {len(report.matches)} features cannot be "
                f"placed on {path.name}: {names}.\n\nThey will be kept and marked "
                f"so you can pick a face for them again.\n\nReplace the part?",
            ):
                return

        self._push_undo("replace part")
        report = replace_part_io.replace_part(self.document, result.part)

        self.profiles.clear()
        self.engine.invalidate()
        self._mesh_pick_cache = None
        self._mesh_region = None
        self._last_result = None
        self._remember_dir("part", path)

        if result.part.warnings:
            self._notify("Note about this part", "\n\n".join(result.part.warnings))

        self._display_geometry(result.part.runtime, result.part.mode)
        self.viewport.fit_all()
        self._refresh_tree()
        self._refresh_properties()
        self._update_enabled_state()
        self._update_title()
        self.request_rebuild(immediate=True)

        self.statusBar().showMessage(report.summary(), 12000)
        if self.interactive:
            dialogs.ReplaceReportDialog(report, path.name, parent=self).exec()

    # --------------------------------------------------------- profile loading

    def add_profile_dialog(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(PROFILE_EXTS))
        path, _ = QFileDialog.getOpenFileName(
            self, "Add a profile", self._last_dir("profile"), f"Profiles ({patterns})"
        )
        if path:
            self.add_profile(Path(path))

    def add_text_dialog(self) -> None:
        """What the toolbar calls.

        ``QAction.triggered`` carries the checked state, thus a slot that takes an
        argument gets a bool.  This one takes nothing, the same as
        :meth:`add_profile_dialog`.
        """
        self.add_text_feature()

    def add_text_feature(self, face_pick=None) -> None:
        """Start a text feature, then wait for the user to click the face.

        The message becomes a profile, thus everything after this point is the
        same as a profile that came from a file.
        """
        if self.document.base is None:
            return
        ref = ProfileRef(text=TextSpec(text="TEXT", size_mm=self._default_text_size()))
        self._pending_profile = ref
        self._pending_profile_size = (0.0, 0.0)
        if face_pick is not None:
            self._create_feature(ref, *face_pick)
            return
        self.viewport.set_selection_mode("face")
        self.selection_box.setCurrentIndex(0)
        self.statusBar().showMessage(
            "Click the face for the text. Press Esc to cancel."
        )

    def _default_text_size(self) -> float:
        """A size that suits the part, so the first text is never invisible."""
        if self.document.base is None:
            return 10.0
        shortest = min(d for d in self.document.base.size if d > 0)
        return max(round(shortest * 0.15, 1), 1.0)

    def add_profile(self, path: Path, face_pick=None) -> None:
        """Import artwork, then wait for the user to click the face it goes on."""
        options = ImportOptions()

        if path.suffix.lower() == ".dxf":
            try:
                layers = dxf_layers(path)
            except Exception:
                layers = []
            if len(layers) > 1:
                dialog = dialogs.LayerFilterDialog(layers, default_dxf_layers(path), self)
                if not self._ask(dialog):
                    return
                options.layers = dialog.layers()

        try:
            result = import_profile(path, options)
        except DwgUnavailable as exc:
            if self._offer_dwg_converter():
                self.add_profile(path, face_pick=face_pick)
            else:
                self._notify("Stamp cannot read DWG", str(exc))
            return
        except Exception as exc:
            self._notify("Stamp cannot read this profile", str(exc))
            return

        result = self._resolve_profile_issues(path, result, options)
        if result is None:
            return

        if result.units_ambiguous:
            size = (result.profile.width, result.profile.height)
            dialog = dialogs.UnitPromptDialog(
                size,
                title="What size is this artwork?",
                note=(
                    f"{path.name} gives no physical size. Read at 96 dpi it is "
                    f"{size[0]:.2f} × {size[1]:.2f} mm. You can also set the exact "
                    f"width later in the properties panel."
                ),
                default="mm",
            )
            if not self._ask(dialog):
                return

        ref = ProfileRef(
            source_path=str(path),
            source_hash=result.source_hash,
            native_units=result.native_units,
            native_size_mm=(result.profile.width, result.profile.height),
            layers=options.layers,
            outline_strokes=options.outline_stroke_width,
            union_overlapping=options.union_overlapping,
        )
        self.profiles.put(ref, result.profile)
        self._remember_dir("profile", path)

        self._pending_profile = ref
        self._pending_profile_size = (result.profile.width, result.profile.height)
        if face_pick is not None:
            self._create_feature(ref, *face_pick)
        else:
            self.viewport.set_selection_mode("face")
            self.selection_box.setCurrentIndex(0)
            self.statusBar().showMessage(
                f"{path.name} is ready. Click the face to place it on. Press Esc to cancel."
            )

    def _resolve_profile_issues(self, path: Path, result, options: ImportOptions):
        """Offer the §5.5 repairs, then re-import with the chosen one."""
        blocking = [i for i in result.profile.issues if i.blocking]
        if not blocking:
            return result

        fatal = [i for i in blocking if i.kind in (IssueKind.LIVE_TEXT, IssueKind.EMPTY)]
        if fatal:
            self._notify("This profile cannot be used", "\n\n".join(i.message for i in fatal))
            return None

        suggested = 0.5
        for issue in blocking:
            if issue.kind is IssueKind.NO_FILL:
                suggested = issue.detail.get("suggested_width_mm", 0.5)

        dialog = dialogs.ProfileRepairDialog(blocking, suggested_stroke_mm=suggested, parent=self)
        if not self._ask(dialog):
            return None

        choice = dialog.choice()
        if choice == "close":
            options.close_open_loops = True
        elif choice == "outline":
            options.outline_stroke_width = dialog.stroke_mm()
        elif choice == "union":
            options.union_overlapping = True
        else:
            return result

        try:
            return import_profile(path, options)
        except Exception as exc:
            self._notify("The repair did not work", str(exc))
            return None

    # -------------------------------------------------------------- face picks

    def _on_picked(self, shape, point) -> None:
        from OCP.TopAbs import TopAbs_ShapeEnum
        from OCP.TopoDS import TopoDS

        if self.document.base is not None and self.document.base.mode == "mesh":
            self._on_mesh_picked()
            return
        if shape is None or shape.IsNull():
            return
        if shape.ShapeType() != TopAbs_ShapeEnum.TopAbs_FACE:
            return
        face = TopoDS.Face_s(shape)
        location = (point.X(), point.Y(), point.Z())

        if self._picking_to_face:
            self._finish_to_face_pick(face, location)
            return
        if self._pending_profile is not None:
            self._create_feature(self._pending_profile, face, location)
            return
        if self._repicking and self.selected_feature is not None:
            self._finish_repick(face, location)
            return

    def _on_nothing_picked(self) -> None:
        pass

    def _create_feature(self, ref: ProfileRef, face, point) -> None:
        plane, warnings = plane_from_face(face, point)
        face_ref = make_face_ref(face, point)

        if ref.is_text:
            first = (ref.text.text.strip().splitlines() or ["Text"])[0]
            name = (first[:24] or "Text")
        else:
            name = Path(ref.source_path).stem or "Feature"
        feature = Feature(
            name=name,
            profile=ref,
            placement=Placement(
                anchor=Anchor(kind=AnchorKind.FACE, face_ref=face_ref, plane=plane)
            ),
            operation=Operation(
                kind=OperationKind.CUT,
                depth=0.5,
                direction=self._default_direction(),
            ),
        )
        self._push_undo("add feature")
        self.document.add_feature(feature)
        self._pending_profile = None

        if warnings:
            self._notify("Note about this face", "\n\n".join(warnings))

        self._refresh_tree()
        self.tree.select_feature(feature.id)
        self._refresh_properties()
        self._update_enabled_state()
        self.request_rebuild(immediate=True)

    @staticmethod
    def _default_direction():
        from stamp.core.document import Direction

        return Direction.INTO

    def _start_to_face_pick(self) -> None:
        self._picking_to_face = True
        self.viewport.set_selection_mode("face")
        self.statusBar().showMessage("Click the face to cut or add up to. Press Esc to cancel.")

    def _finish_to_face_pick(self, face, point) -> None:
        self._picking_to_face = False
        feature = self.selected_feature
        if feature is None:
            return
        self._push_undo("target face")
        feature.operation.to_face_ref = make_face_ref(face, point)
        self.request_rebuild(immediate=True)
        self.statusBar().showMessage("Target face set.")

    _repicking = False

    def _start_repick(self) -> None:
        self._repicking = True
        self.viewport.set_selection_mode("face")
        self.statusBar().showMessage("Click the face this feature should sit on.")

    def _finish_repick(self, face, point) -> None:
        self._repicking = False
        feature = self.selected_feature
        if feature is None:
            return
        plane, warnings = plane_from_face(face, point)
        self._push_undo("re-pick face")
        feature.placement.anchor = Anchor(
            kind=AnchorKind.FACE, face_ref=make_face_ref(face, point), plane=plane
        )
        if warnings:
            self._notify("Note about this face", "\n\n".join(warnings))
        self.request_rebuild(immediate=True)

    def _cancel_pending(self) -> None:
        if self._pending_profile is not None:
            self._pending_profile = None
            self.statusBar().showMessage("Cancelled.")
        self._picking_to_face = False
        self._repicking = False
        self.handles.cancel_drag()

    # ------------------------------------------------------------ tree actions

    # ------------------------------------------------------------- mesh picking

    def _mesh_pick_data(self) -> dict | None:
        """Vertices, triangles, normals and adjacency for the base mesh.

        Built once per part.  The adjacency map is the expensive half, and the base
        mesh never changes, so there is no reason to build it on every click.
        """
        if self.document.base is None or self.document.base.mode != "mesh":
            return None
        if self._mesh_pick_cache is not None:
            return self._mesh_pick_cache
        try:
            vertices, faces = mesh_regions.mesh_arrays(self.document.base.runtime)
            self._mesh_pick_cache = {
                "vertices": vertices,
                "faces": faces,
                "normals": mesh_regions.face_normals(vertices, faces),
                "adjacency": mesh_regions.build_adjacency(faces),
            }
        except Exception:
            self._mesh_pick_cache = None
        return self._mesh_pick_cache

    def _find_mesh_region(self, position=None):
        """Grow a flat region under the cursor and show what it found (§6.1)."""
        data = self._mesh_pick_data()
        if data is None:
            return None
        position = position or self.viewport.last_pick_position
        if position is None:
            return None
        ray = self.viewport.ray_at(position.x(), position.y())
        if ray is None:
            return None

        region = mesh_regions.region_at(
            data["vertices"], data["faces"], ray[0], ray[1],
            tolerance_deg=self.region_tolerance.value(),
            adjacency=data["adjacency"], normals=data["normals"],
        )
        if region is None:
            self.statusBar().showMessage("That click missed the part.", 4000)
            return None

        self._show_mesh_region(region)
        return region

    def _show_mesh_region(self, region) -> None:
        """Highlight the detected region, because the fit is a guess (§6.1)."""
        self._mesh_region = region
        data = self._mesh_pick_data()
        if data is None:
            return
        try:
            shape = mesh_regions.region_shape(
                data["vertices"], data["faces"], region.triangles,
                normal=region.plane.normal,
            )
        except Exception:
            return
        self.viewport.display_shape(
            REGION_KEY, shape, color=REGION_COLOR, transparency=0.15,
            material=False, selectable=False, update=True,
        )
        message = (
            f"Found a flat region of {region.count} triangles, "
            f"{region.area:.0f} mm² . Adjust the tolerance if that is not the "
            f"surface you meant."
        )
        self.statusBar().showMessage(message, 8000)
        if region.warnings:
            self.warning_label.setStyleSheet("color: #c58a2a;")
            self.warning_label.setText(region.warnings[0])

    def _on_mesh_picked(self) -> None:
        region = self._find_mesh_region()
        if region is None:
            return
        if self._pending_profile is not None:
            self._create_mesh_feature(self._pending_profile, region)
        elif self._repicking and self.selected_feature is not None:
            self._repicking = False
            feature = self.selected_feature
            self._push_undo("re-pick surface")
            feature.placement.anchor = Anchor(
                kind=AnchorKind.MESH_REGION,
                mesh_seed=region.point,
                mesh_tolerance=self.region_tolerance.value(),
                plane=region.plane,
            )
            self.request_rebuild(immediate=True)

    def _create_mesh_feature(self, ref: ProfileRef, region) -> None:
        if ref.is_text:
            first = (ref.text.text.strip().splitlines() or ["Text"])[0]
            name = (first[:24] or "Text")
        else:
            name = Path(ref.source_path).stem or "Feature"
        feature = Feature(
            name=name,
            profile=ref,
            placement=Placement(
                anchor=Anchor(
                    kind=AnchorKind.MESH_REGION,
                    mesh_seed=region.point,
                    mesh_tolerance=self.region_tolerance.value(),
                    plane=region.plane,
                )
            ),
            operation=Operation(
                kind=OperationKind.CUT, depth=0.5, direction=self._default_direction()
            ),
        )
        self._push_undo("add feature")
        self.document.add_feature(feature)
        self._pending_profile = None

        self._refresh_tree()
        self.tree.select_feature(feature.id)
        self._refresh_properties()
        self._update_enabled_state()
        self.request_rebuild(immediate=True)

    def _on_region_tolerance_changed(self, value: float) -> None:
        """Re-grow the region from the same click when the tolerance changes."""
        feature = self.selected_feature
        if feature is None or feature.placement.anchor.kind is not AnchorKind.MESH_REGION:
            return
        seed = feature.placement.anchor.mesh_seed
        data = self._mesh_pick_data()
        if seed is None or data is None:
            return

        # Cast a ray straight down the stored plane normal onto the seed point, so
        # the same triangle is picked again.
        normal = feature.placement.anchor.plane.normal
        origin = tuple(s + n * 10.0 for s, n in zip(seed, normal, strict=True))
        direction = tuple(-n for n in normal)
        region = mesh_regions.region_at(
            data["vertices"], data["faces"], origin, direction,
            tolerance_deg=value, adjacency=data["adjacency"], normals=data["normals"],
        )
        if region is None:
            return
        self._push_undo("region tolerance")
        feature.placement.anchor.mesh_tolerance = value
        feature.placement.anchor.plane = region.plane
        self._show_mesh_region(region)
        self.request_rebuild(immediate=True)

    def _on_feature_selected(self, feature_id: str) -> None:
        self._refresh_properties()
        self._show_preview()

    def _on_enabled_toggled(self, feature_id: str, enabled: bool) -> None:
        feature = self.document.feature_by_id(feature_id)
        if feature is None:
            return
        self._push_undo("suppress" if not enabled else "enable")
        feature.enabled = enabled
        self.request_rebuild(immediate=True)

    def _on_renamed(self, feature_id: str, name: str) -> None:
        feature = self.document.feature_by_id(feature_id)
        if feature is None or feature.name == name:
            return
        self._push_undo("rename")
        feature.name = name
        self._refresh_tree()

    def _on_reordered(self, feature_id: str, new_index: int) -> None:
        self._push_undo("reorder")
        self.document.move_feature(feature_id, new_index)
        self.engine.invalidate()
        self.request_rebuild(immediate=True)

    def _duplicate_feature(self, feature_id: str) -> None:
        feature = self.document.feature_by_id(feature_id)
        if feature is None:
            return
        self._push_undo("duplicate")
        clone = feature.copy_with_new_id()
        self.document.add_feature(clone, self.document.index_of(feature_id) + 1)
        self._refresh_tree()
        self.tree.select_feature(clone.id)
        self.request_rebuild(immediate=True)

    def duplicate_selected_feature(self) -> None:
        if self.selected_feature is not None:
            self._duplicate_feature(self.selected_feature.id)

    def _delete_feature(self, feature_id: str) -> None:
        feature = self.document.feature_by_id(feature_id)
        if feature is None:
            return
        self._push_undo("delete")
        self.document.remove_feature(feature_id)
        self.engine.invalidate()
        self._refresh_tree()
        self._refresh_properties()
        self.request_rebuild(immediate=True)

    def delete_selected_feature(self) -> None:
        if self.selected_feature is not None:
            self._delete_feature(self.selected_feature.id)

    def _mirror_feature(self, feature_id: str) -> None:
        feature = self.document.feature_by_id(feature_id)
        if feature is None:
            return
        self._push_undo("mirror")
        clone = feature.copy_with_new_id(f"{feature.name} mirrored")
        clone.placement.mirror_u = not clone.placement.mirror_u
        clone.placement.offset_2d = (-clone.placement.offset_2d[0], clone.placement.offset_2d[1])
        self.document.add_feature(clone, self.document.index_of(feature_id) + 1)
        self._refresh_tree()
        self.request_rebuild(immediate=True)

    def _delete_modifier(self, feature_id: str, modifier_id: str) -> None:
        feature = self.document.feature_by_id(feature_id)
        if feature is None:
            return
        self._push_undo("delete edge treatment")
        feature.modifiers = [m for m in feature.modifiers if m.id != modifier_id]
        self._refresh_tree()
        self._refresh_properties()
        self.request_rebuild(immediate=True)

    def _add_modifier(self, kind: str) -> None:
        feature = self.selected_feature
        if feature is None:
            return
        self._push_undo(f"add {kind}")
        feature.modifiers.append(
            Modifier(
                kind=ModifierKind.FILLET if kind == "fillet" else ModifierKind.CHAMFER,
                value=0.3,
                target=EdgeSelector(role=EdgeRole.TOP),
            )
        )
        self._refresh_tree()
        self._refresh_properties()
        self.request_rebuild(immediate=True)

    # -------------------------------------------------------- property actions

    def _on_property_changed(self, label: str) -> None:
        self._push_undo(label)
        self._refresh_tree()
        self.handles.refresh()
        self.request_rebuild()

    def _center_on_face(self) -> None:
        feature = self.selected_feature
        if feature is None or feature.placement.anchor.plane is None:
            return
        self._push_undo("center on face")
        feature.placement.offset_2d = (0.0, 0.0)
        self._refresh_properties()
        self.request_rebuild(immediate=True)

    def _fit_to_face(self) -> None:
        """Scale the profile so it fits inside the face it sits on."""
        feature = self.selected_feature
        if feature is None or self._last_result is None:
            return
        result = self._last_result.result_for(feature.id)
        if result is None or result.tool is None:
            return
        width, height = self._native_size(feature)
        if width <= 0 or height <= 0:
            return
        face_size = self._anchor_face_size(feature)
        if face_size is None:
            return
        factor = min(face_size[0] * 0.9 / width, face_size[1] * 0.9 / height)
        self._push_undo("fit to face")
        feature.placement.scale = (factor, factor)
        self._refresh_properties()
        self.request_rebuild(immediate=True)

    def _anchor_face_size(self, feature: Feature) -> tuple[float, float] | None:
        from stamp.core.refs import resolve_anchor

        if self.document.base is None or self.document.base.mode != "solid":
            return None
        try:
            plane, _ = resolve_anchor(feature.placement.anchor, self.document.base.runtime)
        except Exception:
            return None
        from stamp.core.refs import resolve_face_ref

        try:
            face = resolve_face_ref(feature.placement.anchor.face_ref, self.document.base.runtime).face
        except Exception:
            return None
        from stamp.io.part_import import bounding_box

        x0, y0, z0, x1, y1, z1 = bounding_box(face)
        extents = sorted([x1 - x0, y1 - y0, z1 - z0], reverse=True)
        return (extents[0], extents[1])

    def _on_handle_moved(self, _label: str) -> None:
        snapped = self.handles.last_snap
        if snapped is not None:
            self.statusBar().showMessage(f"Snapped to the {snapped.kind.value}.", 2000)
        self.request_rebuild()
        self._refresh_properties()

    def _on_handle_committed(self, label: str) -> None:
        self._push_undo(label)
        self.request_rebuild(immediate=True)

    # ----------------------------------------------------------------- rebuild

    def _on_busy_changed(self, busy: bool) -> None:
        if busy:
            self._busy_since = time.monotonic()
            self._elapsed_timer.start()
            QTimer.singleShot(PROGRESS_AFTER_MS, self._maybe_show_progress)
        else:
            self._elapsed_timer.stop()
            self.progress.setVisible(False)
            self.cancel_button.setVisible(False)

    def _maybe_show_progress(self) -> None:
        if self.rebuilder.busy:
            self.progress.setVisible(True)
            self.cancel_button.setVisible(True)

    def _tick_elapsed(self) -> None:
        """Count the seconds of a step that gives no progress of its own."""
        if not self.rebuilder.busy:
            return
        seconds = int(time.monotonic() - self._busy_since)
        if seconds >= 2:
            self.progress.setFormat(f"{self._busy_step} - {seconds}s")

    def _on_progress(self, index: int, total: int, name: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(index)
        self._busy_step = f"{name} ({index}/{total})"
        self.progress.setFormat(self._busy_step)

    def _refresh_status(self) -> None:
        """Rebuild time, volume, and mass when a density is set (§7)."""
        result = self._last_result
        if result is None:
            self.status_label.setText("")
            return
        volume_cm3 = result.volume / 1000.0
        parts = [f"Rebuilt in {result.duration_ms:.0f} ms", f"{volume_cm3:.2f} cm³"]
        density = self.density_field.value()
        if density > 0:
            parts.append(f"{volume_cm3 * density:.1f} g")
        self.status_label.setText(" · ".join(parts))

    def _offer_draft_display(self, duration_ms: float) -> None:
        """A rebuild this slow is worth trading display quality for (§10)."""
        seconds = duration_ms / 1000.0
        if not self.interactive:
            return
        if not dialogs.confirm(
            self,
            "That rebuild was slow",
            f"The last rebuild took {seconds:.1f} seconds. Stamp can draw the part "
            f"more coarsely, which makes each rebuild faster. The geometry and every "
            f"exported file stay exactly the same.\n\nUse draft quality for the view?",
        ):
            self._slow_offer_declined = True
            return
        self.set_draft_display(True)

    def set_draft_display(self, draft: bool) -> None:
        self._draft_display = draft
        self.viewport.set_display_quality(draft)
        self.action_draft.setChecked(draft)
        self.statusBar().showMessage(
            "The view is drawn at draft quality. Exports are not affected."
            if draft
            else "The view is drawn at full quality.",
            5000,
        )

    def _on_rebuild_finished(self, result: RebuildResult) -> None:
        self._last_result = result
        if result.geometry is not None:
            self._display_geometry(result.geometry, result.mode)

        self._refresh_status()
        # Correct a value that will not build *before* anything that can open a
        # dialog.  A modal dialog runs a nested event loop, so a later rebuild can
        # finish inside it and this handler re-enters; the outer call would then
        # act on a result that has already been replaced.
        self._auto_apply_working_values(result)
        if (result.duration_ms > SLOW_REBUILD_MS and not self._draft_display
                and not self._slow_offer_declined):
            self._offer_draft_display(result.duration_ms)
        errors = result.errors
        warnings = result.warnings
        if errors:
            self.warning_label.setText(errors[-1])
            self.warning_label.setStyleSheet("color: #c0453a;")
        elif warnings:
            self.warning_label.setText(warnings[-1])
            self.warning_label.setStyleSheet("color: #c58a2a;")
        elif self._auto_value_note:
            # The corrected rebuild came back clean; say what was changed and why.
            self.warning_label.setText(self._auto_value_note)
            self.warning_label.setStyleSheet("color: #c58a2a;")
            self._auto_value_note = ""
        else:
            self.warning_label.setText("")

        self._refresh_tree()
        self._show_preview()
        self._update_enabled_state()
        # Another feature may have moved, so the alignment targets are recomputed
        # here rather than only when the selection changes.
        self._refresh_snap_targets(self.selected_feature)
        self.handles.refresh()
        if not errors and not warnings:
            feature = self.selected_feature
            if feature is not None:
                self._warn_profile_larger_than_face(feature)

    def _auto_apply_working_values(self, result: RebuildResult) -> None:
        """A fillet or chamfer that failed names the largest value that works.

        That value goes straight into the modifier and the part rebuilds with it,
        so the user never has to copy a number out of a message.  Attempts are
        capped per modifier: a suggestion that itself fails produces a smaller
        one, and three tries is enough for any real artwork.
        """
        if result is not self._last_result:
            return  # a nested event loop replaced it while a dialog was open
        fixes = []
        for row in result.features:
            if not row.suggested_values:
                continue
            feature = next(
                (f for f in self.document.features if f.id == row.feature_id), None
            )
            if feature is None:
                continue
            for modifier in feature.modifiers:
                value = row.suggested_values.get(modifier.id)
                if value is None or value <= 0:
                    continue
                if self._auto_value_attempts.get(modifier.id, 0) >= 3:
                    continue
                # The panel shows three decimals.  Storing more than it can show
                # means the number on screen is larger than the one that builds,
                # and the next edit writes that larger number back and fails
                # again.  Round down, so what is shown is what is stored.
                value = math.floor(value * 1000.0) / 1000.0
                if value <= 0 or abs(modifier.value - value) < 1e-9:
                    continue
                self._auto_value_attempts[modifier.id] = (
                    self._auto_value_attempts.get(modifier.id, 0) + 1
                )
                previous = modifier.value
                modifier.value = float(value)
                word = "radius" if modifier.kind is ModifierKind.FILLET else "distance"
                fixes.append(
                    f"{feature.name} – {modifier.label}: a {word} of {previous:g} mm "
                    f"was too large, so Stamp set {value:.3f} mm, the largest that works."
                )
        # A modifier that came back without a suggestion is healthy again.
        suggested_now = {
            mid for row in result.features for mid in row.suggested_values
        }
        for mid in list(self._auto_value_attempts):
            if mid not in suggested_now:
                del self._auto_value_attempts[mid]
        if not fixes:
            return
        self._auto_value_note = fixes[-1]
        self.statusBar().showMessage(fixes[-1], 10000)
        self._push_undo("working value")
        self._refresh_properties()
        self._refresh_tree()
        self.request_rebuild()

    def _on_rebuild_failed(self, message: str) -> None:
        self.warning_label.setText(message)
        self.warning_label.setStyleSheet("color: #c0453a;")

    def _display_geometry(self, geometry, mode: str) -> None:
        if mode == "solid":
            self.viewport.display_shape(RESULT_KEY, geometry, color=PART_COLOR)
        else:
            try:
                shape = self._mesh_display_shape(geometry)
            except Exception:
                return
            self.viewport.display_shape(RESULT_KEY, shape, color=PART_COLOR)

    def _mesh_display_shape(self, manifold):
        """Draw a very large mesh from a reduced copy, keeping the full one (§5.2).

        The booleans always use the full mesh.  Only what reaches the screen is
        reduced, and only when there is enough of it to matter.
        """
        from stamp.geom import mesh_ops

        triangles = manifold.num_tri()
        if triangles <= DECIMATE_THRESHOLD:
            return manifold_display_shape(manifold)

        reduced = mesh_ops.decimate_for_display(manifold, DECIMATE_THRESHOLD // 2)
        self.statusBar().showMessage(
            f"This mesh has {triangles:,} triangles, so the view is drawn from a "
            f"smaller copy. Booleans and exports use all of them.",
            8000,
        )
        return trimesh_display_shape(reduced)

    _preview_on = True

    def toggle_preview(self) -> None:
        self.action_preview.setChecked(not self.action_preview.isChecked())

    def set_preview_visible(self, on: bool) -> None:
        """Show or hide the translucent tool solid over the part."""
        self._preview_on = bool(on)
        self._show_preview()
        self.statusBar().showMessage(
            "The preview is on." if on else "The preview is off. This is the result."
        )

    def _show_preview(self) -> None:
        """The translucent tool solid, green for add and red for cut (§6.3)."""
        self.viewport.erase(PREVIEW_KEY, update=False)
        self.viewport.erase(FOOTPRINT_KEY, update=False)

        feature = self.selected_feature
        if not self._preview_on or feature is None or self._last_result is None:
            self.viewport.context and self.viewport.context.UpdateCurrentViewer()
            return
        result = self._last_result.result_for(feature.id)
        if result is None or result.tool is None:
            self.viewport.context and self.viewport.context.UpdateCurrentViewer()
            return

        color = ADD_COLOR if feature.operation.kind is OperationKind.ADD else CUT_COLOR
        self.viewport.display_shape(
            PREVIEW_KEY, result.tool.shape, color=color, transparency=0.65,
            material=False, selectable=False, update=False,
        )
        self.viewport.display_shape(
            FOOTPRINT_KEY, result.tool.footprint, color=color, transparency=0.25,
            material=False, selectable=False, update=True,
        )

    # -------------------------------------------------------------- undo, redo

    def undo(self) -> None:
        snapshot = self.undo_stack.undo(self.document.snapshot())
        if snapshot is None:
            return
        self.document.restore(snapshot)
        self._undo_baseline = self.document.snapshot()
        self.engine.invalidate()
        self._refresh_tree()
        self._refresh_properties()
        self._update_enabled_state()
        self.request_rebuild(immediate=True)

    def redo(self) -> None:
        snapshot = self.undo_stack.redo(self.document.snapshot())
        if snapshot is None:
            return
        self.document.restore(snapshot)
        self._undo_baseline = self.document.snapshot()
        self.engine.invalidate()
        self._refresh_tree()
        self._refresh_properties()
        self._update_enabled_state()
        self.request_rebuild(immediate=True)

    # ------------------------------------------------------------ project file

    def open_project_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open a project", self._last_dir("project"), "Stamp projects (*.stamp)"
        )
        if path:
            self.open_project(Path(path))

    def open_project(self, path: Path) -> None:
        try:
            opened = project_io.open_project(path)
        except project_io.ProjectError as exc:
            self._notify("Stamp cannot open this project", str(exc))
            return

        document = opened.document
        if document.base is None:
            self._notify("This project has no part", "The project records no base part.")
            return
        try:
            reloaded = import_part(document.base.source_path)
        except PartImportError as exc:
            self._notify("The part could not be reloaded", str(exc))
            return

        document.base.runtime = reloaded.part.runtime
        self.document = document
        self.profiles.clear()
        self.engine.invalidate()
        self.undo_stack.clear()
        self._mesh_pick_cache = None
        self._mesh_region = None
        self._project_path = path
        self._last_result = None
        self._dirty = False
        self._undo_baseline = self.document.snapshot()
        self._remember_dir("project", path)
        self._remember_recent(path)

        if opened.missing:
            if self.interactive:
                dialogs.relink_prompt(self, opened.missing)

        self._show_viewport()
        self.viewport.clear()
        self._display_geometry(document.base.runtime, document.base.mode)
        self.viewport.fit_all()
        self.units_box.setCurrentIndex(self.units_box.findData(document.units))
        self._refresh_tree()
        self._refresh_properties()
        self._update_enabled_state()
        self._update_title()
        self.request_rebuild(immediate=True)

    def save_project(self) -> None:
        if self.document.base is None:
            return
        path = self._project_path
        if path is None:
            chosen, _ = QFileDialog.getSaveFileName(
                self, "Save the project",
                str(Path(self._last_dir("project")) / f"{self.document.name}.stamp"),
                "Stamp projects (*.stamp)",
            )
            if not chosen:
                return
            path = Path(chosen)

        thumbnail = self._thumbnail()
        try:
            written = project_io.save(self.document, path, thumbnail=thumbnail)
        except project_io.ProjectError as exc:
            self._notify("Stamp could not save", str(exc))
            return
        self._project_path = written
        self._dirty = False
        self._remember_dir("project", written)
        self._remember_recent(written)
        self._update_title()
        self.statusBar().showMessage(f"Saved {written.name}.")

    def _thumbnail(self) -> bytes | None:
        import tempfile

        try:
            with tempfile.TemporaryDirectory() as tmp:
                png = Path(tmp) / "thumb.png"
                if self.viewport.screenshot(str(png)) and png.exists():
                    return png.read_bytes()
        except Exception:
            return None
        return None

    # ----------------------------------------------------------------- exports

    def _geometry(self):
        if self._last_result is not None and self._last_result.geometry is not None:
            return self._last_result.geometry
        return self.document.base.runtime if self.document.base else None

    def export_step(self) -> None:
        if self.document.base is None:
            return
        if self.document.base.mode != "solid":
            self._notify("There is no STEP to write", export_io.MESH_MODE_NO_STEP)
            return
        options = dialogs.StepExportDialog(self)
        if not self._ask(options):
            return
        schema = options.schema_name()
        merge = options.merge_faces()

        suggested = export_io.default_filename(self.document.name, "step")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export STEP", str(Path(self._last_dir("export")) / suggested),
            "STEP files (*.step *.stp)",
        )
        if not path:
            return
        try:
            result = export_io.export_step(
                self._geometry(), path, schema=schema, simplify=merge
            )
        except export_io.ExportError as exc:
            if not self._confirm("Export anyway?", f"{exc}\n\nWrite the file anyway?"):
                return
            result = export_io.export_step(
                self._geometry(), path, schema=schema, simplify=merge, allow_invalid=True
            )
        self._remember_dir("export", Path(path))
        self._report_export(result)

    def export_stl(self) -> None:
        if self.document.base is None:
            return
        mode = self.document.base.mode
        geometry = self._geometry()

        def counter(deflection: float) -> int:
            return export_io.triangle_count_for(geometry, mode, deflection)

        dialog = dialogs.StlExportDialog(counter, mode=mode, parent=self)
        if not self._ask(dialog):
            return

        suggested = export_io.default_filename(self.document.name, "stl")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export STL", str(Path(self._last_dir("export")) / suggested),
            "STL files (*.stl)",
        )
        if not path:
            return
        try:
            result = export_io.export_stl(
                geometry, path, mode=mode, deflection=dialog.deflection_mm(),
                ascii_format=dialog.ascii_format(),
            )
        except export_io.ExportError as exc:
            self._notify("Stamp could not write the STL", str(exc))
            return
        self._remember_dir("export", Path(path))
        self._report_export(result)

    def export_3mf(self) -> None:
        """Write one body per feature plus the base, for multi-color printing."""
        if self.document.base is None:
            return
        if self._last_result is None or self._last_result.geometry is None:
            self._notify("Nothing to export", "Rebuild the part first.")
            return

        from stamp.geom import color_split

        feature_count = sum(1 for f in self.document.features if f.enabled)
        # Remembered, because a slicer makes a filament for every colour it does
        # not have: setting these to the filaments actually loaded, once, is what
        # stops unwanted entries arriving with every export.
        dialog = dialogs.Color3mfDialog(
            feature_count,
            mode=self.document.base.mode,
            base_color=self.settings.value("3mf/base_color", type=str) or None,
            feature_color=self.settings.value("3mf/feature_color", type=str) or None,
            write_colors=self.settings.value("3mf/write_colors", True, type=bool),
            parent=self,
        )
        if not self._ask(dialog):
            return
        self.settings.setValue("3mf/base_color", dialog.base_color())
        self.settings.setValue("3mf/feature_color", dialog.feature_color())
        self.settings.setValue("3mf/write_colors", dialog.write_colors())

        suggested = export_io.default_filename(self.document.name, "3mf")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export 3MF", str(Path(self._last_dir("export")) / suggested),
            "3MF files (*.3mf)",
        )
        if not path:
            return
        try:
            split = color_split.split_for_color(
                self.document, self._last_result, deflection=dialog.deflection_mm()
            )
            result = export_io.export_3mf(
                split.bodies, path,
                base_color=dialog.base_color(),
                feature_color=dialog.feature_color(),
                write_colors=dialog.write_colors(),
            )
        except (color_split.ColorSplitError, export_io.ExportError) as exc:
            self._notify("Stamp could not write the 3MF", str(exc))
            return
        result.warnings.extend(split.warnings)
        self._remember_dir("export", Path(path))
        self._report_export(result)

    def export_for_quote(self) -> None:
        if self.document.base is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Choose a folder for the quote files", self._last_dir("export")
        )
        if not folder:
            return
        try:
            written = export_io.export_for_quote(
                self._geometry(), folder, self.document.name,
                mode=self.document.base.mode,
                screenshot=self._thumbnail(),
                units=self.document.units,
                volume_mm3=self._last_result.volume if self._last_result else 0.0,
                bbox=self.document.base.bbox,
            )
        except Exception as exc:
            self._notify("Stamp could not write the quote files", str(exc))
            return
        self._remember_dir("export", Path(folder))
        names = "\n".join(f"  {r.path.name}  ({r.size_text})" for r in written)
        if self.interactive:
            dialogs.inform(self, "Quote files written", f"Written to {folder}:\n\n{names}")

    def _report_export(self, result: export_io.ExportResult) -> None:
        parts = [f"{result.path.name} · {result.size_text}"]
        if result.triangle_count:
            parts.append(f"{result.triangle_count:,} triangles")
        self.statusBar().showMessage(" · ".join(parts), 8000)
        if result.warnings:
            self._notify("Note about this export", "\n\n".join(result.warnings))

    # -------------------------------------------------------------- misc slots

    def _on_units_changed(self) -> None:
        self.document.units = self.units_box.currentData()
        self._refresh_properties()

    def recent_projects(self) -> list[str]:
        raw = self.settings.value("recent/projects", [])
        if isinstance(raw, str):
            raw = [raw]
        return [p for p in (raw or []) if Path(p).exists()]

    def _remember_recent(self, path: Path) -> None:
        recent = [str(path)] + [p for p in self.recent_projects() if p != str(path)]
        self.settings.setValue("recent/projects", recent[:10])

    def _last_dir(self, key: str) -> str:
        return str(self.settings.value(f"dirs/{key}", str(Path.home())))

    def _remember_dir(self, key: str, path: Path) -> None:
        self.settings.setValue(f"dirs/{key}", str(path.parent))

    # ------------------------------------------------------------- drag & drop

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                suffix = Path(url.toLocalFile()).suffix.lower()
                if suffix in PART_EXTS or suffix in PROFILE_EXTS or suffix == ".stamp":
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            suffix = path.suffix.lower()
            if suffix == ".stamp":
                self.open_project(path)
            elif suffix in PART_EXTS:
                self.open_part(path)
            elif suffix in PROFILE_EXTS and self.document.base is not None:
                self.add_profile(path, face_pick=self._face_under(event))
            else:
                continue
            event.acceptProposedAction()
            return

    def _offer_dwg_converter(self) -> bool:
        """Point Stamp at an ODA File Converter, and report whether to retry (§5.4).

        DWG never blocks the release: the third option in the dialog is always to
        save the drawing as DXF instead.
        """
        if not self.interactive:
            return False
        dialog = dialogs.DwgConverterDialog(self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return False
        chosen = dialog.converter_path()
        if not chosen:
            return False
        set_oda_converter(chosen)
        self.settings.setValue("dwg/converter", chosen)
        return True

    def _face_under(self, event) -> tuple | None:
        """The face beneath a drop, so artwork lands where it was dropped (§7.1).

        Returns None when the drop was not over the viewport, or not over the part;
        the caller then falls back to asking for a click.
        """
        if self.document.base is None or self.document.base.mode != "solid":
            return None
        position = self.viewport.mapFrom(self, event.position().toPoint())
        if not self.viewport.rect().contains(position):
            return None
        self.viewport.set_selection_mode("face")
        picked = self.viewport.pick_at(position.x(), position.y())
        if picked is None:
            return None

        from OCP.TopAbs import TopAbs_ShapeEnum
        from OCP.TopoDS import TopoDS

        shape, point = picked
        if shape.ShapeType() != TopAbs_ShapeEnum.TopAbs_FACE:
            return None
        return TopoDS.Face_s(shape), (point.X(), point.Y(), point.Z())

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._dirty and self.document.base is not None:
            if not self._confirm(
                "Close without saving?",
                "This project has changes that are not saved. Close it anyway?",
            ):
                event.ignore()
                return
        self.rebuilder.shutdown()
        # This run ended because the user closed it, thus the next start must not
        # report a crash.
        diagnostics.mark_clean_exit()
        # Release the geometry explicitly.  A Manifold still referenced when the
        # interpreter exits makes nanobind report a leak on the way out.
        self.engine.invalidate()
        self.profiles.clear()
        self._last_result = None
        if self.document.base is not None:
            self.document.base.runtime = None
        super().closeEvent(event)


__all__ = ["MainWindow"]
