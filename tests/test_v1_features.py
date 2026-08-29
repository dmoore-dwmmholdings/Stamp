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


def _stamped_document(depth: float):
    from stamp.core.document import Document, Operation, OperationKind

    return Document(
        base=BasePart(mode="solid", runtime=object()),
        features=[Feature(name="Emblem",
                          operation=Operation(kind=OperationKind.COLOR, depth=depth))],
    )


def test_preflight_says_a_color_stamp_is_only_a_recess_in_step_and_stl(tmp_path):
    """STEP and STL carry no color, so the user hears it before the printer does."""
    document = _stamped_document(0.2)
    rebuilt = RebuildResult(geometry=object())

    for fmt in ("step", "stl"):
        report = preflight_export(document, rebuilt, fmt, tmp_path / f"part.{fmt}")
        assert report.ok
        assert any("Emblem" in w and fmt.upper() in w for w in report.warnings)


def test_preflight_leaves_a_color_stamp_alone_for_3mf(tmp_path):
    document = _stamped_document(0.2)
    report = preflight_export(document, RebuildResult(geometry=object()), "3mf", tmp_path / "p.3mf")
    assert report.ok
    assert not any("carries none" in w for w in report.warnings)


def test_preflight_calls_out_a_stamp_thinner_than_a_printed_layer(tmp_path):
    document = _stamped_document(0.02)
    report = preflight_export(document, RebuildResult(geometry=object()), "3mf", tmp_path / "p.3mf")
    assert report.ok
    assert any("thinner than one printed layer" in w for w in report.warnings)


def test_a_thin_stamp_is_not_reported_as_a_shallow_machined_mark(tmp_path):
    """0.15 mm is a fine color stamp and a bad engraving.  Only one limit applies."""
    document = _stamped_document(0.15)
    report = preflight_export(document, RebuildResult(geometry=object()), "3mf", tmp_path / "p.3mf")
    assert report.ok
    assert not any("manufacturing limit" in w for w in report.warnings)
    assert not any("thinner than one printed layer" in w for w in report.warnings)


def test_a_shallow_engraving_is_still_reported(tmp_path):
    from stamp.core.document import Document, Operation, OperationKind

    document = Document(
        base=BasePart(mode="solid", runtime=object()),
        features=[Feature(name="Slot",
                          operation=Operation(kind=OperationKind.CUT, depth=0.15))],
    )
    report = preflight_export(document, RebuildResult(geometry=object()), "3mf", tmp_path / "p.3mf")
    assert any("manufacturing limit" in w for w in report.warnings)


def test_a_disabled_color_stamp_warns_about_nothing(tmp_path):
    document = _stamped_document(0.02)
    document.features[0].enabled = False
    report = preflight_export(document, RebuildResult(geometry=object()), "step", tmp_path / "p.step")
    assert not any("Emblem" in w for w in report.warnings)


def test_a_color_stamp_round_trips_and_still_counts_as_removing_material():
    from stamp.core.document import Operation, OperationKind

    operation = Operation(kind=OperationKind.COLOR, depth=0.2)
    restored = Operation.from_dict(operation.to_dict())
    assert restored.kind is OperationKind.COLOR
    assert restored.removes_material
    assert restored.label == "Color stamp"
    # An older project has no colour stamps in it and must not grow one.
    assert Operation.from_dict({}).kind is OperationKind.CUT


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


def test_production_proof_writes_a_pdf_with_preflight_context(tmp_path, qapp):
    from stamp.core.document import BasePart, Document, Feature, Operation, OperationKind
    from stamp.io.export import export_proof_sheet

    document = Document(
        name="bracket-job",
        base=BasePart(mode="solid", runtime=object(), source_path="bracket.step", bbox=(80.0, 40.0, 14.0)),
        features=[Feature(name="Serial", operation=Operation(kind=OperationKind.CUT, depth=0.4))],
    )
    result = RebuildResult(geometry=object(), mode="solid")

    written = export_proof_sheet(document, tmp_path / "proof", rebuild=result)

    assert written.path.suffix == ".pdf"
    assert written.path.exists()
    assert written.size_bytes > 100


def test_preflight_keeps_manufacturing_risks_as_warnings(tmp_path):
    from stamp.core.document import Document, Modifier, ProfileRef

    document = Document(
        base=BasePart(mode="solid", runtime=object()),
        features=[Feature(name="Fine detail", profile=ProfileRef(), modifiers=[Modifier(value=0.01)])],
    )
    report = preflight_export(document, RebuildResult(geometry=object()), "stl", tmp_path / "part.stl")
    assert report.ok
    assert "Fine detail" in report.warnings[0]


def test_manufacturing_rulesets_apply_consistent_preflight_limits():
    from stamp.core.document import InspectionSettings
    from stamp.core.inspection import MANUFACTURING_RULESETS, apply_ruleset

    settings = InspectionSettings()
    apply_ruleset(settings, "FDM printing")

    assert (settings.min_detail_mm, settings.min_depth_mm, settings.min_clearance_mm) == MANUFACTURING_RULESETS["FDM printing"]


def test_inspection_dimensions_measure_the_actual_tool_envelope():
    import pytest
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    from stamp.core.inspection import feature_dimensions

    dimensions = feature_dimensions(BRepPrimAPI_MakeBox(2.0, 3.0, 4.0).Shape())

    assert dimensions is not None
    assert (dimensions.width_mm, dimensions.height_mm, dimensions.depth_mm) == pytest.approx((2.0, 3.0, 4.0))


def test_clearance_measurement_exposes_both_ends_of_the_warning_line(bracket_step, fixtures):
    from stamp.core.document import Anchor, AnchorKind, Document, Operation, ProfileRef
    from stamp.core.inspection import anchor_clearance_measurement
    from stamp.core.refs import (
        face_center,
        faces_of,
        make_face_ref,
        plane_from_face,
        surface_kind,
    )
    from stamp.io.profile_import import file_hash

    face = next(face for face in faces_of(bracket_step.runtime) if surface_kind(face) == "plane")
    point = face_center(face)
    plane, _warnings = plane_from_face(face, point)
    feature = Feature(
        profile=ProfileRef(source_path=str(fixtures / "logo.svg"), source_hash=file_hash(fixtures / "logo.svg")),
        placement=Placement(anchor=Anchor(kind=AnchorKind.FACE, face_ref=make_face_ref(face, point), plane=plane)),
        operation=Operation(depth=0.2),
    )

    measurement = anchor_clearance_measurement(Document(base=bracket_step, features=[feature]), feature)

    assert measurement is not None
    assert measurement.distance_mm >= 0
    assert len(measurement.origin) == len(measurement.boundary) == 3


def test_batch_substitutes_named_text_fields_and_rejects_missing_values():
    import pytest

    from stamp.batch import BatchError, _substitute
    from stamp.core.document import Document, ProfileRef, TextSpec

    document = Document(features=[Feature(profile=ProfileRef(text=TextSpec(text="SN-{{serial}}")))])
    _substitute(document, {"serial": "0042"})
    assert document.features[0].profile.text.text == "SN-0042"
    with pytest.raises(BatchError, match="serial"):
        _substitute(Document(features=[Feature(profile=ProfileRef(text=TextSpec(text="{{serial}}")))]), {})


def test_batch_simulation_rejects_invalid_format_before_it_can_queue_outputs(tmp_path):
    import pytest

    from stamp.batch import BatchError, simulate_batch
    from stamp.core.document import BasePart, Document
    from stamp.io.project import save

    template = tmp_path / "template.stamp"
    save(Document(base=BasePart(mode="solid", runtime=object())), template)
    csv = tmp_path / "rows.csv"
    csv.write_text("input,output\npart.step,marked\n", encoding="utf-8")

    with pytest.raises(BatchError, match="format"):
        simulate_batch(template, csv, "dxf")


def test_batch_simulation_catches_empty_and_escaping_rows(tmp_path):
    from stamp.batch import simulate_batch
    from stamp.core.document import BasePart, Document
    from stamp.io.project import save

    template = tmp_path / "template.stamp"
    save(Document(base=BasePart(mode="solid", runtime=object())), template)
    csv = tmp_path / "rows.csv"
    csv.write_text("input,output\n,mark\npart.step,../escape\n", encoding="utf-8")

    report = simulate_batch(template, csv, "stl")

    assert [row.status for row in report.rows] == ["failed", "failed"]
    assert "input" in report.rows[0].detail
    assert "inside" in report.rows[1].detail


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
