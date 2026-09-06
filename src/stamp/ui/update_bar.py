"""The strip that says a newer Stamp exists.

A bar rather than a dialog, and that is the whole design.  An update is never
urgent enough to interrupt somebody halfway through placing a stamp on a face,
and a modal box at start-up trains people to dismiss modal boxes.  This sits
under the ribbon, says one sentence, and can be ignored for as long as the user
likes - including forever.

It has four things to say, and says only one at a time: an update exists, it is
downloading, it is downloaded and waiting, or the attempt failed.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from stamp.ui import icons
from stamp.ui.ribbon import Theme

#: Tall enough to read, short enough that it is not a second toolbar.
BAR_H = 32


class UpdateBar(QWidget):
    """One line under the ribbon, hidden until there is something to say."""

    install_requested = Signal()
    later_requested = Signal()
    notes_requested = Signal()
    skip_requested = Signal()
    dismissed = Signal()
    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("updateBar")
        # A plain QWidget draws no background from a stylesheet without
        # this: the rule is parsed, matches, and paints nothing.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(BAR_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 3, 6, 3)
        row.setSpacing(8)

        self._icon = QLabel()
        row.addWidget(self._icon)

        self.message = QLabel("")
        self.message.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        row.addWidget(self.message, 1)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(170)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        row.addWidget(self.progress)

        self._buttons: dict[str, QPushButton] = {}
        for key, caption, signal in (
            ("notes", "What's new", self.notes_requested),
            ("install", "Install", self.install_requested),
            ("later", "Install when I quit", self.later_requested),
            ("cancel", "Cancel", self.cancel_requested),
            ("skip", "Skip this version", self.skip_requested),
        ):
            button = QPushButton(caption)
            button.setFlat(key in ("notes", "skip"))
            button.clicked.connect(signal)
            button.setVisible(False)
            self._buttons[key] = button
            row.addWidget(button)

        self._close = QToolButton()
        self._close.setObjectName("updateClose")
        self._close.setAutoRaise(True)
        self._close.setFixedSize(22, 22)
        self._close.setToolTip("Hide this until the next time Stamp starts")
        self._close.clicked.connect(self.dismissed)
        row.addWidget(self._close)

        self.setVisible(False)
        self.apply_theme(Theme(self.palette()))

    # -- appearance -------------------------------------------------------

    def apply_theme(self, theme: Theme) -> None:
        """Match the ribbon, so the top of the window stays one piece."""
        self._theme = theme
        self.setStyleSheet(
            f"""
            QWidget#updateBar {{
                background: {theme.notice.name()};
                border-bottom: 1px solid {theme.hairline.name()};
            }}
            QWidget#updateBar QLabel {{ color: {theme.text.name()}; }}
            QToolButton#updateClose {{
                border: 0px; border-radius: 4px; color: {theme.dim.name()};
            }}
            QToolButton#updateClose:hover {{ background: {theme.hover.name()}; }}
            """
        )
        self._icon.setPixmap(
            icons.pixmap("insert-preset", 16, theme.accent, theme.accent)
        )
        self._close.setIcon(icons.icon("chevron-up", theme.dim, theme.accent, 12))

    def _show_only(self, *keys: str) -> None:
        for key, button in self._buttons.items():
            button.setVisible(key in keys)

    # -- what it can say --------------------------------------------------

    def offer(self, version: str, urgent: bool, installable: bool) -> None:
        self.progress.setVisible(False)
        if urgent:
            self.message.setText(
                f"<b>Stamp {version} is available.</b> The version you are running "
                "is no longer supported."
            )
        else:
            self.message.setText(f"<b>Stamp {version} is available.</b>")
        if installable:
            self._show_only("notes", "install", "skip")
        else:
            self._buttons["notes"].setText("Get it")
            self._show_only("notes", "skip")
        self.setVisible(True)

    def downloading(self, version: str) -> None:
        self.message.setText(f"Downloading Stamp {version}…")
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self._show_only("cancel")
        self.setVisible(True)

    def advance(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        megabytes = done / (1024 * 1024)
        whole = total / (1024 * 1024) if total else 0
        self.progress.setToolTip(
            f"{megabytes:.0f} of {whole:.0f} MB" if total else f"{megabytes:.0f} MB"
        )

    def ready(self, version: str) -> None:
        self.progress.setVisible(False)
        self.message.setText(
            f"<b>Stamp {version} is ready to install.</b> Stamp will close while "
            "it installs, and open again afterwards."
        )
        self._buttons["install"].setText("Install now")
        self._show_only("install", "later")
        self.setVisible(True)

    def trouble(self, reason: str) -> None:
        self.progress.setVisible(False)
        self.message.setText(f"Stamp could not update: {reason}")
        self._show_only("notes")
        self._buttons["notes"].setText("Open the release page")
        self.setVisible(True)

    def hide_bar(self) -> None:
        self.setVisible(False)


__all__ = ["BAR_H", "UpdateBar"]
