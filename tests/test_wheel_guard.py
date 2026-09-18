"""The wheel scrolls; it does not edit in passing - spec §9.3.

Scrolling a panel of spin boxes changed whatever the pointer happened to cross -
a depth here, a rotation there - with nothing said about it.  A control answers
the wheel only once it has been chosen.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QComboBox, QScrollArea, QVBoxLayout, QWidget

from stamp.ui import wheel_guard
from stamp.ui.properties import NumberField


def a_wheel(widget, notches: int = -1) -> QWheelEvent:
    """A wheel roll over the middle of *widget*."""
    where = QPointF(widget.rect().center())
    return QWheelEvent(
        where,
        QPointF(widget.mapToGlobal(widget.rect().center())),
        QPoint(0, 0),
        QPoint(0, notches * 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


@pytest.fixture(autouse=True)
def guard(qtbot):
    return wheel_guard.install(QApplication.instance())


class TestAFieldNobodyChose:
    def test_the_wheel_leaves_a_spin_box_alone(self, qtbot):
        field = NumberField()
        qtbot.addWidget(field)
        field.setValue(2.5)

        QApplication.sendEvent(field, a_wheel(field))

        assert field.value() == pytest.approx(2.5)

    def test_the_wheel_leaves_a_drop_down_alone(self, qtbot):
        box = QComboBox()
        box.addItems(["Cut material", "Add material", "Colour stamp"])
        qtbot.addWidget(box)

        QApplication.sendEvent(box, a_wheel(box))

        assert box.currentIndex() == 0

    def test_a_wheel_cannot_hand_a_field_the_focus_it_needs(self, qtbot):
        """Otherwise the first roll arms the field and the second one edits it."""
        field = NumberField()
        qtbot.addWidget(field)
        field.show()
        qtbot.waitExposed(field)
        field.setValue(2.5)

        QApplication.sendEvent(field, a_wheel(field))
        QApplication.sendEvent(field, a_wheel(field))

        assert not field.hasFocus()
        assert field.value() == pytest.approx(2.5)


class ChosenField(NumberField):
    """A field that holds the focus, whatever the window manager thinks.

    A bare X display has nothing to give a window focus, so asking for it there
    and waiting is a test that hangs rather than one that checks anything.  What
    matters is the guard's rule: a field with the focus keeps its wheel.
    """

    def hasFocus(self) -> bool:  # noqa: N802 - Qt naming
        return True


class TestAFieldThatWasChosen:
    def test_the_wheel_works_on_the_field_with_the_focus(self, qtbot):
        field = ChosenField(step=0.5)
        qtbot.addWidget(field)
        field.setValue(2.5)

        QApplication.sendEvent(field, a_wheel(field, notches=1))

        assert field.value() == pytest.approx(3.0)

    def test_it_still_edits_inside_a_panel_that_could_scroll(self, qtbot):
        """The scroll-instead path must not steal the wheel from a chosen field."""
        area = QScrollArea()
        inner = QWidget()
        layout = QVBoxLayout(inner)
        field = ChosenField(step=0.5)
        field.setValue(2.5)
        layout.addWidget(field)
        for _ in range(40):
            layout.addWidget(NumberField())
        area.setWidget(inner)
        area.setWidgetResizable(True)
        area.resize(220, 180)
        qtbot.addWidget(area)
        area.show()
        qtbot.waitExposed(area)
        bar = area.verticalScrollBar()
        qtbot.waitUntil(lambda: bar.maximum() > 0, timeout=2000)
        before = bar.value()

        QApplication.sendEvent(field, a_wheel(field, notches=1))

        assert field.value() == pytest.approx(3.0)
        assert bar.value() == before


class TestThePanelStillScrolls:
    def test_a_wheel_over_a_field_scrolls_the_panel_behind_it(self, qtbot):
        """The whole point of taking the wheel away from the field."""
        area = QScrollArea()
        inner = QWidget()
        layout = QVBoxLayout(inner)
        fields = [NumberField() for _ in range(40)]
        for field in fields:
            layout.addWidget(field)
        area.setWidget(inner)
        area.setWidgetResizable(True)
        area.resize(220, 180)
        qtbot.addWidget(area)
        area.show()
        qtbot.waitExposed(area)
        bar = area.verticalScrollBar()
        qtbot.waitUntil(lambda: bar.maximum() > 0, timeout=2000)
        before = bar.value()

        QApplication.sendEvent(fields[2], a_wheel(fields[2]))

        assert bar.value() > before
        assert fields[2].value() == pytest.approx(0.0)
