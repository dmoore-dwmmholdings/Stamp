"""The definition of done - spec §14.

The seven steps, in order, through the real window.  If this passes on a real part
with real artwork, the spec says ship it.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

pytestmark = pytest.mark.skipif(
    os.environ.get("QT_QPA_PLATFORM") == "offscreen",
    reason="the OCC viewport needs a real window",
)

LOGO_AREA = 36 * 16 - 8 * 8  # logo.svg, after the overlapping circle is resolved


@pytest.fixture
def window(qtbot):
    from stamp.ui.main_window import MainWindow

    win = MainWindow()
    win.interactive = False
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    yield win
    win.rebuilder.shutdown()


def settle(qtbot, window) -> None:
    qtbot.wait(400)
    for _ in range(200):
        if not window.rebuilder.busy:
            break
        qtbot.wait(100)
    qtbot.wait(200)


def top_face(window):
    from stamp.core.refs import face_center, face_normal_at, faces_of, surface_kind

    for face in faces_of(window.document.base.runtime):
        if surface_kind(face) != "plane":
            continue
        center = face_center(face)
        if abs(center[2] - 8.0) < 1e-6 and face_normal_at(face, center)[2] > 0.9:
            return face
    pytest.fail("no top face on the bracket")


def test_definition_of_done(window, qtbot, fixtures, tmp_path):
    from stamp.core.document import Direction, EdgeRole, OperationKind
    from stamp.io import export as export_io
    from stamp.io.part_import import import_part

    # 1. Open a STEP file of a bracket.
    window.open_part(fixtures / "bracket.step")
    settle(qtbot, window)
    assert window.document.base is not None
    assert window.document.base.mode == "solid"
    base_volume = window.document.base.volume

    # 2. Drop an SVG logo onto a flat face.
    window.add_profile(fixtures / "logo.svg")
    assert window._pending_profile is not None
    window._create_feature(window._pending_profile, top_face(window), (30.0, 20.0, 8.0))
    settle(qtbot, window)
    logo = window.document.features[0]
    window.tree.select_feature(logo.id)
    qtbot.wait(150)

    # 3. Size it to exactly 40 mm wide, center it, and rotate it 90 degrees.
    window.properties.width_field.setValue(40.0)
    window._center_on_face()
    window.properties.rotation_field.setValue(90.0)
    settle(qtbot, window)
    assert window.properties.width_field.value() == pytest.approx(40.0)
    assert logo.placement.offset_2d == (0.0, 0.0)
    assert logo.placement.rotation == pytest.approx(90.0)
    scale = logo.placement.scale[0]

    # 4. Emboss it 0.8 mm proud with a 0.3 mm fillet on the top edges.
    logo.operation.kind = OperationKind.ADD
    logo.operation.direction = Direction.OUT_OF
    window.properties.show_feature(window.document, logo, window._native_size(logo))
    window.properties.depth_field.setValue(0.8)
    settle(qtbot, window)
    window.properties.add_modifier_requested.emit("fillet")
    settle(qtbot, window)

    assert len(logo.modifiers) == 1
    assert logo.modifiers[0].target.role is EdgeRole.TOP
    assert logo.modifiers[0].value == pytest.approx(0.3)
    embossed = window._last_result.volume
    # The emboss adds roughly its footprint times its depth, less the fillet.
    added = embossed - base_volume
    assert added == pytest.approx(LOGO_AREA * scale**2 * 0.8, rel=0.05)
    assert not window._last_result.errors

    # 5. Add a second feature that cuts a serial number 0.5 mm deep.
    window.add_profile(fixtures / "serial.dxf")
    window._create_feature(window._pending_profile, top_face(window), (20.0, 32.0, 8.0))
    settle(qtbot, window)
    serial = window.document.features[1]
    window.tree.select_feature(serial.id)
    qtbot.wait(150)
    window.properties.width_field.setValue(24.0)
    window.properties.depth_field.setValue(0.5)
    settle(qtbot, window)

    assert len(window.document.features) == 2
    assert serial.operation.kind is OperationKind.CUT
    assert window._last_result.volume < embossed  # the cut removed material
    assert not window._last_result.errors

    # 6a. Export a STEP.  It must be a valid solid that reads back identically.
    step_path = tmp_path / "bracket.step"
    step = export_io.export_step(window._geometry(), step_path)
    assert step.path.exists() and step.size_bytes > 0

    reread = import_part(step_path).part
    assert reread.valid
    assert reread.volume == pytest.approx(window._last_result.volume, rel=1e-6)

    # 6b. Export an STL.  It must be watertight, or a slicer will refuse it.
    stl_path = tmp_path / "bracket.stl"
    stl = export_io.export_stl(window._geometry(), stl_path, mode="solid", quality="normal")
    assert stl.triangle_count > 0
    assert not stl.warnings, stl.warnings

    import trimesh

    mesh = trimesh.load(str(stl_path), force="mesh")
    assert mesh.is_watertight
    assert mesh.volume == pytest.approx(window._last_result.volume, rel=0.01)

    # 7. Reopen the project, change the emboss to 1.2 mm, and watch it rebuild.
    project_path = tmp_path / "bracket.stamp"
    window._project_path = project_path
    window.save_project()
    assert project_path.exists()

    window.open_project(project_path)
    settle(qtbot, window)
    assert len(window.document.features) == 2
    reopened_volume = window._last_result.volume

    reopened_logo = window.document.features[0]
    assert reopened_logo.operation.depth == pytest.approx(0.8)
    assert reopened_logo.placement.rotation == pytest.approx(90.0)
    assert len(reopened_logo.modifiers) == 1

    window.tree.select_feature(reopened_logo.id)
    qtbot.wait(150)
    window.properties.depth_field.setValue(1.2)
    settle(qtbot, window)

    assert not window._last_result.errors
    grown = window._last_result.volume - reopened_volume
    assert grown == pytest.approx(LOGO_AREA * scale**2 * 0.4, rel=0.05)


def test_a_mesh_part_goes_all_the_way_through(window, qtbot, fixtures, tmp_path):
    """STL in, STL out - the §2 promise, end to end."""
    from stamp.core.document import Direction, OperationKind
    from stamp.io import export as export_io

    window.open_part(fixtures / "bracket.stl")
    settle(qtbot, window)
    assert window.document.base.mode == "mesh"
    assert not window.action_export_step.isEnabled()

    # A mesh part has no faces to pick, so the plane comes from a datum.
    from stamp.core.document import Anchor, AnchorKind, Plane

    window.add_profile(fixtures / "logo.svg")
    ref = window._pending_profile
    from stamp.core.document import Feature, Operation, Placement

    feature = Feature(
        name="Logo",
        profile=ref,
        placement=Placement(
            anchor=Anchor(
                kind=AnchorKind.DATUM,
                datum="XY",
                datum_offset=8.0,
                plane=Plane(origin=(30.0, 20.0, 8.0), normal=(0.0, 0.0, 1.0),
                            u_axis=(1.0, 0.0, 0.0)),
            )
        ),
        operation=Operation(kind=OperationKind.ADD, depth=0.8,
                            direction=Direction.OUT_OF),
    )
    window.document.add_feature(feature)
    window._pending_profile = None
    window.request_rebuild(immediate=True)
    settle(qtbot, window)

    assert not window._last_result.errors
    assert window._last_result.volume > window.document.base.volume

    stl_path = tmp_path / "mesh_out.stl"
    result = export_io.export_stl(window._geometry(), stl_path, mode="mesh")
    assert result.triangle_count > 0
    assert not result.warnings

    import trimesh

    mesh = trimesh.load(str(stl_path), force="mesh")
    assert mesh.is_watertight
