"""Dialogs for the cases the app cannot decide on its own - spec §5, §9, §10.

Each one covers something the importers cannot infer: an STL with no unit, a DXF
full of dimension layers, artwork whose loops will not close.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from stamp.io.normalize import Issue, IssueKind
from stamp.units import TO_MM

UNIT_CHOICES = [("Millimetres", "mm"), ("Centimetres", "cm"), ("Inches", "in"),
                ("Metres", "m"), ("Feet", "ft")]


class UnitPromptDialog(QDialog):
    """Ask for a unit with a live size preview - §5.2, §10 "units ambiguous".

    The preview is the whole point: "the bounding box is 84 × 40 × 12 - is that
    millimetres or inches?" is answerable, "what unit is this file in?" is not.
    """

    def __init__(self, size: tuple[float, ...], *, title: str, note: str = "",
                 default: str = "mm", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._size = size

        layout = QVBoxLayout(self)
        message = QLabel(note or "This file does not say what unit its numbers are in.")
        message.setWordWrap(True)
        layout.addWidget(message)

        self.unit_box = QComboBox()
        for caption, value in UNIT_CHOICES:
            self.unit_box.addItem(caption, value)
        self.unit_box.setCurrentIndex(self.unit_box.findData(default))
        self.unit_box.currentIndexChanged.connect(self._update_preview)

        form = QFormLayout()
        form.addRow("The numbers are in:", self.unit_box)
        layout.addLayout(form)

        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_preview()

    def _update_preview(self) -> None:
        scale = TO_MM[self.unit()]
        parts = " × ".join(f"{v * scale:.2f}" for v in self._size)
        self.preview.setText(f"That makes it {parts} mm.")

    def unit(self) -> str:
        return self.unit_box.currentData()

    def scale(self) -> float:
        return TO_MM[self.unit()]


class LayerFilterDialog(QDialog):
    """Pick which DXF layers carry the profile - §5.4.

    Real DXFs have construction lines, dimensions, and title blocks, so this is not
    optional polish.
    """

    def __init__(self, layers: list[str], selected: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose the layers to import")
        layout = QVBoxLayout(self)

        note = QLabel(
            "Only the layers you select become geometry. Construction lines, "
            "dimensions, and title blocks usually live on their own layers."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.list = QListWidget()
        chosen = {name.lower() for name in selected}
        for name in layers:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if name.lower() in chosen else Qt.CheckState.Unchecked
            )
            self.list.addItem(item)
        layout.addWidget(self.list)

        row = QHBoxLayout()
        all_button = QPushButton("Select all")
        none_button = QPushButton("Select none")
        all_button.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        none_button.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        row.addWidget(all_button)
        row.addWidget(none_button)
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_all(self, state: Qt.CheckState) -> None:
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)

    def layers(self) -> list[str]:
        return [
            self.list.item(i).text()
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.CheckState.Checked
        ]


class ProfileRepairDialog(QDialog):
    """Offer the repairs for a profile that will not import as it stands - §5.5, §10.

    Never a generic failure: each option names the problem it fixes.
    """

    def __init__(self, issues: list[Issue], *, suggested_stroke_mm: float = 0.5,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("This profile needs a repair")
        layout = QVBoxLayout(self)

        for issue in issues:
            label = QLabel(("• " if len(issues) > 1 else "") + issue.message)
            label.setWordWrap(True)
            if issue.blocking:
                label.setStyleSheet("color: #c0453a;")
            layout.addWidget(label)

        kinds = {issue.kind for issue in issues}
        self.options: list[tuple[QRadioButton, str]] = []

        if IssueKind.OPEN_LOOP in kinds:
            self._add_option(layout, "Close the open loops with a straight line", "close")
        if IssueKind.NO_FILL in kinds:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            button = QRadioButton("Outline the strokes at")
            self.stroke_width = QDoubleSpinBox()
            self.stroke_width.setSuffix(" mm")
            self.stroke_width.setDecimals(2)
            self.stroke_width.setRange(0.01, 100.0)
            self.stroke_width.setValue(max(suggested_stroke_mm, 0.01))
            row_layout.addWidget(button)
            row_layout.addWidget(self.stroke_width)
            row_layout.addStretch(1)
            layout.addWidget(row)
            self.options.append((button, "outline"))
        if IssueKind.SELF_INTERSECTION in kinds:
            self._add_option(layout, "Union the loops that overlap", "union")

        self._add_option(layout, "Import it as it is, and leave the problem", "ignore")
        if self.options:
            self.options[0][0].setChecked(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_option(self, layout, caption: str, key: str) -> None:
        button = QRadioButton(caption)
        layout.addWidget(button)
        self.options.append((button, key))

    def choice(self) -> str:
        for button, key in self.options:
            if button.isChecked():
                return key
        return "ignore"

    def stroke_mm(self) -> float:
        return getattr(self, "stroke_width", None).value() if hasattr(self, "stroke_width") else 0.5


class SolidChoiceDialog(QDialog):
    """A STEP file with several solids - ask which one, or treat them as one body (§5.1)."""

    def __init__(self, count: int, *, disjoint: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("This file has more than one solid")
        layout = QVBoxLayout(self)

        note = QLabel(
            f"This file contains {count} separate solids. Choose the one to work on."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.combo = QComboBox()
        for i in range(count):
            self.combo.addItem(f"Solid {i + 1}", i)
        layout.addWidget(self.combo)

        self.as_one = QCheckBox("Treat them as one body")
        self.as_one.setEnabled(disjoint)
        if not disjoint:
            self.as_one.setToolTip(
                "Two of these solids overlap, so they cannot be treated as one body."
            )
        layout.addWidget(self.as_one)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def solid_index(self) -> int | None:
        return None if self.as_one.isChecked() else self.combo.currentData()


class StlExportDialog(QDialog):
    """Tessellation quality with a live triangle count - §9."""

    PRESETS = [("Draft (0.1 mm)", "draft"), ("Normal (0.02 mm)", "normal"),
               ("Fine (0.005 mm)", "fine"), ("Custom", "custom")]

    def __init__(self, counter, *, mode: str = "solid", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export STL")
        self._counter = counter
        self._mode = mode

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.quality = QComboBox()
        for caption, key in self.PRESETS:
            self.quality.addItem(caption, key)
        self.quality.setCurrentIndex(1)
        self.quality.currentIndexChanged.connect(self._update)

        self.deflection = QDoubleSpinBox()
        self.deflection.setSuffix(" mm")
        self.deflection.setDecimals(4)
        self.deflection.setRange(0.0005, 5.0)
        self.deflection.setValue(0.02)
        self.deflection.valueChanged.connect(self._update)

        self.ascii_box = QCheckBox("Write ASCII instead of binary")

        form.addRow("Quality:", self.quality)
        form.addRow("Deflection:", self.deflection)
        form.addRow("", self.ascii_box)
        layout.addLayout(form)

        self.count_label = QLabel("")
        layout.addWidget(self.count_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if mode == "mesh":
            self.quality.setEnabled(False)
            self.deflection.setEnabled(False)
        self._update()

    def _update(self) -> None:
        key = self.quality.currentData()
        from stamp.geom.mesh_ops import QUALITY_PRESETS

        if key != "custom":
            blocked = self.deflection.blockSignals(True)
            self.deflection.setValue(QUALITY_PRESETS[key])
            self.deflection.blockSignals(blocked)
        self.deflection.setEnabled(key == "custom" and self._mode != "mesh")
        try:
            count = self._counter(self.deflection.value())
            self.count_label.setText(f"{count:,} triangles")
        except Exception:
            self.count_label.setText("")

    def deflection_mm(self) -> float:
        return self.deflection.value()

    def ascii_format(self) -> bool:
        return self.ascii_box.isChecked()



class StepExportDialog(QDialog):
    """Schema and the co-planar face merge - spec §9.

    Both are offered because both change what the shop receives: AP242 is the
    modern schema but some older systems only read AP214, and merging co-planar
    faces gives a far cleaner file at the cost of the face layout Stamp worked in.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export STEP")
        layout = QVBoxLayout(self)

        self.schema = QComboBox()
        self.schema.addItem("AP242 (recommended)", "AP242")
        self.schema.addItem("AP214", "AP214")

        form = QFormLayout()
        form.addRow("Schema:", self.schema)
        layout.addLayout(form)

        self.simplify = QCheckBox("Merge faces that lie in the same plane")
        self.simplify.setChecked(True)
        self.simplify.setToolTip(
            "This gives the shop a much cleaner file. It applies to the exported "
            "copy only, not to the project."
        )
        layout.addWidget(self.simplify)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def schema_name(self) -> str:
        return self.schema.currentData()

    def merge_faces(self) -> bool:
        return self.simplify.isChecked()


ODA_DOWNLOAD_URL = "https://www.opendesign.com/guestfiles/oda_file_converter"


class DwgConverterDialog(QDialog):
    """No DWG reader is present - offer the download and a path picker (§5.4)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Stamp cannot read DWG yet")
        self._path = ""
        layout = QVBoxLayout(self)

        note = QLabel(
            "Stamp reads DWG through the ODA File Converter, which is free and is "
            "not installed on this machine. You have three choices."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        link = QLabel(
            f'1. Download it from <a href="{ODA_DOWNLOAD_URL}">Open Design Alliance</a>, '
            f"install it, and try again."
        )
        link.setOpenExternalLinks(True)
        link.setWordWrap(True)
        layout.addWidget(link)

        layout.addWidget(QLabel("2. Point Stamp at a copy you already have:"))
        row = QHBoxLayout()
        self.path_label = QLabel("(none chosen)")
        self.path_label.setWordWrap(True)
        browse = QPushButton("Choose...")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_label, 1)
        row.addWidget(browse)
        layout.addLayout(row)

        third = QLabel(
            "3. Save the drawing as DXF from your CAD program. That always works, "
            "and it is the fastest route if you only have one file."
        )
        third.setWordWrap(True)
        layout.addWidget(third)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        chosen, _ = QFileDialog.getOpenFileName(
            self, "Find the ODA File Converter", "", "Programs (*.exe);;All files (*)"
        )
        if chosen:
            self._path = chosen
            self.path_label.setText(chosen)

    def converter_path(self) -> str:
        return self._path


def warn(parent, title: str, message: str) -> None:
    QMessageBox.warning(parent, title, message)


def inform(parent, title: str, message: str) -> None:
    QMessageBox.information(parent, title, message)


def confirm(parent, title: str, message: str) -> bool:
    answer = QMessageBox.question(
        parent, title, message,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def relink_prompt(parent, missing: list[str]) -> None:
    """Name the files that are gone and offer to find them again (§10)."""
    names = "\n".join(f"  {Path(p).name}" for p in missing)
    QMessageBox.warning(
        parent,
        "Some source files are missing",
        f"This project refers to files that are not on this machine:\n\n{names}\n\n"
        f"The rest of the project opened correctly. Use Relink to find them.",
    )



class ReportDialog(QDialog):
    """The form a user fills in before Stamp drafts the email.

    Every box is optional.  A report with only the environment in it is still
    worth more than no report, so nothing here blocks on an empty box.
    """

    def __init__(self, kind: str, parent=None) -> None:
        super().__init__(parent)
        self._kind = kind
        crash = kind == "crash"
        self.setWindowTitle("Report a crash" if crash else "Report a bug")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        if crash:
            lead = (
                "Stamp stopped without warning. Tell us what you did, and Stamp "
                "puts the log and the details into an email for you to send."
            )
        else:
            lead = (
                "Tell us what went wrong. Stamp puts the details and the log into "
                "an email for you to send."
            )
        note = QLabel(lead)
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        self.summary = QLineEdit()
        self.summary.setPlaceholderText("One line, for the subject")
        form.addRow("Summary:", self.summary)

        self.detail = QPlainTextEdit()
        self.detail.setPlaceholderText("What happened?")
        self.detail.setFixedHeight(80)
        form.addRow("What happened:", self.detail)

        self.expected = QPlainTextEdit()
        self.expected.setPlaceholderText("What did you expect instead?")
        self.expected.setFixedHeight(56)
        form.addRow("What you expected:", self.expected)

        self.steps = QPlainTextEdit()
        self.steps.setPlaceholderText("1. Open a part\n2. Add a profile\n3. ...")
        self.steps.setFixedHeight(80)
        form.addRow("Steps:", self.steps)

        self.part = QLineEdit()
        self.part.setPlaceholderText("Which part file and which artwork?")
        form.addRow("Part and artwork:", self.part)
        layout.addLayout(form)

        privacy = QLabel(
            "Stamp sends nothing on its own. Your mail application opens with the "
            "report in it, and you push send."
        )
        privacy.setWordWrap(True)
        privacy.setStyleSheet("color: #8a8f98;")
        layout.addWidget(privacy)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Write the email")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def report(self):
        from stamp.reporting import Report

        return Report(
            kind=self._kind,
            summary=self.summary.text(),
            detail=self.detail.toPlainText(),
            expected=self.expected.toPlainText(),
            steps=self.steps.toPlainText(),
            part=self.part.text(),
        )


def crash_prompt(parent) -> bool:
    """Offer to report a crash that happened the last time Stamp ran."""
    answer = QMessageBox.question(
        parent,
        "Stamp stopped without warning",
        "The last time Stamp ran, it stopped without warning.\n\n"
        "Do you want to send a report? Stamp fills in the log and the details, "
        "and your mail application opens for you to check it and send it.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


__all__ = [
    "DwgConverterDialog",
    "ReportDialog",
    "LayerFilterDialog",
    "ProfileRepairDialog",
    "SolidChoiceDialog",
    "StepExportDialog",
    "StlExportDialog",
    "UnitPromptDialog",
    "confirm",
    "crash_prompt",
    "inform",
    "relink_prompt",
    "warn",
]


class Color3mfDialog(QDialog):
    """Colors and quality for the multi-color 3MF export - §9.

    The file carries one body per feature plus the base, each bound to a filament
    slot, so a color printer (Bambu, Orca and friends) prints the artwork in a
    second color without any painting in the slicer.
    """

    PRESETS = [("Draft (0.1 mm)", 0.1), ("Normal (0.02 mm)", 0.02), ("Fine (0.005 mm)", 0.005)]

    #: What a fresh install offers.  A slicer makes a filament for every colour it
    #: finds, so these are only a starting point - see :meth:`_pick`.
    DEFAULT_BASE = "#2B2B2B"
    DEFAULT_FEATURE = "#C8A24A"

    def __init__(
        self,
        feature_count: int,
        *,
        mode: str = "solid",
        base_color: str | None = None,
        feature_color: str | None = None,
        write_colors: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export 3MF for color printing")
        self._base_color = base_color or self.DEFAULT_BASE
        self._feature_color = feature_color or self.DEFAULT_FEATURE

        layout = QVBoxLayout(self)
        note = QLabel(
            f"The base and {feature_count} feature bod"
            f"{'y' if feature_count == 1 else 'ies'} are written as one object with "
            f"separate parts, so the part moves in one piece and you can give each "
            f"part its own filament."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.color_box = QCheckBox("Write these colors into the file")
        self.color_box.setChecked(write_colors)
        self.color_box.setToolTip(
            "On: the slicer offers to map these colors to filament slots when it "
            "opens the file. "
            "Off: the file carries no color, and you pick the filament for each "
            "part in the slicer, as with any other 3MF."
        )
        self.color_box.toggled.connect(self._sync_enabled)
        layout.addWidget(self.color_box)

        # A slicer makes a new filament for every colour string it does not have.
        # Matching the artwork colours to the filaments actually loaded is what
        # stops a pile of unwanted entries appearing next to them.
        expected = QLabel(
            "Set these to the filaments you print with. Bambu Studio adds a "
            "filament for every color it does not recognise, so colors that do not "
            "match yours arrive as extra entries you have to change back. It also "
            "says “The 3mf file has invalid config, load geometry data only” for "
            "every file it did not write, which is expected and harmless."
        )
        expected.setWordWrap(True)
        expected.setStyleSheet("color: #8a8f98;")
        layout.addWidget(expected)

        form = QFormLayout()
        self.base_button = QPushButton()
        self.base_button.clicked.connect(lambda: self._pick("base"))
        self.feature_button = QPushButton()
        self.feature_button.clicked.connect(lambda: self._pick("feature"))
        self._paint_buttons()
        form.addRow("Base color (slot 1):", self.base_button)
        form.addRow("Feature color (slot 2):", self.feature_button)

        self.quality = QComboBox()
        for caption, value in self.PRESETS:
            self.quality.addItem(caption, value)
        self.quality.setCurrentIndex(1)
        if mode == "mesh":
            self.quality.setEnabled(False)
            self.quality.setToolTip("A mesh part is exported at its own resolution.")
        form.addRow("Quality:", self.quality)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync_enabled(self.color_box.isChecked())

    def _sync_enabled(self, on: bool) -> None:
        self.base_button.setEnabled(on)
        self.feature_button.setEnabled(on)

    def write_colors(self) -> bool:
        return self.color_box.isChecked()

    def _paint_buttons(self) -> None:
        for button, value in (
            (self.base_button, self._base_color),
            (self.feature_button, self._feature_color),
        ):
            button.setText(value)
            button.setStyleSheet(
                f"QPushButton {{ background-color: {value}; padding: 4px 12px; }}"
            )

    def _pick(self, which: str) -> None:
        from PySide6.QtGui import QColor

        current = self._base_color if which == "base" else self._feature_color
        chosen = QColorDialog.getColor(QColor(current), self, "Pick a color")
        if not chosen.isValid():
            return
        if which == "base":
            self._base_color = chosen.name().upper()
        else:
            self._feature_color = chosen.name().upper()
        self._paint_buttons()

    def base_color(self) -> str:
        return self._base_color

    def feature_color(self) -> str:
        return self._feature_color

    def deflection_mm(self) -> float:
        return float(self.quality.currentData())


class ReplaceReportDialog(QDialog):
    """What happened to each stamp when the part was replaced - §8.2.

    A replacement that silently moved artwork would be worse than one that
    refused, so every feature is listed with what became of it, and the ones
    needing attention are named rather than counted.
    """

    def __init__(self, report, part_name: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Part replaced")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        headline = QLabel(f"{part_name} is now the part. {report.summary()}")
        headline.setWordWrap(True)
        headline.setStyleSheet("font-weight: 600;")
        layout.addWidget(headline)

        for warning in report.warnings:
            note = QLabel(warning)
            note.setWordWrap(True)
            note.setStyleSheet("color: #c58a2a;")
            layout.addWidget(note)

        rows = QListWidget()
        for match in report.matches:
            if match.status == "kept":
                text = f"✓  {match.name} — stayed where it was"
                color = None
            elif match.status == "moved":
                text = (
                    f"→  {match.name} — followed its face, "
                    f"{match.moved_mm:.2f} mm"
                )
                color = "#c58a2a"
            else:
                text = f"!  {match.name} — pick a face for it again"
                color = "#c0453a"
            if match.detail and match.status != "lost":
                text += f" ({match.detail})"
            item = QListWidgetItem(text)
            if color:
                from PySide6.QtGui import QColor

                item.setForeground(QColor(color))
            rows.addItem(item)
        if report.matches:
            rows.setMaximumHeight(min(240, 28 * len(report.matches) + 12))
            layout.addWidget(rows)

        if report.lost:
            hint = QLabel(
                "Select a feature that needs attention and use “Pick the face "
                "again” in the panel on the right. Nothing was deleted."
            )
            hint.setWordWrap(True)
            layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
