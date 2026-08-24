from __future__ import annotations

from stamp.core.document import (
    BasePart,
    Feature,
    PatternKind,
    PatternSpec,
    Placement,
    PlacementMode,
)
from stamp.core.rebuild import RebuildResult
from stamp.io.export import preflight_export


def test_pattern_round_trips_and_expands_linearly():
    feature = Feature(pattern=PatternSpec(kind=PatternKind.LINEAR, count=3, spacing=5.0))
    feature.placement.offset_2d = (2.0, 4.0)
    restored = Feature.from_dict(feature.to_dict())
    assert restored.pattern is not None
    assert [copy.placement.offset_2d for copy in restored.pattern_instances()] == [
        (2.0, 4.0), (7.0, 4.0), (12.0, 4.0)
    ]


def test_mirror_pattern_is_two_instances_regardless_of_count():
    feature = Feature(pattern=PatternSpec(kind=PatternKind.MIRROR, count=9))
    feature.placement.offset_2d = (3.0, 4.0)
    copies = feature.pattern_instances()
    assert len(copies) == 2
    assert copies[1].placement.offset_2d == (3.0, -4.0)


def test_circular_pattern_rotates_instances_about_its_center():
    feature = Feature(
        pattern=PatternSpec(kind=PatternKind.CIRCULAR, count=4, angle=360.0, center=(0.0, 0.0))
    )
    feature.placement.offset_2d = (10.0, 0.0)
    copies = feature.pattern_instances()
    assert [tuple(round(value, 6) for value in copy.placement.offset_2d) for copy in copies] == [
        (10.0, 0.0), (0.0, 10.0), (-10.0, 0.0), (0.0, -10.0)
    ]


def test_linear_pattern_rebuilds_each_instance(bracket_step, fixtures):
    from stamp.core.document import (
        Anchor,
        AnchorKind,
        Document,
        Operation,
        OperationKind,
        ProfileRef,
    )
    from stamp.core.profiles import ProfileCache
    from stamp.core.rebuild import RebuildEngine
    from stamp.core.refs import (
        face_center,
        faces_of,
        make_face_ref,
        plane_from_face,
        surface_kind,
    )
    from stamp.io.profile_import import file_hash

    face = max((face for face in faces_of(bracket_step.runtime) if surface_kind(face) == "plane"), key=lambda face: face_center(face)[2])
    point = face_center(face)
    plane, _warnings = plane_from_face(face, point)
    feature = Feature(
        profile=ProfileRef(
            source_path=str(fixtures / "logo.svg"), source_hash=file_hash(fixtures / "logo.svg")
        ),
        placement=Placement(anchor=Anchor(kind=AnchorKind.FACE, face_ref=make_face_ref(face, point), plane=plane), scale=(0.1, 0.1)),
        operation=Operation(kind=OperationKind.ADD, depth=0.2),
        pattern=PatternSpec(kind=PatternKind.LINEAR, count=2, spacing=10.0),
    )
    result = RebuildEngine(ProfileCache().get).rebuild(Document(base=bracket_step, features=[feature]))
    assert result.ok, result.errors


def test_wrap_placement_round_trips_without_breaking_old_projects():
    placement = Placement(mode=PlacementMode.WRAP)
    assert Placement.from_dict(placement.to_dict()).mode is PlacementMode.WRAP
    assert Placement.from_dict({}).mode is PlacementMode.PLANAR


def test_preflight_blocks_step_for_mesh_and_allows_warnings():
    from stamp.core.document import Document

    document = Document(base=BasePart(mode="mesh", runtime=object()))
    rebuilt = RebuildResult(geometry=object(), mode="mesh")
    report = preflight_export(document, rebuilt, "step", "out.step")
    assert not report.ok
    assert "STL" in report.errors[0]


def test_preflight_rejects_a_missing_output_folder(tmp_path):
    from stamp.core.document import Document

    document = Document(base=BasePart(mode="solid", runtime=object()))
    report = preflight_export(document, RebuildResult(geometry=object()), "stl", tmp_path / "gone" / "x.stl")
    assert not report.ok
    assert report.errors == ["The export folder does not exist."]


def test_preflight_keeps_manufacturing_risks_as_warnings(tmp_path):
    from stamp.core.document import Document, Modifier, ProfileRef

    document = Document(
        base=BasePart(mode="solid", runtime=object()),
        features=[Feature(name="Fine detail", profile=ProfileRef(), modifiers=[Modifier(value=0.01)])],
    )
    report = preflight_export(document, RebuildResult(geometry=object()), "stl", tmp_path / "part.stl")
    assert report.ok
    assert "Fine detail" in report.warnings[0]


def test_batch_substitutes_named_text_fields_and_rejects_missing_values():
    import pytest

    from stamp.batch import BatchError, _substitute
    from stamp.core.document import Document, ProfileRef, TextSpec

    document = Document(features=[Feature(profile=ProfileRef(text=TextSpec(text="SN-{{serial}}")))])
    _substitute(document, {"serial": "0042"})
    assert document.features[0].profile.text.text == "SN-0042"
    with pytest.raises(BatchError, match="serial"):
        _substitute(Document(features=[Feature(profile=ProfileRef(text=TextSpec(text="{{serial}}")))]), {})


def test_cylindrical_wrap_rebuilds_add_and_cut(bracket_step, fixtures):
    from stamp.core.document import (
        Anchor,
        AnchorKind,
        Direction,
        Document,
        Operation,
        OperationKind,
        ProfileRef,
    )
    from stamp.core.profiles import ProfileCache
    from stamp.core.rebuild import RebuildEngine
    from stamp.core.refs import (
        face_center,
        faces_of,
        make_face_ref,
        plane_from_face,
        surface_kind,
    )
    from stamp.io.profile_import import file_hash

    face = next(face for face in faces_of(bracket_step.runtime) if surface_kind(face) == "cylinder")
    plane, _warnings = plane_from_face(face, face_center(face))
    for kind, direction in ((OperationKind.CUT, Direction.INTO), (OperationKind.ADD, Direction.OUT_OF)):
        feature = Feature(
            name="Wrapped",
            profile=ProfileRef(
                source_path=str(fixtures / "logo.svg"), source_hash=file_hash(fixtures / "logo.svg")
            ),
            placement=Placement(
                anchor=Anchor(kind=AnchorKind.FACE, face_ref=make_face_ref(face, plane.origin), plane=plane),
                scale=(0.05, 0.05), mode=PlacementMode.WRAP,
            ),
            operation=Operation(kind=kind, direction=direction, depth=0.1),
        )
        result = RebuildEngine(ProfileCache().get).rebuild(
            Document(base=bracket_step, features=[feature])
        )
        assert result.ok, result.errors
        if kind is OperationKind.CUT:
            assert result.volume < bracket_step.volume
        else:
            assert result.volume > bracket_step.volume


def test_conical_wrap_rebuilds():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCone

    from stamp.core.document import (
        Anchor,
        AnchorKind,
        BasePart,
        Direction,
        Document,
        Operation,
        OperationKind,
        ProfileRef,
    )
    from stamp.core.profiles import ProfileCache
    from stamp.core.rebuild import RebuildEngine
    from stamp.core.refs import (
        face_center,
        faces_of,
        make_face_ref,
        plane_from_face,
        surface_kind,
    )
    from stamp.io.profile_import import file_hash

    shape = BRepPrimAPI_MakeCone(8.0, 4.0, 20.0).Shape()
    face = next(face for face in faces_of(shape) if surface_kind(face) == "cone")
    plane, _warnings = plane_from_face(face, face_center(face))
    base = BasePart(mode="solid", runtime=shape, bbox=(-8, -8, 0, 8, 8, 20))
    for kind, direction in ((OperationKind.CUT, Direction.INTO), (OperationKind.ADD, Direction.OUT_OF)):
        feature = Feature(
            name="Wrapped cone",
            profile=ProfileRef(
                source_path="tests/fixtures/logo.svg", source_hash=file_hash("tests/fixtures/logo.svg")
            ),
            placement=Placement(
                anchor=Anchor(kind=AnchorKind.FACE, face_ref=make_face_ref(face, plane.origin), plane=plane),
                scale=(0.05, 0.05), mode=PlacementMode.WRAP,
            ),
            operation=Operation(kind=kind, direction=direction, depth=0.1),
        )
        result = RebuildEngine(ProfileCache().get).rebuild(Document(base=base, features=[feature]))
        assert result.ok, result.errors


def test_batch_stamps_a_saved_template_to_csv_part(bracket_step, fixtures, tmp_path):
    from stamp.batch import run_batch
    from stamp.core.document import Anchor, AnchorKind, Document, Operation, ProfileRef
    from stamp.core.refs import (
        face_center,
        faces_of,
        make_face_ref,
        plane_from_face,
        surface_kind,
    )
    from stamp.io.profile_import import file_hash
    from stamp.io.project import save

    face = next(face for face in faces_of(bracket_step.runtime) if surface_kind(face) == "plane")
    point = face_center(face)
    plane, _warnings = plane_from_face(face, point)
    feature = Feature(
        profile=ProfileRef(
            source_path=str(fixtures / "logo.svg"), source_hash=file_hash(fixtures / "logo.svg")
        ),
        placement=Placement(anchor=Anchor(kind=AnchorKind.FACE, face_ref=make_face_ref(face, point), plane=plane)),
        operation=Operation(depth=0.1),
    )
    template = save(Document(base=bracket_step, features=[feature]), tmp_path / "template.stamp")
    csv_path = tmp_path / "jobs.csv"
    csv_path.write_text(f"input,output\n{fixtures / 'bracket.step'},bracket.stl\n", encoding="utf-8")
    report = run_batch(template, csv_path, tmp_path / "out", "stl")
    assert not report.stopped
    assert report.rows[0].status == "ok"
    assert (tmp_path / "out" / "bracket.stl").exists()


def test_batch_cli_validates_arguments_without_importing_the_gui(capsys):
    from stamp.main import main

    assert main(["stamp", "batch"]) == 2
    assert "requires --template" in capsys.readouterr().err
