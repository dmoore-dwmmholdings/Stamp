"""The single window - spec §7.

No modes, no floating palettes.  A command ribbon across the top, feature tree on
the left, viewport in the middle, properties on the right, and a status line
underneath the viewport.

The ribbon replaced a single toolbar along the bottom.  Thirty commands do not
fit on one bar at any ordinary window size, and Qt's answer - moving the overflow
into a chevron menu that shuts again at every layout pass - left the last third
of them with no working path at all.  See :mod:`stamp.ui.ribbon`.
"""

from __future__ import annotations

import math
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
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

from stamp import __version__, diagnostics, reporting, update
from stamp.batch import BatchError, run_batch, simulate_batch
from stamp.core import replace_part as replace_part_io
from stamp.core import snapping
from stamp.core.document import (
    Anchor,
    AnchorKind,
    CodeSpec,
    DatumDefinition,
    Document,
    EdgeRole,
    EdgeSelector,
    Feature,
    MirrorPlane,
    Modifier,
    ModifierKind,
    Operation,
    OperationKind,
    PartTransform,
    Placement,
    ProfileRef,
    TextSpec,
    UndoStack,
)
from stamp.core.inspection import anchor_clearance_measurement, feature_dimensions, settings_for
from stamp.core.profiles import ProfileCache
from stamp.core.rebuild import RebuildEngine, RebuildResult
from stamp.core.refs import (
    make_edge_ref,
    make_face_ref,
    make_hole_ref,
    make_vertex_ref,
    plane_from_face,
    resolve_face_ref,
)
from stamp.geom import mesh_regions, part_transform
from stamp.geom.color_split import divides_by_color, effective_colors
from stamp.geom.mesh_regions import DEFAULT_TOLERANCE_DEG
from stamp.geom.tool_solid import component_footprints
from stamp.io import export as export_io
from stamp.io import project as project_io
from stamp.io.normalize import IssueKind
from stamp.io.part_import import (
    DECIMATE_THRESHOLD,
    PART_EXTS,
    PartImportError,
    PartImportResult,
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
from stamp.ui.import_worker import ImportCancelled, import_part_for_ui
from stamp.ui.properties import PropertiesPanel
from stamp.ui.rebuild_worker import PROGRESS_AFTER_MS, RebuildController
from stamp.ui.ribbon import Ribbon, header_stylesheet
from stamp.ui.update_bar import UpdateBar
from stamp.ui.update_worker import UpdateController
from stamp.ui.viewport import Viewport

BASE_KEY = "base"
RESULT_KEY = "result"
PREVIEW_KEY = "preview"
FOOTPRINT_KEY = "footprint"
REGION_KEY = "mesh_region"
DIMENSIONS_KEY = "inspection_dimensions"
CLEARANCE_KEY = "inspection_clearance"

#: A rebuild slower than this offers to draw the view more coarsely (§10).
SLOW_REBUILD_MS = 10_000.0

ADD_COLOR = (0.36, 0.72, 0.42)
CUT_COLOR = (0.82, 0.36, 0.32)
#: A colour stamp neither adds nor removes anything you can feel, so it gets its
#: own preview colour rather than borrowing the cut's red.
STAMP_COLOR = (0.35, 0.55, 0.90)


def user_settings() -> QSettings:
    """Where Stamp keeps its preferences.

    Behind a function so a test run can put them somewhere of its own: the real
    ones are a registry key on this machine, so tests that read them depend on
    whatever the person at the keyboard last chose, and tests that write them
    change it.
    """
    return QSettings("Stamp", "Stamp")


def _rgb(value: str) -> tuple[float, float, float] | None:
    """``#rrggbb`` as the 0-1 triple the viewport wants, or None."""
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return None
    try:
        return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None
PART_COLOR = (0.62, 0.66, 0.72)
REGION_COLOR = (0.36, 0.62, 0.92)
DIMENSIONS_COLOR = (0.25, 0.78, 0.94)
CLEARANCE_OK_COLOR = (0.94, 0.70, 0.18)
CLEARANCE_WARN_COLOR = (0.88, 0.28, 0.22)


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
        self.settings = user_settings()

        diagnostics.error_reporter = self._report_uncaught

        self._project_path: Path | None = None
        self._last_result: RebuildResult | None = None
        self._pending_profile: ProfileRef | None = None
        self._pending_profile_size: tuple[float, float] = (0.0, 0.0)
        self._pending_feature_template: Feature | None = None
        self._picking_to_face = False
        self._picking_alignment_edge = False
        self._picking_origin_vertex = False
        self._picking_hole_center = False
        self._dirty = False
        self._draft_display = False
        #: A part was just opened and is waiting for the rebuild to draw it.
        self._fit_after_display = False
        self._slow_offer_declined = False
        #: Draw the part the way it will be exported.  Read-only: the artwork is
        #: placed against the part as it came in, so a pick here would land on
        #: the wrong face.
        self._show_transformed = False
        self._busy_since = 0.0
        self._busy_step = ""
        self._mesh_pick_cache: dict | None = None
        self._auto_value_attempts: dict[str, int] = {}
        self._auto_value_note = ""
        self._mesh_region = None
        #: Why the 3D view could not start, or None while it is fine.
        self._viewport_error: str | None = None
        #: The per-component decals currently on screen, to erase next time.
        self._component_footprint_keys: list[str] = []
        #: The per-part shapes currently on screen, same reason.
        self._part_shape_keys: list[str] = []

        #: The newer release that was found, the installer once it has been
        #: fetched and hash-checked, and whether the user asked to have it put
        #: on at the end rather than now.
        self._update_release = None
        self._update_installer: Path | None = None
        self._install_on_quit = False
        self._installing_update = False
        #: Whether this check was asked for.  An automatic one that fails says
        #: nothing; one the user asked for owes them an answer either way.
        self._update_announce = False
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
        self._build_menus()
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
        splitter.setSizes([230, 760, 450])

        # The update bar spans the window above the splitter rather than living
        # in the status strip, because the status strip belongs to the viewport
        # page and there is nothing to see there before a part is opened.
        self.update_bar = UpdateBar()
        page = QWidget()
        stack = QVBoxLayout(page)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(0)
        stack.addWidget(self.update_bar)
        stack.addWidget(splitter, 1)
        self.setCentralWidget(page)

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
        if self._viewport_error is not None:
            return
        self._center.setCurrentWidget(self._viewport_page)

    def _on_viewport_failed(self, reason: str) -> None:
        """Say the 3D view is gone, once, and stop switching to it.

        The rest of Stamp still works without a viewer - a file opens, a feature
        builds, an export writes - so the app stays up and says what is missing
        instead of stopping at launch.
        """
        if self._viewport_error is not None:
            return
        self._viewport_error = reason
        self.viewport.hide()
        self._center.setCurrentIndex(0)
        where = diagnostics.log_path()
        tail = f" The log is at {where}." if where else ""
        self.statusBar().showMessage(f"The 3D view could not start: {reason}.{tail}")
        self._notify(
            "The 3D view could not start",
            "Stamp cannot open a 3D window on this machine, so the part cannot be "
            f"shown. Everything else still works.\n\n{reason}{tail}",
        )

    def _build_status_strip(self) -> QWidget:
        strip = QWidget()
        strip.setFixedHeight(30)
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(10, 2, 10, 2)
        self._status_strip_layout = layout

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

        self.action_show_transformed = QAction("Show the export", self)
        self.action_show_transformed.setCheckable(True)
        self.action_show_transformed.setToolTip(
            "Draw the part as it will be exported - mirrored and scaled. Faces "
            "cannot be picked while this is on, because the artwork is placed "
            "against the part as it came in."
        )
        self.action_show_transformed.toggled.connect(self.set_show_transformed)

        self.action_draft = QAction("Draft view", self)
        self.action_draft.setCheckable(True)
        self.action_draft.setToolTip(
            "Draw the part more coarsely. The geometry and the exports do not change."
        )
        self.action_draft.toggled.connect(self.set_draft_display)

        self.action_inspection = QAction("Inspect", self)
        self.action_inspection.setCheckable(True)
        self.action_inspection.setToolTip(
            "Show the selected stamp's measured envelope and its nearest face-edge or hole clearance."
        )
        self.action_inspection.toggled.connect(self.set_inspection_visible)

        self.action_fit = QAction("Fit to window", self)
        self.action_fit.setToolTip("Frame the whole part in the view (F).")
        self.action_fit.triggered.connect(self.viewport.fit_all)

        self.action_views = QAction("Standard views", self)
        self.action_views.setToolTip(
            "Snap the camera to a standard view. The cube in the corner of the "
            "3D view does the same in one click, and 1-7 do it from the keyboard."
        )

        self.action_view_normal = QAction("Normal to face", self)
        self.action_view_normal.setToolTip(
            "Look straight down the selected stamp's face (Ctrl+8)."
        )
        self.action_view_normal.triggered.connect(self.view_normal_to_face)

        self.action_roll_left = QAction("Roll left", self)
        self.action_roll_left.setToolTip(
            "Spin the view anticlockwise, 15° a press (Alt+Left)."
        )
        self.action_roll_left.triggered.connect(lambda: self.viewport.roll_by(15.0))

        self.action_roll_right = QAction("Roll right", self)
        self.action_roll_right.setToolTip(
            "Spin the view clockwise, 15° a press (Alt+Right)."
        )
        self.action_roll_right.triggered.connect(lambda: self.viewport.roll_by(-15.0))

        self.action_collapse_ribbon = QAction("Collapse the ribbon", self)
        self.action_collapse_ribbon.setCheckable(True)
        self.action_collapse_ribbon.setToolTip(
            "Give the height back to the 3D view. Double-clicking a ribbon tab "
            "does the same (Ctrl+F1)."
        )
        self.action_collapse_ribbon.setShortcut(QKeySequence("Ctrl+F1"))
        self.action_collapse_ribbon.toggled.connect(self._set_ribbon_collapsed)
        self.addAction(self.action_collapse_ribbon)

        self.action_large_ribbon = QAction("Large ribbon buttons", self)
        self.action_large_ribbon.setCheckable(True)
        self.action_large_ribbon.setToolTip(
            "Labels under the icons, in captioned groups. It is about twice as "
            "tall as the compact ribbon, and that height comes off the 3D view."
        )
        self.action_large_ribbon.toggled.connect(self._set_large_ribbon)
        self.addAction(self.action_large_ribbon)

        layout.addWidget(self.status_label)
        layout.addSpacing(16)
        layout.addWidget(self.warning_label, 1)
        layout.addWidget(self.progress)
        layout.addWidget(self.cancel_button)
        for action in (self.action_preview, self.action_inspection, self.action_draft):
            button = QToolButton()
            button.setDefaultAction(action)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setAutoRaise(True)
            layout.addWidget(button)
        return strip

    def _build_actions(self) -> None:
        """Build every command, and lay the ribbon out over them.

        The actions are created first and placed second, because they are shared:
        the menus in :meth:`_build_menus` hold the same objects, so enabling a
        command enables it everywhere it appears.
        """
        self.ribbon = Ribbon()
        self.ribbon.collapsed_changed.connect(self._on_ribbon_collapsed)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._ribbon_holder())

        def make(text: str, slot, shortcut: str | None = None) -> QAction:
            action = QAction(text, self)
            action.triggered.connect(slot)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
                action.setToolTip(f"{text} ({QKeySequence(shortcut).toString()})")
            self.addAction(action)
            return action

        self.action_open_part = make("Open part", self.open_part_dialog, "Ctrl+O")
        self.action_open_project = make("Open project", self.open_project_dialog)
        self.action_save = make("Save", self.save_project, "Ctrl+S")
        self.action_replace_part = make("Replace part", self.replace_part_dialog)
        self.action_relink = make("Relink", self.relink_sources)
        self.action_add_profile = make("Add profile", self.add_profile_dialog, "Ctrl+I")
        self.action_add_text = make("Add text", self.add_text_dialog, "Ctrl+T")
        self.action_add_code = make("Add code", self.add_code_dialog)
        self.action_save_preset = make("Save stamp preset", self.save_preset)
        self.action_insert_preset = make("Insert stamp preset", self.insert_preset)
        self.action_align_edge = make("Align stamp to edge", self.pick_alignment_edge)
        self.action_origin_vertex = make("Set stamp origin to vertex", self.pick_origin_vertex)
        self.action_origin_hole = make("Set stamp origin to hole", self.pick_origin_hole)
        self.action_create_datum = make("Create datum from stamp", self.create_datum_from_feature)
        self.action_place_datum = make("Place stamp on datum", self.place_on_datum)
        self.action_inspection_limits = make("Manufacturing limits", self.edit_inspection_limits)
        self.action_export_step = make("Export STEP", self.export_step)
        self.action_export_stl = make("Export STL", self.export_stl)
        self.action_export_3mf = make("Export 3MF", self.export_3mf)
        self.action_export_quote = make("Export for quote", self.export_for_quote)
        self.action_export_proof = make("Export production proof", self.export_proof_sheet)
        self.action_export_package = make("Export job package", self.export_job_package)
        self.action_batch = make("Batch stamp", self.batch_stamp)
        self.action_undo = make("Undo", self.undo, "Ctrl+Z")
        self.action_redo = make("Redo", self.redo, "Ctrl+Y")
        # Mirror and scale act on the part rather than opening something, and
        # the three planes are toggles, so they are built by hand rather than
        # through make().
        self.action_mirror_yz = QAction(MirrorPlane.YZ.label, self)
        self.action_mirror_yz.setCheckable(True)
        self.action_mirror_yz.triggered.connect(lambda: self.toggle_part_mirror(MirrorPlane.YZ))
        self.addAction(self.action_mirror_yz)
        self.action_mirror_xz = QAction(MirrorPlane.XZ.label, self)
        self.action_mirror_xz.setCheckable(True)
        self.action_mirror_xz.triggered.connect(lambda: self.toggle_part_mirror(MirrorPlane.XZ))
        self.addAction(self.action_mirror_xz)
        self.action_mirror_xy = QAction(MirrorPlane.XY.label, self)
        self.action_mirror_xy.setCheckable(True)
        self.action_mirror_xy.triggered.connect(lambda: self.toggle_part_mirror(MirrorPlane.XY))
        self.addAction(self.action_mirror_xy)
        self.action_mirror = QAction("Mirror the part", self)
        self.action_mirror.setToolTip(
            "Reflect the whole part across a plane on the way out. The stamps "
            "go with it, so a left-hand part is the right-hand one mirrored."
        )
        self.action_scale_part = make("Scale part to size", self.scale_part_dialog)
        self.action_reset_transform = make("Reset mirror and scale", self.reset_part_transform)

        self.units_box = QComboBox()
        self.units_box.addItem("Units: mm", "mm")
        self.units_box.addItem("Units: in", "in")
        self.units_box.currentIndexChanged.connect(self._on_units_changed)

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
        # This is contextual to mesh picking and must remain reachable.  The
        # toolbar can overflow on ordinary laptop-sized windows, so keep this
        # compact control in the viewport status strip instead.
        self.region_tolerance.setVisible(False)
        self._status_strip_layout.addWidget(self.region_tolerance)

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

        self.selection_box = QComboBox()
        self.selection_box.addItem("Select: faces", "face")
        self.selection_box.addItem("Select: edges", "edge")
        self.selection_box.addItem("Select: vertices", "vertex")
        self.selection_box.currentIndexChanged.connect(
            lambda: self.viewport.set_selection_mode(self.selection_box.currentData())
        )

        self.action_preview.setShortcut(QKeySequence("Space"))
        self.addAction(self.action_preview)
        self.addAction(self.action_draft)

        # The report commands stay off the ribbon and live in the status bar,
        # where they are out of the way of the work but never move.
        self.action_report_bug = QAction("Report a bug", self)
        self.action_report_bug.setToolTip(
            "Write an email about a problem, with the log already in it."
        )
        self.action_report_bug.triggered.connect(self.report_bug)
        self.addAction(self.action_report_bug)

        self.action_check_updates = QAction("Check for updates", self)
        self.action_check_updates.setToolTip(
            "Ask github.com whether there is a newer Stamp."
        )
        self.action_check_updates.triggered.connect(self.check_for_updates)
        self.addAction(self.action_check_updates)

        self.action_auto_updates = QAction("Check for updates at startup", self)
        self.action_auto_updates.setCheckable(True)
        self.action_auto_updates.setChecked(
            self.settings.value("update/check_automatically", False, type=bool)
        )
        self.action_auto_updates.setToolTip(
            "Read one small file from github.com when Stamp starts, at most once "
            "a day. Nothing about you or your parts is sent."
        )
        self.action_auto_updates.toggled.connect(
            lambda on: self.settings.setValue("update/check_automatically", bool(on))
        )
        self.addAction(self.action_auto_updates)

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
            ["front", "back", "left", "right", "top", "bottom", "iso"], start=1
        ):
            self._hidden_action(
                f"View {name}", lambda _=False, n=name: self.viewport.set_preset_view(n), str(index)
            )
        # Rolling and "normal to" from the window rather than the 3D view, so they
        # answer wherever the keyboard focus happens to be.  Orbiting with the
        # arrows stays on the viewport: the arrows belong to whatever is focused,
        # and taking them window-wide would break every spin box in the panel.
        self.action_roll_left.setShortcut(QKeySequence("Alt+Left"))
        self.action_roll_right.setShortcut(QKeySequence("Alt+Right"))
        self.action_view_normal.setShortcut(QKeySequence("Ctrl+8"))
        for action in (
            self.action_roll_left, self.action_roll_right, self.action_view_normal
        ):
            self.addAction(action)
        self._hidden_action("Cancel", self._cancel_pending, "Esc")
        self._lay_out_ribbon()

    def _ribbon_holder(self) -> QToolBar:
        """A toolbar whose only job is to carry the ribbon.

        QMainWindow reserves the top strip for toolbars, so putting the ribbon in
        one keeps the menu bar, the ribbon and the central splitter stacking the
        way they should without a second layout of our own.
        """
        holder = QToolBar("Ribbon")
        holder.setObjectName("ribbonHolder")
        holder.setMovable(False)
        holder.setFloatable(False)
        holder.setContentsMargins(0, 0, 0, 0)
        holder.addWidget(self.ribbon)
        self._ribbon_holder_bar = holder
        return holder

    def _apply_header_theme(self) -> None:
        """Give the menu bar and the ribbon's strip one surface.

        A menu bar in the desktop's style sitting on top of a ribbon in its own
        reads as two programs stacked.  Both are painted from the same mix, so
        the whole top of the window is one piece of chrome - and both follow a
        light or a dark desktop, because the mix comes from the palette.
        """
        sheet = header_stylesheet(self.ribbon.theme)
        self.menuBar().setStyleSheet(sheet)
        self._ribbon_holder_bar.setStyleSheet(sheet)
        self.update_bar.apply_theme(self.ribbon.theme)

    def _set_ribbon_collapsed(self, collapsed: bool) -> None:
        self.ribbon.set_collapsed(collapsed)

    def _set_large_ribbon(self, large: bool) -> None:
        """The ribbon opens compact; this is for anyone who wants it roomy."""
        self.ribbon.set_dense(not large)
        self.settings.setValue("ui/large_ribbon", bool(large))

    def _on_ribbon_collapsed(self, collapsed: bool) -> None:
        """Keep the menu item in step when the ribbon is collapsed by other means."""
        if self.action_collapse_ribbon.isChecked() != collapsed:
            self.action_collapse_ribbon.setChecked(collapsed)

    def _mirror_menu(self):
        """The three planes in one press.

        They are toggles rather than commands - a part can be mirrored across
        two planes at once - so the button holds them rather than being one of
        them.
        """
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        menu.addAction(self.action_mirror_yz)
        menu.addAction(self.action_mirror_xz)
        menu.addAction(self.action_mirror_xy)
        menu.addSeparator()
        menu.addAction(self.action_reset_transform)
        return menu

    def _views_menu(self):
        """Every standard view in one press, each with the key that also does it."""
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        for name, key in (
            ("Isometric", "7"), ("Front", "1"), ("Back", "2"), ("Left", "3"),
            ("Right", "4"), ("Top", "5"), ("Bottom", "6"),
        ):
            view = name.lower().replace("isometric", "iso")
            action = menu.addAction(f"{name}	{key}")
            action.triggered.connect(
                lambda _checked=False, v=view: self.viewport.set_preset_view(v)
            )
        menu.addSeparator()
        menu.addAction(self.action_view_normal)
        menu.addAction(self.action_fit)
        return menu

    def view_normal_to_face(self) -> None:
        """Look straight down the face the selected stamp sits on.

        The face is the one the feature is anchored to, which is the face the
        user picked; without a selection there is nothing to be normal to, and
        saying so is more use than turning the camera somewhere arbitrary.
        """
        feature = self.selected_feature
        anchor = feature.placement.anchor if feature is not None else None
        face_ref = getattr(anchor, "face_ref", None) if anchor else None
        if face_ref is None:
            self.statusBar().showMessage(
                "Select a stamp first - 'Normal to face' looks down the face it is on.",
                6000,
            )
            return
        self.viewport.look_along(face_ref.normal)

    def _lay_out_ribbon(self) -> None:
        """Put every command on a tab, in a group named for what it is for.

        The short caption is given here rather than derived from the command
        name: a button is narrower than most of these names, and a name left to
        elide reads as "Save ...reset".  The full name stays on the action, for
        the tooltip and for the menus.
        """
        home = self.ribbon.add_tab("Home")
        project = home.add_group("Project")
        project.add_action(self.action_open_part, "open-part", "Open\npart")
        project.add_small_action(self.action_open_project, "open-project", "Open project")
        project.add_small_action(self.action_save, "save", "Save")

        artwork = home.add_group("Artwork")
        artwork.add_action(self.action_add_profile, "add-profile", "Add\nprofile")
        artwork.add_action(self.action_add_text, "add-text", "Add\ntext")
        artwork.add_action(self.action_add_code, "add-code", "Add\ncode")

        edit = home.add_group("Edit")
        edit.add_small_action(self.action_undo, "undo", "Undo")
        edit.add_small_action(self.action_redo, "redo", "Redo")

        presets = home.add_group("Presets")
        presets.add_action(self.action_insert_preset, "insert-preset", "Insert\npreset")
        presets.add_small_action(self.action_save_preset, "save-preset", "Save preset")

        place = self.ribbon.add_tab("Place")
        align = place.add_group("Align")
        align.add_action(self.action_align_edge, "align-edge", "Align\nto edge")
        align.add_small_action(self.action_origin_vertex, "origin-vertex", "Origin: vertex")
        align.add_small_action(self.action_origin_hole, "origin-hole", "Origin: hole")

        datums = place.add_group("Datums")
        datums.add_action(self.action_create_datum, "datum-new", "New\ndatum")
        datums.add_action(self.action_place_datum, "datum-place", "On\ndatum")

        part = place.add_group("Part")
        part.add_small_action(self.action_replace_part, "replace-part", "Replace part")
        part.add_small_action(self.action_relink, "relink", "Relink")
        part.add_small_action(self.action_inspection_limits, "limits", "Limits")

        transform = place.add_group("Transform")
        transform.add_menu_action(
            self.action_mirror, "mirror", "Mirror", self._mirror_menu()
        )
        transform.add_action(self.action_scale_part, "scale-part", "Scale\nto size")
        transform.add_small_action(
            self.action_reset_transform, "reset-transform", "Reset"
        )
        transform.add_small_action(
            self.action_show_transformed, "show-export", "Show the export"
        )

        export = self.ribbon.add_tab("Export")
        files = export.add_group("Files")
        files.add_action(self.action_export_step, "export-step", "STEP")
        files.add_action(self.action_export_stl, "export-stl", "STL")
        files.add_action(self.action_export_3mf, "export-3mf", "3MF")

        packages = export.add_group("For other people")
        packages.add_action(self.action_export_package, "export-package", "Job\npackage")
        packages.add_small_action(self.action_export_quote, "export-quote", "For quote")
        packages.add_small_action(self.action_export_proof, "export-proof", "Proof sheet")

        production = export.add_group("Production")
        production.add_action(self.action_batch, "batch", "Batch")

        view = self.ribbon.add_tab("View")
        cameras = view.add_group("Orient")
        cameras.add_menu_action(self.action_views, "views", "Views", self._views_menu())
        cameras.add_action(self.action_view_normal, "view-normal", "Normal\nto face")
        cameras.add_action(self.action_fit, "fit", "Fit to\nwindow")
        cameras.add_stack([self.view_box])

        turning = view.add_group("Turn")
        turning.add_small_action(self.action_roll_left, "roll-left", "Roll left")
        turning.add_small_action(self.action_roll_right, "roll-right", "Roll right")

        showing = view.add_group("Showing")
        showing.add_action(self.action_preview, "preview", "Preview")
        showing.add_action(self.action_draft, "draft", "Draft\nview")
        showing.add_action(self.action_inspection, "inspect", "Inspect")

        picking = view.add_group("Picking")
        picking.add_stack([self.selection_box])

        measure = view.add_group("Measurement")
        measure.add_stack([self.units_box, self.density_field])

    def _build_menus(self) -> None:
        """Put every command somewhere it can be found and clicked.

        The toolbar holds more than fits: at an ordinary 1400 px window Qt moves
        seventeen commands into the overflow chevron, and that menu shuts again at
        each layout pass - so Export STEP, which has no shortcut either, had no
        working path at all.  These are the same QAction objects the toolbar uses,
        so nothing here duplicates state; a menu is just a second way in.
        """
        bar = self.menuBar()

        file_menu = bar.addMenu("&File")
        file_menu.addAction(self.action_open_part)
        file_menu.addAction(self.action_open_project)
        file_menu.addAction(self.action_save)
        file_menu.addSeparator()
        file_menu.addAction(self.action_replace_part)
        file_menu.addAction(self.action_relink)
        file_menu.addSeparator()
        file_menu.addAction(self.action_batch)

        edit_menu = bar.addMenu("&Edit")
        edit_menu.addAction(self.action_undo)
        edit_menu.addAction(self.action_redo)

        insert_menu = bar.addMenu("&Insert")
        insert_menu.addAction(self.action_add_profile)
        insert_menu.addAction(self.action_add_text)
        insert_menu.addAction(self.action_add_code)
        insert_menu.addSeparator()
        insert_menu.addAction(self.action_save_preset)
        insert_menu.addAction(self.action_insert_preset)
        insert_menu.addSeparator()
        insert_menu.addAction(self.action_align_edge)
        insert_menu.addAction(self.action_origin_vertex)
        insert_menu.addAction(self.action_origin_hole)
        insert_menu.addAction(self.action_create_datum)
        insert_menu.addAction(self.action_place_datum)

        export_menu = bar.addMenu("E&xport")
        for action in (
            self.action_export_step,
            self.action_export_stl,
            self.action_export_3mf,
            self.action_export_quote,
            self.action_export_proof,
            self.action_export_package,
        ):
            export_menu.addAction(action)

        part_menu = bar.addMenu("&Part")
        for action in (
            self.action_mirror_yz,
            self.action_mirror_xz,
            self.action_mirror_xy,
        ):
            part_menu.addAction(action)
        part_menu.addSeparator()
        part_menu.addAction(self.action_scale_part)
        part_menu.addAction(self.action_reset_transform)

        view_menu = bar.addMenu("&View")
        orient = view_menu.addMenu("Orientation")
        for name, key in (
            ("Isometric", "7"), ("Front", "1"), ("Back", "2"), ("Left", "3"),
            ("Right", "4"), ("Top", "5"), ("Bottom", "6"),
        ):
            preset = name.lower().replace("isometric", "iso")
            entry = orient.addAction(name)
            entry.setShortcut(QKeySequence(key))
            entry.triggered.connect(
                lambda _checked=False, v=preset: self.viewport.set_preset_view(v)
            )
        view_menu.addAction(self.action_view_normal)
        view_menu.addAction(self.action_fit)
        view_menu.addSeparator()
        view_menu.addAction(self.action_roll_left)
        view_menu.addAction(self.action_roll_right)
        view_menu.addSeparator()
        view_menu.addAction(self.action_preview)
        view_menu.addAction(self.action_show_transformed)
        view_menu.addAction(self.action_inspection)
        view_menu.addAction(self.action_draft)
        view_menu.addAction(self.action_collapse_ribbon)
        view_menu.addAction(self.action_large_ribbon)
        view_menu.addSeparator()
        view_menu.addAction(self.action_inspection_limits)

        help_menu = bar.addMenu("&Help")
        help_menu.addAction(self.action_check_updates)
        help_menu.addAction(self.action_auto_updates)
        help_menu.addSeparator()
        help_menu.addAction(self.action_report_bug)
        help_menu.addAction(self.action_report_crash)

        # Save, undo and redo are wanted from whichever tab you happen to be on,
        # so they also sit level with the menus, the way Word and SolidWorks both
        # keep a quick-access bar up there.  Same actions - a ribbon shows one
        # command in more than one place on purpose.
        self.quick_access = self.ribbon.quick_access_bar()
        self.quick_access.add_action(self.action_save, "save")
        self.quick_access.add_action(self.action_undo, "undo")
        self.quick_access.add_action(self.action_redo, "redo")
        bar.setCornerWidget(self.quick_access, Qt.Corner.TopRightCorner)

        self._apply_header_theme()

        # Last, because the ribbon has to be built before it can change size.
        if self.settings.value("ui/large_ribbon", False, type=bool):
            self.action_large_ribbon.setChecked(True)

    def _hidden_action(self, text: str, slot, shortcut: str) -> QAction:
        action = QAction(text, self)
        action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(slot)
        action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.addAction(action)
        return action

    def _wire(self) -> None:
        self.updater = UpdateController(self)
        self.updater.found.connect(self._on_update_found)
        self.updater.up_to_date.connect(self._on_update_absent)
        self.updater.failed.connect(self._on_update_failed)
        self.updater.progress.connect(self.update_bar.advance)
        self.updater.ready.connect(self._on_update_ready)

        self.update_bar.install_requested.connect(self._on_update_install)
        self.update_bar.later_requested.connect(self._on_update_later)
        self.update_bar.notes_requested.connect(self._on_update_notes)
        self.update_bar.skip_requested.connect(self._on_update_skip)
        self.update_bar.cancel_requested.connect(self._on_update_cancel)
        self.update_bar.dismissed.connect(self.update_bar.hide_bar)

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
        self.viewport.init_failed.connect(self._on_viewport_failed)

        self.tree.feature_selected.connect(self._on_feature_selected)
        self.tree.modifier_selected.connect(lambda fid, _mid: self._on_feature_selected(fid))
        self.tree.enabled_toggled.connect(self._on_enabled_toggled)
        self.tree.renamed.connect(self._on_renamed)
        self.tree.reordered.connect(self._on_reordered)
        self.tree.duplicate_requested.connect(self._duplicate_feature)
        self.tree.delete_requested.connect(self._delete_feature)
        self.tree.mirror_requested.connect(self._mirror_feature)
        self.tree.part_visibility_toggled.connect(self.set_part_visible)
        self.tree.part_isolate_requested.connect(self.isolate_part)
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


    # ------------------------------------------------------------------ updates

    def begin_update_check(self) -> None:
        """Ask about a newer Stamp, if the user has ever agreed to that.

        Called from the entry point once the window is up, never from __init__:
        a test builds a window, and a test must not make a network call.  The
        first run asks before anything is fetched - Stamp already promises that
        a crash report is never sent without being looked at, and a version
        check is the same kind of promise about the same kind of machine.
        """
        if not self.interactive or not update.is_configured():
            return
        decided = self.settings.contains("update/check_automatically")
        if not decided:
            wanted = dialogs.confirm(
                self,
                "Check for updates?",
                "May Stamp check github.com for a new version when it starts?\n\n"
                "It sends nothing about you or your parts - it reads one small "
                "file. You can change this later in Help.",
            )
            self.settings.setValue("update/check_automatically", bool(wanted))
        if not self.settings.value("update/check_automatically", False, type=bool):
            return
        # Once a day is plenty for a program somebody opens to do a job.
        last = int(self.settings.value("update/last_check", 0, type=int))
        if time.time() - last < 20 * 60 * 60:
            return
        self._check_for_updates(announce=False)

    def check_for_updates(self) -> None:
        """Help -> Check for updates. Always asks, and always says what it found."""
        if not update.is_configured():
            self._notify(
                "Updates are switched off",
                "This build of Stamp has no release key compiled into it, so it "
                "cannot tell a real update from a fake one and will not look.",
            )
            return
        self._check_for_updates(announce=True)

    def _check_for_updates(self, announce: bool) -> None:
        self._update_announce = announce
        self.settings.setValue("update/last_check", int(time.time()))
        if announce:
            self.statusBar().showMessage("Checking for a newer Stamp…")
        # Three seconds in when it is automatic: a start is the one moment the
        # user is waiting on, and nothing about this is urgent.
        self.updater.check(__version__, after_ms=0 if announce else 3000)

    def _on_update_found(self, release) -> None:
        self._update_release = release
        if not self._update_announce and self.settings.value(
            "update/skipped_version", "", type=str
        ) == release.version and not release.urgent:
            return
        self.update_bar.offer(
            release.version,
            urgent=release.urgent,
            installable=update.can_install() and release.artifact is not None,
        )

    def _on_update_absent(self) -> None:
        if self._update_announce:
            self.statusBar().showMessage(f"Stamp {__version__} is the newest version.")

    def _on_update_failed(self, reason: str) -> None:
        # A failed check is not the user's problem unless they asked for one.
        # Stamp is a program for putting artwork on parts; it does not owe
        # anybody a red banner because a laptop is on a train.
        if self._update_announce:
            self.update_bar.trouble(reason)
        else:
            diagnostics.breadcrumb("update check failed: %s", reason)

    def _on_update_install(self) -> None:
        if self._update_installer is not None:
            self._apply_update(now=True)
            return
        release = self._update_release
        if release is None or release.artifact is None:
            return
        self._update_announce = True
        self.update_bar.downloading(release.version)
        self.updater.fetch(release, Path(tempfile.gettempdir()) / "stamp-update")

    def _on_update_ready(self, path: str) -> None:
        self._update_installer = Path(path)
        version = self._update_release.version if self._update_release else ""
        self.update_bar.ready(version)

    def _on_update_later(self) -> None:
        """Install on the way out, which is when nobody is waiting for it."""
        self._install_on_quit = True
        self.update_bar.hide_bar()
        self.statusBar().showMessage("Stamp will install the update when you close it.")

    def _on_update_notes(self) -> None:
        release = self._update_release
        url = (release.notes_url if release else "") or (
            "https://github.com/dmoore-dwmmholdings/Stamp/releases/latest"
        )
        QDesktopServices.openUrl(QUrl(url))

    def _on_update_skip(self) -> None:
        if self._update_release is not None:
            self.settings.setValue(
                "update/skipped_version", self._update_release.version
            )
        self.update_bar.hide_bar()

    def _on_update_cancel(self) -> None:
        self.updater.cancel()
        self.update_bar.hide_bar()

    def _apply_update(self, now: bool) -> None:
        """Start the installer and close, in that order and only in that order.

        Never over unsaved work: the installer closes Stamp behind us, and a
        program that throws away somebody's part to install a newer copy of
        itself has failed at the only thing that matters.
        """
        if self._update_installer is None:
            return
        if now and self._dirty and self.document.base is not None:
            if not self._confirm(
                "Save first?",
                "This project has changes that are not saved, and Stamp has to "
                "close to install. Install anyway?",
            ):
                return
        if now and self.rebuilder.busy:
            self._notify(
                "Stamp is busy",
                "Wait for the rebuild to finish, then install the update.",
            )
            return
        # Set before the installer starts, not after.  Closing asks about
        # unsaved work, and the question has already been asked here - being
        # asked it twice, with an installer already running behind the window,
        # is how somebody ends up answering "no" to a close that happens anyway.
        self._installing_update = True
        try:
            update.install(self._update_installer)
        except update.UpdateError as exc:
            self._installing_update = False
            self._update_announce = True
            self.update_bar.trouble(str(exc))
            return
        if now:
            self.close()

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
        self.action_add_code.setEnabled(has_part)
        self.action_save.setEnabled(has_part)
        self.action_replace_part.setEnabled(has_part)
        self.action_export_step.setEnabled(solid)
        self.action_export_stl.setEnabled(has_part)
        self.action_export_3mf.setEnabled(has_part)
        self.action_export_package.setEnabled(has_part)
        self.action_export_quote.setEnabled(has_part)
        self.action_export_proof.setEnabled(has_part)
        self.action_undo.setEnabled(self.undo_stack.can_undo())
        self.action_redo.setEnabled(self.undo_stack.can_redo())
        self.action_export_step.setToolTip(
            "" if solid else export_io.MESH_MODE_NO_STEP
        )
        mesh = has_part and self.document.base.mode == "mesh"
        self.region_tolerance.setVisible(mesh)
        self._refresh_transform_actions()

    def _refresh_tree(self) -> None:
        results = {}
        if self._last_result is not None:
            results = {r.feature_id: r for r in self._last_result.features}
        self.tree.set_document(self.document, results)

    def _refresh_properties(self) -> None:
        feature = self.selected_feature
        if feature is None:
            self.properties.show_base(
                self.document.base, self.document.units, document=self.document
            )
            self.handles.set_feature(None, None)
            return
        size = self._native_size(feature)
        self.properties.show_feature(
            self.document, feature, size,
            mesh_mode=self.document.base is not None and self.document.base.mode == "mesh",
            profile=self._profile_of(feature),
        )
        self._refresh_snap_targets(feature)
        self.handles.set_feature(feature, size)

    def _profile_of(self, feature: Feature):
        """The normalized artwork behind a feature, or None while it cannot be read.

        Only the colour list wants it, and a missing source is already reported
        as a broken feature, so this stays quiet.
        """
        try:
            return self.profiles.get(feature.profile)
        except Exception:  # noqa: BLE001
            return None

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

    def _import_part(self, path: Path, **options) -> PartImportResult | None:
        """Read a part with the window still alive, or report why it could not.

        Returns None when the import failed or the user stopped it; both are
        "leave everything as it was", and the message has already been shown.
        """
        try:
            return import_part_for_ui(self, path, draft=self._draft_display, **options)
        except ImportCancelled:
            self.statusBar().showMessage(f"Opening {path.name} was stopped.", 5000)
            return None
        except PartImportError as exc:
            self._notify("Stamp cannot open this part", str(exc))
            return None

    @staticmethod
    def _solids_disjoint(result) -> bool:
        """Whether the file's solids are separate bodies.

        The import subprocess works this out while it has the shapes, because it
        is a boolean per pair and the dialog asking the question cannot wait for
        that.  In process, the shapes are still here, so it is worked out now.
        """
        if result.solids_disjoint is not None:
            return result.solids_disjoint
        return not solids_intersect(result.solids)

    def open_part(self, path: Path) -> None:
        result = self._import_part(path)
        if result is None:
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
                result = self._import_part(path, unit_scale=dialog.scale())
                if result is None:
                    return

        if result.solid_count > 1:
            choice = dialogs.SolidChoiceDialog(
                result.solid_count, disjoint=self._solids_disjoint(result), parent=self
            )
            if not self._ask(choice):
                return
            index = choice.solid_index()
            if index is not None:
                result = self._import_part(path, solid_index=index)
                if result is None:
                    return

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
        self._fit_after_display = True
        self._refresh_tree()
        self._refresh_properties()
        self._update_enabled_state()
        self._update_title()
        # The rebuild both puts the part on screen and gives the status line a
        # volume.  Displaying it here as well builds the same presentation twice,
        # which on a converted mesh is a second and a half thrown away.
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

        result = self._import_part(path)
        if result is None:
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
                result = self._import_part(path, unit_scale=dialog.scale())
                if result is None:
                    return

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

        self._fit_after_display = True
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

    def add_code_dialog(self) -> None:
        """Create a generated code and use the normal face-placement flow."""
        if self.document.base is None:
            return
        payload, accepted = QInputDialog.getText(self, "Add code", "Payload", text="STAMP")
        if not accepted:
            return
        kind, accepted = QInputDialog.getItem(
            self, "Add code", "Symbology", ["QR", "Data Matrix"], 0, False
        )
        if not accepted:
            return
        from stamp.core.document import CodeKind

        spec = CodeSpec(kind=CodeKind.QR if kind == "QR" else CodeKind.DATA_MATRIX, payload=payload)
        ref = ProfileRef(code=spec)
        try:
            profile = self.profiles.get(ref)
        except Exception as exc:
            self._notify("Stamp cannot create this code", str(exc))
            return
        self.profiles.put(ref, profile)
        self._pending_profile = ref
        self._pending_feature_template = None
        self._pending_profile_size = (profile.width, profile.height)
        self.viewport.set_selection_mode("face")
        self.selection_box.setCurrentIndex(0)
        self.statusBar().showMessage("Click the face for the code. Press Esc to cancel.")

    def save_preset(self) -> None:
        if self.selected_feature is None:
            self._notify("Choose a stamp", "Select a feature in the tree before saving a preset.")
            return
        from stamp.io.presets import library_dir, save_preset

        suggested = library_dir() / f"{self.selected_feature.name}.stamp-preset"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save stamp preset", str(suggested), "Stamp presets (*.stamp-preset)"
        )
        if not path:
            return
        try:
            written = save_preset(self.selected_feature, path)
        except Exception as exc:
            self._notify("Stamp could not save the preset", str(exc))
            return
        self._notify("Preset saved", f"Saved {written.name} to the preset library.")

    def insert_preset(self) -> None:
        if self.document.base is None:
            return
        from stamp.io.presets import list_preset_info, load_preset

        catalog = list_preset_info()
        if catalog:
            dialog = dialogs.PresetLibraryDialog(catalog, self)
            if dialog.exec() != dialog.DialogCode.Accepted:
                if not dialog.browse_external:
                    return
                path, _ = QFileDialog.getOpenFileName(
                    self, "Insert stamp preset", self._last_dir("project"), "Stamp presets (*.stamp-preset)"
                )
            else:
                path = dialog.selected_path()
        else:
            path, _ = QFileDialog.getOpenFileName(
                self, "Insert stamp preset", self._last_dir("project"), "Stamp presets (*.stamp-preset)"
            )
        if path is None or not str(path):
            return
        try:
            feature = load_preset(path, Path(self._last_dir("project")) / ".stamp_presets")
            profile = self.profiles.get(feature.profile)
        except Exception as exc:
            self._notify("Stamp could not open the preset", str(exc))
            return
        self.profiles.put(feature.profile, profile)
        self._pending_profile = feature.profile
        self._pending_feature_template = feature
        self._pending_profile_size = (profile.width, profile.height)
        self.viewport.set_selection_mode("face")
        self.selection_box.setCurrentIndex(0)
        self.statusBar().showMessage("Click the face for the preset. Press Esc to cancel.")

    def pick_alignment_edge(self) -> None:
        if self.document.base is None or self.document.base.mode != "solid" or self.selected_feature is None:
            self._notify("Choose a solid stamp", "Select a feature on a solid part before choosing an alignment edge.")
            return
        self._picking_alignment_edge = True
        self.viewport.set_selection_mode("edge")
        self.selection_box.setCurrentIndex(1)
        self.statusBar().showMessage("Click an edge to set the stamp origin and horizontal direction. Press Esc to cancel.")

    def pick_origin_vertex(self) -> None:
        if self.document.base is None or self.document.base.mode != "solid" or self.selected_feature is None:
            self._notify("Choose a solid stamp", "Select a feature on a solid part before choosing a vertex.")
            return
        self._picking_origin_vertex = True
        self.viewport.set_selection_mode("vertex")
        self.selection_box.setCurrentIndex(2)
        self.statusBar().showMessage("Click a vertex to set the stamp origin. Press Esc to cancel.")

    def pick_origin_hole(self) -> None:
        if self.document.base is None or self.document.base.mode != "solid" or self.selected_feature is None:
            self._notify("Choose a solid stamp", "Select a feature on a solid part before choosing a hole.")
            return
        self._picking_hole_center = True
        self.viewport.set_selection_mode("face")
        self.selection_box.setCurrentIndex(0)
        self.statusBar().showMessage("Click a cylindrical hole wall to set the stamp origin. Press Esc to cancel.")

    def create_datum_from_feature(self) -> None:
        feature = self.selected_feature
        if feature is None or feature.placement.anchor.plane is None:
            self._notify("Choose a stamp", "Select a placed feature to create a named datum from its plane.")
            return
        name, accepted = QInputDialog.getText(self, "Create datum", "Name", text=f"{feature.name} datum")
        if not accepted or not name.strip():
            return
        self._push_undo("create datum")
        self.document.datums.append(DatumDefinition(name=name.strip(), plane=feature.placement.anchor.plane))
        self.statusBar().showMessage(f"Created datum {name.strip()}.", 5000)

    def place_on_datum(self) -> None:
        feature = self.selected_feature
        if feature is None:
            return
        choices = [(datum.name, datum.id) for datum in self.document.datums]
        choices.extend((name, name) for name in ("XY", "XZ", "YZ"))
        labels = [item[0] for item in choices]
        if not labels:
            return
        label, accepted = QInputDialog.getItem(self, "Place stamp on datum", "Datum", labels, 0, False)
        if not accepted:
            return
        datum_id = dict(choices)[label]
        self._push_undo("place on datum")
        feature.placement.anchor = Anchor(kind=AnchorKind.DATUM, datum=datum_id)
        self.request_rebuild(immediate=True)

    def edit_inspection_limits(self) -> None:
        settings = self.document.inspection
        from stamp.core.inspection import MANUFACTURING_RULESETS, apply_ruleset

        choices = ["Custom", *MANUFACTURING_RULESETS]
        selected, accepted = QInputDialog.getItem(
            self, "Manufacturing limits", "Ruleset", choices, 0, False
        )
        if not accepted:
            return
        if selected != "Custom":
            self._push_undo(f"{selected.lower()} ruleset")
            apply_ruleset(settings, selected)
            self.statusBar().showMessage(f"Applied {selected} ruleset. You can refine it next time.", 5000)
            return
        detail, accepted = QInputDialog.getDouble(
            self, "Manufacturing limits", "Minimum detail (mm)", settings.min_detail_mm, 0.01, 100.0, 3
        )
        if not accepted:
            return
        depth, accepted = QInputDialog.getDouble(
            self, "Manufacturing limits", "Minimum depth (mm)", settings.min_depth_mm, 0.01, 100.0, 3
        )
        if not accepted:
            return
        clearance, accepted = QInputDialog.getDouble(
            self, "Manufacturing limits", "Minimum edge/hole clearance (mm)", settings.min_clearance_mm, 0.01, 100.0, 3
        )
        if not accepted:
            return
        self._push_undo("manufacturing limits")
        settings.min_detail_mm = detail
        settings.min_depth_mm = depth
        settings.min_clearance_mm = clearance
        self.statusBar().showMessage("Manufacturing limits updated.", 5000)

    def add_text_feature(self, face_pick=None) -> None:
        """Start a text feature, then wait for the user to click the face.

        The message becomes a profile, thus everything after this point is the
        same as a profile that came from a file.
        """
        if self.document.base is None:
            return
        ref = ProfileRef(text=TextSpec(text="TEXT", size_mm=self._default_text_size()))
        self._pending_profile = ref
        self._pending_feature_template = None
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
        self._pending_feature_template = None
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

        if self._show_transformed:
            # What is on screen is the export, not the part the artwork is placed
            # against, so a pick here would anchor to a face that does not exist in
            # the document's own space.
            self.statusBar().showMessage(
                "Turn off View \u2192 Show the export to pick a face.", 4000
            )
            return
        if self.document.base is not None and self.document.base.mode == "mesh":
            self._on_mesh_picked()
            return
        if shape is None or shape.IsNull():
            return
        if self._picking_origin_vertex:
            if shape.ShapeType() != TopAbs_ShapeEnum.TopAbs_VERTEX or self.selected_feature is None:
                return
            self._push_undo("set origin to vertex")
            self.selected_feature.placement.anchor.origin_ref = make_vertex_ref(TopoDS.Vertex_s(shape))
            self._picking_origin_vertex = False
            self.viewport.set_selection_mode("face")
            self.selection_box.setCurrentIndex(0)
            self.request_rebuild(immediate=True)
            return
        if self._picking_hole_center:
            if shape.ShapeType() != TopAbs_ShapeEnum.TopAbs_FACE or self.selected_feature is None:
                return
            try:
                point_ref = make_hole_ref(TopoDS.Face_s(shape), (point.X(), point.Y(), point.Z()))
            except Exception as exc:
                self._notify("That is not a hole", str(exc))
                return
            self._push_undo("set origin to hole")
            self.selected_feature.placement.anchor.origin_ref = point_ref
            self._picking_hole_center = False
            self.request_rebuild(immediate=True)
            return
        if self._picking_alignment_edge:
            if shape.ShapeType() != TopAbs_ShapeEnum.TopAbs_EDGE:
                return
            feature = self.selected_feature
            if feature is None:
                return
            self._push_undo("align to edge")
            feature.placement.anchor.alignment_ref = make_edge_ref(TopoDS.Edge_s(shape))
            self._picking_alignment_edge = False
            self.viewport.set_selection_mode("face")
            self.selection_box.setCurrentIndex(0)
            self.request_rebuild(immediate=True)
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
        elif ref.is_code:
            name = "QR code" if str(ref.code.kind) == "qr" else "Data Matrix"
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
        if self._pending_feature_template is not None:
            template = self._pending_feature_template.copy_with_new_id()
            feature.name = template.name
            feature.operation = template.operation
            feature.modifiers = template.modifiers
            feature.pattern = template.pattern
            feature.metadata = template.metadata
            feature.inspection = template.inspection
            feature.placement.offset_2d = template.placement.offset_2d
            feature.placement.rotation = template.placement.rotation
            feature.placement.scale = template.placement.scale
            feature.placement.uniform_scale = template.placement.uniform_scale
            feature.placement.mirror_u = template.placement.mirror_u
            feature.placement.mirror_v = template.placement.mirror_v
            feature.placement.lift = template.placement.lift
            feature.placement.mode = template.placement.mode
        self._push_undo("add feature")
        self.document.add_feature(feature)
        self._pending_profile = None
        self._pending_feature_template = None
        # The prompt that asked for this click has been answered.  Left up, it goes
        # on telling the user to click a face to place something already placed.
        self.statusBar().showMessage(f"Placed {feature.name}.")

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

    def _part_index_at(self, point) -> int:
        """Which part a click landed on, or -1 when the file is one part."""
        base = self.document.base
        if base is None or not base.parts:
            return -1
        part = base.part_at(tuple(point))
        return part.index if part is not None else -1

    def _create_mesh_feature(self, ref: ProfileRef, region) -> None:
        if ref.is_text:
            first = (ref.text.text.strip().splitlines() or ["Text"])[0]
            name = (first[:24] or "Text")
        else:
            name = Path(ref.source_path).stem or "Feature"
        feature = Feature(
            name=name,
            profile=ref,
            part_index=self._part_index_at(region.point),
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
            plane, _ = resolve_anchor(feature.placement.anchor, self.document.base.runtime, self.document.datums)
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
            if self._fit_after_display:
                self._fit_after_display = False
                self.viewport.fit_all()

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
        if self._fit_after_display and self.document.base is not None:
            # The rebuild that was going to put a freshly opened part on screen
            # did not get there.  Show the part itself rather than an empty view.
            self._fit_after_display = False
            self._display_geometry(self.document.base.runtime, self.document.base.mode)
            self.viewport.fit_all()

    def _transformed(self, geometry):
        """The geometry as it would leave the exporter, when asked for.

        "Show the export" previews the mirror and scale, so the same
        transform the exporter applies has to run on the way to the screen.
        A part that cannot be transformed is drawn untransformed with the
        reason in the warning label - blanking the view explains nothing.
        """
        if not self._show_transformed or geometry is None:
            return geometry
        try:
            return part_transform.for_export(self.document, geometry)
        except part_transform.PartTransformError as exc:
            self.warning_label.setText(str(exc))
            self.warning_label.setStyleSheet("color: #c0453a;")
            return geometry

    def _display_geometry(self, geometry, mode: str) -> None:
        geometry = self._transformed(geometry)
        if self._display_parts(mode):
            return
        if mode == "solid":
            self.viewport.display_shape(RESULT_KEY, geometry, color=PART_COLOR)
        else:
            try:
                shape = self._mesh_display_shape(geometry)
            except Exception:
                return
            # A mesh is one triangulated face.  The aluminium material and
            # per-face boundary treatment used for B-rep solids makes every
            # triangle compete with the surface lighting, producing a washed
            # out, faceted result.  Keep mesh shading neutral and let region
            # highlighting provide the interaction feedback.
            self.viewport.display_shape(
                RESULT_KEY, shape, color=PART_COLOR, material=False, matte=True
            )

    def _display_parts(self, mode: str) -> bool:
        """Draw an assembly one part at a time, leaving out the hidden ones.

        One shape per part rather than one for the whole thing, because that is
        the only way a part can be hidden: what reaches the screen has to be
        divided the same way the file was.  Returns False when there is nothing
        to divide, so the caller draws the single shape it always did.
        """
        base = self.document.base
        result = self._last_result
        if base is None or len(base.parts) < 2 or result is None or not result.parts:
            self._clear_part_shapes()
            return False

        wanted: list[str] = []
        for piece in result.parts:
            part = next((p for p in base.parts if p.index == piece.index), None)
            if part is not None and not part.visible:
                continue
            if piece.geometry is None:
                continue
            key = f"{RESULT_KEY}:{piece.index}"
            try:
                piece_geometry = self._transformed(piece.geometry)
                shape = (
                    piece_geometry if mode == "solid"
                    else self._mesh_display_shape(piece_geometry)
                )
            except Exception:  # noqa: BLE001 - one bad part must not blank the view
                continue
            self.viewport.display_shape(
                key, shape, color=PART_COLOR,
                material=mode == "solid", matte=mode != "solid", update=False,
            )
            wanted.append(key)

        for key in self._part_shape_keys:
            if key not in wanted:
                self.viewport.erase(key, update=False)
        self._part_shape_keys = wanted
        # The single-shape view and the per-part view cannot both be up.
        self.viewport.erase(RESULT_KEY, update=False)
        if self.viewport.context:
            self.viewport.context.UpdateCurrentViewer()
        return True

    def _clear_part_shapes(self) -> None:
        for key in self._part_shape_keys:
            self.viewport.erase(key, update=False)
        self._part_shape_keys = []

    def set_part_visible(self, index: int, visible: bool) -> None:
        """Show or hide one part of an assembly."""
        base = self.document.base
        part = next((p for p in (base.parts if base else []) if p.index == index), None)
        if part is None or part.visible == visible:
            return
        part.visible = bool(visible)
        self._display_geometry(
            self._last_result.geometry if self._last_result else None,
            base.mode,
        )
        self._refresh_tree()

    def isolate_part(self, index: int) -> None:
        """Show one part alone, or -1 to show them all again."""
        base = self.document.base
        if base is None or not base.parts:
            return
        for part in base.parts:
            part.visible = index < 0 or part.index == index
        self._display_geometry(
            self._last_result.geometry if self._last_result else None, base.mode
        )
        self._refresh_tree()
        if index >= 0:
            named = next((p for p in base.parts if p.index == index), None)
            if named is not None:
                self.statusBar().showMessage(
                    f"Showing {named.name} on its own. Right-click a part to show them all."
                )

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
        for key in self._component_footprint_keys:
            self.viewport.erase(key, update=False)
        self._component_footprint_keys.clear()
        self._show_inspection_overlay(update=False)

        feature = self.selected_feature
        if not self._preview_on or feature is None or self._last_result is None:
            self.viewport.context and self.viewport.context.UpdateCurrentViewer()
            return
        result = self._last_result.result_for(feature.id)
        if result is None or result.tool is None:
            self.viewport.context and self.viewport.context.UpdateCurrentViewer()
            return

        color = {
            OperationKind.ADD: ADD_COLOR,
            OperationKind.COLOR: STAMP_COLOR,
        }.get(feature.operation.kind, CUT_COLOR)
        self.viewport.display_shape(
            PREVIEW_KEY, result.tool.shape, color=color, transparency=0.65,
            material=False, selectable=False, update=False,
        )
        if not self._show_component_footprints(feature, result):
            self.viewport.display_shape(
                FOOTPRINT_KEY, result.tool.footprint, color=color, transparency=0.25,
                material=False, selectable=False, update=False,
            )
        if self.viewport.context:
            self.viewport.context.UpdateCurrentViewer()
        if feature.placement.mode.value == "wrap":
            self.statusBar().showMessage(
                "Curved-face proof: the translucent stamp follows the selected cylindrical or conical face.",
                5000,
            )

    def _show_component_footprints(self, feature: Feature, result) -> bool:
        """Draw the decal in the colours it will print in, one shape per part.

        A colour stamp is only a layer or two deep, so the translucent solid
        above it is nearly flat against the face and says very little about
        where the artwork actually is.  In its own colours it says it at a
        glance - and they are the colours the export will use, because both come
        from :func:`effective_colors`.

        Returns False when there is nothing to divide, so the caller draws the
        single-colour decal it always did.
        """
        profile = self._profile_of(feature)
        if profile is None or not divides_by_color(profile, feature):
            return False
        try:
            footprints = component_footprints(profile, feature.placement, result.tool)
        except Exception:  # noqa: BLE001 - a preview must never stop a rebuild
            return False
        if len(footprints) < 2:
            return False

        wanted = effective_colors(profile, feature)
        drawn = 0
        for key, shape in footprints.items():
            rgb = _rgb(wanted.get(key, ""))
            if rgb is None:
                continue
            self.viewport.display_shape(
                f"{FOOTPRINT_KEY}:{key}", shape, color=rgb, transparency=0.15,
                material=False, selectable=False, update=False,
            )
            self._component_footprint_keys.append(f"{FOOTPRINT_KEY}:{key}")
            drawn += 1
        return drawn >= 2

    def set_inspection_visible(self, on: bool) -> None:
        """Toggle the selected tool's manufacturing measurement overlay."""
        self._show_inspection_overlay(update=True)
        if on:
            self.statusBar().showMessage(
                "Inspection overlay shows the rebuilt envelope and nearest face boundary clearance."
            )
        else:
            self._refresh_status()

    def _show_inspection_overlay(self, *, update: bool) -> None:
        """Draw the real tool envelope and an exact face-boundary clearance line."""
        self.viewport.erase(DIMENSIONS_KEY, update=False)
        self.viewport.erase(CLEARANCE_KEY, update=False)
        feature = self.selected_feature
        result = self._last_result.result_for(feature.id) if feature and self._last_result else None
        if not self.action_inspection.isChecked() or feature is None or result is None or result.tool is None:
            if update and self.viewport.context:
                self.viewport.context.UpdateCurrentViewer()
            return
        try:
            from OCP.Bnd import Bnd_Box
            from OCP.BRepBndLib import BRepBndLib
            from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon
            from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
            from OCP.gp import gp_Pnt

            box = Bnd_Box()
            BRepBndLib.Add_s(result.tool.shape, box)
            if not box.IsVoid():
                x0, y0, z0, x1, y1, z1 = box.Get()
                envelope = BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), gp_Pnt(x1, y1, z1)).Shape()
                self.viewport.display_shape(
                    DIMENSIONS_KEY, envelope, color=DIMENSIONS_COLOR, transparency=0.88,
                    material=False, selectable=False, update=False,
                )
                dimensions = feature_dimensions(result.tool.shape)
                if dimensions is not None:
                    self.status_label.setText(
                        f"Inspect: {dimensions.width_mm:.2f} × {dimensions.height_mm:.2f} × {dimensions.depth_mm:.2f} mm"
                    )
            measurement = anchor_clearance_measurement(self.document, feature)
            if measurement is not None:
                line = BRepBuilderAPI_MakePolygon()
                line.Add(gp_Pnt(*measurement.origin))
                line.Add(gp_Pnt(*measurement.boundary))
                if line.IsDone():
                    color = (CLEARANCE_WARN_COLOR if measurement.distance_mm < settings_for(self.document, feature).min_clearance_mm
                             else CLEARANCE_OK_COLOR)
                    self.viewport.display_shape(
                        CLEARANCE_KEY, line.Wire(), color=color, material=False,
                        selectable=False, update=False,
                    )
        except Exception:
            # Measurements are guidance; a display failure must never block a rebuild.
            pass
        if update and self.viewport.context:
            self.viewport.context.UpdateCurrentViewer()

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
        source = Path(document.base.source_path)
        try:
            reloaded = import_part_for_ui(self, source, draft=self._draft_display)
        except ImportCancelled:
            self.statusBar().showMessage(f"Opening {path.name} was stopped.", 5000)
            return
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
        self._fit_after_display = True
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

    # -------------------------------------------------- part mirror and scale

    def toggle_part_mirror(self, plane: MirrorPlane) -> None:
        """Turn the mirror on across *plane*, or off if it is already that one."""
        if self.document.base is None:
            return
        current = self.document.transform.mirror
        wanted = MirrorPlane.NONE if current is plane else plane
        self._push_undo("part mirror")
        self.document.transform = replace(self.document.transform, mirror=wanted)
        self._after_transform_changed()

    def scale_part_dialog(self) -> None:
        """Type a finished size or a percentage for the whole part."""
        if self.document.base is None:
            return
        dialog = dialogs.PartScaleDialog(
            self.document.base.size, self.document.transform, units=self.document.units,
            parent=self,
        )
        if not self._ask(dialog):
            return
        transform = dialog.transform()
        problems = transform.validate()
        if problems:
            self._notify("That scale will not work", "\n".join(problems))
            return
        self._push_undo("part scale")
        self.document.transform = transform
        self._after_transform_changed()

    def reset_part_transform(self) -> None:
        if self.document.base is None or self.document.transform.is_identity:
            return
        self._push_undo("part transform")
        self.document.transform = PartTransform()
        self._after_transform_changed()

    def set_show_transformed(self, on: bool) -> None:
        """Draw the part the way it will be exported, read-only."""
        self._show_transformed = bool(on)
        if self._last_result is not None and self._last_result.geometry is not None:
            self._display_geometry(self._last_result.geometry, self._last_result.mode)
        self._refresh_status()

    def _after_transform_changed(self) -> None:
        self._refresh_transform_actions()
        self._refresh_properties()
        if self._last_result is not None and self._last_result.geometry is not None:
            self._display_geometry(self._last_result.geometry, self._last_result.mode)
        self._refresh_status()

    def _refresh_transform_actions(self) -> None:
        mirror = self.document.transform.mirror
        has_part = self.document.base is not None
        for action, plane in (
            (self.action_mirror_yz, MirrorPlane.YZ),
            (self.action_mirror_xz, MirrorPlane.XZ),
            (self.action_mirror_xy, MirrorPlane.XY),
        ):
            action.setChecked(mirror is plane)
            action.setEnabled(has_part)
        self.action_scale_part.setEnabled(has_part)
        self.action_reset_transform.setEnabled(
            has_part and not self.document.transform.is_identity
        )
        self.action_show_transformed.setEnabled(has_part)

    # ----------------------------------------------------------------- exports

    def _geometry(self):
        """The rebuilt part, in its own untransformed space."""
        if self._last_result is not None and self._last_result.geometry is not None:
            return self._last_result.geometry
        return self.document.base.runtime if self.document.base else None

    def _export_geometry(self):
        """What an export writes: the rebuilt part with the part transform applied.

        Returns ``None`` and says why when the transform cannot be applied, so an
        export command can simply stop.
        """
        geometry = self._geometry()
        try:
            return part_transform.for_export(self.document, geometry)
        except part_transform.PartTransformError as exc:
            self._notify("Stamp could not apply the part transform", str(exc))
            return None

    def _export_suffix(self) -> str:
        return self.document.transform.suffix()

    def export_step(self) -> None:
        if self.document.base is None:
            return
        if self.document.base.mode != "solid":
            self._notify("There is no STEP to write", export_io.MESH_MODE_NO_STEP)
            return
        geometry = self._export_geometry()
        if geometry is None:
            return
        options = dialogs.StepExportDialog(self)
        if not self._ask(options):
            return
        schema = options.schema_name()
        merge = options.merge_faces()

        suggested = export_io.default_filename(
            self.document.name, "step", suffix=self._export_suffix()
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Export STEP", str(Path(self._last_dir("export")) / suggested),
            "STEP files (*.step *.stp)",
        )
        if not path:
            return
        preflight = export_io.preflight_export(self.document, self._last_result, "step", path)
        if not preflight.ok:
            self._notify("STEP export needs attention", "\n".join(preflight.errors))
            return
        if preflight.warnings and not self._confirm(
            "Export warnings", "\n\n".join(preflight.warnings) + "\n\nContinue?"
        ):
            return
        try:
            result = export_io.export_step(
                geometry, path, schema=schema, simplify=merge
            )
        except export_io.ExportError as exc:
            if not self._confirm("Export anyway?", f"{exc}\n\nWrite the file anyway?"):
                return
            result = export_io.export_step(
                geometry, path, schema=schema, simplify=merge, allow_invalid=True
            )
        self._remember_dir("export", Path(path))
        result.warnings.extend(preflight.warnings)
        self._report_export(result)

    def export_stl(self) -> None:
        if self.document.base is None:
            return
        mode = self.document.base.mode
        geometry = self._export_geometry()
        if geometry is None:
            return

        def counter(deflection: float) -> int:
            return export_io.triangle_count_for(geometry, mode, deflection)

        dialog = dialogs.StlExportDialog(counter, mode=mode, parent=self)
        if not self._ask(dialog):
            return

        suggested = export_io.default_filename(
            self.document.name, "stl", suffix=self._export_suffix()
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Export STL", str(Path(self._last_dir("export")) / suggested),
            "STL files (*.stl)",
        )
        if not path:
            return
        preflight = export_io.preflight_export(self.document, self._last_result, "stl", path)
        if not preflight.ok:
            self._notify("STL export needs attention", "\n".join(preflight.errors))
            return
        if preflight.warnings and not self._confirm(
            "Export warnings", "\n\n".join(preflight.warnings) + "\n\nContinue?"
        ):
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
        result.warnings.extend(preflight.warnings)
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
        stamp_count = sum(
            1 for f in self.document.features
            if f.enabled and f.operation.kind is OperationKind.COLOR
        )
        dialog = dialogs.Color3mfDialog(
            feature_count,
            stamp_count=stamp_count,
            mode=self.document.base.mode,
            base_color=self.settings.value("3mf/base_color", type=str) or None,
            feature_color=self.settings.value("3mf/feature_color", type=str) or None,
            write_colors=self.settings.value("3mf/write_colors", True, type=bool),
            parts=self.document.base.parts,
            features_on=[f.part_index for f in self.document.features if f.enabled],
            parent=self,
        )
        if not self._ask(dialog):
            return
        self.settings.setValue("3mf/base_color", dialog.base_color())
        self.settings.setValue("3mf/feature_color", dialog.feature_color())
        self.settings.setValue("3mf/write_colors", dialog.write_colors())

        suggested = export_io.default_filename(
            self.document.name, "3mf", suffix=self._export_suffix()
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Export 3MF", str(Path(self._last_dir("export")) / suggested),
            "3MF files (*.3mf)",
        )
        if not path:
            return
        preflight = export_io.preflight_export(self.document, self._last_result, "3mf", path)
        if not preflight.ok:
            self._notify("3MF export needs attention", "\n".join(preflight.errors))
            return
        if preflight.warnings and not self._confirm(
            "Export warnings", "\n\n".join(preflight.warnings) + "\n\nContinue?"
        ):
            return
        try:
            split = color_split.split_for_color(
                self.document, self._last_result, deflection=dialog.deflection_mm(),
                part_index=dialog.part_index(),
            )
            bodies = part_transform.transform_bodies(self.document, split.bodies)
            result = export_io.export_3mf(
                bodies, path,
                base_color=dialog.base_color(),
                feature_color=dialog.feature_color(),
                write_colors=dialog.write_colors(),
            )
        except (
            color_split.ColorSplitError,
            export_io.ExportError,
            part_transform.PartTransformError,
        ) as exc:
            self._notify("Stamp could not write the 3MF", str(exc))
            return
        result.warnings.extend(split.warnings)
        result.warnings.extend(preflight.warnings)
        self._remember_dir("export", Path(path))
        self._report_export(result)

    def export_for_quote(self) -> None:
        if self.document.base is None:
            return
        fmt = "step" if self.document.base.mode == "solid" else "stl"
        preflight = export_io.preflight_export(self.document, self._last_result, fmt)
        if not preflight.ok:
            self._notify("Quote export needs attention", "\n".join(preflight.errors))
            return
        if preflight.warnings and not self._confirm(
            "Export warnings", "\n\n".join(preflight.warnings) + "\n\nContinue?"
        ):
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Choose a folder for the quote files", self._last_dir("export")
        )
        if not folder:
            return
        geometry = self._export_geometry()
        if geometry is None:
            return
        try:
            written = export_io.export_for_quote(
                geometry, folder, self.document.name,
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
        if preflight.warnings:
            self._notify("Note about this export", "\n\n".join(preflight.warnings))
        names = "\n".join(f"  {r.path.name}  ({r.size_text})" for r in written)
        if self.interactive:
            dialogs.inform(self, "Quote files written", f"Written to {folder}:\n\n{names}")

    def export_proof_sheet(self) -> None:
        """Write the compact, reviewable production sheet without exporting geometry."""
        if self.document.base is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export production proof",
            str(Path(self._last_dir("export")) / f"{self.document.name}-proof.pdf"),
            "PDF files (*.pdf)",
        )
        if not path:
            return
        try:
            result = export_io.export_proof_sheet(
                self.document, path, screenshot=self._thumbnail(), rebuild=self._last_result
            )
        except export_io.ExportError as exc:
            self._notify("Stamp could not write the production proof", str(exc))
            return
        self._remember_dir("export", Path(path))
        self._report_export(result)

    def export_job_package(self) -> None:
        if self.document.base is None:
            return
        fmt = "step" if self.document.base.mode == "solid" else "stl"
        preflight = export_io.preflight_export(self.document, self._last_result, fmt)
        if not preflight.ok:
            self._notify("Job package needs attention", "\n".join(preflight.errors))
            return
        if preflight.warnings and not self._confirm(
            "Package warnings", "\n\n".join(preflight.warnings) + "\n\nContinue?"
        ):
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export job package", str(Path(self._last_dir("export")) / f"{self.document.name}.zip"),
            "ZIP packages (*.zip)"
        )
        if not path:
            return
        geometry = self._export_geometry()
        if geometry is None:
            return
        try:
            result = export_io.export_job_package(
                self.document, geometry, path, fmt=fmt,
                screenshot=self._thumbnail(), rebuild=self._last_result,
            )
        except Exception as exc:
            self._notify("Stamp could not write the job package", str(exc))
            return
        self._remember_dir("export", Path(path))
        self._report_export(result)

    def batch_stamp(self) -> None:
        """Small guided front end for the same runner exposed as ``stamp batch``."""
        template, _ = QFileDialog.getOpenFileName(
            self, "Choose Stamp template", self._last_dir("project"), "Stamp projects (*.stamp)"
        )
        if not template:
            return
        csv_path, _ = QFileDialog.getOpenFileName(
            self, "Choose batch CSV", self._last_dir("part"), "CSV files (*.csv)"
        )
        if not csv_path:
            return
        fmt, accepted = QInputDialog.getItem(self, "Batch format", "Export format", ["step", "stl", "3mf"], 0, False)
        if not accepted:
            return
        try:
            simulation = simulate_batch(template, csv_path, fmt)
        except BatchError as exc:
            self._notify("Batch simulation could not start", str(exc))
            return
        failed = [row for row in simulation.rows if row.status == "failed"]
        if failed:
            first = failed[0]
            self._notify("Batch simulation found a problem", f"Row {first.index}: {first.detail}")
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Choose batch output folder", self._last_dir("export")
        )
        if not folder:
            return
        if not self._confirm(
            "Batch simulation", f"{len(simulation.rows)} row(s) are ready. Start the batch?"
        ):
            return
        try:
            report = run_batch(template, csv_path, folder, fmt)
        except BatchError as exc:
            self._notify("Batch could not start", str(exc))
            return
        self._remember_dir("export", Path(folder))
        if report.stopped:
            failed = report.rows[-1]
            self._notify("Batch stopped", f"Row {failed.index}: {failed.detail}\n\nA report was written to {folder}.")
        else:
            self._notify("Batch complete", f"Exported {len(report.rows)} part(s).\n\nA report was written to {folder}.")

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
        if self._dirty and self.document.base is not None and not self._installing_update:
            if not self._confirm(
                "Close without saving?",
                "This project has changes that are not saved. Close it anyway?",
            ):
                event.ignore()
                return
        # After the save question and before anything is torn down: the user
        # agreed to close, and this is the moment nobody is waiting on Stamp.
        if self._install_on_quit and not self._installing_update:
            self._apply_update(now=False)
        self.updater.shutdown()
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
