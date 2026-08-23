"""The interface - spec §6.2, §6.6, §7.

These drive the real widgets.  The viewport needs a real window and an OpenGL
context, so the tests that build a whole window are marked and skipped when no
display is available.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

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


class TestPropertiesPanel:
    @pytest.fixture
    def panel(self, qtbot):
        from stamp.ui.properties import PropertiesPanel

        widget = PropertiesPanel()
        qtbot.addWidget(widget)
        return widget

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
