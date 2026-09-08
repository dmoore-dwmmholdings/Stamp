"""The interface - spec §6.2, §6.6, §7.

These drive the real widgets.  The viewport needs a real window and an OpenGL
context, so the tests that build a whole window are marked and skipped when no
display is available.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt  # noqa: E402

from stamp.core.document import (  # noqa: E402
    Anchor,
    AnchorKind,
    DepthMode,
    Direction,
    Document,
    Feature,
    Modifier,
    ModifierKind,
    Operation,
    OperationKind,
    Placement,
    Plane,
    ProfileRef,
)

HEADLESS = os.environ.get("QT_QPA_PLATFORM") == "offscreen"
needs_gl = pytest.mark.skipif(HEADLESS, reason="the OCC viewport needs a real window")


def a_feature(name: str = "Logo") -> Feature:
    return Feature(
        name=name,
        profile=ProfileRef(source_path="logo.svg", source_hash="abc",
                           native_size_mm=(36.0, 16.0)),
        placement=Placement(
            anchor=Anchor(
                kind=AnchorKind.FACE,
                plane=Plane(origin=(30.0, 20.0, 8.0), normal=(0.0, 0.0, 1.0),
                            u_axis=(1.0, 0.0, 0.0)),
            )
        ),
        operation=Operation(kind=OperationKind.CUT, depth=0.5),
    )


class TestViewportWindowBinding:
    @pytest.mark.skipif(sys.platform != "linux", reason="covers the Linux X11 binding")
    def test_linux_window_binding_receives_an_integer_handle(self, monkeypatch, qtbot):
        """OCP 7.9's X11 wrapper rejects the PyCapsule used by older bindings."""
        import OCP.Xw as xw

        from stamp.ui import viewport as viewport_module
        from stamp.ui.viewport import Viewport

        captured: dict[str, object] = {}
        sentinel = object()

        def fake_window(connection, handle):
            captured["connection"] = connection
            captured["handle"] = handle
            return sentinel

        monkeypatch.setattr(viewport_module.platform, "system", lambda: "Linux")
        monkeypatch.setattr(Viewport, "winId", lambda _self: 12345)
        monkeypatch.setattr(xw, "Xw_Window", fake_window)

        widget = Viewport()
        qtbot.addWidget(widget)
        widget._display_connection = object()

        assert widget._make_window() is sentinel
        assert captured["handle"] == 12345
        assert type(captured["handle"]) is int

    @pytest.mark.skipif(sys.platform != "win32", reason="covers the Windows HWND binding")
    def test_windows_window_binding_receives_a_pointer_capsule(self, monkeypatch, qtbot):
        """OCP 7.9 binds the HWND as a pointer, so an int is refused."""
        import ctypes

        import OCP.WNT as wnt

        from stamp.ui import viewport as viewport_module
        from stamp.ui.viewport import Viewport

        captured: dict[str, object] = {}
        sentinel = object()

        def fake_window(handle):
            captured["handle"] = handle
            return sentinel

        monkeypatch.setattr(viewport_module.platform, "system", lambda: "Windows")
        monkeypatch.setattr(Viewport, "winId", lambda _self: 12345)
        monkeypatch.setattr(wnt, "WNT_Window", fake_window)

        widget = Viewport()
        qtbot.addWidget(widget)

        assert widget._make_window() is sentinel
        handle = captured["handle"]
        assert type(handle).__name__ == "PyCapsule"
        get = ctypes.pythonapi.PyCapsule_GetPointer
        get.restype = ctypes.c_void_p
        get.argtypes = [ctypes.py_object, ctypes.c_char_p]
        assert get(handle, None) == 12345

    def test_a_viewer_that_cannot_start_reports_once_and_stops_trying(self, qtbot):
        """The failure used to repaint, raise, and open another error box forever."""
        from stamp.ui.viewport import Viewport

        widget = Viewport()
        qtbot.addWidget(widget)

        attempts: list[int] = []

        def explode() -> None:
            attempts.append(1)
            raise RuntimeError("no GL here")

        widget._start_viewer = explode
        reasons: list[str] = []
        widget.init_failed.connect(reasons.append)

        widget._init_viewer()
        widget._init_viewer()
        widget.paintEvent(None)

        assert len(attempts) == 1
        assert reasons == ["RuntimeError: no GL here"]
        assert widget.context is None
        # A dead viewport shows nothing rather than raising on every later call.
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

        box = BRepPrimAPI_MakeBox(1.0, 1.0, 1.0).Shape()
        assert widget.display_shape("part", box) is None
    def test_a_scaled_display_picks_where_the_user_clicked(self, qtbot, monkeypatch):
        """OCC's view is sized in device pixels; a Qt click arrives in logical ones.

        At 150% - the ordinary Windows laptop setting - handing one to the other put
        every click a third of the way up and left of the cursor.
        """
        from PySide6.QtCore import QPoint

        from stamp.ui.viewport import Viewport

        widget = Viewport()
        qtbot.addWidget(widget)
        monkeypatch.setattr(Viewport, "devicePixelRatioF", lambda _self: 1.5)

        moves: list[tuple[int, int]] = []

        class FakeContext:
            def MoveTo(self, x, y, view, update):  # noqa: N802 - OCC naming
                moves.append((x, y))

            def Select(self, _update):  # noqa: N802
                pass

            def HasDetected(self):  # noqa: N802
                return False

        widget.context = FakeContext()
        widget.view = object()
        widget._do_pick(QPoint(100, 200), additive=False)

        assert moves == [(150, 300)]
        assert widget.logical_px(150) == pytest.approx(100)


class TestTheRibbon:
    """The command ribbon - spec §7.

    The bottom toolbar held thirty commands, overflowed at any ordinary window
    size, and moved the overflow into a chevron menu that shut again at every
    layout pass.  These cover what the ribbon has to promise instead: every
    command reachable, and one enabled state shared with the menus.
    """

    @pytest.fixture
    def window(self, qtbot):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        return win

    def _buttons(self, window):
        return window.ribbon.command_buttons()

    def test_every_command_is_on_a_tab(self, window):
        """The old bar could not promise this; that is why the menus exist."""
        for action in (
            window.action_open_part, window.action_save, window.action_add_profile,
            window.action_export_step, window.action_export_3mf,
            window.action_export_package, window.action_batch,
            window.action_align_edge, window.action_create_datum,
            window.action_undo, window.action_redo,
            window.action_mirror, window.action_scale_part,
            window.action_reset_transform, window.action_show_transformed,
        ):
            assert window.ribbon.button_for(action) is not None, action.text()

    def test_the_transform_commands_are_on_the_ribbon_and_still_work(
        self, window, fixtures
    ):
        """Mirror and scale arrived after the ribbon did, and a command that is
        only in a menu is the thing the ribbon exists to stop."""
        from stamp.core.document import MirrorPlane

        window.open_part(fixtures / "bracket.step")
        window.ribbon.button_for(window.action_mirror).menu().actions()[0].trigger()
        assert window.document.transform.mirror is MirrorPlane.YZ

        window.action_reset_transform.trigger()
        assert window.document.transform.is_identity

    def test_the_mirror_button_drops_the_three_planes(self, window):
        """A part can be mirrored across two planes at once, so they are toggles
        under one button rather than three commands taking three slots."""
        menu = window.ribbon.button_for(window.action_mirror).menu()
        planes = [a for a in menu.actions() if a.isCheckable()]
        assert len(planes) == 3
        assert all(not a.isChecked() for a in planes)

    def test_the_tabs_are_named_for_what_they_hold(self, window):
        names = [window.ribbon.tabText(i) for i in range(window.ribbon.count())]
        assert names == ["Home", "Place", "Export", "View"]

    def test_a_button_carries_a_short_caption_and_the_full_name(self, window):
        """A button is narrower than most command names.  Left to elide, "Insert
        stamp preset" reads as "Insert ...reset"."""
        button = self._button_for(window, window.action_insert_preset)
        assert button.text() == "Insert preset"
        assert "Insert stamp preset" in button.toolTip()
        window.ribbon.set_dense(False)
        assert button.text() == "Insert\npreset"

    def test_a_tooltip_leads_with_the_name_and_the_shortcut(self, window):
        """A tooltip that repeats the caption teaches nothing."""
        tip = self._button_for(window, window.action_save).toolTip()
        assert tip.startswith("<b>Save</b>")
        assert "Ctrl+S" in tip

    def test_every_button_has_an_icon_that_was_drawn(self, window):
        """Emoji were the old stand-in: some render in colour, some as a box, and
        the grey they fell back to was invisible on a light desktop theme."""
        buttons = self._buttons(window)
        assert buttons
        for button in buttons:
            assert not button.icon().isNull(), button.text()

    def test_the_secondary_commands_are_small_buttons_when_it_is_roomy(self, window):
        """A group whose commands are all one size has no shape."""
        from stamp.ui.ribbon import BUTTON_H, DENSE_H, SMALL_H

        undo = self._button_for(window, window.action_undo)
        add_text = self._button_for(window, window.action_add_text)
        assert undo.height() == add_text.height() == DENSE_H  # compact: one size

        window.ribbon.set_dense(False)
        assert undo.height() == SMALL_H
        assert add_text.height() == BUTTON_H

    def test_save_undo_and_redo_are_also_on_the_quick_access_bar(self, window):
        """They are wanted from whichever tab you are on."""
        from PySide6.QtWidgets import QToolButton

        quick = window.quick_access.findChildren(QToolButton)
        assert len(quick) == 3
        fired = []
        window.action_redo.triggered.connect(lambda: fired.append(1))
        window.action_redo.setEnabled(True)
        quick[2].click()
        assert fired == [1]

    def test_the_buttons_survive_a_change_of_size(self, window, qtbot):
        """Re-parenting a widget hides it, and a button hidden that way stays
        hidden however tidy the new layout is."""
        window.show()
        qtbot.waitExposed(window)
        button = self._button_for(window, window.action_add_text)
        for dense in (False, True):
            window.ribbon.set_dense(dense)
            assert button.isVisible(), f"lost the button at dense={dense}"
            assert button.icon().isNull() is False

    def test_the_view_menu_offers_the_roomy_layout(self, window):
        assert not window.action_large_ribbon.isChecked()
        assert window.ribbon.dense

    def test_disabling_the_command_disables_its_button(self, window):
        """One command, two views.  The window enables actions, not buttons."""
        button = self._button_for(window, window.action_export_step)
        window.action_export_step.setEnabled(False)
        assert not button.isEnabled()
        window.action_export_step.setEnabled(True)
        assert button.isEnabled()

    def test_a_checkable_command_and_its_button_agree(self, window):
        button = self._button_for(window, window.action_draft)
        assert button.isCheckable()
        window.action_draft.setChecked(True)
        assert button.isChecked()
        button.setChecked(False)
        assert not window.action_draft.isChecked()

    def test_clicking_a_button_runs_the_command(self, window, qtbot):
        """Deliberately not Batch: its first line is a QFileDialog, which is
        modal, does not consult `interactive`, and under Xvfb never comes back -
        so a test that clicks it wedges the whole container run rather than
        failing.  Fit to window is a real command that only touches the camera."""
        fired = []
        window.action_fit.triggered.connect(lambda: fired.append(1))
        self._button_for(window, window.action_fit).click()
        assert fired == [1]

    def test_a_narrow_window_hides_nothing(self, window, qtbot):
        """The old bar's answer to a small window was to take commands away."""
        window.resize(900, 700)
        window.show()
        qtbot.waitExposed(window)
        assert len(self._buttons(window)) >= 20

    @staticmethod
    def _button_for(window, action):
        button = window.ribbon.button_for(action)
        if button is None:
            raise AssertionError(f"no ribbon button for {action.text()!r}")
        return button


class TestViewControl:
    """Getting the camera where you want it - spec §7.

    A combo box is two clicks to a standard view and nothing at all to a rotation.
    These cover the four ways round that: the navigation cube, the arrow keys,
    roll, and looking straight down a face.
    """

    @pytest.fixture
    def view(self, qtbot):
        from stamp.ui.viewport import Viewport

        widget = Viewport()
        qtbot.addWidget(widget)
        widget.show()
        qtbot.waitExposed(widget)
        if widget.view is None:  # pragma: no cover - no GL on this machine
            pytest.skip("no GL context available")
        return widget

    def test_the_cube_is_there_to_click(self, view):
        assert view._cube is not None

    def test_an_arrow_key_turns_the_part(self, view):
        from PySide6.QtCore import Qt as QtNs
        from PySide6.QtGui import QKeyEvent

        before = tuple(view.view.Proj())
        view.keyPressEvent(
            QKeyEvent(QKeyEvent.Type.KeyPress, QtNs.Key.Key_Right,
                      QtNs.KeyboardModifier.NoModifier)
        )
        after = tuple(view.view.Proj())
        assert any(abs(a - b) > 1e-3 for a, b in zip(before, after, strict=True))

    def test_shift_takes_a_quarter_turn(self, view):
        """Fifteen degrees a press is for aiming; ninety is for getting there."""
        from PySide6.QtCore import Qt as QtNs
        from PySide6.QtGui import QKeyEvent

        def turn(mods):
            view.set_preset_view("front")
            before = tuple(view.view.Proj())
            view.keyPressEvent(
                QKeyEvent(QKeyEvent.Type.KeyPress, QtNs.Key.Key_Right, mods)
            )
            after = tuple(view.view.Proj())
            return sum((a - b) ** 2 for a, b in zip(before, after, strict=True)) ** 0.5

        small = turn(QtNs.KeyboardModifier.NoModifier)
        large = turn(QtNs.KeyboardModifier.ShiftModifier)
        assert large > small

    def test_roll_spins_the_view_about_what_it_looks_at(self, view):
        import math

        before = view.view.Twist()
        view.roll_by(15.0)
        assert math.degrees(view.view.Twist() - before) == pytest.approx(15.0, abs=0.01)

    def test_alt_and_an_arrow_rolls_instead_of_turning(self, view):
        from PySide6.QtCore import Qt as QtNs
        from PySide6.QtGui import QKeyEvent

        before = view.view.Twist()
        view.keyPressEvent(
            QKeyEvent(QKeyEvent.Type.KeyPress, QtNs.Key.Key_Left,
                      QtNs.KeyboardModifier.AltModifier)
        )
        assert view.view.Twist() != before

    def test_looking_along_a_normal_points_the_camera_down_it(self, view):
        view.look_along((0.0, 0.0, 1.0))
        assert tuple(round(v, 3) for v in view.view.Proj()) == (0.0, 0.0, 1.0)

    def test_a_normal_of_nothing_is_ignored(self, view):
        """A face reference that never resolved must not send the camera nowhere."""
        before = tuple(view.view.Proj())
        view.look_along((0.0, 0.0, 0.0))
        assert tuple(view.view.Proj()) == before


class TestTheRibbonCollapses:
    """The ribbon's height is height the 3D view does not get."""

    @pytest.fixture
    def ribbon(self, qtbot):
        from PySide6.QtGui import QAction

        from stamp.ui.ribbon import Ribbon

        widget = Ribbon()
        tab = widget.add_tab("Home")
        group = tab.add_group("Project")
        group.add_action(QAction("Open part", widget), "open-part", "Open\npart")
        group.add_small_action(QAction("Save", widget), "save")
        qtbot.addWidget(widget)
        return widget

    def test_it_starts_open(self, ribbon):
        assert not ribbon.collapsed
        assert ribbon.height() == ribbon.expanded_height()

    def test_collapsing_leaves_only_the_tab_strip(self, ribbon):
        from stamp.ui.ribbon import COLLAPSED_H

        ribbon.set_collapsed(True)
        assert ribbon.collapsed
        assert ribbon.height() == COLLAPSED_H

    def test_double_clicking_a_tab_toggles_it(self, ribbon):
        ribbon.tabBarDoubleClicked.emit(0)
        assert ribbon.collapsed
        ribbon.tabBarDoubleClicked.emit(0)
        assert not ribbon.collapsed

    def test_it_says_when_it_changed(self, ribbon):
        seen = []
        ribbon.collapsed_changed.connect(seen.append)
        ribbon.set_collapsed(True)
        ribbon.set_collapsed(True)  # already there; not a change
        assert seen == [True]

    def test_the_open_ribbon_is_shorter_than_it_used_to_be(self, ribbon):
        """126 px of chrome for thirty commands was the complaint, twice over.

        The answer the second time was to open compact: one row of small
        buttons, no captions.  Everything is still on a tab; it is the labels
        under the icons that go, and they come back from the View menu."""
        assert ribbon.dense
        assert ribbon.expanded_height() <= 60

    def test_the_roomy_layout_is_there_for_anyone_who_wants_it(self, ribbon):
        ribbon.set_dense(False)
        assert ribbon.expanded_height() > 90
        assert ribbon.height() == ribbon.expanded_height()
        ribbon.set_dense(True)
        assert ribbon.height() == ribbon.expanded_height() <= 60

    def test_changing_size_says_so(self, ribbon):
        seen = []
        ribbon.dense_changed.connect(seen.append)
        ribbon.set_dense(False)
        ribbon.set_dense(False)  # already there; not a change
        assert seen == [False]

    def test_a_collapsed_ribbon_stays_collapsed_when_the_size_changes(self, ribbon):
        from stamp.ui.ribbon import COLLAPSED_H

        ribbon.set_collapsed(True)
        ribbon.set_dense(False)
        assert ribbon.height() == COLLAPSED_H


class TestTheColourPreview:
    """A colour stamp is only a layer or two deep, so the translucent solid over
    it is nearly flat against the face and says very little about where the
    artwork is.  In its own colours it says it at a glance.

    The engine is driven directly rather than through the window: the window's
    rebuild is asynchronous, and what is under test here is the drawing, not the
    plumbing that gets a result to it.
    """

    @pytest.fixture
    def window(self, qtbot):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        return win

    @pytest.fixture
    def part(self, fixtures):
        """A part of this test's own, not the session-scoped one.

        Closing a window releases its geometry - ``closeEvent`` sets
        ``document.base.runtime`` to None to keep nanobind quiet about leaks on
        exit - so handing the shared fixture to a window that qtbot will close
        empties it for every test that runs afterwards.
        """
        from stamp.io.part_import import import_part

        return import_part(fixtures / "bracket.step").part

    def _stamped(self, window, bracket_step, art):
        from stamp.core.document import (
            Anchor,
            AnchorKind,
            DepthMode,
            Direction,
            Feature,
            Operation,
            OperationKind,
            Placement,
            ProfileRef,
        )
        from stamp.core.rebuild import RebuildEngine
        from stamp.core.refs import FaceRef
        from stamp.io.profile_import import file_hash

        window.document.base = bracket_step
        feature = Feature(
            name="Logo",
            profile=ProfileRef(source_path=str(art), source_hash=file_hash(art)),
            placement=Placement(anchor=Anchor(kind=AnchorKind.FACE, face_ref=FaceRef(
                point=(40.0, 20.0, 14.0), normal=(0.0, 0.0, 1.0), surface_type="plane"))),
            operation=Operation(kind=OperationKind.COLOR, depth_mode=DepthMode.BLIND,
                                depth=0.2, direction=Direction.INTO),
        )
        window.document.add_feature(feature)
        result = RebuildEngine(window.profiles.get).rebuild(window.document)
        assert result.ok
        row = result.result_for(feature.id)
        assert row is not None and row.tool is not None
        return feature, row

    def test_a_two_colour_stamp_previews_in_two_colours(self, window, fixtures, part):
        feature, row = self._stamped(window, part, fixtures / "two_color.svg")
        assert window._show_component_footprints(feature, row) is True
        assert len(window._component_footprint_keys) == 2

    def test_one_colour_keeps_the_single_decal(self, window, fixtures, part):
        """The usual case must stay exactly as cheap as it was."""
        feature, row = self._stamped(window, part, fixtures / "logo.svg")
        assert window._show_component_footprints(feature, row) is False
        assert window._component_footprint_keys == []


class TestTheUpdateBar:
    """Telling somebody a new Stamp exists - see :mod:`stamp.update`.

    A bar rather than a dialog, because an update is never urgent enough to
    interrupt somebody halfway through placing a stamp on a face.  These cover
    the rules that matter: it is silent until there is something to say, it
    never installs over unsaved work, and building a window never touches the
    network.

    These ask isHidden() rather than isVisible() where the window itself is
    not shown: a widget in a hidden window is not visible however loudly it
    asked to be, and the question here is whether the bar put itself up.
    """

    @pytest.fixture
    def window(self, qtbot):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        return win

    @staticmethod
    def _release(version="9.9.9", urgent=False, artifact=None):
        from stamp.update import Release

        return Release(
            version=version,
            notes_url="https://example.invalid/notes",
            artifact=artifact,
            urgent=urgent,
        )

    def test_it_says_nothing_until_there_is_something_to_say(self, window):
        assert window.update_bar.isHidden()

    def test_building_a_window_checks_nothing(self, window, monkeypatch):
        """A test builds a window.  A test must not make a network call."""
        called = []
        monkeypatch.setattr(
            "stamp.update.check", lambda *a, **k: called.append(1) or None
        )
        window.begin_update_check()  # interactive is False
        assert called == []

    def test_no_thread_is_started_until_something_asks_for_one(self, window):
        """Every test builds a window, and a thread nobody asked for is one that
        has to be shut down correctly on a path nobody exercises."""
        assert not window.updater.running

    def test_a_found_release_is_offered(self, window, qtbot):
        window.show()
        qtbot.waitExposed(window)
        window._on_update_found(self._release())
        assert window.update_bar.isVisible()
        assert "9.9.9" in window.update_bar.message.text()

    def test_a_skipped_version_is_not_offered_again(self, window):
        window._update_announce = False
        window.settings.setValue("update/skipped_version", "9.9.9")
        try:
            window._on_update_found(self._release())
            assert window.update_bar.isHidden()
        finally:
            window.settings.remove("update/skipped_version")

    def test_a_withdrawn_version_is_offered_even_if_skipped(self, window):
        """Skipping is a preference; being told to stop using a build is not."""
        window._update_announce = False
        window.settings.setValue("update/skipped_version", "9.9.9")
        try:
            window._on_update_found(self._release(urgent=True))
            assert not window.update_bar.isHidden()
        finally:
            window.settings.remove("update/skipped_version")

    def test_a_quiet_check_that_fails_stays_quiet(self, window):
        """A laptop on a train is not the user's problem to be told about."""
        window._update_announce = False
        window._on_update_failed("no route to host")
        assert window.update_bar.isHidden()

    def test_a_check_the_user_asked_for_reports_a_failure(self, window):
        window._update_announce = True
        window._on_update_failed("no route to host")
        assert not window.update_bar.isHidden()
        assert "no route to host" in window.update_bar.message.text()

    def test_it_will_not_install_over_unsaved_work(self, window, monkeypatch):
        """Closing to install and losing the part is the one unforgivable bug."""
        started = []
        monkeypatch.setattr("stamp.update.install", lambda *a, **k: started.append(1))
        monkeypatch.setattr(window, "_confirm", lambda *a: False)
        window._update_installer = Path("nowhere.exe")
        window._dirty = True
        window.document.base = object()
        try:
            window._apply_update(now=True)
        finally:
            window.document.base = None
        assert started == []

    def test_install_when_i_quit_waits_for_the_quit(self, window, monkeypatch):
        started = []
        monkeypatch.setattr("stamp.update.install", lambda *a, **k: started.append(1))
        window._update_installer = Path("nowhere.exe")
        window._on_update_later()
        assert window.update_bar.isHidden()
        assert started == []
        window.close()
        assert started == [1]

    def test_the_automatic_check_matches_the_readme_s_once_a_day(
        self, window, monkeypatch
    ):
        """W2: the window was 20 hours, so an automatic check ran twice in a day
        for anybody who opens Stamp at roughly the same time each morning."""
        import time

        monkeypatch.setattr("stamp.update.is_configured", lambda: True)
        window.interactive = True
        window.settings.setValue("update/check_automatically", True)
        checked = []
        window._check_for_updates = lambda announce: checked.append(announce)
        try:
            window.settings.setValue("update/last_check", int(time.time()) - 21 * 60 * 60)
            window.begin_update_check()
            assert checked == []

            window.settings.setValue("update/last_check", int(time.time()) - 25 * 60 * 60)
            window.begin_update_check()
            assert checked == [False]
        finally:
            window.settings.remove("update/last_check")
            window.settings.remove("update/check_automatically")
            window.interactive = False

    def test_the_installer_hash_is_carried_to_the_install(self, window, monkeypatch):
        """W3: install re-checks the hash, and without one it refuses to run the
        file - so "install when I quit" could never work."""
        from stamp.update import Artifact

        artifact = Artifact(
            name="Stamp-Setup.exe",
            url="https://example.invalid/Stamp-Setup.exe",
            size=10,
            sha256="ab" * 32,
        )
        window._update_release = self._release(artifact=artifact)

        seen = {}
        monkeypatch.setattr(
            "stamp.update.install",
            lambda path, **kwargs: seen.update(kwargs) or seen.update(path=path),
        )
        window._on_update_ready("C:\\Temp\\Stamp-Setup.exe")
        assert window._update_sha256 == "ab" * 32

        window._apply_update(now=False)
        assert seen["sha256"] == "ab" * 32

    def test_the_download_asks_for_no_directory_of_its_own(self, window, monkeypatch):
        """update.download makes a fresh directory with permissions of its own;
        a guessable %TEMP%\\stamp-update is where an installer gets swapped."""
        from stamp.update import Artifact

        artifact = Artifact(
            name="Stamp-Setup.exe",
            url="https://example.invalid/Stamp-Setup.exe",
            size=10,
            sha256="ab" * 32,
        )
        window._update_release = self._release(artifact=artifact)
        asked = []
        monkeypatch.setattr(window.updater, "fetch", lambda *a, **k: asked.append((a, k)))
        window._on_update_install()
        assert asked and len(asked[0][0]) == 1 and not asked[0][1]

    def test_the_menu_switch_is_remembered(self, window):
        before = window.settings.value("update/check_automatically", False, type=bool)
        try:
            window.action_auto_updates.setChecked(True)
            assert window.settings.value(
                "update/check_automatically", False, type=bool
            )
        finally:
            window.settings.setValue("update/check_automatically", before)


class TestTheIcons:
    """The drawn icon set - see :mod:`stamp.ui.icons`.

    The set replaced emoji, which rendered in colour on one machine and as a box
    with a hex number in it on the next, and whose grey fallback was white on
    white against a light desktop theme.  These check the two things that made
    that fail: that something is actually drawn, and that it is drawn in the
    colours it was asked for rather than a colour baked into the code.
    """

    @pytest.fixture(autouse=True)
    def _app(self, qtbot):
        return qtbot

    def test_every_icon_draws_something(self):
        from PySide6.QtGui import QColor

        from stamp.ui import icons

        for name in icons.names():
            image = icons.pixmap(
                name, 24, QColor("#202020"), QColor("#1a73e8"), ratio=2
            ).toImage()
            painted = sum(
                1
                for y in range(image.height())
                for x in range(image.width())
                if image.pixelColor(x, y).alpha() > 40
            )
            assert painted > 60, f"{name} is all but blank"

    def test_an_icon_is_drawn_in_the_colour_it_is_given(self):
        """The old fallback painted a fixed light grey, so on a light desktop
        theme the ribbon's icons were white on white."""
        from PySide6.QtGui import QColor

        from stamp.ui import icons

        image = icons.pixmap(
            "save", 24, QColor("#c81e2d"), QColor("#c81e2d"), ratio=2
        ).toImage()
        hues = {
            image.pixelColor(x, y).hue()
            for y in range(image.height())
            for x in range(image.width())
            if image.pixelColor(x, y).alpha() > 200
        }
        assert hues and all(abs(hue - 355) < 12 for hue in hues if hue >= 0)

    def test_a_light_theme_and_a_dark_one_get_different_ink(self):
        from PySide6.QtGui import QColor, QPalette

        from stamp.ui.ribbon import Theme

        light = QPalette()
        light.setColor(QPalette.ColorRole.Window, QColor("#f0f0f0"))
        light.setColor(QPalette.ColorRole.WindowText, QColor("#101010"))
        dark = QPalette()
        dark.setColor(QPalette.ColorRole.Window, QColor("#2b2b2b"))
        dark.setColor(QPalette.ColorRole.WindowText, QColor("#e8e8e8"))

        assert not Theme(light).dark
        assert Theme(dark).dark
        # The ink has to stand off the ribbon's own ground at both ends.
        for palette in (light, dark):
            theme = Theme(palette)
            contrast = abs(
                (0.299 * theme.icon.red() + 0.587 * theme.icon.green()
                 + 0.114 * theme.icon.blue())
                - (0.299 * theme.body.red() + 0.587 * theme.body.green()
                   + 0.114 * theme.body.blue())
            )
            assert contrast > 100


class TestArtworkColours:
    """Per-component colour on the properties panel - spec §9."""

    @pytest.fixture
    def panel(self, qtbot):
        from stamp.ui.properties import PropertiesPanel

        widget = PropertiesPanel()
        qtbot.addWidget(widget)
        return widget

    def _two_color(self, fixtures):
        from stamp.io.profile_import import import_profile

        return import_profile(fixtures / "two_color.svg").profile

    def test_one_component_shows_no_colour_list(self, panel, fixtures):
        """A single swatch that changes nothing is worse than no box at all."""
        from stamp.io.profile_import import import_profile

        feature = a_feature()
        panel.show_feature(
            Document(), feature, (36.0, 16.0),
            profile=import_profile(fixtures / "logo.svg").profile,
        )
        assert not panel._components.isVisible()

    def test_several_components_are_listed(self, panel, fixtures, qtbot):
        from PySide6.QtWidgets import QLabel

        feature = a_feature()
        panel.show_feature(Document(), feature, (28.0, 12.0), profile=self._two_color(fixtures))
        labels = [
            w.text() for w in panel._component_rows.findChildren(QLabel)
        ]
        assert "Black" in labels
        assert "Red" in labels

    def test_setting_one_colour_leaves_the_others_alone(self, panel, fixtures):
        feature = a_feature()
        panel.show_feature(Document(), feature, (28.0, 12.0), profile=self._two_color(fixtures))
        feature.component_colors["#ff0000"] = "#e4002b"
        assert feature.color_for("#ff0000", "#111111") == "#e4002b"
        assert feature.color_for("#000000", "#111111") == "#111111"

    def test_resetting_puts_it_all_back_to_one_colour(self, panel, fixtures):
        feature = a_feature()
        feature.component_colors = {"#ff0000": "#e4002b"}
        panel.show_feature(Document(), feature, (28.0, 12.0), profile=self._two_color(fixtures))
        changes = []
        panel.changed.connect(changes.append)

        panel._on_components_reset()
        assert feature.component_colors == {}
        assert changes == ["artwork colour"]

    def test_the_dropped_backdrop_is_named_with_an_offer_to_keep_it(
        self, panel, fixtures
    ):
        from stamp.io.profile_import import import_profile

        result = import_profile(fixtures / "background.svg")
        feature = a_feature()
        panel.show_feature(Document(), feature, (28.0, 12.0), profile=result.profile)

        assert panel.keep_background.isVisible() or not panel.isVisible()
        assert "backdrop" in panel.background_note.text()
        assert not panel.keep_background.isChecked()

    def test_keeping_the_backdrop_asks_for_a_reimport(self, panel, fixtures):
        from stamp.io.profile_import import import_profile

        feature = a_feature()
        panel.show_feature(
            Document(), feature, (28.0, 12.0),
            profile=import_profile(fixtures / "background.svg").profile,
        )
        changes = []
        panel.changed.connect(changes.append)

        panel.keep_background.setChecked(True)
        assert feature.profile.keep_background is True
        assert changes == ["background layer"]


class TestHeavyPartDisplay:
    """A converted mesh reaches Stamp as one planar face per triangle (§5.2).

    Nothing in the viewport is wrong at ten faces and ruinous at eighty thousand
    except how often it is done and how much of it there is, so these cover the
    work that is now skipped, deferred, or reused rather than repeated.
    """

    @pytest.fixture
    def box(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

        return BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()

    @pytest.fixture
    def widget(self, qtbot):
        from stamp.ui.viewport import Viewport

        view = Viewport()
        qtbot.addWidget(view)
        view.context = _RecordingContext()
        view.view = object()
        return view

    def test_face_count_counts_faces(self, box):
        from stamp.io.part_import import face_count

        assert face_count(box) == 6

    def test_an_ordinary_part_keeps_its_boundary_edges(self, widget, box):
        ais = widget.display_shape("part", box)
        assert ais.Attributes().FaceBoundaryDraw()

    def test_a_converted_mesh_is_drawn_without_them(self, widget, box, monkeypatch):
        """Outlining every triangle costs a second and draws the part as a smudge."""
        from stamp.ui import viewport as viewport_module

        monkeypatch.setattr(viewport_module, "face_count", lambda _shape: 200_000)
        ais = widget.display_shape("part", box)
        assert not ais.Attributes().FaceBoundaryDraw()

    def test_selection_waits_for_the_user_to_reach_for_it(self, widget, box):
        """Computing it costs as much as drawing, and most displays are never picked."""
        widget.display_shape("part", box)
        assert widget.context.activated == []

        widget._do_pick(QPoint(10, 10), additive=False)
        assert len(widget.context.activated) == 1

    def test_redisplaying_the_same_shape_does_not_rebuild_it(self, widget, box):
        first = widget.display_shape("part", box)
        second = widget.display_shape("part", box)
        assert second is first
        assert widget.context.display_calls == 1

    def test_a_changed_colour_does_rebuild_it(self, widget, box):
        widget.display_shape("part", box, color=(0.1, 0.2, 0.3))
        widget.display_shape("part", box, color=(0.9, 0.2, 0.3))
        assert widget.context.display_calls == 2

    def test_setting_the_mode_it_is_already_in_does_nothing(self, widget, box):
        """The window puts the view back to face picking after nearly every action."""
        widget.display_shape("part", box)
        widget._do_pick(QPoint(10, 10), additive=False)
        widget.context.activated.clear()
        widget.context.deactivated.clear()

        widget.set_selection_mode("face")
        assert widget.context.activated == []
        assert widget.context.deactivated == []

    def test_changing_the_mode_defers_the_work_to_the_next_pick(self, widget, box):
        widget.display_shape("part", box)
        widget._do_pick(QPoint(10, 10), additive=False)
        widget.context.activated.clear()

        widget.set_selection_mode("edge")
        assert widget.context.activated == []
        widget._do_pick(QPoint(10, 10), additive=False)
        assert len(widget.context.activated) == 1

    def test_hover_does_not_chase_the_pointer_over_a_converted_mesh(
        self, widget, box, monkeypatch
    ):
        """Highlighting one triangle is not worth a second of frozen mouse."""
        from stamp.ui import viewport as viewport_module

        monkeypatch.setattr(viewport_module, "face_count", lambda _shape: 200_000)
        widget.display_shape("part", box)
        widget.mouseMoveEvent(_a_move(40, 40))
        assert widget.context.moves == []

    def test_pretessellate_leaves_triangulation_the_display_reuses(self, box):
        """Same call the presentation makes, so AIS_Shape finds the mesh already there."""
        from OCP.StdPrs import StdPrs_ToolTriangulatedShape

        from stamp.io.part_import import display_drawer, pretessellate

        assert not StdPrs_ToolTriangulatedShape.IsTriangulated_s(box)
        pretessellate(box)
        assert StdPrs_ToolTriangulatedShape.IsTriangulated_s(box)
        assert display_drawer(draft=True).DeviationCoefficient() > (
            display_drawer().DeviationCoefficient()
        )


class _RecordingContext:
    """Enough of ``AIS_InteractiveContext`` to see what the viewport asked for."""

    def __init__(self) -> None:
        self.displayed: list[object] = []
        self.activated: list[object] = []
        self.deactivated: list[object] = []
        self.moves: list[tuple[int, int]] = []
        self.display_calls = 0

    def Display(self, ais, _mode, _sel, _update):  # noqa: N802 - OCC naming
        self.displayed.append(ais)
        self.display_calls += 1

    def Remove(self, ais, _update):  # noqa: N802
        if ais in self.displayed:
            self.displayed.remove(ais)

    def Activate(self, ais, _mode):  # noqa: N802
        self.activated.append(ais)

    def Deactivate(self, ais):  # noqa: N802
        self.deactivated.append(ais)

    def MoveTo(self, x, y, _view, _update):  # noqa: N802
        self.moves.append((x, y))

    def Select(self, _update):  # noqa: N802
        pass

    def ShiftSelect(self, _update):  # noqa: N802
        pass

    def HasDetected(self):  # noqa: N802
        return False

    def UpdateCurrentViewer(self):  # noqa: N802
        pass


def _a_move(x: int, y: int):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent

    return QMouseEvent(
        QMouseEvent.Type.MouseMove,
        QPointF(x, y),
        QPointF(x, y),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )


class TestColorStampMode:
    """The third operation kind - §9.  A thin recess the 3MF export fills back in."""

    @pytest.fixture
    def panel(self, qtbot):
        from stamp.ui.properties import PropertiesPanel

        widget = PropertiesPanel()
        qtbot.addWidget(widget)
        return widget

    def _select_stamp(self, panel, feature):
        panel.show_feature(Document(), feature, (36.0, 16.0))
        panel.stamp_radio.setChecked(True)
        return feature.operation

    def test_choosing_it_sets_the_kind_and_pins_the_depth_mode(self, panel):
        operation = self._select_stamp(panel, a_feature())
        assert operation.kind is OperationKind.COLOR
        assert operation.depth_mode is DepthMode.BLIND

    def test_an_engraving_depth_becomes_an_ink_thickness(self, panel):
        """3 mm of the second filament buried in the part is nobody's intent."""
        from stamp.core.document import COLOR_STAMP_DEPTH

        feature = a_feature()
        feature.operation.depth = 3.0
        operation = self._select_stamp(panel, feature)
        assert operation.depth == COLOR_STAMP_DEPTH
        assert panel.depth_field.value() == COLOR_STAMP_DEPTH

    def test_a_depth_already_thin_enough_is_left_where_it_was(self, panel):
        feature = a_feature()
        feature.operation.depth = 0.3
        assert self._select_stamp(panel, feature).depth == 0.3

    def test_the_depth_mode_picker_gives_way_to_a_thickness_field(self, panel, qtbot):
        panel.show_feature(Document(), a_feature(), (36.0, 16.0))
        panel.show()
        qtbot.waitExposed(panel)
        assert panel.depth_mode.isVisible()

        panel.stamp_radio.setChecked(True)
        assert not panel.depth_mode.isVisible()
        assert panel.depth_field.isVisible()
        assert panel._depth_value_label.text() == "Thickness:"
        assert panel.stamp_hint.isVisible()

    def test_going_back_to_a_cut_restores_the_depth_picker(self, panel, qtbot):
        panel.show_feature(Document(), a_feature(), (36.0, 16.0))
        panel.show()
        qtbot.waitExposed(panel)
        panel.stamp_radio.setChecked(True)
        panel.cut_radio.setChecked(True)

        assert panel.depth_mode.isVisible()
        assert not panel.stamp_hint.isVisible()

    def test_reopening_a_stamped_feature_shows_it_as_a_stamp(self, panel):
        feature = a_feature()
        feature.operation.kind = OperationKind.COLOR
        panel.show_feature(Document(), feature, (36.0, 16.0))
        assert panel.stamp_radio.isChecked()
        assert not panel.cut_radio.isChecked()

    def test_the_export_dialog_says_which_bodies_are_stamps(self, qtbot):
        from PySide6.QtWidgets import QLabel

        from stamp.ui.dialogs import Color3mfDialog

        dialog = Color3mfDialog(2, stamp_count=1)
        qtbot.addWidget(dialog)
        text = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert "1 of them is a color stamp" in text

        plain = Color3mfDialog(2)
        qtbot.addWidget(plain)
        text = " ".join(label.text() for label in plain.findChildren(QLabel))
        assert "color stamp" not in text

    def test_the_tree_gives_a_stamp_its_own_icon(self, qtbot):
        from stamp.ui.feature_tree import CUT_ICON, STAMP_ICON, FeatureTree

        tree = FeatureTree()
        qtbot.addWidget(tree)
        stamp = a_feature("Emblem")
        stamp.operation.kind = OperationKind.COLOR
        tree.set_document(Document(features=[a_feature("Slot"), stamp]))

        labels = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
        assert any(text.startswith(STAMP_ICON) and "Emblem" in text for text in labels)
        assert any(text.startswith(CUT_ICON) and "Slot" in text for text in labels)


class TestPropertiesPanel:
    @pytest.fixture
    def panel(self, qtbot):
        from stamp.ui.properties import PropertiesPanel

        widget = PropertiesPanel()
        qtbot.addWidget(widget)
        return widget

    def test_a_field_squeezed_to_its_minimum_still_shows_its_unit(self, qtbot):
        """A fixed 88 px minimum left no room for the suffix Qt does not measure."""
        from PySide6.QtWidgets import QHBoxLayout, QWidget

        from stamp.ui.properties import NumberField

        field = NumberField()
        field.setValue(888.88)

        # Hold the field in a child pinned to its own minimum, which is what the
        # panel's grid does to it.  The squeeze has to happen inside a window, not
        # to one: Windows will not let a top-level window go this narrow.
        window = QWidget()
        qtbot.addWidget(window)
        outer = QHBoxLayout(window)
        pinned = QWidget(window)
        pinned.setFixedWidth(field.minimumWidth())
        row = QHBoxLayout(pinned)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(field)
        outer.addWidget(pinned)
        outer.addStretch(1)
        window.resize(400, 80)
        window.show()
        qtbot.waitExposed(window)

        text = field.lineEdit().text()
        assert text.endswith("mm")
        assert field.fontMetrics().horizontalAdvance(text) <= (
            field.lineEdit().contentsRect().width()
        )

    def test_unit_suffixes_survive_a_panel_at_its_minimum_width(self, panel, qtbot):
        """A fixed 88 px minimum let the grid cut every "mm" down to "m"."""
        from stamp.ui.properties import NumberField

        feature = a_feature()
        panel.show_feature(Document(), feature, (36.0, 16.0))
        panel.resize(panel.minimumWidth(), 900)
        panel.show()
        qtbot.waitExposed(panel)

        assert not panel.horizontalScrollBar().isVisible()
        clipped = []
        for field in panel.findChildren(NumberField):
            if not field.isVisible():
                continue
            text = field.lineEdit().text()
            if field.fontMetrics().horizontalAdvance(text) > (
                field.lineEdit().contentsRect().width()
            ):
                clipped.append(text)
        assert clipped == []

    def test_width_and_height_stay_in_proportion(self, panel, qtbot):
        """Editing W with the lock on updates H and the scale (§6.2)."""
        feature = a_feature()
        panel.show_feature(Document(), feature, (36.0, 16.0))
        assert panel.lock_button.isChecked()

        panel.width_field.setValue(72.0)
        assert panel.height_field.value() == pytest.approx(32.0)
        assert panel.scale_field.value() == pytest.approx(200.0)
        assert feature.placement.scale == pytest.approx((2.0, 2.0))

    def test_width_alone_when_the_lock_is_off(self, panel):
        feature = a_feature()
        panel.show_feature(Document(), feature, (36.0, 16.0))
        panel.lock_button.setChecked(False)
        panel.width_field.setValue(72.0)
        assert panel.height_field.value() == pytest.approx(16.0)
        assert feature.placement.scale[1] == pytest.approx(1.0)

    def test_scale_percent_drives_the_size(self, panel):
        feature = a_feature()
        panel.show_feature(Document(), feature, (36.0, 16.0))
        panel.scale_field.setValue(50.0)
        assert panel.width_field.value() == pytest.approx(18.0)
        assert panel.height_field.value() == pytest.approx(8.0)

    def test_exact_width_is_reachable(self, panel):
        """§14 step 3: size it to exactly 40 mm wide."""
        feature = a_feature()
        panel.show_feature(Document(), feature, (36.0, 16.0))
        panel.width_field.setValue(40.0)
        assert feature.placement.scale[0] * 36.0 == pytest.approx(40.0)

    def test_rotate_buttons_step_by_ninety(self, panel):
        feature = a_feature()
        panel.show_feature(Document(), feature, (36.0, 16.0))
        panel._turn(90.0)
        assert feature.placement.rotation == pytest.approx(90.0)
        panel._turn(90.0)
        assert feature.placement.rotation == pytest.approx(180.0)

    def test_enums_survive_the_combo_box_round_trip(self, panel):
        """PySide6 stores a StrEnum as a plain str; it must come back as the enum."""
        feature = a_feature()
        panel.show_feature(Document(), feature, (36.0, 16.0))

        panel.direction.setCurrentIndex(panel.direction.findData(Direction.OUT_OF))
        assert isinstance(feature.operation.direction, Direction)
        assert feature.operation.direction is Direction.OUT_OF

        panel.depth_mode.setCurrentIndex(panel.depth_mode.findData(DepthMode.THROUGH_ALL))
        assert isinstance(feature.operation.depth_mode, DepthMode)
        # A document holding these must still serialize.
        document = Document()
        document.features.append(feature)
        assert document.to_dict()["features"][0]["operation"]["direction"] == "out_of"

    def test_depth_field_hides_for_through_all(self, panel):
        feature = a_feature()
        panel.show_feature(Document(), feature, (36.0, 16.0))
        assert panel.depth_field.isVisibleTo(panel)
        panel.depth_mode.setCurrentIndex(panel.depth_mode.findData(DepthMode.THROUGH_ALL))
        assert not panel.depth_field.isVisibleTo(panel)

    def test_changing_the_operation_reports_it(self, panel, qtbot):
        feature = a_feature()
        panel.show_feature(Document(), feature, (36.0, 16.0))
        with qtbot.waitSignal(panel.changed, timeout=1000):
            panel.add_radio.setChecked(True)
        assert feature.operation.kind is OperationKind.ADD

    def test_base_part_info(self, panel, bracket_step):
        panel.show_base(bracket_step, "mm")
        assert "80.00" in panel._info_labels["size"].text()
        assert panel._info_labels["mode"].text().startswith("Solid")

    def test_mesh_mode_explains_the_blend_limit(self, panel, bracket_stl):
        feature = a_feature()
        feature.modifiers.append(
            Modifier(kind=ModifierKind.FILLET, value=0.3)
        )
        panel.show_feature(Document(), feature, (36.0, 16.0), mesh_mode=True)
        assert "STEP file" in panel.blend_note.text()


class TestPresetLibraryDialog:
    def test_search_filters_by_tag_and_returns_selected_path(self, qtbot):
        from stamp.io.presets import PresetInfo
        from stamp.ui.dialogs import PresetLibraryDialog

        serial = PresetInfo(Path("serial.stamp-preset"), "Serial plate", ("production", "text"), "add · text")
        logo = PresetInfo(Path("logo.stamp-preset"), "Brand logo", ("profile", "cut"), "cut · profile")
        dialog = PresetLibraryDialog([serial, logo])
        qtbot.addWidget(dialog)

        dialog.search.setText("production")

        assert dialog.list.count() == 1
        assert dialog.selected_path() == serial.path

    def test_a_code_preset_gets_a_preview_like_every_other_one(self, qtbot):
        """io.presets tags every QR and Data Matrix preset "code", and the
        preview for that branch unpacked one string into two names.  Saving a
        code preset made the whole library unopenable."""
        from stamp.io.presets import PresetInfo
        from stamp.ui.dialogs import PresetLibraryDialog

        code = PresetInfo(Path("serial-qr.stamp-preset"), "Serial QR", ("cut", "code", "qr"), "cut · qr")
        dialog = PresetLibraryDialog([code])
        qtbot.addWidget(dialog)

        assert dialog.list.count() == 1
        assert not dialog.list.item(0).icon().isNull()
        assert dialog.selected_path() == code.path


class TestFeatureTree:
    @pytest.fixture
    def tree(self, qtbot):
        from stamp.ui.feature_tree import FeatureTree

        widget = FeatureTree()
        qtbot.addWidget(widget)
        return widget

    def test_base_is_pinned_at_the_top(self, tree, bracket_step):
        document = Document(base=bracket_step)
        document.add_feature(a_feature())
        tree.set_document(document)
        assert tree.topLevelItemCount() == 2
        assert "bracket" in tree.topLevelItem(0).text(0)

    def test_modifiers_appear_under_their_feature(self, tree, bracket_step):
        document = Document(base=bracket_step)
        feature = a_feature()
        feature.modifiers.append(Modifier(kind=ModifierKind.FILLET, value=0.3))
        document.add_feature(feature)
        tree.set_document(document)
        assert tree.topLevelItem(1).childCount() == 1

    def test_suppressed_features_are_unchecked(self, tree, bracket_step):
        document = Document(base=bracket_step)
        feature = a_feature()
        feature.enabled = False
        document.add_feature(feature)
        tree.set_document(document)
        assert tree.topLevelItem(1).checkState(0) == Qt.CheckState.Unchecked

    def test_a_broken_feature_is_marked(self, tree, bracket_step):
        from stamp.core.rebuild import FeatureResult

        document = Document(base=bracket_step)
        feature = a_feature()
        document.add_feature(feature)
        result = FeatureResult(feature_id=feature.id, errors=["the face is gone"])
        tree.set_document(document, {feature.id: result})
        item = tree.topLevelItem(1)
        assert "✖" in item.text(0)
        assert "the face is gone" in item.toolTip(0)

    def test_a_warning_is_shown_without_breaking_the_feature(self, tree, bracket_step):
        from stamp.core.rebuild import FeatureResult

        document = Document(base=bracket_step)
        feature = a_feature()
        document.add_feature(feature)
        result = FeatureResult(feature_id=feature.id, warnings=["the fillet did not fit"])
        tree.set_document(document, {feature.id: result})
        assert "the fillet did not fit" in tree.topLevelItem(1).toolTip(0)

    def test_selection_reports_the_feature_id(self, tree, bracket_step, qtbot):
        document = Document(base=bracket_step)
        feature = a_feature()
        document.add_feature(feature)
        tree.set_document(document)
        with qtbot.waitSignal(tree.feature_selected, timeout=1000) as blocker:
            tree.select_feature(feature.id)
        assert blocker.args[0] == feature.id

    def test_unchecking_reports_a_suppress(self, tree, bracket_step, qtbot):
        document = Document(base=bracket_step)
        feature = a_feature()
        document.add_feature(feature)
        tree.set_document(document)
        with qtbot.waitSignal(tree.enabled_toggled, timeout=1000) as blocker:
            tree.topLevelItem(1).setCheckState(0, Qt.CheckState.Unchecked)
        assert blocker.args == [feature.id, False]


class TestRebuildController:
    def test_a_rebuild_runs_off_the_gui_thread(self, qtbot, bracket_step):
        from stamp.core.profiles import ProfileCache
        from stamp.core.rebuild import RebuildEngine
        from stamp.ui.rebuild_worker import RebuildController

        controller = RebuildController(RebuildEngine(ProfileCache().get))
        document = Document(base=bracket_step)
        try:
            with qtbot.waitSignal(controller.finished, timeout=15000) as blocker:
                controller.request(document)
            assert blocker.args[0].volume == pytest.approx(bracket_step.volume)
        finally:
            controller.shutdown()

    def test_requests_coalesce(self, qtbot, bracket_step):
        from stamp.core.profiles import ProfileCache
        from stamp.core.rebuild import RebuildEngine
        from stamp.ui.rebuild_worker import RebuildController

        controller = RebuildController(RebuildEngine(ProfileCache().get))
        document = Document(base=bracket_step)
        finished = []
        controller.finished.connect(finished.append)
        try:
            for _ in range(5):
                controller.request(document)
            qtbot.wait(1500)
            assert len(finished) == 1
        finally:
            controller.shutdown()

    def test_an_interrupted_rebuild_still_delivers(self, qtbot):
        """A request that cancels one in flight must not wedge the controller.

        A cancelled worker returns without a result, so unless it reports the
        cancellation the busy flag stays set and no rebuild ever runs again.
        That looked like a feature whose geometry never moved.
        """
        import time

        from stamp.core.rebuild import Cancelled
        from stamp.ui.rebuild_worker import RebuildController

        class SlowEngine:
            def __init__(self) -> None:
                self.runs = 0

            def rebuild(self, document, should_cancel=None, progress=None):
                self.runs += 1
                mine = self.runs
                for _ in range(30):
                    time.sleep(0.01)
                    if should_cancel and should_cancel():
                        raise Cancelled()
                return mine

        engine = SlowEngine()
        controller = RebuildController(engine)
        try:
            controller.request(Document(), immediate=True)
            qtbot.wait(120)  # let the first rebuild get in flight
            with qtbot.waitSignal(controller.finished, timeout=5000) as blocker:
                controller.request(Document(), immediate=True)
            assert blocker.args[0] == 2, "the newer rebuild must be the one delivered"
            assert engine.runs == 2
            assert not controller.busy
        finally:
            controller.shutdown()

    def test_many_interruptions_settle_on_the_last(self, qtbot):
        """What a drag does: each move cancels the rebuild before it."""
        import time

        from stamp.core.rebuild import Cancelled
        from stamp.ui.rebuild_worker import RebuildController

        class SlowEngine:
            def __init__(self) -> None:
                self.runs = 0

            def rebuild(self, document, should_cancel=None, progress=None):
                self.runs += 1
                mine = self.runs
                for _ in range(20):
                    time.sleep(0.01)
                    if should_cancel and should_cancel():
                        raise Cancelled()
                return mine

        engine = SlowEngine()
        controller = RebuildController(engine)
        finished = []
        controller.finished.connect(finished.append)
        try:
            for _ in range(8):
                controller.request(Document(), immediate=True)
                qtbot.wait(40)
            qtbot.waitUntil(lambda: bool(finished) and not controller.busy, timeout=8000)
            assert finished[-1] == engine.runs
            assert not controller.busy
        finally:
            controller.shutdown()


@needs_gl
class TestMainWindow:
    @pytest.fixture
    def window(self, qtbot):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        # No one is here to answer a modal dialog, and an STL import asks for its
        # unit.  With this off, every prompt takes its default.
        win.interactive = False
        qtbot.addWidget(win)
        win.show()
        qtbot.waitExposed(win)
        yield win
        win.rebuilder.shutdown()

    def _settle(self, qtbot, window):
        qtbot.wait(400)
        for _ in range(150):
            if not window.rebuilder.busy:
                break
            qtbot.wait(100)

    def test_the_add_text_button_starts_a_text_feature(self, qtbot, window, fixtures):
        """Drive the toolbar, not the method.

        ``QAction.triggered`` carries the checked state.  A slot that takes an
        argument receives that bool, and the command dies before it starts.
        """
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)

        window.action_add_text.trigger()
        qtbot.wait(50)

        assert window._pending_profile is not None
        assert window._pending_profile.is_text
        assert window._pending_profile.text.text

    def test_the_add_profile_button_opens_its_picker(self, qtbot, window, fixtures):
        """The same trap applies to every toolbar slot that takes an argument."""
        import inspect

        for slot in (
            window.add_text_dialog,
            window.add_profile_dialog,
            window.report_bug,
            window.report_crash,
        ):
            positional = [
                name
                for name, p in inspect.signature(slot).parameters.items()
                if p.kind
                in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            assert positional == [], (
                f"{slot.__name__} takes {positional}; the triggered signal would "
                f"put the checked state there"
            )
        qtbot.wait(200)

    def _top_face(self, window):
        from stamp.core.refs import face_center, face_normal_at, faces_of, surface_kind

        for face in faces_of(window.document.base.runtime):
            if surface_kind(face) != "plane":
                continue
            center = face_center(face)
            if abs(center[2] - 8.0) < 1e-6 and face_normal_at(face, center)[2] > 0.9:
                return face
        pytest.fail("no top face")

    def test_open_a_part(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        assert window.document.base is not None
        assert window.tree.topLevelItemCount() == 1
        assert window.action_export_step.isEnabled()

    def test_step_export_is_refused_in_mesh_mode(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.stl")
        self._settle(qtbot, window)
        assert not window.action_export_step.isEnabled()
        assert "STL" in window.action_export_step.toolTip()

    def test_place_a_feature_and_rebuild(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        base_volume = window.document.base.volume

        window.add_profile(fixtures / "logo.svg")
        assert window._pending_profile is not None
        window._create_feature(window._pending_profile, self._top_face(window),
                               (30.0, 20.0, 8.0))
        self._settle(qtbot, window)

        assert len(window.document.features) == 1
        assert window._last_result is not None
        assert window._last_result.volume < base_volume  # a cut, by default
        assert window.tree.topLevelItemCount() == 2

    def test_undo_restores_the_previous_value(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        window.add_profile(fixtures / "logo.svg")
        window._create_feature(window._pending_profile, self._top_face(window),
                               (30.0, 20.0, 8.0))
        self._settle(qtbot, window)

        feature = window.document.features[0]
        window.tree.select_feature(feature.id)
        qtbot.wait(150)
        window.properties.depth_field.setValue(2.0)
        self._settle(qtbot, window)
        assert feature.operation.depth == pytest.approx(2.0)

        window.undo()
        self._settle(qtbot, window)
        assert window.document.features[0].operation.depth == pytest.approx(0.5)

        window.redo()
        self._settle(qtbot, window)
        assert window.document.features[0].operation.depth == pytest.approx(2.0)

    def test_save_and_reopen(self, window, qtbot, fixtures, tmp_path):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        window.add_profile(fixtures / "logo.svg")
        window._create_feature(window._pending_profile, self._top_face(window),
                               (30.0, 20.0, 8.0))
        self._settle(qtbot, window)
        volume = window._last_result.volume

        window._project_path = tmp_path / "test.stamp"
        window.save_project()
        assert (tmp_path / "test.stamp").exists()

        window.open_project(tmp_path / "test.stamp")
        self._settle(qtbot, window)
        assert len(window.document.features) == 1
        assert window._last_result.volume == pytest.approx(volume, rel=1e-9)

    def test_handles_appear_for_the_selected_feature(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        window.add_profile(fixtures / "logo.svg")
        window._create_feature(window._pending_profile, self._top_face(window),
                               (30.0, 20.0, 8.0))
        self._settle(qtbot, window)
        window.tree.select_feature(window.document.features[0].id)
        qtbot.wait(200)

        overlay = window.handles
        # Four corners plus the rotation handle; edge handles only with the lock off.
        assert len(overlay._handles) == 5
        assert window.viewport.has("handles")

    def test_a_screen_round_trip_lands_back_on_the_plane(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        window.add_profile(fixtures / "logo.svg")
        window._create_feature(window._pending_profile, self._top_face(window),
                               (30.0, 20.0, 8.0))
        self._settle(qtbot, window)
        window.tree.select_feature(window.document.features[0].id)
        qtbot.wait(200)

        overlay = window.handles
        overlay.feature.placement.offset_2d = (4.0, -3.0)
        overlay.refresh()
        screen = overlay._screen(4.0, -3.0)
        assert screen is not None
        back = overlay._uv_at(screen)
        assert back == pytest.approx((4.0, -3.0), abs=0.3)

    def test_nudge_moves_by_a_tenth_of_a_millimetre(self, window, qtbot, fixtures):
        from PySide6.QtGui import QKeyEvent

        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        window.add_profile(fixtures / "logo.svg")
        window._create_feature(window._pending_profile, self._top_face(window),
                               (30.0, 20.0, 8.0))
        self._settle(qtbot, window)
        window.tree.select_feature(window.document.features[0].id)
        qtbot.wait(200)

        overlay = window.handles
        start = overlay.feature.placement.offset_2d
        event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Right,
                          Qt.KeyboardModifier.NoModifier)
        assert overlay._nudge(event)
        assert overlay.feature.placement.offset_2d[0] == pytest.approx(start[0] + 0.1)

    def test_the_tool_preview_is_shown_for_the_selection(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        window.add_profile(fixtures / "logo.svg")
        window._create_feature(window._pending_profile, self._top_face(window),
                               (30.0, 20.0, 8.0))
        self._settle(qtbot, window)
        window.tree.select_feature(window.document.features[0].id)
        qtbot.wait(200)
        assert window.viewport.has("preview")

        window.toggle_preview()
        qtbot.wait(150)
        assert not window.viewport.has("preview")


@needs_gl
class TestSpecGaps:
    """The §10 rows and §6.2 snapping that the first pass of the window left open."""

    @pytest.fixture
    def window(self, qtbot):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        win.show()
        qtbot.waitExposed(win)
        yield win
        win.rebuilder.shutdown()

    def _settle(self, qtbot, window):
        qtbot.wait(400)
        for _ in range(150):
            if not window.rebuilder.busy:
                break
            qtbot.wait(100)
        qtbot.wait(200)

    def _top_face(self, window):
        from stamp.core.refs import face_center, face_normal_at, faces_of, surface_kind

        for face in faces_of(window.document.base.runtime):
            if surface_kind(face) != "plane":
                continue
            center = face_center(face)
            if abs(center[2] - 8.0) < 1e-6 and face_normal_at(face, center)[2] > 0.9:
                return face
        pytest.fail("no top face")

    def _place(self, window, qtbot, source, point=(30.0, 20.0, 8.0)):
        window.add_profile(source)
        window._create_feature(window._pending_profile, self._top_face(window), point)
        self._settle(qtbot, window)
        return window.document.features[-1]

    def test_alignment_targets_come_from_the_same_plane(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        first = self._place(window, qtbot, fixtures / "logo.svg")
        second = self._place(window, qtbot, fixtures / "serial.dxf")
        window.tree.select_feature(first.id)
        qtbot.wait(200)
        window.properties.u_field.setValue(5.0)
        window.properties.v_field.setValue(7.0)
        self._settle(qtbot, window)

        window.tree.select_feature(second.id)
        qtbot.wait(200)
        assert (5.0, 7.0) in window.handles.alignment_targets

    def test_a_feature_is_not_its_own_alignment_target(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        only = self._place(window, qtbot, fixtures / "logo.svg")
        window.tree.select_feature(only.id)
        qtbot.wait(200)
        assert window.handles.alignment_targets == []

    def test_snapping_lines_features_up_on_one_axis(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        feature = self._place(window, qtbot, fixtures / "logo.svg")
        window.tree.select_feature(feature.id)
        qtbot.wait(200)

        overlay = window.handles
        overlay.alignment_targets = [(12.0, -6.0)]
        overlay.grid_pitch = 0.0
        snapped = overlay._snap((12.0 + 1e-4, 3.0))
        assert snapped[0] == pytest.approx(12.0)
        assert snapped[1] == pytest.approx(3.0)

    def test_snapping_prefers_an_exact_target_over_an_axis(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        feature = self._place(window, qtbot, fixtures / "logo.svg")
        window.tree.select_feature(feature.id)
        qtbot.wait(200)

        overlay = window.handles
        overlay.alignment_targets = [(12.0, 3.0)]
        overlay.grid_pitch = 0.0
        assert overlay._snap((12.0 + 1e-4, 3.0 + 1e-4)) == pytest.approx((12.0, 3.0))

    def test_a_profile_larger_than_the_face_warns_but_is_allowed(
        self, window, qtbot, fixtures
    ):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        feature = self._place(window, qtbot, fixtures / "logo.svg")
        window.tree.select_feature(feature.id)
        qtbot.wait(200)

        window.properties.width_field.setValue(400.0)
        self._settle(qtbot, window)
        assert "larger than" in window.warning_label.text()
        # allowed, not blocked: the geometry still rebuilt
        assert window._last_result is not None
        assert not window._last_result.errors

    def test_relink_reports_when_nothing_is_missing(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        self._place(window, qtbot, fixtures / "logo.svg")
        window.relink_sources()
        assert "where it should be" in window.statusBar().currentMessage()

    def test_recent_projects_records_a_save(self, window, qtbot, fixtures, tmp_path):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        window._project_path = tmp_path / "recent.stamp"
        window.save_project()
        assert str(tmp_path / "recent.stamp") in window.recent_projects()

    def test_recent_projects_drops_files_that_are_gone(self, window, tmp_path):
        window.settings.setValue("recent/projects", [str(tmp_path / "not_here.stamp")])
        assert window.recent_projects() == []

    def test_preset_views_are_reachable_from_the_toolbar(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        assert window.view_box.count() == 7
        window.view_box.setCurrentIndex(window.view_box.findData("top"))
        qtbot.wait(150)  # no exception means the view accepted it


@needs_gl
class TestSnapping:
    """The full §6.2 snap-target set, derived from the part."""

    @pytest.fixture
    def window(self, qtbot):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        win.show()
        qtbot.waitExposed(win)
        yield win
        win.rebuilder.shutdown()

    def _settle(self, qtbot, window):
        qtbot.wait(400)
        for _ in range(150):
            if not window.rebuilder.busy:
                break
            qtbot.wait(100)
        qtbot.wait(200)

    def _ready(self, window, qtbot, fixtures):
        from stamp.core.refs import face_center, face_normal_at, faces_of, surface_kind

        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        face = None
        for candidate in faces_of(window.document.base.runtime):
            if surface_kind(candidate) != "plane":
                continue
            center = face_center(candidate)
            if abs(center[2] - 8.0) < 1e-6 and face_normal_at(candidate, center)[2] > 0.9:
                face = candidate
        assert face is not None
        window.add_profile(fixtures / "logo.svg")
        window._create_feature(window._pending_profile, face, (30.0, 20.0, 8.0))
        self._settle(qtbot, window)
        feature = window.document.features[0]
        window.tree.select_feature(feature.id)
        qtbot.wait(200)
        return feature

    def test_the_boss_axis_is_a_snap_target(self, window, qtbot, fixtures):
        """Centering a logo on a boss is the case §6.2 calls critical."""
        from stamp.core.snapping import SnapKind

        self._ready(window, qtbot, fixtures)
        axes = [t for t in window.handles.snap_targets if t.kind is SnapKind.CYLINDER_AXIS]
        # the boss at (60, 20) and the two holes at (12, 12) and (12, 28), taken
        # relative to the sketch origin at (30, 20)
        assert len(axes) == 3
        found = [(pytest.approx(t.u), pytest.approx(t.v)) for t in axes]
        assert (30.0, 0.0) in found
        assert (-18.0, -8.0) in found
        assert (-18.0, 8.0) in found

    def test_the_face_center_is_a_snap_target(self, window, qtbot, fixtures):
        from stamp.core.snapping import SnapKind

        self._ready(window, qtbot, fixtures)
        centers = [t for t in window.handles.snap_targets if t.kind is SnapKind.FACE_CENTER]
        assert len(centers) == 1

    def test_face_corners_and_edge_midpoints_are_targets(self, window, qtbot, fixtures):
        from stamp.core.snapping import SnapKind

        self._ready(window, qtbot, fixtures)
        kinds = {t.kind for t in window.handles.snap_targets}
        assert SnapKind.FACE_CORNER in kinds
        assert SnapKind.FACE_EDGE_MIDPOINT in kinds

    def test_a_drag_snaps_onto_the_boss(self, window, qtbot, fixtures):
        from stamp.core.snapping import SnapKind

        self._ready(window, qtbot, fixtures)
        overlay = window.handles
        overlay.grid_pitch = 0.0
        snapped = overlay._snap((30.4, 0.3))
        assert snapped == pytest.approx((30.0, 0.0))
        assert overlay.last_snap.kind is SnapKind.CYLINDER_AXIS

    def test_a_point_far_from_everything_is_left_alone(self, window, qtbot, fixtures):
        """The tolerance follows the zoom, so the point is chosen against it.

        ``_snap`` measures in pixels and converts to millimetres, thus a point
        written down as a constant is only far enough at one viewport size.
        """
        from stamp.ui.handles import SNAP_RADIUS_PX

        self._ready(window, qtbot, fixtures)
        overlay = window.handles
        overlay.grid_pitch = 0.0
        tolerance = overlay._pixels_to_mm(SNAP_RADIUS_PX)

        def clear_of_everything(point) -> bool:
            # Axis alignment snaps on one coordinate alone, so both must be clear.
            return all(
                abs(point[0] - t.u) > 2 * tolerance and abs(point[1] - t.v) > 2 * tolerance
                for t in overlay.snap_targets
            )

        target = next(
            (
                (13.37 + i * tolerance, -6.66 - i * tolerance)
                for i in range(200)
                if clear_of_everything((13.37 + i * tolerance, -6.66 - i * tolerance))
            ),
            None,
        )
        assert target is not None, "no point on this face is clear of every target"

        assert overlay._snap(target) == pytest.approx(target)
        assert overlay.last_snap is None

    def test_snap_targets_come_from_the_base_part_only(self, window, qtbot, fixtures):
        """A target on a feature-made edge would move whenever that feature changed."""
        from stamp.core.refs import resolve_face_ref

        self._ready(window, qtbot, fixtures)
        before = len(window.handles.snap_targets)

        window.add_profile(fixtures / "serial.dxf")
        ref = window.document.features[0].placement.anchor.face_ref
        resolved = resolve_face_ref(ref, window.document.base.runtime).face
        # A different click point, so the new target does not land on the sketch
        # origin and get deduplicated away.
        window._create_feature(window._pending_profile, resolved, (44.0, 31.0, 8.0))
        self._settle(qtbot, window)
        window.tree.select_feature(window.document.features[0].id)
        qtbot.wait(200)

        # Exactly one more target: where the second feature sits.
        assert len(window.handles.snap_targets) == before + 1


@needs_gl
class TestFirstRun:
    """§7.1 - the opening screen and the drop gesture."""

    @pytest.fixture
    def window(self, qtbot):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        win.show()
        qtbot.waitExposed(win)
        yield win
        win.rebuilder.shutdown()

    def _settle(self, qtbot, window):
        qtbot.wait(400)
        for _ in range(150):
            if not window.rebuilder.busy:
                break
            qtbot.wait(100)
        qtbot.wait(200)

    def test_the_app_opens_on_the_welcome_page(self, window):
        assert window._center.currentIndex() == 0

    def test_opening_a_part_shows_the_viewport(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        assert window._center.currentWidget() is window._viewport_page

    def test_a_drop_on_a_face_places_the_feature_there(self, window, qtbot, fixtures):
        from PySide6.QtCore import QMimeData, QPointF, QUrl
        from PySide6.QtGui import QDropEvent

        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        window.viewport.fit_all()
        qtbot.wait(300)

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(fixtures / "logo.svg"))])
        centre = window.viewport.mapTo(window, window.viewport.rect().center())
        event = QDropEvent(
            QPointF(centre), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        window.dropEvent(event)
        self._settle(qtbot, window)

        # The drop made the feature outright, with no second click.
        assert len(window.document.features) == 1
        assert window._pending_profile is None
        assert window.document.features[0].placement.anchor.face_ref is not None

    def test_a_drop_away_from_the_part_still_stages_the_profile(
        self, window, qtbot, fixtures
    ):
        from PySide6.QtCore import QMimeData, QPointF, QUrl
        from PySide6.QtGui import QDropEvent

        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(fixtures / "logo.svg"))])
        event = QDropEvent(
            QPointF(2.0, 2.0), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        window.dropEvent(event)
        qtbot.wait(300)

        assert window.document.features == []
        assert window._pending_profile is not None
        assert "Click the face" in window.statusBar().currentMessage()


@needs_gl
class TestStatusAndQuality:
    """§7 status line and the §10 draft-quality offer."""

    @pytest.fixture
    def window(self, qtbot):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        win.show()
        qtbot.waitExposed(win)
        yield win
        win.rebuilder.shutdown()

    def _settle(self, qtbot, window):
        qtbot.wait(400)
        for _ in range(150):
            if not window.rebuilder.busy:
                break
            qtbot.wait(100)
        qtbot.wait(200)

    def test_mass_appears_once_a_density_is_set(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        assert "cm" in window.status_label.text()
        assert " g" not in window.status_label.text()

        window.density_field.setValue(2.70)  # aluminium
        qtbot.wait(100)
        text = window.status_label.text()
        assert " g" in text
        # 26.674 cm3 of aluminium weighs about 72 g
        assert "72." in text

    def test_draft_quality_can_be_switched_on_and_off(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)

        window.set_draft_display(True)
        qtbot.wait(150)
        assert window._draft_display
        assert window.action_draft.isChecked()
        assert "draft quality" in window.statusBar().currentMessage()

        window.set_draft_display(False)
        qtbot.wait(150)
        assert not window._draft_display

    def test_draft_quality_does_not_change_the_geometry(self, window, qtbot, fixtures):
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        before = window._last_result.volume
        window.set_draft_display(True)
        self._settle(qtbot, window)
        assert window._last_result.volume == pytest.approx(before)


@needs_gl
class TestMeshPicking:
    """§6.1 mesh mode: click a triangle, grow a region, fit a plane."""

    @pytest.fixture
    def window(self, qtbot, fixtures):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        win.show()
        qtbot.waitExposed(win)
        win.open_part(fixtures / "bracket.stl")
        self._settle(qtbot, win)
        win.viewport.set_preset_view("top")
        qtbot.wait(250)
        win.viewport.fit_all()
        qtbot.wait(300)
        yield win
        win.rebuilder.shutdown()

    def _settle(self, qtbot, window):
        qtbot.wait(400)
        for _ in range(200):
            if not window.rebuilder.busy:
                break
            qtbot.wait(100)
        qtbot.wait(200)

    def _aim(self, window, world):
        """Put the pick position on the pixel showing a world point."""
        from PySide6.QtCore import QPoint

        view = window.viewport.view
        vx, vy = view.Project(*world)
        px, py = view.Convert(vx, vy)
        window.viewport.last_pick_position = QPoint(int(px), int(py))

    def test_the_tolerance_control_appears_only_for_a_mesh(
        self, window, qtbot, fixtures
    ):
        assert window.region_tolerance.isVisible()
        window.open_part(fixtures / "bracket.step")
        self._settle(qtbot, window)
        assert not window.region_tolerance.isVisible()

    def test_a_click_finds_the_flat_plate(self, window, qtbot):
        self._aim(window, (30.0, 20.0, 8.0))
        region = window._find_mesh_region()
        assert region is not None
        assert region.plane.normal == pytest.approx((0.0, 0.0, 1.0), abs=1e-6)
        assert region.plane.origin[2] == pytest.approx(8.0, abs=1e-6)
        assert region.count > 50  # the whole plate top, not one triangle
        assert region.flatness == pytest.approx(0.0, abs=1e-6)

    def test_the_region_is_highlighted(self, window, qtbot):
        self._aim(window, (30.0, 20.0, 8.0))
        window._find_mesh_region()
        qtbot.wait(150)
        assert window.viewport.has("mesh_region")

    def test_a_click_on_the_boss_finds_its_own_top(self, window, qtbot):
        self._aim(window, (60.0, 20.0, 14.0))
        region = window._find_mesh_region()
        assert region is not None
        assert region.plane.origin[2] == pytest.approx(14.0, abs=1e-6)

    def test_a_click_places_a_feature_on_the_region(self, window, qtbot, fixtures):
        from stamp.core.document import AnchorKind

        base_volume = window.document.base.volume
        window.add_profile(fixtures / "logo.svg")
        self._aim(window, (30.0, 20.0, 8.0))
        window._on_mesh_picked()
        self._settle(qtbot, window)

        assert len(window.document.features) == 1
        feature = window.document.features[0]
        assert feature.placement.anchor.kind is AnchorKind.MESH_REGION
        assert feature.placement.anchor.mesh_seed is not None
        assert feature.placement.anchor.plane is not None
        assert not window._last_result.errors
        assert window._last_result.volume < base_volume  # a cut, by default

    def test_the_anchor_survives_a_save_and_reopen(
        self, window, qtbot, fixtures, tmp_path
    ):
        from stamp.core.document import AnchorKind

        window.add_profile(fixtures / "logo.svg")
        self._aim(window, (30.0, 20.0, 8.0))
        window._on_mesh_picked()
        self._settle(qtbot, window)
        volume = window._last_result.volume

        window._project_path = tmp_path / "mesh.stamp"
        window.save_project()
        window.open_project(tmp_path / "mesh.stamp")
        self._settle(qtbot, window)

        feature = window.document.features[0]
        assert feature.placement.anchor.kind is AnchorKind.MESH_REGION
        assert window._last_result.volume == pytest.approx(volume, rel=1e-9)
        assert not window._last_result.errors

    def test_raising_the_tolerance_regrows_the_region(self, window, qtbot, fixtures):
        window.add_profile(fixtures / "logo.svg")
        self._aim(window, (30.0, 20.0, 8.0))
        window._on_mesh_picked()
        self._settle(qtbot, window)

        feature = window.document.features[0]
        assert feature.placement.anchor.mesh_tolerance == pytest.approx(5.0)
        origin = feature.placement.anchor.plane.origin

        window.region_tolerance.setValue(30.0)
        self._settle(qtbot, window)
        assert feature.placement.anchor.mesh_tolerance == pytest.approx(30.0)
        # The plate is genuinely flat, so a wider tolerance changes nothing here.
        assert feature.placement.anchor.plane.origin == pytest.approx(origin, abs=1e-6)
        assert not window._last_result.errors

    def test_a_click_that_misses_the_part_says_so(self, window, qtbot):
        from PySide6.QtCore import QPoint

        window.viewport.last_pick_position = QPoint(3, 3)
        assert window._find_mesh_region() is None
        assert "missed" in window.statusBar().currentMessage()

    def test_the_mesh_cache_is_built_once(self, window, qtbot):
        first = window._mesh_pick_data()
        second = window._mesh_pick_data()
        assert first is second
        assert "adjacency" in first


@needs_gl
class TestVersionInTheWindow:
    """The running version is visible without opening a file (§7)."""

    @pytest.fixture
    def window(self, qtbot):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        win.show()
        qtbot.waitExposed(win)
        yield win
        win.rebuilder.shutdown()

    def test_the_title_carries_the_version(self, window):
        import stamp

        assert stamp.__version__ in window.windowTitle()

    def test_the_version_survives_a_title_refresh(self, window):
        import stamp

        window._update_title()
        title = window.windowTitle()
        assert stamp.__version__ in title
        assert "Untitled" in title


@needs_gl
class TestWorkingValueIsAppliedAutomatically:
    """A fillet that is too large is corrected without asking (§6.4)."""

    @pytest.fixture
    def window(self, qtbot, fixtures):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        win.show()
        qtbot.waitExposed(win)
        win.open_part(fixtures / "bracket.step")
        self._settle(qtbot, win)
        win.viewport.fit_all()
        qtbot.wait(300)
        yield win
        win.rebuilder.shutdown()

    def _settle(self, qtbot, window):
        qtbot.wait(300)
        for _ in range(300):
            if not window.rebuilder.busy:
                break
            qtbot.wait(100)
        qtbot.wait(200)

    def _drop_logo(self, window, qtbot, fixtures):
        from PySide6.QtCore import QMimeData, QPointF, QUrl
        from PySide6.QtGui import QDropEvent

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(fixtures / "logo.svg"))])
        centre = window.viewport.mapTo(window, window.viewport.rect().center())
        window.dropEvent(QDropEvent(
            QPointF(centre), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        ))
        self._settle(qtbot, window)
        assert window.document.features, "the drop did not make a feature"
        return window.document.features[0]

    def test_a_value_that_is_too_large_is_replaced(self, window, qtbot, fixtures):
        from stamp.core.document import EdgeRole, EdgeSelector, Modifier, ModifierKind

        feature = self._drop_logo(window, qtbot, fixtures)
        # 9 mm is far larger than any edge of this artwork can take.
        feature.modifiers.append(
            Modifier(kind=ModifierKind.FILLET, value=9.0,
                     target=EdgeSelector(role=EdgeRole.TOP))
        )
        window.request_rebuild(immediate=True)
        for _ in range(8):
            self._settle(qtbot, window)
            if feature.modifiers[0].value < 9.0:
                break

        assert feature.modifiers[0].value < 9.0, "the value must be corrected for the user"
        assert feature.modifiers[0].value > 0.0
        assert "largest that works" in window.warning_label.text()

    def test_the_panel_has_no_button_to_push(self, window, qtbot, fixtures):
        """The correction is automatic, so the old "Use N" button is gone."""
        from PySide6.QtWidgets import QPushButton

        self._drop_logo(window, qtbot, fixtures)
        window._add_modifier("fillet")
        self._settle(qtbot, window)
        captions = [b.text() for b in window.properties.findChildren(QPushButton)]
        assert not any(c.startswith("Use ") for c in captions), captions

    def test_the_3mf_export_is_offered(self, window, qtbot, fixtures):
        self._drop_logo(window, qtbot, fixtures)
        assert window.action_export_3mf.isEnabled()


@needs_gl
class TestTheDefaultValueIsCorrectedToo:
    """Add a fillet, take the default, and it must fix itself (§6.4).

    This is the case a user actually meets: "+ Fillet" starts at 0.3 mm, fine
    artwork cannot take that, and the correction has to happen without anyone
    reading a number out of a message and typing it back in.  The first test of
    this feature typed a value in by hand, which is not how anyone reaches it.
    """

    DEFAULT = 0.3

    @pytest.fixture
    def window(self, qtbot, fixtures):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        win.show()
        qtbot.waitExposed(win)
        win.open_part(fixtures / "bracket.step")
        self._settle(qtbot, win)
        win.viewport.fit_all()
        qtbot.wait(300)
        yield win
        win.rebuilder.shutdown()

    def _settle(self, qtbot, window):
        qtbot.wait(300)
        for _ in range(400):
            if not window.rebuilder.busy:
                break
            qtbot.wait(100)
        qtbot.wait(200)

    def _fine_feature(self, window, qtbot, fixtures):
        """Artwork too fine for the default, made by shrinking the logo."""
        from PySide6.QtCore import QMimeData, QPointF, QUrl
        from PySide6.QtGui import QDropEvent

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(fixtures / "logo.svg"))])
        centre = window.viewport.mapTo(window, window.viewport.rect().center())
        window.dropEvent(QDropEvent(
            QPointF(centre), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        ))
        self._settle(qtbot, window)
        assert window.document.features, "the drop did not make a feature"
        feature = window.document.features[0]
        feature.placement.scale = (0.05, 0.05)
        window.request_rebuild(immediate=True)
        self._settle(qtbot, window)
        window.tree.select_feature(feature.id)
        qtbot.wait(50)
        return feature

    def test_the_default_fillet_is_corrected_without_being_typed(
        self, window, qtbot, fixtures
    ):
        feature = self._fine_feature(window, qtbot, fixtures)
        window._add_modifier("fillet")
        assert feature.modifiers[0].value == self.DEFAULT

        for _ in range(6):
            self._settle(qtbot, window)
            if feature.modifiers[0].value != self.DEFAULT:
                break

        value = feature.modifiers[0].value
        assert value != self.DEFAULT, (
            "the default was too large and nothing corrected it"
        )
        assert 0 < value < self.DEFAULT
        assert "largest that works" in window.warning_label.text()

    def test_the_corrected_value_leaves_no_warning_behind(
        self, window, qtbot, fixtures
    ):
        """The value it settles on has to be one that really builds."""
        feature = self._fine_feature(window, qtbot, fixtures)
        window._add_modifier("fillet")
        for _ in range(6):
            self._settle(qtbot, window)
            if feature.modifiers[0].value != self.DEFAULT:
                break
        self._settle(qtbot, window)

        assert window._last_result is not None
        row = window._last_result.result_for(feature.id)
        assert row is not None
        assert not row.suggested_values, (
            f"the value it chose still does not build: {row.warnings}"
        )

    def test_a_chamfer_default_is_corrected_as_well(self, window, qtbot, fixtures):
        feature = self._fine_feature(window, qtbot, fixtures)
        window._add_modifier("chamfer")
        for _ in range(6):
            self._settle(qtbot, window)
            if feature.modifiers[0].value != self.DEFAULT:
                break
        assert feature.modifiers[0].value != self.DEFAULT

    def test_the_panel_shows_the_value_it_chose(self, window, qtbot, fixtures):
        """The number in the box has to match the model, or the next edit undoes it."""
        from PySide6.QtWidgets import QDoubleSpinBox

        feature = self._fine_feature(window, qtbot, fixtures)
        window._add_modifier("fillet")
        for _ in range(6):
            self._settle(qtbot, window)
            if feature.modifiers[0].value != self.DEFAULT:
                break
        qtbot.wait(200)

        boxes = window.properties._modifiers.findChildren(QDoubleSpinBox)
        assert boxes, "no modifier row in the panel"
        shown = [round(b.value(), 6) for b in boxes]
        assert round(feature.modifiers[0].value, 6) in shown, shown


@needs_gl
class TestReplacePartCommand:
    """Open a part, stamp it, swap in a revision - the artwork stays put (§8.2)."""

    @pytest.fixture
    def window(self, qtbot, fixtures):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        win.show()
        qtbot.waitExposed(win)
        win.open_part(fixtures / "bracket.step")
        self._settle(qtbot, win)
        win.viewport.fit_all()
        qtbot.wait(300)
        yield win
        win.rebuilder.shutdown()

    def _settle(self, qtbot, window):
        qtbot.wait(300)
        for _ in range(300):
            if not window.rebuilder.busy:
                break
            qtbot.wait(100)
        qtbot.wait(200)

    def _stamp_it(self, window, qtbot, fixtures):
        from PySide6.QtCore import QMimeData, QPointF, QUrl
        from PySide6.QtGui import QDropEvent

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(fixtures / "logo.svg"))])
        centre = window.viewport.mapTo(window, window.viewport.rect().center())
        window.dropEvent(QDropEvent(
            QPointF(centre), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        ))
        self._settle(qtbot, window)
        assert window.document.features, "the drop did not make a feature"
        return window.document.features[0]

    def test_the_stamp_survives_a_revision(self, window, qtbot, fixtures):
        feature = self._stamp_it(window, qtbot, fixtures)
        before = feature.placement.anchor.plane
        assert before is not None

        window.replace_part(fixtures / "bracket_rev_b.step")
        self._settle(qtbot, window)

        assert len(window.document.features) == 1
        assert window.document.base.source_path.endswith("bracket_rev_b.step")
        after = window.document.features[0].placement.anchor.plane
        assert after is not None
        assert abs(after.origin[2] - before.origin[2]) < 0.05

    def test_the_part_rebuilds_clean_after_the_swap(self, window, qtbot, fixtures):
        self._stamp_it(window, qtbot, fixtures)
        window.replace_part(fixtures / "bracket_rev_b.step")
        self._settle(qtbot, window)

        assert window._last_result is not None
        assert window._last_result.ok, window._last_result.errors
        assert not window._last_result.errors

    def test_a_thicker_part_carries_the_stamp_up(self, window, qtbot, fixtures):
        feature = self._stamp_it(window, qtbot, fixtures)
        assert feature.placement.anchor.plane.origin[2] == pytest.approx(8.0)

        window.replace_part(fixtures / "bracket_thicker.step")
        self._settle(qtbot, window)

        plane = window.document.features[0].placement.anchor.plane
        assert plane.origin[2] == pytest.approx(12.0, abs=1e-6)
        assert window._last_result.ok

    def test_replacing_is_undoable(self, window, qtbot, fixtures):
        self._stamp_it(window, qtbot, fixtures)
        window.replace_part(fixtures / "bracket_rev_b.step")
        self._settle(qtbot, window)
        assert window.action_undo.isEnabled()

        window.undo()
        self._settle(qtbot, window)
        assert window.document.base.source_path.endswith("bracket.step")
        assert len(window.document.features) == 1

    def test_the_status_line_says_what_happened(self, window, qtbot, fixtures):
        self._stamp_it(window, qtbot, fixtures)
        window.replace_part(fixtures / "bracket_rev_b.step")
        self._settle(qtbot, window)
        assert "part was replaced" in window.statusBar().currentMessage().lower()

    def test_replacing_with_a_part_that_will_not_open_changes_nothing(
        self, window, qtbot, fixtures
    ):
        self._stamp_it(window, qtbot, fixtures)
        before = window.document.base.source_path

        window.replace_part(fixtures / "logo.svg")  # not a part at all
        self._settle(qtbot, window)

        assert window.document.base.source_path == before
        assert len(window.document.features) == 1


class TestPartTransformPanel:
    """The mirror and scale group in the base-part view."""

    @pytest.fixture
    def panel(self, qtbot):
        from stamp.ui.properties import PropertiesPanel

        widget = PropertiesPanel()
        qtbot.addWidget(widget)
        return widget

    def _document(self, part):
        return Document(base=part)

    def test_the_group_shows_for_a_part_and_hides_for_a_feature(self, panel, bracket_step):
        panel.show_base(bracket_step, "mm", document=self._document(bracket_step))
        assert panel._transform.isVisibleTo(panel)
        panel.show_feature(Document(), a_feature(), (36.0, 16.0))
        assert not panel._transform.isVisibleTo(panel)

    def test_choosing_a_mirror_plane_reports_it(self, panel, bracket_step, qtbot):
        from stamp.core.document import MirrorPlane

        document = self._document(bracket_step)
        panel.show_base(bracket_step, "mm", document=document)
        with qtbot.waitSignal(panel.changed, timeout=1000):
            panel.part_mirror.setCurrentIndex(panel.part_mirror.findData(MirrorPlane.YZ))
        assert document.transform.mirror is MirrorPlane.YZ

    def test_a_uniform_scale_percentage_reaches_the_document(self, panel, bracket_step, qtbot):
        document = self._document(bracket_step)
        panel.show_base(bracket_step, "mm", document=document)
        with qtbot.waitSignal(panel.changed, timeout=1000):
            panel.part_scale_percent.setValue(125.0)
        assert document.transform.scale == (1.25, 1.25, 1.25)

    def test_typing_a_finished_size_solves_the_factor(self, panel, bracket_step, qtbot):
        document = self._document(bracket_step)
        panel.show_base(bracket_step, "mm", document=document)
        with qtbot.waitSignal(panel.changed, timeout=1000):
            panel.part_size_fields[0].setValue(160.0)
        # The bracket is 80 mm across, so 160 mm is twice the size.
        assert document.transform.scale[0] == pytest.approx(2.0)

    def test_per_axis_factors_need_uniform_off(self, panel, bracket_step, qtbot):
        document = self._document(bracket_step)
        panel.show_base(bracket_step, "mm", document=document)
        assert not panel.part_axis_fields[0].isEnabled()
        with qtbot.waitSignal(panel.changed, timeout=1000):
            panel.part_uniform.setChecked(False)
        assert panel.part_axis_fields[0].isEnabled()
        with qtbot.waitSignal(panel.changed, timeout=1000):
            panel.part_axis_fields[1].setValue(0.5)
        assert document.transform.scale[1] == pytest.approx(0.5)
        assert not document.transform.uniform

    def test_reset_puts_the_part_back(self, panel, bracket_step, qtbot):
        from stamp.core.document import MirrorPlane, PartTransform

        document = self._document(bracket_step)
        document.transform = PartTransform(mirror=MirrorPlane.XZ, scale=(2.0, 2.0, 2.0))
        panel.show_base(bracket_step, "mm", document=document)
        with qtbot.waitSignal(panel.changed, timeout=1000):
            panel._reset_part_transform()
        assert document.transform.is_identity

    def test_the_note_says_the_finished_size(self, panel, bracket_step):
        from stamp.core.document import PartTransform

        document = self._document(bracket_step)
        document.transform = PartTransform(scale=(2.0, 2.0, 2.0))
        panel.show_base(bracket_step, "mm", document=document)
        assert "160" in panel.part_transform_note.text()

    def test_the_note_reports_a_scale_that_will_not_run(self, panel, bracket_step):
        from stamp.core.document import PartTransform

        document = self._document(bracket_step)
        document.transform = PartTransform(scale=(1e9, 1e9, 1e9))
        panel.show_base(bracket_step, "mm", document=document)
        assert "larger than Stamp will go" in panel.part_transform_note.text()


class TestPartScaleDialog:
    def test_typing_a_size_updates_the_percentage(self, qtbot):
        from stamp.core.document import PartTransform
        from stamp.ui.dialogs import PartScaleDialog

        dialog = PartScaleDialog((80.0, 40.0, 14.0), PartTransform(), units="mm")
        qtbot.addWidget(dialog)
        dialog.sizes[0].setValue(120.0)
        assert dialog.percent.value() == pytest.approx(150.0)
        assert dialog.transform().scale == pytest.approx((1.5, 1.5, 1.5))

    def test_inches_are_converted_on_the_way_in(self, qtbot):
        from stamp.core.document import PartTransform
        from stamp.ui.dialogs import PartScaleDialog

        dialog = PartScaleDialog((80.0, 40.0, 14.0), PartTransform(), units="in")
        qtbot.addWidget(dialog)
        # 80 mm is 3.1496 in; ask for 6.2992 in and the factor is 2.
        dialog.sizes[0].setValue(80.0 * 2 / 25.4)
        # The field shows three decimals of an inch, so the factor lands within
        # a thousandth of an inch of two - which is the precision on screen.
        assert dialog.transform().scale[0] == pytest.approx(2.0, abs=1e-3)

    def test_per_axis_scaling_keeps_the_axes_apart(self, qtbot):
        from stamp.core.document import PartTransform
        from stamp.ui.dialogs import PartScaleDialog

        dialog = PartScaleDialog((80.0, 40.0, 14.0), PartTransform(), units="mm")
        qtbot.addWidget(dialog)
        dialog.uniform.setChecked(False)
        dialog.factors[0].setValue(2.0)
        dialog.factors[2].setValue(0.5)
        assert dialog.transform().scale == pytest.approx((2.0, 1.0, 0.5))
        assert not dialog.transform().uniform

    def test_the_fields_cannot_be_pushed_past_what_stamp_accepts(self, qtbot):
        """The spin ranges and PartTransform.validate agree, so OK never lies."""
        from stamp.core.document import MAX_PART_SCALE, MIN_PART_SCALE, PartTransform
        from stamp.ui.dialogs import PartScaleDialog

        dialog = PartScaleDialog((80.0, 40.0, 14.0), PartTransform(), units="mm")
        qtbot.addWidget(dialog)
        dialog.percent.setValue(1e12)
        assert dialog.percent.value() == pytest.approx(MAX_PART_SCALE * 100.0)
        assert not dialog.transform().validate()
        dialog.percent.setValue(0.0)
        assert dialog.percent.value() == pytest.approx(MIN_PART_SCALE * 100.0)
        assert not dialog.transform().validate()


class TestTheViewportRefusesAPlatformWithNoNativeWindow:
    """Item 0 of the adversarial review, and the reason the rest can be tested.

    Under Qt's offscreen platform ``winId()`` is not a native handle, and OCC's
    Cocoa_Window dereferenced it and took the process down - so the "fail once
    and disable the viewport" design in _init_viewer never got a turn.
    """

    @pytest.mark.skipif(not HEADLESS, reason="only the non-native platforms refuse")
    def test_starting_the_viewer_is_refused_rather_than_attempted(self, qtbot):
        from stamp.ui.viewport import Viewport

        widget = Viewport()
        qtbot.addWidget(widget)
        with pytest.raises(RuntimeError, match="no native window"):
            widget._start_viewer()

    @pytest.mark.skipif(not HEADLESS, reason="only the non-native platforms refuse")
    def test_the_window_still_opens_a_part_with_the_viewport_disabled(
        self, qtbot, fixtures
    ):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)

        win.viewport._init_viewer()
        assert win.viewport._init_failed
        assert win.viewport.context is None

        win.open_part(fixtures / "bracket.step")
        qtbot.waitUntil(lambda: win._last_result is not None, timeout=20000)
        assert win.document.base is not None


class TestUnsavedWorkIsNotThrownAway:
    """Item 1.  Only closeEvent asked; every other path that replaced the
    document cleared the undo stack with it and said nothing."""

    @pytest.fixture
    def window(self, qtbot, fixtures):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        win.open_part(fixtures / "bracket.step")
        win._dirty = True
        yield win
        win.rebuilder.shutdown()

    def test_open_part_asks_and_keeps_the_document_on_no(
        self, window, fixtures, monkeypatch
    ):
        monkeypatch.setattr(window, "_confirm", lambda *a: False)
        before = window.document
        window.open_part(fixtures / "bracket_rev_b.step")
        assert window.document is before

    def test_open_project_asks_too(self, window, tmp_path, monkeypatch):
        monkeypatch.setattr(window, "_confirm", lambda *a: False)
        opened = []
        monkeypatch.setattr(
            "stamp.io.project.open_project", lambda *a, **k: opened.append(1)
        )
        before = window.document
        window.open_project(tmp_path / "nothing.stamp")
        assert window.document is before
        assert not opened, "the file must not even be read after a No"

    def test_a_clean_document_is_never_asked_about(self, window, fixtures, monkeypatch):
        window._dirty = False
        asked = []
        monkeypatch.setattr(window, "_confirm", lambda *a: asked.append(1) or True)
        window.open_part(fixtures / "bracket_rev_b.step")
        assert not asked


class TestReopeningAProject:
    """Items 3 and W1/W4: what a project needs back before it can be rebuilt."""

    @pytest.fixture
    def window(self, qtbot):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        yield win
        win.rebuilder.shutdown()

    def test_the_base_part_is_reimported_at_the_unit_the_user_chose(
        self, window, fixtures, tmp_path, monkeypatch
    ):
        """An STL answered "inches" on import came back 25.4x smaller on reopen,
        which put every anchor off the part."""
        from stamp.core.document import Document
        from stamp.io import project as project_io
        from stamp.io.part_import import import_part
        from stamp.ui import main_window as mw

        part = import_part(fixtures / "bracket.stl", unit_scale=25.4).part
        assert part.unit_scale == 25.4
        path = tmp_path / "inches.stamp"
        project_io.save(Document(base=part, name="inches"), path)

        seen = {}

        def spy(parent, source, **options):
            seen.update(options)
            return import_part(source, unit_scale=options.get("unit_scale"))

        monkeypatch.setattr(mw, "import_part_for_ui", spy)
        window.open_project(path)

        assert seen["unit_scale"] == 25.4
        assert window.document.base.size == pytest.approx(part.size)

    def test_undo_brings_the_geometry_back_with_the_snapshot(
        self, window, fixtures, monkeypatch
    ):
        """A snapshot is JSON, so a restored base part carries no runtime shape.
        Without a re-import every rebuild after the undo had nothing to cut."""
        from stamp.io.part_import import import_part
        from stamp.ui import main_window as mw

        window.open_part(fixtures / "bracket.step")
        window.undo_stack.push("something", window.document.snapshot())

        calls = []

        def spy(parent, source, **options):
            calls.append(Path(source))
            return import_part(source)

        monkeypatch.setattr(mw, "import_part_for_ui", spy)
        window.document.base.runtime = None
        window.undo()

        assert calls == [Path(fixtures / "bracket.step")]
        assert window.document.base.runtime is not None
        assert window._dirty and "•" in window.windowTitle()


class TestSelectingAFeatureChangesNothing:
    """Item 4.  The pattern widgets were filled after _updating was cleared, so
    merely clicking a patterned feature replaced its pattern with a default one
    and put an undo entry, a dirty mark and a rebuild behind the click."""

    def test_a_circular_pattern_survives_being_shown(self, qtbot):
        from stamp.core.document import PatternKind, PatternSpec
        from stamp.ui.properties import PropertiesPanel

        panel = PropertiesPanel()
        qtbot.addWidget(panel)

        feature = a_feature()
        feature.pattern = PatternSpec(kind=PatternKind.CIRCULAR, count=6, angle=60.0)
        document = Document()
        document.add_feature(feature)

        changed = []
        panel.changed.connect(changed.append)
        panel.show_feature(document, feature, (36.0, 16.0))

        assert changed == []
        assert feature.pattern is not None
        assert feature.pattern.kind is PatternKind.CIRCULAR
        assert feature.pattern.count == 6
        assert panel.pattern_count.value() == 6


class TestDraggingAFeatureInTheTree:
    """Item 5.  QTreeWidget does an InternalMove itself instead of going through
    the model's moveRows, so rowsMoved never fired and the drag reordered the
    rows and nothing else."""

    def test_a_drop_reports_the_new_order(self, qtbot, bracket_step, monkeypatch):
        from PySide6.QtWidgets import QAbstractItemView, QTreeWidget

        from stamp.ui.feature_tree import FeatureTree

        tree = FeatureTree()
        qtbot.addWidget(tree)
        document = Document(base=bracket_step)
        first, second = a_feature("First"), a_feature("Second")
        document.add_feature(first)
        document.add_feature(second)
        tree.set_document(document)
        tree.select_feature(second.id)

        def rearrange(self, event):
            """What QTreeWidget's own drop does: take the row out, put it back."""
            self.insertTopLevelItem(1, self.takeTopLevelItem(2))

        monkeypatch.setattr(QTreeWidget, "dropEvent", rearrange)
        monkeypatch.setattr(
            FeatureTree, "dropIndicatorPosition",
            lambda self: QAbstractItemView.DropIndicatorPosition.AboveItem,
        )

        with qtbot.waitSignal(tree.reordered, timeout=1000) as blocker:
            tree.dropEvent(object())
        assert blocker.args == [second.id, 0]

    def test_a_drop_onto_a_row_is_refused(self, qtbot, bracket_step, monkeypatch):
        """Nesting one feature under another is not something a document can say."""
        from PySide6.QtWidgets import QAbstractItemView, QTreeWidget

        from stamp.ui.feature_tree import FeatureTree

        tree = FeatureTree()
        qtbot.addWidget(tree)
        document = Document(base=bracket_step)
        document.add_feature(a_feature("First"))
        document.add_feature(a_feature("Second"))
        tree.set_document(document)

        moved = []
        monkeypatch.setattr(QTreeWidget, "dropEvent", lambda self, e: moved.append(1))
        monkeypatch.setattr(
            FeatureTree, "dropIndicatorPosition",
            lambda self: QAbstractItemView.DropIndicatorPosition.OnItem,
        )

        class Event:
            def ignore(self):
                self.ignored = True

        event = Event()
        tree.dropEvent(event)
        assert not moved
        assert event.ignored

    def test_renaming_a_colour_stamp_does_not_collect_glyphs(
        self, qtbot, bracket_step
    ):
        """Item 14: the strip list had the add and cut arrows but not the stamp
        diamond, so every rename put another one in front of the name."""
        from stamp.core.document import OperationKind
        from stamp.ui.feature_tree import FeatureTree

        tree = FeatureTree()
        qtbot.addWidget(tree)
        document = Document(base=bracket_step)
        feature = a_feature("Badge")
        feature.operation.kind = OperationKind.COLOR
        document.add_feature(feature)
        tree.set_document(document)

        item = tree.topLevelItem(1)
        assert "◈" in item.text(0)
        with qtbot.waitSignal(tree.renamed, timeout=1000) as blocker:
            item.setText(0, item.text(0).replace("Badge", "Crest"))
        assert blocker.args == [feature.id, "Crest"]


class TestWindowKeys:
    @pytest.fixture
    def window(self, qtbot, fixtures):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        # A window shortcut needs a shown window; QTest posts the key to the
        # widget, and Qt looks for the shortcut up the shown hierarchy.
        win.show()
        qtbot.waitExposed(win)
        yield win
        win.rebuilder.shutdown()

    def test_the_preset_view_keys_fire(self, window, qtbot, monkeypatch):
        """Item 6: 1-7 were on hidden actions and on the View menu entries at the
        same time, so Qt called the overload ambiguous and fired neither."""
        from PySide6.QtTest import QTest

        seen = []
        monkeypatch.setattr(window.viewport, "set_preset_view", seen.append)
        for key, preset in (
            (Qt.Key.Key_1, "front"), (Qt.Key.Key_5, "top"), (Qt.Key.Key_7, "iso"),
        ):
            QTest.keyClick(window, key)
            assert seen and seen[-1] == preset, preset

    def test_every_orientation_key_is_bound_exactly_once(self, window):
        from PySide6.QtGui import QKeySequence

        for digit in "1234567":
            wanted = QKeySequence(digit)
            bound = [a for a in window.actions() if a.shortcut() == wanted]
            assert len(bound) == 1, f"{digit} is bound {len(bound)} times"

    def test_space_leaves_a_focused_button_alone(self, window, qtbot):
        """Item 15: Space was a window shortcut, so it toggled the preview
        instead of ticking whatever checkbox had the keyboard."""
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QCheckBox

        box = QCheckBox(window)
        box.show()
        box.setFocus()
        assert box.hasFocus()

        before = window.action_preview.isChecked()
        QTest.keyClick(window, Qt.Key.Key_Space)
        assert window.action_preview.isChecked() == before

    def test_space_still_toggles_the_preview_from_the_viewport(self, window, qtbot):
        from PySide6.QtTest import QTest

        window.viewport.setFocus()
        before = window.action_preview.isChecked()
        QTest.keyClick(window, Qt.Key.Key_Space)
        assert window.action_preview.isChecked() != before


class TestTheDocumentIsMarkedDirty:
    """Item 8.  Undo, redo, a unit change and the size lock all edited the
    document and left the title saying it was saved."""

    @pytest.fixture
    def window(self, qtbot, fixtures):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        win.open_part(fixtures / "bracket.step")
        win._dirty = False
        yield win
        win.rebuilder.shutdown()

    def test_undo_and_redo_both_mark_it(self, window):
        window.undo_stack.push("something", window.document.snapshot())
        window.undo()
        assert window._dirty
        assert "•" in window.windowTitle()

        window._dirty = False
        window.redo()
        assert window._dirty

    def test_changing_the_units_marks_it(self, window):
        window.units_box.setCurrentIndex(window.units_box.findData("in"))
        assert window.document.units == "in"
        assert window._dirty

    def test_reloading_the_same_units_does_not(self, window):
        window.units_box.setCurrentIndex(window.units_box.findData(window.document.units))
        assert not window._dirty

    def test_the_size_lock_marks_it(self, qtbot):
        from stamp.ui.properties import PropertiesPanel

        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        feature = a_feature()
        document = Document()
        document.add_feature(feature)
        panel.show_feature(document, feature, (36.0, 16.0))

        with qtbot.waitSignal(panel.changed, timeout=1000):
            panel.lock_button.setChecked(not panel.lock_button.isChecked())


class TestExportsWaitForTheRebuild:
    """Item 7.  An export inside the 250 ms debounce read _last_result and wrote
    the shape from before the edit that was still waiting."""

    @pytest.fixture
    def window(self, qtbot, fixtures):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        win.open_part(fixtures / "bracket.step")
        qtbot.waitUntil(lambda: win._last_result is not None, timeout=20000)
        yield win
        win.rebuilder.shutdown()

    def test_an_export_during_the_debounce_is_refused(self, window, monkeypatch):
        written = []
        monkeypatch.setattr(
            "stamp.io.export.export_step", lambda *a, **k: written.append(1)
        )
        window.rebuilder._timer.start()
        assert window.rebuilder.pending

        window.export_step()

        assert not written
        assert "still rebuilding" in window.statusBar().currentMessage()

    def test_an_export_while_the_worker_runs_is_refused(self, window, monkeypatch):
        written = []
        monkeypatch.setattr(
            "stamp.io.export.export_stl", lambda *a, **k: written.append(1)
        )
        monkeypatch.setattr(type(window.rebuilder), "busy", property(lambda _s: True))
        window.export_stl()
        assert not written
        assert "still rebuilding" in window.statusBar().currentMessage()


class TestCancellingAPick:
    """Item 9.  Esc cleared the profile, to-face and re-pick flags and left the
    three origin picks, the preset template and the selection filter set."""

    @pytest.fixture
    def window(self, qtbot, fixtures):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        win.open_part(fixtures / "bracket.step")
        yield win
        win.rebuilder.shutdown()

    def test_it_clears_every_pick_and_the_filter(self, window):
        from stamp.core.document import ProfileRef, TextSpec

        window._pending_profile = ProfileRef(text=TextSpec(text="X"))
        window._pending_profile_size = (12.0, 4.0)
        window._pending_feature_template = a_feature()
        window._picking_to_face = True
        window._repicking = True
        window._picking_alignment_edge = True
        window._picking_origin_vertex = True
        window._picking_hole_center = True
        window.selection_box.setCurrentIndex(window.selection_box.findData("vertex"))

        window._cancel_pending()

        assert window._pending_profile is None
        assert window._pending_feature_template is None
        assert not window._picking_to_face
        assert not window._repicking
        assert not window._picking_alignment_edge
        assert not window._picking_origin_vertex
        assert not window._picking_hole_center
        assert window.selection_box.currentData() == "face"
        assert not window._pick_waiting()


class TestPlacingAPresetOnAMesh:
    """Item 10.  The mesh path ignored the preset template entirely, so a preset
    inserted onto an STL arrived as a plain 0.5 mm cut called after its file."""

    @pytest.fixture
    def window(self, qtbot, fixtures):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        win.open_part(fixtures / "bracket.stl")
        yield win
        win.rebuilder.shutdown()

    def test_the_template_is_applied_and_then_cleared(self, window):
        from stamp.core.document import (
            FeatureMetadata,
            Modifier,
            ModifierKind,
            OperationKind,
            PatternSpec,
            ProfileRef,
            TextSpec,
        )

        template = a_feature("Serial plate")
        template.operation.kind = OperationKind.ADD
        template.operation.depth = 1.25
        template.modifiers.append(Modifier(kind=ModifierKind.FILLET, value=0.3))
        template.pattern = PatternSpec(count=3)
        template.metadata = FeatureMetadata(identifier="1234")
        template.placement.rotation = 30.0
        window._pending_feature_template = template

        class Region:
            point = (30.0, 20.0, 8.0)
            plane = template.placement.anchor.plane
            warnings: list[str] = []

        ref = ProfileRef(text=TextSpec(text="ABC"))
        window._create_mesh_feature(ref, Region())

        made = window.document.features[-1]
        assert made.name.startswith("Serial plate")
        assert made.operation.kind is OperationKind.ADD
        assert made.operation.depth == 1.25
        assert [m.kind for m in made.modifiers] == [ModifierKind.FILLET]
        assert made.pattern is not None and made.pattern.count == 3
        assert made.metadata.identifier == "1234"
        assert made.placement.rotation == 30.0
        assert window._pending_feature_template is None

    def test_a_code_gets_its_own_name(self, window):
        from stamp.core.document import CodeSpec, ProfileRef

        class Region:
            point = (30.0, 20.0, 8.0)
            plane = a_feature().placement.anchor.plane
            warnings: list[str] = []

        window._create_mesh_feature(ProfileRef(code=CodeSpec(payload="X")), Region())
        assert window.document.features[-1].name in ("QR code", "Data Matrix")

    def test_to_face_depth_says_why_it_cannot_work(self, window):
        """Item 18b: the pick was swallowed and the flag never cleared, so every
        later click on the mesh went nowhere."""
        window._picking_to_face = True
        window._on_mesh_picked()
        assert not window._picking_to_face
        assert "solid part" in window.statusBar().currentMessage()


class TestTheHandleOverlay:
    """Item 11.  Any left press inside the frame started a translate drag, and
    the release committed one whether or not anything had moved."""

    @pytest.fixture
    def overlay(self, qtbot):
        from stamp.ui.handles import HandleOverlay
        from stamp.ui.viewport import Viewport

        viewport = Viewport()
        qtbot.addWidget(viewport)
        widget = HandleOverlay(viewport)
        widget.set_feature(a_feature(), (36.0, 16.0))
        return widget

    def _press(self, overlay, uv=(0.0, 0.0)):
        from stamp.ui.handles import Mode, _Drag

        overlay._drag = _Drag(
            mode=Mode.TRANSLATE,
            handle=0,
            start_screen=QPoint(0, 0),
            start_offset=overlay.feature.placement.offset_2d,
            start_scale=overlay.feature.placement.scale,
            start_rotation=overlay.feature.placement.rotation,
            start_uv=uv,
        )

    def test_a_click_that_moves_nothing_commits_nothing(self, overlay):
        committed = []
        overlay.placement_committed.connect(committed.append)
        self._press(overlay)
        overlay._end_drag()
        assert committed == [], "a click was worth an undo entry and a rebuild"

    def test_a_drag_that_moves_still_commits(self, overlay):
        committed = []
        overlay.placement_committed.connect(committed.append)
        self._press(overlay)
        overlay.feature.placement.offset_2d = (4.0, 0.0)
        overlay._end_drag()
        assert committed == ["move"]

    def test_it_stands_down_while_the_window_waits_for_a_click(self, overlay, qtbot):
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent

        overlay.pick_pending = lambda: True
        event = QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(10.0, 10.0), QPointF(10.0, 10.0),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        assert overlay._begin_drag(event) is False
        assert overlay._drag is None


class TestPartVisibilityBeforeTheFirstRebuild:
    """Item 13.  Both calls handed None to _display_geometry, which raises in
    solid mode - so ticking a part off before the first rebuild crashed."""

    @pytest.fixture
    def window(self, qtbot):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        yield win
        win.rebuilder.shutdown()

    def _two_part_base(self, window, bracket_step):
        from dataclasses import replace as _replace

        from stamp.core.document import PartBody

        base = _replace(bracket_step)
        base.parts = [
            PartBody(index=0, name="one", bbox=base.bbox),
            PartBody(index=1, name="two", bbox=base.bbox),
        ]
        window.document = Document(base=base)
        window._last_result = None

    def test_hiding_a_part_is_recorded_and_does_not_raise(self, window, bracket_step):
        self._two_part_base(window, bracket_step)
        window.set_part_visible(1, False)
        assert window.document.base.parts[1].visible is False

    def test_isolating_a_part_does_not_raise_either(self, window, bracket_step):
        self._two_part_base(window, bracket_step)
        window.isolate_part(0)
        assert [p.visible for p in window.document.base.parts] == [True, False]


class TestARebuildThatWasCancelled:
    """Item 17.  _dispatch cancelled through the running generation but left it
    as the one the controller accepts, so a worker that had already passed its
    last cancel check delivered its result over the document that replaced it."""

    def test_a_late_result_from_a_cancelled_rebuild_is_dropped(self, qtbot):
        import time

        class UncancellableEngine:
            """Checks for a cancel once, at the start, and then commits."""

            def __init__(self) -> None:
                self.runs = 0

            def rebuild(self, document, should_cancel=None, progress=None):
                self.runs += 1
                mine = self.runs
                if should_cancel and should_cancel():
                    from stamp.core.rebuild import Cancelled

                    raise Cancelled()
                time.sleep(0.25)
                return mine

        from stamp.ui.rebuild_worker import RebuildController

        engine = UncancellableEngine()
        controller = RebuildController(engine)
        delivered = []
        controller.finished.connect(delivered.append)
        try:
            controller.request(Document(), immediate=True)
            qtbot.wait(100)  # past the first and only cancel check
            controller.request(Document(), immediate=True)
            qtbot.waitUntil(lambda: engine.runs == 2 and not controller.busy, timeout=8000)
            qtbot.wait(100)
            assert delivered == [2], "the stale answer must not be applied"
        finally:
            controller.shutdown()


class TestTheRecentProjectsMenu:
    """Item 18a.  Projects were recorded and nothing ever showed them."""

    @pytest.fixture
    def window(self, qtbot):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        yield win
        win.rebuilder.shutdown()

    def test_it_lists_what_is_there_and_drops_what_is_not(self, window, tmp_path):
        here = tmp_path / "here.stamp"
        here.write_bytes(b"")
        gone = tmp_path / "gone.stamp"

        window.settings.setValue("recent/projects", [str(gone), str(here)])
        window._fill_recent_menu()

        names = [a.text() for a in window.recent_menu.actions()]
        assert names == ["here.stamp"]

    def test_an_empty_list_says_so_rather_than_offering_nothing(self, window):
        window.settings.setValue("recent/projects", [])
        window._fill_recent_menu()
        actions = window.recent_menu.actions()
        assert len(actions) == 1 and not actions[0].isEnabled()

    def test_opening_one_goes_through_the_unsaved_work_question(
        self, window, tmp_path, monkeypatch
    ):
        path = tmp_path / "here.stamp"
        path.write_bytes(b"")
        window.settings.setValue("recent/projects", [str(path)])
        window._fill_recent_menu()

        window.document = Document(base=None)
        opened = []
        monkeypatch.setattr(window, "open_project", lambda p: opened.append(p))
        window.recent_menu.actions()[0].trigger()
        assert opened == [path]


class TestTheHeaderFollowsTheDesktopTheme:
    """Item 16.  The ribbon re-themed itself on a palette change; the menu bar,
    the ribbon's holder and the update bar were painted once in _build_menus."""

    def test_a_palette_change_repaints_the_header(self, qtbot):
        from PySide6.QtCore import QEvent

        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        try:
            applied = []
            win._apply_header_theme = lambda: applied.append(1)
            win.changeEvent(QEvent(QEvent.Type.PaletteChange))
            assert applied == [1]
        finally:
            win.rebuilder.shutdown()


class TestFitToFaceOnATiltedFace:
    """Item 18c.  The face was measured with a world-axis bounding box, so on a
    face that is not square to the axes the fit used the box the face sits in -
    bigger than the face, and in neither of the two directions the profile is
    actually scaled along."""

    @pytest.fixture
    def window(self, qtbot):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        yield win
        win.rebuilder.shutdown()

    def test_the_face_is_measured_in_its_own_axes(self, window):
        import math

        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf

        from stamp.core.document import Anchor, AnchorKind, BasePart
        from stamp.core.refs import faces_of, make_face_ref, plane_from_face

        # A 60 x 20 slab turned 45 degrees about z.  Its top face is still 60 x 20,
        # but its world-axis box is about 56.6 x 56.6.
        box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(60, 20, 10)).Shape()
        turn = gp_Trsf()
        turn.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.radians(45))
        shape = BRepBuilderAPI_Transform(box, turn, True).Shape()

        from stamp.io.part_import import bounding_box

        def centre(face):
            x0, y0, z0, x1, y1, z1 = bounding_box(face)
            return ((x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0)

        top = next(
            f for f in faces_of(shape)
            if make_face_ref(f, centre(f)).normal[2] > 0.99
        )
        point = centre(top)
        plane, _ = plane_from_face(top, point)

        window.document = Document(base=BasePart(mode="solid", runtime=shape))
        feature = a_feature()
        feature.placement.anchor = Anchor(
            kind=AnchorKind.FACE, face_ref=make_face_ref(top, point), plane=plane
        )
        window.document.add_feature(feature)

        size = window._anchor_face_size(feature)
        assert size is not None
        assert sorted(size) == pytest.approx([20.0, 60.0], abs=1e-6)


class TestTheBatchRunsOffTheGuiThread:
    """Item 12.  simulate_batch and run_batch both ran on the GUI thread, so a
    batch of any size froze the window until the desktop offered to kill it."""

    @pytest.fixture
    def window(self, qtbot):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        qtbot.addWidget(win)
        yield win
        win.rebuilder.shutdown()

    def test_the_run_happens_on_a_worker_and_the_report_comes_back(
        self, window, qtbot, fixtures, tmp_path
    ):
        import csv as csv_module

        from stamp.core.document import Document as Doc
        from stamp.io import project as project_io
        from stamp.io.part_import import import_part

        template = tmp_path / "template.stamp"
        project_io.save(
            Doc(base=import_part(fixtures / "bracket.step").part, name="template"),
            template,
        )
        csv_path = tmp_path / "rows.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv_module.DictWriter(handle, ["input", "output"])
            writer.writeheader()
            writer.writerow({"input": str(fixtures / "bracket.step"), "output": "one"})
        out = tmp_path / "out"
        out.mkdir()

        gui_thread = window.thread()
        ran_on = []
        import stamp.ui.main_window as mw

        real = mw.run_batch

        def spy(*args, **kwargs):
            from PySide6.QtCore import QThread

            ran_on.append(QThread.currentThread())
            return real(*args, **kwargs)

        monkeyed = mw.run_batch
        mw.run_batch = spy
        try:
            problems = []
            window._on_batch_failed = problems.append
            thread = window.start_batch(str(template), str(csv_path), str(out), "step")
            qtbot.waitUntil(
                lambda: bool(window._batch_report is not None or problems), timeout=60000
            )
            assert not problems, problems
            thread.wait(5000)
            assert window._batch_report is not None
        finally:
            mw.run_batch = monkeyed

        assert ran_on and ran_on[0] is not gui_thread, "the batch blocked the window"
        assert (out / "stamp-batch-report.json").exists()
        assert (out / "one.step").exists()
        assert not window._batch_report.stopped

    def test_the_output_folder_is_chosen_before_the_dry_run(
        self, window, monkeypatch, tmp_path
    ):
        """From the batch fixer: simulate_batch checks output containment against
        the folder, so asking afterwards let the dry run pass rows the real run
        refused."""
        from PySide6.QtWidgets import QFileDialog, QInputDialog

        order = []
        monkeypatch.setattr(
            QFileDialog, "getOpenFileName",
            staticmethod(lambda *a, **k: (str(tmp_path / "t.stamp"), "")),
        )
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory",
            staticmethod(lambda *a, **k: order.append("folder") or str(tmp_path)),
        )
        monkeypatch.setattr(
            QInputDialog, "getItem", staticmethod(lambda *a, **k: ("step", True))
        )

        def fake_simulate(template, csv_path, fmt, output_dir=None):
            order.append(("simulate", output_dir))
            raise BatchErrorForTest()

        class BatchErrorForTest(Exception):
            pass

        import stamp.ui.main_window as mw

        monkeypatch.setattr(mw, "BatchError", BatchErrorForTest)
        monkeypatch.setattr(mw, "simulate_batch", fake_simulate)
        window.batch_stamp()

        assert order[0] == "folder"
        assert order[1] == ("simulate", str(tmp_path))


class TestTheUnitAnswerForAmbiguousArtwork:
    """The unit prompt for an SVG or DXF with no physical size took an answer and
    threw it away: whatever unit was picked, the artwork stayed at the 96 dpi
    reading the dialog had just complained about."""

    @pytest.fixture
    def window(self, qtbot, fixtures):
        from stamp.ui.main_window import MainWindow

        win = MainWindow()
        # Not interactive: _ask then takes the stub dialog's default without
        # showing anything, which is the seam these tests need.
        win.interactive = False
        qtbot.addWidget(win)
        win.open_part(fixtures / "bracket.step")
        yield win
        win.rebuilder.shutdown()

    def test_the_artwork_is_rescaled_to_the_answer(
        self, window, fixtures, monkeypatch
    ):
        from stamp.ui import dialogs

        class InchesDialog:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def scale(self):
                return 25.4

            def unit(self):
                return "in"

        monkeypatch.setattr(dialogs, "UnitPromptDialog", InchesDialog)
        window.add_profile(fixtures / "no_units.dxf")

        ref = window._pending_profile
        assert ref is not None
        assert ref.unit_scale == 25.4
        native = ref.native_size_mm
        assert native[0] > 0 and native[1] > 0

        plain = window.profiles.get(ref)
        assert plain.width == pytest.approx(native[0])

    def test_the_import_options_reach_the_reference(self, window, fixtures):
        """close_open_loops was carried; join tolerance, unit scale and the
        background flag were dropped, so the cache key did not describe the
        import that filled it."""
        window.add_profile(fixtures / "logo.svg")
        ref = window._pending_profile
        assert ref is not None
        assert ref.join_tolerance > 0
        assert ref.unit_scale == 1.0
        assert ref.keep_background is False


class TestTheDraftFieldOnAWrap:
    """W6.  The geometry refuses a draft on a cylindrical wrap - the walls of a
    wrap are radial - so a field that can only produce an error row is switched
    off with the reason on it."""

    @pytest.fixture
    def panel(self, qtbot):
        from stamp.ui.properties import PropertiesPanel

        widget = PropertiesPanel()
        qtbot.addWidget(widget)
        return widget

    def _shown(self, panel, surface: str, mode):
        from stamp.core.document import Anchor, AnchorKind, FaceRef

        feature = a_feature()
        feature.placement.mode = mode
        feature.placement.anchor = Anchor(
            kind=AnchorKind.FACE,
            face_ref=FaceRef(
                point=(0.0, 0.0, 0.0), normal=(0.0, 0.0, 1.0), surface_type=surface
            ),
            plane=feature.placement.anchor.plane,
        )
        document = Document()
        document.add_feature(feature)
        panel.show_feature(document, feature, (36.0, 16.0))
        return feature

    def test_a_wrap_on_a_cylinder_disables_it(self, panel):
        from stamp.core.document import PlacementMode

        self._shown(panel, "cylinder", PlacementMode.WRAP)
        assert not panel.draft_field.isEnabled()
        assert "radial" in panel.draft_field.toolTip()

    def test_a_wrap_on_a_cone_keeps_it(self, panel):
        from stamp.core.document import PlacementMode

        self._shown(panel, "cone", PlacementMode.WRAP)
        assert panel.draft_field.isEnabled()

    def test_flat_placement_on_a_cylinder_keeps_it(self, panel):
        from stamp.core.document import PlacementMode

        self._shown(panel, "cylinder", PlacementMode.PLANAR)
        assert panel.draft_field.isEnabled()

    def test_switching_to_a_wrap_switches_it_off(self, panel):
        from stamp.core.document import PlacementMode

        self._shown(panel, "cylinder", PlacementMode.PLANAR)
        panel.placement_mode.setCurrentIndex(
            panel.placement_mode.findData(PlacementMode.WRAP)
        )
        assert not panel.draft_field.isEnabled()
