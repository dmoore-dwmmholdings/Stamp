"""The command ribbon along the top of the window - spec §7.

Stamp has about thirty commands.  On one toolbar that is a row of text buttons
wider than the window, and Qt answers by moving the overflow into a chevron menu
that shuts again at every layout pass - so the last third of the commands had no
working path at all, and the menus exist because of it.

A ribbon fixes the shape of the problem rather than the symptom.  Commands are
divided into a few tabs, so only the ones belonging to what the user is doing are
on screen; within a tab they are gathered into captioned groups, so a command is
found by what it is for rather than by reading along a row.  Each tab scrolls
sideways if the window is narrower than its groups, which means nothing is ever
unreachable - the one thing the old bar could not promise.

Three things make it look like a ribbon rather than a row of buttons in a box,
and the first version had none of them:

* **Drawn icons.**  See :mod:`stamp.ui.icons`.  The set is one grid and one
  stroke weight, and it takes its colours from the palette - the old emoji
  fallback painted a fixed light grey, which on a light desktop theme is white
  on white.
* **A theme of its own.**  Word and SolidWorks both sit the ribbon body on a
  lighter ground than the window, mark the current tab with the accent colour,
  and give every button a hover and a pressed state.  Those states are what tell
  you a thing is a button before you click it; flat text on flat grey does not.
  All of it is mixed from the palette here, so it follows a light or a dark
  desktop rather than assuming one.
* **Two button sizes.**  A group whose commands are all the same size has no
  shape.  The command you reach for is large; the ones beside it stack three to
  a column, small, with the label alongside - which is also how thirty commands
  fit across a laptop screen.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from stamp.ui import icons

#: Size of the painted glyph on a large button, in logical pixels.
ICON_PX = 22

#: ...and on a small one, where the label sits beside the icon instead of under.
SMALL_ICON_PX = 15

#: Large buttons are a fixed width, so captions of different lengths still line
#: up in a row.  Deliberately restrained: the ribbon is the frame around the
#: work, not the work, and every pixel it takes is one the 3D view does not get.
BUTTON_W = 64
BUTTON_H = 56

#: A small button, and how many stack in one column before a new one starts.
SMALL_H = 18
COLUMN = 3

#: The compact layout: one row of these, and no caption under the group.  This
#: is what the ribbon opens as, because the ribbon is the frame around the work.
DENSE_H = 22
DENSE_ICON_PX = 16

#: Caption under a group of buttons, in the roomy layout only.
CAPTION_PX = 11
CAPTION_H = 14

#: Height of the ribbon when it is collapsed to its tab strip alone.
COLLAPSED_H = 26

#: How much the groups take under the tab strip, at each of the two sizes.
DENSE_BODY_H = 30
ROOMY_BODY_H = BUTTON_H + CAPTION_H + 6

#: Qt's own "no maximum", for undoing a setFixedSize.
QWIDGETSIZE_MAX = 16777215


# --------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------

def _mix(base: QColor, other: QColor, amount: float) -> QColor:
    """*amount* of *other* laid over *base*."""
    keep = 1.0 - amount
    return QColor(
        round(base.red() * keep + other.red() * amount),
        round(base.green() * keep + other.green() * amount),
        round(base.blue() * keep + other.blue() * amount),
    )


def _luma(color: QColor) -> float:
    """Rec. 601, which is what every "is this dark?" check in a UI ends up being."""
    return 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()


def _readable(color: QColor, against: QColor) -> QColor:
    """Push *color* away from *against* until the two can be told apart.

    A desktop accent that happens to be near the ribbon's own ground - a pale
    blue on white, a navy on charcoal - would otherwise make the accent half of
    every icon disappear.
    """
    result = QColor(color)
    ground = _luma(against)
    for _ in range(12):
        if abs(_luma(result) - ground) >= 70:
            break
        result = result.darker(112) if ground > 128 else result.lighter(112)
    return result


class Theme:
    """Every colour the ribbon paints with, mixed from the desktop palette.

    Held as one object rather than scattered through the stylesheet so that a
    palette change - the user switching to dark mode with Stamp open - is one
    rebuild rather than thirty hard-coded hexes that no longer match.
    """

    def __init__(self, palette: QPalette) -> None:
        window = palette.color(QPalette.ColorRole.Window)
        text = palette.color(QPalette.ColorRole.WindowText)
        accent = palette.color(QPalette.ColorRole.Highlight)
        self.dark = _luma(window) < 128

        # The body sits a shade off the strip above it and the window below, so
        # the ribbon reads as a surface of its own rather than as a stretch of
        # window with buttons on it.  That step is small on purpose: large enough
        # to see the edge, not so large that the chrome outshouts the part.
        self.strip = window.lighter(112) if self.dark else window.darker(103)
        self.body = window.lighter(126) if self.dark else _mix(window, QColor(255, 255, 255), 0.75)
        self.accent = _readable(accent, self.body)
        self.text = text
        self.icon = _mix(self.body, text, 0.88)
        self.dim = _mix(self.body, text, 0.62)
        self.disabled = _mix(self.body, text, 0.34)
        self.hairline = _mix(self.body, text, 0.17)
        self.hover = _mix(self.body, self.accent, 0.15)
        self.hover_edge = _mix(self.body, self.accent, 0.34)
        self.pressed = _mix(self.body, self.accent, 0.28)
        self.checked = _mix(self.body, self.accent, 0.21)
        self.checked_edge = _mix(self.body, self.accent, 0.60)
        self.tab_hover = _mix(self.strip, self.accent, 0.11)
        #: A band the colour of the accent, faint enough to read text on - the
        #: update bar under the ribbon uses it.
        self.notice = _mix(self.body, self.accent, 0.16)


def _button_rules(name: str, theme: Theme) -> str:
    """The hover, pressed, checked and disabled states, for one button class.

    Written once for both sizes: the states are what make a flat rectangle read
    as a button, and they have to agree between the two or the ribbon looks like
    two toolbars that met by accident.
    """
    return f"""
    QToolButton#{name} {{
        border: 1px solid transparent;
        border-radius: 4px;
        background: transparent;
        color: {theme.text.name()};
        padding: 1px;
    }}
    QToolButton#{name}:hover {{
        background: {theme.hover.name()};
        border-color: {theme.hover_edge.name()};
    }}
    QToolButton#{name}:pressed {{
        background: {theme.pressed.name()};
        border-color: {theme.hover_edge.name()};
    }}
    QToolButton#{name}:checked {{
        background: {theme.checked.name()};
        border-color: {theme.checked_edge.name()};
    }}
    QToolButton#{name}:disabled {{
        color: {theme.disabled.name()};
        background: transparent;
        border-color: transparent;
    }}
    QToolButton#{name}::menu-indicator {{
        subcontrol-origin: padding;
        subcontrol-position: bottom right;
        width: 7px;
        height: 7px;
        right: 2px;
        bottom: 1px;
    }}
    """


def ribbon_stylesheet(theme: Theme) -> str:
    """The whole ribbon, from the tab strip down.

    Deliberately nothing here for QComboBox or QDoubleSpinBox.  Styling a combo
    box at all switches Qt to the stylesheet's box model for the whole widget,
    and its drop-down arrow then has to be supplied as an image - leave that
    half-done and the arrow comes out as a small dark square, or vanishes.  A
    field should look like the desktop's; the ribbon only makes it compact, in
    :func:`_compact`.
    """
    return f"""
    QWidget#ribbonBody {{ background: {theme.body.name()}; }}
    QTabWidget#ribbon::pane {{
        border: 0px;
        border-bottom: 1px solid {theme.hairline.name()};
        background: {theme.body.name()};
    }}
    QTabBar {{ background: {theme.strip.name()}; qproperty-drawBase: 0; }}
    QTabBar::tab {{
        background: transparent;
        color: {theme.dim.name()};
        border: 0px;
        border-bottom: 2px solid transparent;
        padding: 4px 14px 3px 14px;
        margin: 0px;
        font-size: 12px;
    }}
    QTabBar::tab:hover {{ color: {theme.text.name()}; background: {theme.tab_hover.name()}; }}
    QTabBar::tab:selected {{
        color: {theme.accent.name()};
        background: {theme.body.name()};
        border-bottom: 2px solid {theme.accent.name()};
    }}
    QLabel#ribbonCaption {{ color: {theme.dim.name()}; font-size: {CAPTION_PX}px; }}
    QFrame#ribbonDivider {{ background: {theme.hairline.name()}; }}
    QToolButton#ribbonLarge {{ font-size: 10px; }}
    QToolButton#ribbonSmall {{ font-size: 11px; text-align: left; padding-left: 3px; }}
    {_button_rules("ribbonLarge", theme)}
    {_button_rules("ribbonSmall", theme)}
    {_button_rules("ribbonPin", theme)}
    """


def header_stylesheet(theme: Theme) -> str:
    """The menu bar and the strip the ribbon sits in.

    A menu bar in one style above a ribbon in another reads as two programs
    stacked.  This makes the whole top of the window one surface.
    """
    return f"""
    QMenuBar {{
        background: {theme.strip.name()};
        color: {theme.text.name()};
        border: 0px;
        padding: 1px 4px;
    }}
    QMenuBar::item {{
        background: transparent;
        padding: 3px 9px;
        border-radius: 4px;
        margin: 0px 1px;
    }}
    QMenuBar::item:selected {{ background: {theme.hover.name()}; }}
    QMenuBar::item:pressed {{ background: {theme.pressed.name()}; }}
    QToolBar#ribbonHolder {{
        background: {theme.strip.name()};
        border: 0px;
        border-bottom: 1px solid {theme.hairline.name()};
        padding: 0px;
        margin: 0px;
        spacing: 0px;
    }}
    {_button_rules("quickButton", theme)}
    """


# --------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------

def _move(layout, widget: QWidget, index: int | None = None) -> None:
    """Put *widget* into *layout*, taking it out of whatever it was in.

    Switching between the ribbon's two sizes moves every button into a freshly
    built row.  Taking it out of the old layout first keeps Qt from warning that
    it is already in one, and showing it afterwards is not optional: re-parenting
    a widget hides it, and a button that has been hidden that way stays hidden
    however tidy the new layout is.
    """
    old = widget.parentWidget()
    if old is not None and old.layout() is not None:
        old.layout().removeWidget(widget)
    if index is None:
        layout.addWidget(widget)
    else:
        layout.insertWidget(index, widget)
    widget.setVisible(True)


def _compact(widget: QWidget) -> None:
    """Make a field short enough for the ribbon, without restyling it.

    Two of these stack in the height of one large button, and a combo box at its
    natural height does not.  Only the metrics change: the field keeps the
    desktop's own look, which is what a field should have.
    """
    font = widget.font()
    if font.pointSizeF() > 0:
        font.setPointSizeF(max(7.0, font.pointSizeF() - 0.5))
        widget.setFont(font)
    widget.setFixedHeight(23)


def _rich_tooltip(action: QAction) -> str:
    """The name in bold, the shortcut beside it, the explanation under it.

    A tooltip that repeats the caption teaches nothing.  This is the shape every
    ribbon uses, and it is where a command with no obvious icon earns its place.
    """
    name = action.text().replace("&", "").strip()
    shortcut = action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
    head = f"<b>{name}</b>" + (f" &nbsp;<span>{shortcut}</span>" if shortcut else "")
    body = (action.toolTip() or "").strip()
    # `make()` in the window already writes "Name (Ctrl+S)" into the tooltip of a
    # command that has a shortcut; that would repeat both halves of the heading.
    if body in ("", name, f"{name} ({shortcut})"):
        return head
    return f"{head}<br>{body}"


def _bind(button: QToolButton, action: QAction) -> None:
    """Make *button* a second view of *action* rather than a second command.

    Deliberately not setDefaultAction: that keeps the button's text tied to the
    action's, which overwrote every short caption with the full command name and
    left a row reading "Align... edge" and "Set st...vertex".  The rest of what a
    default action gives is wired by hand, so the window still enables and checks
    one object and both views follow.
    """
    button.setCheckable(action.isCheckable())
    button.setChecked(action.isChecked())
    button.setEnabled(action.isEnabled())
    button.setToolTip(_rich_tooltip(action))
    if action.isCheckable():
        button.toggled.connect(
            lambda on, a=action: a.setChecked(on) if a.isChecked() != on else None
        )
        action.toggled.connect(
            lambda on, b=button: b.setChecked(on) if b.isChecked() != on else None
        )
    else:
        button.clicked.connect(action.trigger)
    action.changed.connect(
        lambda a=action, b=button: (
            b.setEnabled(a.isEnabled()), b.setToolTip(_rich_tooltip(a))
        )
    )


@dataclass
class _Command:
    """One command in a group, and how it wants to be drawn at each size."""

    button: QToolButton
    icon: str
    large: bool
    tall_text: str   # under the icon, on two lines, in the roomy layout
    flat_text: str   # beside the icon, on one line


class RibbonGroup(QWidget):
    """A cluster of related commands within one tab.

    It lays itself out twice over: one row of small buttons when the ribbon is
    compact, and the tall arrangement - large buttons with columns of small ones
    beside them, under a caption - when it is not.  The buttons themselves are
    made once and re-dressed, because each one carries live connections to its
    action and rebuilding them would leave those behind.
    """

    def __init__(self, ribbon: Ribbon, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ribbon = ribbon
        self._items: list[tuple[str, object]] = []

        self._outer = QVBoxLayout(self)
        self._outer.setSpacing(1)

        self._body = QWidget()
        self._outer.addWidget(self._body, 1)

        self._caption = QLabel(caption)
        self._caption.setObjectName("ribbonCaption")
        self._caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._caption.setFixedHeight(CAPTION_H)
        self._outer.addWidget(self._caption)

        self.apply_mode()

    # -- building ---------------------------------------------------------

    def add_action(self, action: QAction, icon: str, caption: str | None = None) -> QToolButton:
        """Put *action* in this group as one of its main commands.

        *caption* is the short name for under the icon; a button is narrower than
        a command name, and a name left to elide reads as "Save ...reset".  The
        full name stays on the action, for the tooltip and for the menus.
        """
        return self._add(action, icon, caption, large=True)

    def add_small_action(
        self, action: QAction, icon: str, caption: str | None = None
    ) -> QToolButton:
        """Put *action* in as a secondary command.

        In the roomy layout three of these stack in the height of one large
        button, which is how a ribbon says "these matter less than the one next
        to them".  In the compact layout every command is this size.
        """
        return self._add(action, icon, caption, large=False)

    def add_widget(self, widget: QWidget) -> QWidget:
        """Put a control - a combo box, a spin box - in this group."""
        self.add_stack([widget])
        return widget

    def add_menu_action(
        self, action: QAction, icon: str, caption: str, menu
    ) -> QToolButton:
        """A button that drops a menu - one press to a list of related commands."""
        button = self.add_action(action, icon, caption)
        button.setMenu(menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        return button

    def add_stack(self, widgets) -> None:
        """Fields, stacked in the roomy layout and side by side in the compact."""
        for widget in widgets:
            _compact(widget)
        self._items.append(("fields", list(widgets)))
        self.apply_mode()

    def _add(
        self, action: QAction, icon: str, caption: str | None, large: bool
    ) -> QToolButton:
        button = QToolButton()
        _bind(button, action)
        self._ribbon.remember(button, action)
        name = (caption or action.text()).replace("&", "")
        self._items.append((
            "command",
            _Command(
                button=button,
                icon=icon,
                large=large,
                tall_text=caption or _short(action.text()),
                flat_text=name.replace("\n", " "),
            ),
        ))
        self.apply_mode()
        return button

    # -- laying out -------------------------------------------------------

    def apply_mode(self) -> None:
        """Build the group's row for whichever size the ribbon is at.

        The old body is replaced rather than emptied: every button is moved into
        the new one first, so deleting the old takes only the columns it made.
        """
        dense = self._ribbon.dense
        body = QWidget()
        row = QHBoxLayout(body)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(1 if dense else 2)

        column: QVBoxLayout | None = None
        for kind, payload in self._items:
            if kind == "command":
                command: _Command = payload  # type: ignore[assignment]
                self._dress(command, dense)
                if dense or command.large:
                    _move(row, command.button)
                    column = None  # a large button ends any column beside it
                else:
                    column = self._pour(row, column, command.button)
            else:
                row.addWidget(self._fields(payload, dense))
                column = None

        self._outer.replaceWidget(self._body, body)
        self._body.setParent(None)
        self._body.deleteLater()
        self._body = body
        # A widget put into a layout is not shown until the layout next
        # activates, which is one trip round the event loop away.  The ribbon
        # has to be right in the frame the switch happens in, not the frame
        # after it, so say so here.  Hidden ancestors still win, which is what
        # keeps this from re-opening a collapsed ribbon.
        body.setVisible(True)
        self._caption.setVisible(not dense)
        self._outer.setContentsMargins(5, 3, 5, 3 if dense else 1)

    def _dress(self, command: _Command, dense: bool) -> None:
        """Give one button the size and shape the current layout wants."""
        button = command.button
        button.setMinimumSize(0, 0)
        button.setMaximumSize(QWIDGETSIZE_MAX, QWIDGETSIZE_MAX)
        if not dense and command.large:
            button.setObjectName("ribbonLarge")
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setText(command.tall_text)
            button.setFixedSize(BUTTON_W, BUTTON_H)
            size = ICON_PX
        else:
            button.setObjectName("ribbonSmall")
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setText(command.flat_text)
            button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            button.setFixedHeight(DENSE_H if dense else SMALL_H)
            size = DENSE_ICON_PX if dense else SMALL_ICON_PX
        button.setIconSize(QSize(size, size))
        # The object name selects the rule in the stylesheet, and Qt only
        # re-reads that when the widget is polished again.
        button.style().unpolish(button)
        button.style().polish(button)
        self._ribbon.reface(button, command.icon, size)

    @staticmethod
    def _pour(
        row: QHBoxLayout, column: QVBoxLayout | None, button: QToolButton
    ) -> QVBoxLayout:
        """Add to the open column, starting a new one when this one is full."""
        if column is None or column.count() > COLUMN + 1:
            holder = QWidget()
            column = QVBoxLayout(holder)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(1)
            # A stretch at each end, so a column of two sits centred against the
            # large button beside it rather than hanging from the top of the group.
            column.addStretch(1)
            column.addStretch(1)
            row.addWidget(holder)
        _move(column, button, column.count() - 1)
        return column

    @staticmethod
    def _fields(widgets, dense: bool) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder) if dense else QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        if not dense:
            layout.addStretch(1)
        for widget in widgets:
            _move(layout, widget)
        if not dense:
            layout.addStretch(1)
        return holder


class RibbonTab(QWidget):
    """One page of the ribbon: groups in a row, scrolling if the window is narrow."""

    def __init__(self, ribbon: Ribbon, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ribbon = ribbon
        self._groups: list[RibbonGroup] = []
        self._dividers: list[QFrame] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        outer.addWidget(self._scroll)

        self._body = QWidget()
        self._body.setObjectName("ribbonBody")
        self._body.setAutoFillBackground(True)
        self._body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._row = QHBoxLayout(self._body)
        self._row.setContentsMargins(4, 0, 4, 0)
        self._row.setSpacing(0)
        self._row.addStretch(1)
        self._scroll.setWidget(self._body)
        self._scroll.viewport().setObjectName("ribbonBody")

    def apply_mode(self) -> None:
        for group in self._groups:
            group.apply_mode()
        for divider in self._dividers:
            divider.setStyleSheet(self._divider_margin())

    def _divider_margin(self) -> str:
        """The rule stops short of the caption, which only the roomy layout has."""
        return (
            "margin: 4px 3px 4px 3px;"
            if self._ribbon.dense
            else "margin: 5px 2px 16px 2px;"
        )

    def add_group(self, caption: str) -> RibbonGroup:
        group = RibbonGroup(self._ribbon, caption)
        self._groups.append(group)
        # Before the trailing stretch, so groups stay packed to the left.
        self._row.insertWidget(self._row.count() - 1, group)
        # A styled QFrame, not a VLine: the frame shapes take their colour from
        # the palette's mid role, which in some themes is the ground they are
        # drawn on, so a VLine there is invisible.
        divider = QFrame()
        divider.setObjectName("ribbonDivider")
        divider.setFixedWidth(1)
        divider.setContentsMargins(0, 0, 0, 0)
        divider.setStyleSheet(self._divider_margin())
        self._dividers.append(divider)
        self._row.insertWidget(self._row.count() - 1, divider)
        return group


class QuickAccessBar(QWidget):
    """The three or four commands that are worth a click from anywhere.

    Word and SolidWorks both keep one, up level with the title rather than on a
    tab, because save-undo-redo are wanted while you are on whichever tab you
    happen to be on.  These are the same actions as the ones on the tabs; a
    ribbon shows a command in more than one place on purpose.
    """

    def __init__(self, ribbon: Ribbon, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ribbon = ribbon
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 0, 6, 0)
        row.setSpacing(1)

    def add_action(self, action: QAction, icon: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("quickButton")
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setIconSize(QSize(SMALL_ICON_PX + 1, SMALL_ICON_PX + 1))
        button.setFixedSize(24, 22)
        _bind(button, action)
        self._ribbon.register(button, action, icon, SMALL_ICON_PX + 1, primary=False)
        self.layout().addWidget(button)
        return button


class Ribbon(QTabWidget):
    """The whole ribbon.  Tabs across the top, groups of commands beneath.

    The ribbon's height is height the 3D view does not get, and it gives that
    back twice over.  It opens **compact** - one row of small buttons per tab,
    no group captions, 56 px including the tab strip - which is the size the
    thing is meant to be for a program whose subject is the part.  "Large ribbon
    buttons" in the View menu gives the roomy arrangement back for anyone who
    wants labels under icons.  And either way it collapses to its tab strip on a
    double-click or Ctrl+F1, as it does in every program that has one.
    """

    collapsed_changed = Signal(bool)
    dense_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ribbon")
        self.setDocumentMode(True)
        # Expanding, so the ribbon is as wide as the window rather than as wide
        # as its own contents - otherwise the groups stop halfway across and the
        # last of them are simply cut off.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._collapsed = False
        self._dense = True
        #: button -> (icon name, size), so a palette change repaints all of them.
        self._icons: dict[QToolButton, tuple[str, int]] = {}
        #: action -> the button that stands for it, for the tests and the window.
        self._buttons: dict[QAction, QToolButton] = {}
        self.theme = Theme(QApplication.palette())

        self.setFixedHeight(self.expanded_height())
        self.tabBarDoubleClicked.connect(lambda _index: self.toggle_collapsed())

        self._pin = QToolButton()
        self._pin.setObjectName("ribbonPin")
        self._pin.setFixedSize(22, 20)
        self._pin.setIconSize(QSize(14, 14))
        self._pin.clicked.connect(self.toggle_collapsed)
        corner = QWidget()
        row = QHBoxLayout(corner)
        row.setContentsMargins(0, 0, 6, 2)
        row.setSpacing(0)
        row.addWidget(self._pin)
        self.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        self._describe_pin()
        self.apply_theme()

    # -- theme ------------------------------------------------------------

    def apply_theme(self) -> None:
        """Mix the colours from the palette and repaint every icon in them."""
        self.theme = Theme(QApplication.palette())
        self.setStyleSheet(ribbon_stylesheet(self.theme))
        for button, (name, size) in self._icons.items():
            button.setIcon(icons.icon(name, self.theme.icon, self.theme.accent, size))
        self._describe_pin()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        from PySide6.QtCore import QEvent

        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ThemeChange):
            self.apply_theme()

    def remember(self, button: QToolButton, action: QAction) -> None:
        """Note which button stands for *action*, for the tests and the window."""
        self._buttons.setdefault(action, button)

    def reface(self, button: QToolButton, icon: str, size: int) -> None:
        """Give *button* its icon at *size*, and keep it for the next repaint."""
        self._icons[button] = (icon, size)
        button.setIcon(icons.icon(icon, self.theme.icon, self.theme.accent, size))

    def register(
        self, button: QToolButton, action: QAction, icon: str, size: int,
        primary: bool = True,
    ) -> None:
        """Remember a button and give it its icon, in one call."""
        self.reface(button, icon, size)
        if primary:
            self.remember(button, action)

    def button_for(self, action: QAction) -> QToolButton | None:
        """The button that stands for *action* on a tab, if it has one."""
        return self._buttons.get(action)

    def command_buttons(self) -> list[QToolButton]:
        """Every button that runs a command.

        Not findChildren(QToolButton): a QTabBar keeps two scroller buttons of
        its own, and they are not commands.
        """
        return list(self._icons)

    # -- how much room it takes -------------------------------------------

    def expanded_height(self) -> int:
        return COLLAPSED_H + (DENSE_BODY_H if self._dense else ROOMY_BODY_H)

    @property
    def dense(self) -> bool:
        """Whether the ribbon is at its compact size.  It opens that way."""
        return self._dense

    def set_dense(self, dense: bool) -> None:
        """Switch between the compact row and the roomy arrangement.

        Every group re-dresses its own buttons; nothing is rebuilt, so the
        actions keep the connections they already have.
        """
        if bool(dense) == self._dense:
            return
        self._dense = bool(dense)
        for index in range(self.count()):
            tab = self.widget(index)
            if isinstance(tab, RibbonTab):
                tab.apply_mode()
        if not self._collapsed:
            self.setFixedHeight(self.expanded_height())
        self.dense_changed.emit(self._dense)

    # -- collapsing -------------------------------------------------------

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed == self._collapsed:
            return
        self._collapsed = bool(collapsed)
        for index in range(self.count()):
            self.widget(index).setVisible(not self._collapsed)
        self.setFixedHeight(COLLAPSED_H if self._collapsed else self.expanded_height())
        self._describe_pin()
        self.collapsed_changed.emit(self._collapsed)

    def _describe_pin(self) -> None:
        name = "chevron-down" if self._collapsed else "chevron-up"
        self._pin.setIcon(icons.icon(name, self.theme.dim, self.theme.accent, 14))
        self._pin.setToolTip(
            "<b>Show the ribbon</b><br>Or double-click a tab."
            if self._collapsed
            else "<b>Collapse the ribbon</b><br>Or double-click a tab."
        )

    # -- building ---------------------------------------------------------

    def add_tab(self, caption: str) -> RibbonTab:
        tab = RibbonTab(self)
        self.addTab(tab, caption)
        return tab

    def quick_access_bar(self) -> QuickAccessBar:
        return QuickAccessBar(self)


def _short(text: str) -> str:
    """Trim a command name down to what fits under an icon.

    The full name stays on the action, so the tooltip and the menus still read
    in whole words.
    """
    cleaned = text.replace("&", "").removeprefix("+ ").strip()
    words = cleaned.split()
    if len(words) <= 2 and len(cleaned) <= 14:
        return cleaned
    # Two lines of up to twelve characters is what the button has room for.
    first: list[str] = []
    second: list[str] = []
    for word in words:
        target = first if len(" ".join([*first, word])) <= 12 or not first else second
        target.append(word)
    top = " ".join(first)
    bottom = " ".join(second)
    if len(bottom) > 13:
        bottom = bottom[:12] + "…"
    return f"{top}\n{bottom}" if bottom else top


__all__ = [
    "QuickAccessBar",
    "Ribbon",
    "RibbonGroup",
    "RibbonTab",
    "Theme",
    "header_stylesheet",
    "ribbon_stylesheet",
]
