"""The properties panel - spec §6.2, §6.3, §7 right panel.

Numbers are authoritative.  Every drag in the viewport has a field here, the fields
are always visible, and editing one is the way anything exact happens.  Editing W or
H with the lock on updates the other and the scale, which is how "make the logo
exactly 40 mm wide" works - the single most-used control in the app.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from stamp.core.document import (
    BasePart,
    DepthMode,
    Direction,
    Document,
    EdgeRole,
    Feature,
    Modifier,
    ModifierKind,
    OperationKind,
    TextAlign,
    TextSpec,
)
from stamp.units import format_length


class NumberField(QDoubleSpinBox):
    """A length or angle field.  Values are held in millimetres or degrees."""

    def __init__(self, *, suffix: str = " mm", decimals: int = 2,
                 minimum: float = -1e6, maximum: float = 1e6, step: float = 0.1):
        super().__init__()
        self.setSuffix(suffix)
        self.setDecimals(decimals)
        self.setRange(minimum, maximum)
        self.setSingleStep(step)
        self.setKeyboardTracking(False)
        self.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.UpDownArrows)
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.setMinimumWidth(88)

    def set_silently(self, value: float) -> None:
        blocked = self.blockSignals(True)
        self.setValue(value)
        self.blockSignals(blocked)


class PropertiesPanel(QScrollArea):
    """Contextual to the tree selection.  Empty state shows base part info (§7)."""

    #: Emitted after any edit.  The caller pushes undo and requests a rebuild.
    changed = Signal(str)                 # a short label for the undo stack
    center_on_face_requested = Signal()
    fit_to_face_requested = Signal()
    add_modifier_requested = Signal(str)  # "fillet" | "chamfer"
    pick_to_face_requested = Signal()
    repick_face_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        # Wide enough for the font list and a value beside its caption.
        self.setMinimumWidth(330)

        self._document: Document | None = None
        self._feature: Feature | None = None
        self._suggestions: dict[str, float] = {}
        self._native_size = (1.0, 1.0)
        self._updating = False

        self._body = QWidget()
        self._layout = QVBoxLayout(self._body)
        self._layout.setContentsMargins(10, 10, 10, 10)
        self._layout.setSpacing(10)
        self.setWidget(self._body)

        self._empty = self._build_empty()
        self._text = self._build_text()
        self._placement = self._build_placement()
        self._operation = self._build_operation()
        self._modifiers = self._build_modifiers()

        for widget in (
            self._empty, self._text, self._placement, self._operation, self._modifiers
        ):
            self._layout.addWidget(widget)
        self._layout.addStretch(1)
        self.show_base(None)

    # ------------------------------------------------------------------ layout

    def _build_empty(self) -> QWidget:
        box = QGroupBox("Part")
        layout = QFormLayout(box)
        layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._info_labels: dict[str, QLabel] = {}
        for key, caption in (
            ("file", "File"),
            ("mode", "Kind"),
            ("size", "Bounding box"),
            ("volume", "Volume"),
            ("count", "Faces"),
            ("watertight", "Watertight"),
        ):
            label = QLabel("-")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._info_labels[key] = label
            layout.addRow(f"{caption}:", label)
        self._info_warnings = QLabel("")
        self._info_warnings.setWordWrap(True)
        self._info_warnings.setStyleSheet("color: #c58a2a;")
        layout.addRow(self._info_warnings)
        return box

    def _build_text(self) -> QWidget:
        """The message and its formatting, for a text feature only."""
        box = QGroupBox("Text")
        layout = QVBoxLayout(box)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("Type the message")
        self.text_edit.setFixedHeight(64)
        self.text_edit.setTabChangesFocus(True)
        self.text_edit.textChanged.connect(self._on_text_edited)
        layout.addWidget(self.text_edit)

        top = QFormLayout()
        top.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.text_family = QFontComboBox()
        self.text_family.currentFontChanged.connect(
            lambda f: self._set_text("family", f.family(), "font")
        )
        top.addRow("Font:", self.text_family)

        self.text_size = NumberField(minimum=0.1, step=0.5)
        self.text_size.valueChanged.connect(
            lambda v: self._set_text("size_mm", v, "text size")
        )
        top.addRow("Size:", self.text_size)
        layout.addLayout(top)

        style = QHBoxLayout()
        self.text_bold = QCheckBox("Bold")
        self.text_italic = QCheckBox("Italic")
        self.text_underline = QCheckBox("Underline")
        self.text_bold.toggled.connect(lambda on: self._set_text("bold", on, "bold"))
        self.text_italic.toggled.connect(lambda on: self._set_text("italic", on, "italic"))
        self.text_underline.toggled.connect(
            lambda on: self._set_text("underline", on, "underline")
        )
        for widget in (self.text_bold, self.text_italic, self.text_underline):
            style.addWidget(widget)
        style.addStretch(1)
        layout.addLayout(style)

        bottom = QFormLayout()
        bottom.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.text_align = QComboBox()
        for role, caption in (
            (TextAlign.LEFT, "Left"),
            (TextAlign.CENTER, "Center"),
            (TextAlign.RIGHT, "Right"),
            (TextAlign.JUSTIFY, "Justify"),
        ):
            self.text_align.addItem(caption, role)
        self.text_align.currentIndexChanged.connect(
            lambda: self._set_text(
                "align", TextAlign(self.text_align.currentData()), "alignment"
            )
        )
        bottom.addRow("Align:", self.text_align)

        wrap_row = QHBoxLayout()
        self.text_wrap_on = QCheckBox("Wrap at")
        self.text_wrap = NumberField(minimum=1.0, step=1.0)
        self.text_wrap_on.toggled.connect(self._on_wrap_toggled)
        self.text_wrap.valueChanged.connect(self._on_wrap_width)
        wrap_row.addWidget(self.text_wrap_on)
        wrap_row.addWidget(self.text_wrap)
        bottom.addRow("", wrap_row)

        self.text_letter = NumberField(suffix=" em", decimals=3, minimum=-0.5,
                                       maximum=2.0, step=0.01)
        self.text_letter.valueChanged.connect(
            lambda v: self._set_text("letter_spacing", v, "letter spacing")
        )
        bottom.addRow("Letter spacing:", self.text_letter)

        self.text_line = NumberField(suffix=" x", decimals=2, minimum=0.5,
                                     maximum=5.0, step=0.05)
        self.text_line.valueChanged.connect(
            lambda v: self._set_text("line_spacing", v, "line spacing")
        )
        bottom.addRow("Line spacing:", self.text_line)
        layout.addLayout(bottom)

        note = QLabel("Justify needs a wrap width.")
        note.setStyleSheet("color: #8a8f98;")
        note.setWordWrap(True)
        layout.addWidget(note)
        return box

    # --------------------------------------------------------------- text edits

    @property
    def _spec(self) -> TextSpec | None:
        if self._feature is None:
            return None
        return self._feature.profile.text

    def _set_text(self, field: str, value, label: str) -> None:
        spec = self._spec
        if self._updating or spec is None:
            return
        if getattr(spec, field) == value:
            return
        setattr(spec, field, value)
        self.changed.emit(label)

    def _on_text_edited(self) -> None:
        self._set_text("text", self.text_edit.toPlainText(), "message")

    def _on_wrap_toggled(self, on: bool) -> None:
        self.text_wrap.setEnabled(on)
        if self._updating or self._spec is None:
            return
        self._set_text("wrap_mm", self.text_wrap.value() if on else None, "wrap")

    def _on_wrap_width(self, value: float) -> None:
        if self.text_wrap_on.isChecked():
            self._set_text("wrap_mm", value, "wrap")

    def _fill_text(self, feature: Feature) -> None:
        spec = feature.profile.text
        if spec is None:
            return
        from PySide6.QtGui import QFont

        self._updating = True
        if self.text_edit.toPlainText() != spec.text:
            self.text_edit.setPlainText(spec.text)
        self.text_family.setCurrentFont(QFont(spec.family))
        self.text_size.set_silently(spec.size_mm)
        self.text_bold.setChecked(spec.bold)
        self.text_italic.setChecked(spec.italic)
        self.text_underline.setChecked(spec.underline)
        self.text_align.setCurrentIndex(self.text_align.findData(spec.align))
        self.text_wrap_on.setChecked(spec.wrap_mm is not None)
        self.text_wrap.setEnabled(spec.wrap_mm is not None)
        self.text_wrap.set_silently(spec.wrap_mm or 50.0)
        self.text_letter.set_silently(spec.letter_spacing)
        self.text_line.set_silently(spec.line_spacing)
        self._updating = False


    def _build_placement(self) -> QWidget:
        box = QGroupBox("Placement")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)

        self.u_field = NumberField()
        self.v_field = NumberField()
        self.rotation_field = NumberField(suffix="°", decimals=2, minimum=-360, maximum=360, step=1)
        self.width_field = NumberField(minimum=0.001, step=0.5)
        self.height_field = NumberField(minimum=0.001, step=0.5)
        self.scale_field = NumberField(suffix=" %", decimals=2, minimum=0.01, maximum=100000, step=1)
        self.lift_field = NumberField()

        self.lock_button = QToolButton()
        self.lock_button.setCheckable(True)
        self.lock_button.setChecked(True)
        self.lock_button.setText("🔒")
        self.lock_button.setToolTip("Keep the width and height in proportion")

        row = 0
        grid.addWidget(QLabel("U"), row, 0)
        grid.addWidget(self.u_field, row, 1)
        grid.addWidget(QLabel("V"), row, 2)
        grid.addWidget(self.v_field, row, 3)
        row += 1
        center = QPushButton("Center on face")
        center.clicked.connect(self.center_on_face_requested)
        grid.addWidget(center, row, 0, 1, 4)
        row += 1
        grid.addWidget(QLabel("Rotation"), row, 0)
        grid.addWidget(self.rotation_field, row, 1)
        turn_left = QPushButton("↺ 90°")
        turn_right = QPushButton("↻ 90°")
        turn_left.clicked.connect(lambda: self._turn(-90.0))
        turn_right.clicked.connect(lambda: self._turn(90.0))
        grid.addWidget(turn_left, row, 2)
        grid.addWidget(turn_right, row, 3)
        row += 1
        grid.addWidget(QLabel("W"), row, 0)
        grid.addWidget(self.width_field, row, 1)
        grid.addWidget(QLabel("H"), row, 2)
        grid.addWidget(self.height_field, row, 3)
        row += 1
        grid.addWidget(self.lock_button, row, 0)
        fit = QPushButton("Fit to face")
        fit.clicked.connect(self.fit_to_face_requested)
        grid.addWidget(fit, row, 1, 1, 3)
        row += 1
        grid.addWidget(QLabel("Scale"), row, 0)
        grid.addWidget(self.scale_field, row, 1)
        reset = QPushButton("Reset")
        reset.clicked.connect(self._reset_scale)
        grid.addWidget(reset, row, 2, 1, 2)
        row += 1
        grid.addWidget(QLabel("Lift"), row, 0)
        grid.addWidget(self.lift_field, row, 1)
        row += 1
        self.mirror_u = QCheckBox("Mirror horizontal")
        self.mirror_v = QCheckBox("Mirror vertical")
        grid.addWidget(self.mirror_u, row, 0, 1, 2)
        grid.addWidget(self.mirror_v, row, 2, 1, 2)
        row += 1
        self.repick = QPushButton("Pick the face again")
        self.repick.clicked.connect(self.repick_face_requested)
        grid.addWidget(self.repick, row, 0, 1, 4)

        self.u_field.valueChanged.connect(lambda v: self._set_offset(0, v))
        self.v_field.valueChanged.connect(lambda v: self._set_offset(1, v))
        self.rotation_field.valueChanged.connect(self._set_rotation)
        self.width_field.valueChanged.connect(lambda v: self._set_size(0, v))
        self.height_field.valueChanged.connect(lambda v: self._set_size(1, v))
        self.scale_field.valueChanged.connect(self._set_scale_percent)
        self.lift_field.valueChanged.connect(self._set_lift)
        self.mirror_u.toggled.connect(lambda on: self._set_mirror("u", on))
        self.mirror_v.toggled.connect(lambda on: self._set_mirror("v", on))
        self.lock_button.toggled.connect(self._set_lock)
        return box

    def _build_operation(self) -> QWidget:
        box = QGroupBox("Operation")
        layout = QVBoxLayout(box)

        kind_row = QHBoxLayout()
        self.add_radio = QRadioButton("Add material")
        self.cut_radio = QRadioButton("Cut material")
        self._kind_group = QButtonGroup(box)
        self._kind_group.addButton(self.add_radio)
        self._kind_group.addButton(self.cut_radio)
        kind_row.addWidget(self.add_radio)
        kind_row.addWidget(self.cut_radio)
        layout.addLayout(kind_row)

        self.depth_mode = QComboBox()
        for mode, caption in (
            (DepthMode.BLIND, "Blind"),
            (DepthMode.THROUGH_ALL, "Through all"),
            (DepthMode.TO_FACE, "To face"),
            (DepthMode.SYMMETRIC, "Symmetric"),
        ):
            self.depth_mode.addItem(caption, mode)

        self.depth_field = NumberField(minimum=0.001, step=0.1)
        self.draft_field = NumberField(suffix="°", minimum=-89, maximum=89, step=0.5)
        self.direction = QComboBox()
        self.direction.addItem("Into the part", Direction.INTO)
        self.direction.addItem("Out of the part", Direction.OUT_OF)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow("Depth:", self.depth_mode)
        form.addRow("", self.depth_field)
        self.pick_face_button = QPushButton("Pick the target face")
        self.pick_face_button.clicked.connect(self.pick_to_face_requested)
        form.addRow("", self.pick_face_button)
        form.addRow("Direction:", self.direction)
        form.addRow("Draft:", self.draft_field)
        layout.addLayout(form)

        hint = QLabel("A positive draft angle makes the walls flare outward toward the opening.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8a8f98;")
        layout.addWidget(hint)

        self.add_radio.toggled.connect(self._set_kind)
        self.depth_mode.currentIndexChanged.connect(self._set_depth_mode)
        self.depth_field.valueChanged.connect(self._set_depth)
        self.direction.currentIndexChanged.connect(self._set_direction)
        self.draft_field.valueChanged.connect(self._set_draft)
        return box

    def _build_modifiers(self) -> QWidget:
        box = QGroupBox("Edges")
        layout = QVBoxLayout(box)
        row = QHBoxLayout()
        fillet = QPushButton("+ Fillet")
        chamfer = QPushButton("+ Chamfer")
        fillet.clicked.connect(lambda: self.add_modifier_requested.emit("fillet"))
        chamfer.clicked.connect(lambda: self.add_modifier_requested.emit("chamfer"))
        row.addWidget(fillet)
        row.addWidget(chamfer)
        layout.addLayout(row)

        self._modifier_area = QWidget()
        self._modifier_layout = QVBoxLayout(self._modifier_area)
        self._modifier_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._modifier_area)

        self.blend_note = QLabel("")
        self.blend_note.setWordWrap(True)
        self.blend_note.setStyleSheet("color: #8a8f98;")
        layout.addWidget(self.blend_note)
        return box

    # ------------------------------------------------------------------- state

    def show_base(self, part: BasePart | None, units: str = "mm") -> None:
        self._feature = None
        self._empty.setVisible(True)
        for widget in (self._text, self._placement, self._operation, self._modifiers):
            widget.setVisible(False)

        if part is None:
            for label in self._info_labels.values():
                label.setText("-")
            self._info_labels["file"].setText("Open a part to begin.")
            self._info_warnings.setText("")
            return

        from pathlib import Path

        dx, dy, dz = part.size
        self._info_labels["file"].setText(Path(part.source_path).name or "-")
        self._info_labels["mode"].setText(
            "Solid (exact surfaces)" if part.mode == "solid" else "Mesh (triangles)"
        )
        self._info_labels["size"].setText(
            f"{format_length(dx, units)} × {format_length(dy, units)} × "
            f"{format_length(dz, units)} {units}"
        )
        self._info_labels["volume"].setText(f"{part.volume / 1000.0:.2f} cm³")
        if part.mode == "solid":
            self._info_labels["count"].setText(f"{part.face_count} faces")
        else:
            self._info_labels["count"].setText(f"{part.triangle_count:,} triangles")
        self._info_labels["watertight"].setText("Yes" if part.watertight else "No")
        self._info_warnings.setText("\n".join(part.warnings))

    def show_feature(
        self,
        document: Document,
        feature: Feature,
        native_size: tuple[float, float],
        *,
        mesh_mode: bool = False,
        suggestions: dict[str, float] | None = None,
    ) -> None:
        self._document = document
        self._feature = feature
        self._suggestions = dict(suggestions or {})
        self._native_size = (max(native_size[0], 1e-9), max(native_size[1], 1e-9))

        self._empty.setVisible(False)
        for widget in (self._placement, self._operation, self._modifiers):
            widget.setVisible(True)
        self._text.setVisible(feature.profile.is_text)
        if feature.profile.is_text:
            self._fill_text(feature)

        self._updating = True
        placement = feature.placement
        self.u_field.set_silently(placement.offset_2d[0])
        self.v_field.set_silently(placement.offset_2d[1])
        self.rotation_field.set_silently(placement.rotation)
        self.width_field.set_silently(self._native_size[0] * abs(placement.scale[0]))
        self.height_field.set_silently(self._native_size[1] * abs(placement.scale[1]))
        self.scale_field.set_silently(abs(placement.scale[0]) * 100.0)
        self.lift_field.set_silently(placement.lift)
        self.mirror_u.setChecked(placement.mirror_u)
        self.mirror_v.setChecked(placement.mirror_v)
        self.lock_button.setChecked(placement.uniform_scale)

        operation = feature.operation
        self.add_radio.setChecked(operation.kind is OperationKind.ADD)
        self.cut_radio.setChecked(operation.kind is OperationKind.CUT)
        self.depth_mode.setCurrentIndex(self.depth_mode.findData(operation.depth_mode))
        self.depth_field.set_silently(operation.depth)
        self.direction.setCurrentIndex(self.direction.findData(operation.direction))
        self.draft_field.set_silently(operation.draft_angle)
        self._updating = False

        self._sync_depth_widgets()
        self._fill_modifiers(feature, mesh_mode=mesh_mode)

    def _sync_depth_widgets(self) -> None:
        mode = DepthMode(self.depth_mode.currentData())
        self.depth_field.setVisible(mode in (DepthMode.BLIND, DepthMode.SYMMETRIC))
        self.pick_face_button.setVisible(mode is DepthMode.TO_FACE)

    def _fill_modifiers(self, feature: Feature, *, mesh_mode: bool) -> None:
        while self._modifier_layout.count():
            item = self._modifier_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        cutting = feature.operation.kind is OperationKind.CUT
        for modifier in feature.modifiers:
            self._modifier_layout.addWidget(
                self._modifier_row(
                    modifier,
                    mesh_mode=mesh_mode,
                    cutting=cutting,
                    suggested=self._suggestions.get(modifier.id),
                )
            )

        if mesh_mode:
            from stamp.geom.mesh_ops import BLEND_NOT_AVAILABLE

            self.blend_note.setText(BLEND_NOT_AVAILABLE)
        else:
            self.blend_note.setText("")

    def _modifier_row(
        self,
        modifier: Modifier,
        *,
        mesh_mode: bool,
        cutting: bool = False,
        suggested: float | None = None,
    ) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)

        enabled = QCheckBox()
        enabled.setChecked(modifier.enabled)
        enabled.toggled.connect(lambda on, m=modifier: self._set_modifier_enabled(m, on))
        layout.addWidget(enabled)

        label = QLabel("Fillet" if modifier.kind is ModifierKind.FILLET else "Chamfer")
        label.setMinimumWidth(52)
        layout.addWidget(label)

        target = QComboBox()
        # The roles are named along the sweep of the tool, thus for a cut the
        # "top" of the tool is the floor of the pocket.  Say which is which.
        if cutting:
            far, near = "Floor edges (deepest)", "Edges at the face"
        else:
            far, near = "Top edges (highest)", "Edges at the face"
        options = [
            (EdgeRole.TOP, far),
            (EdgeRole.BOTTOM, near),
            (EdgeRole.SIDE, "Side edges"),
            (EdgeRole.ALL, "All feature edges"),
            (EdgeRole.MANUAL, "Picked edges"),
            (EdgeRole.BLEND, "Blend into the part"),
        ]
        for role, caption in options:
            target.addItem(caption, role)
            if role is EdgeRole.BLEND and mesh_mode:
                index = target.count() - 1
                target.model().item(index).setEnabled(False)
        target.setCurrentIndex(target.findData(modifier.target.role))
        target.currentIndexChanged.connect(
            lambda _i, m=modifier, box=target: self._set_modifier_role(
                m, EdgeRole(box.currentData())
            )
        )
        target.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(target)

        value = NumberField(minimum=0.0, decimals=3, step=0.05)
        value.set_silently(modifier.value)
        value.valueChanged.connect(lambda v, m=modifier: self._set_modifier_value(m, v))
        layout.addWidget(value)

        if suggested is not None:
            # The rebuild found a value the whole edge set accepts.  One push puts
            # it in, because the alternative is reading it out of a message.
            use = QPushButton(f"Use {suggested:g}")
            use.setToolTip(
                f"{suggested:g} mm works on every edge of this artwork. "
                f"The value now set is too large for the smallest detail."
            )
            use.setStyleSheet("QPushButton { color: #c58a2a; }")
            use.clicked.connect(
                lambda _=False, m=modifier, v=suggested: self._use_suggested(m, v)
            )
            layout.addWidget(use)
        return row

    def _use_suggested(self, modifier: Modifier, value: float) -> None:
        """Put the value that works into the field, and rebuild."""
        modifier.value = float(value)
        self.changed.emit("modifier value")

    # ------------------------------------------------------------------- edits

    def _emit(self, label: str) -> None:
        if not self._updating:
            self.changed.emit(label)

    def _set_offset(self, axis: int, value: float) -> None:
        if self._feature is None or self._updating:
            return
        u, v = self._feature.placement.offset_2d
        self._feature.placement.offset_2d = (value, v) if axis == 0 else (u, value)
        self._emit("move")

    def _set_rotation(self, value: float) -> None:
        if self._feature is None or self._updating:
            return
        self._feature.placement.rotation = value
        self._emit("rotate")

    def _turn(self, delta: float) -> None:
        if self._feature is None:
            return
        value = (self._feature.placement.rotation + delta) % 360.0
        self.rotation_field.setValue(value)

    def _set_size(self, axis: int, value: float) -> None:
        """Editing W or H with the lock on updates the other and the scale (§6.2)."""
        if self._feature is None or self._updating or value <= 0:
            return
        placement = self._feature.placement
        factor = value / self._native_size[axis]
        sx, sy = abs(placement.scale[0]), abs(placement.scale[1])
        if placement.uniform_scale:
            sx = sy = factor
        elif axis == 0:
            sx = factor
        else:
            sy = factor
        placement.scale = (sx, sy)

        self._updating = True
        self.width_field.set_silently(self._native_size[0] * sx)
        self.height_field.set_silently(self._native_size[1] * sy)
        self.scale_field.set_silently(sx * 100.0)
        self._updating = False
        self._emit("resize")

    def _set_scale_percent(self, percent: float) -> None:
        if self._feature is None or self._updating or percent <= 0:
            return
        factor = percent / 100.0
        placement = self._feature.placement
        placement.scale = (factor, factor) if placement.uniform_scale else (factor, abs(placement.scale[1]))

        self._updating = True
        self.width_field.set_silently(self._native_size[0] * placement.scale[0])
        self.height_field.set_silently(self._native_size[1] * placement.scale[1])
        self._updating = False
        self._emit("scale")

    def _reset_scale(self) -> None:
        self.scale_field.setValue(100.0)

    def _set_lift(self, value: float) -> None:
        if self._feature is None or self._updating:
            return
        self._feature.placement.lift = value
        self._emit("lift")

    def _set_mirror(self, axis: str, on: bool) -> None:
        if self._feature is None or self._updating:
            return
        if axis == "u":
            self._feature.placement.mirror_u = on
        else:
            self._feature.placement.mirror_v = on
        self._emit("mirror")

    def _set_lock(self, on: bool) -> None:
        if self._feature is None or self._updating:
            return
        self._feature.placement.uniform_scale = on
        self.lock_button.setText("🔒" if on else "🔓")

    def _set_kind(self, add_selected: bool) -> None:
        if self._feature is None or self._updating:
            return
        self._feature.operation.kind = (
            OperationKind.ADD if add_selected else OperationKind.CUT
        )
        self._emit("operation")

    def _set_depth_mode(self, _index: int) -> None:
        if self._feature is None or self._updating:
            return
        # PySide6 stores a StrEnum in a QComboBox as a plain str, so coerce it back.
        self._feature.operation.depth_mode = DepthMode(self.depth_mode.currentData())
        self._sync_depth_widgets()
        self._emit("depth mode")

    def _set_depth(self, value: float) -> None:
        if self._feature is None or self._updating:
            return
        self._feature.operation.depth = value
        self._emit("depth")

    def _set_direction(self, _index: int) -> None:
        if self._feature is None or self._updating:
            return
        self._feature.operation.direction = Direction(self.direction.currentData())
        self._emit("direction")

    def _set_draft(self, value: float) -> None:
        if self._feature is None or self._updating:
            return
        self._feature.operation.draft_angle = value
        self._emit("draft")

    def _set_modifier_value(self, modifier: Modifier, value: float) -> None:
        if self._updating:
            return
        modifier.value = value
        self._emit("edge size")

    def _set_modifier_role(self, modifier: Modifier, role: EdgeRole) -> None:
        if self._updating or role is None:
            return
        modifier.target.role = EdgeRole(role)
        self._emit("edge selection")

    def _set_modifier_enabled(self, modifier: Modifier, on: bool) -> None:
        if self._updating:
            return
        modifier.enabled = on
        self._emit("edge suppress")


__all__ = ["NumberField", "PropertiesPanel"]
