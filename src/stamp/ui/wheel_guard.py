"""The mouse wheel scrolls the panel; it does not change values in passing.

A spin box, a drop-down or a slider takes the wheel whenever the pointer is over
it, so scrolling down a panel of them changes whatever it passes under: a depth,
a rotation, an operation.  Nothing says it happened, and it is the kind of edit
that is noticed three steps later.

So a control only answers the wheel once it has been chosen - clicked into, or
tabbed to.  Until then the wheel belongs to whatever the panel is sitting in,
which is where it is sent, so the panel scrolls as it should.  The guard is one
filter on the application, so it covers the panel, the ribbon and every dialog,
including ones written later.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
)

#: The widgets that change value on a wheel they were never given.
GUARDED = (QAbstractSpinBox, QComboBox, QAbstractSlider)

_installed: WheelGuard | None = None


class WheelGuard(QObject):
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt
        if event.type() is not QEvent.Type.Wheel or not isinstance(watched, GUARDED):
            return False
        if watched.hasFocus():
            return False  # chosen deliberately, so it behaves as it always did
        # Qt stops propagating an event a filter has taken, so the panel behind
        # the field is scrolled here instead - otherwise a panel would not
        # scroll at all where its fields cover it.  Scrolled rather than handed
        # the event: a scroll area sends a wheel it is given back down to the
        # child under the pointer, which is this field again.
        _scroll_behind(watched, event)
        return True


def _scroll_behind(widget, event) -> None:
    """Scroll the panel the widget sits in, by what Qt would have scrolled it."""
    bar = _scrollbar_over(widget)
    notches = event.angleDelta().y() / 120.0
    if bar is None or not notches:
        return
    lines = QApplication.wheelScrollLines() or 3
    bar.setValue(bar.value() - round(notches * lines * max(bar.singleStep(), 1)))


def _scrollbar_over(widget):
    """The bar of the first scroll area above *widget* that has room to move."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            bar = parent.verticalScrollBar()
            if bar is not None and bar.minimum() != bar.maximum():
                return bar
            # An inner area with nothing to scroll is not the end of the search:
            # a panel inside a scrollable panel is still meant to scroll.
        parent = parent.parentWidget()
    return None


def install(app: QApplication | None = None) -> WheelGuard | None:
    """Guard every value widget in *app*.  Installing twice does nothing."""
    global _installed

    app = app or QApplication.instance()
    if app is None or _installed is not None:
        return _installed
    _installed = WheelGuard(app)
    app.installEventFilter(_installed)
    return _installed


def wheel_focus_policy(widget) -> None:
    """Stop a wheel *giving* a widget focus, which would arm the next scroll."""
    if widget.focusPolicy() == Qt.FocusPolicy.WheelFocus:
        widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)


__all__ = ["GUARDED", "WheelGuard", "install", "wheel_focus_policy"]
