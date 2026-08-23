"""Replacing the part with a newer file, without replacing the artwork - §8.2.

A part gets revised.  The stamps on it should still be where they were put, and
where they cannot be, the user should be told rather than have geometry quietly
land somewhere wrong.
"""

from __future__ import annotations

import math

import pytest

from stamp.core.document import (
    Anchor,
    AnchorKind,
    DepthMode,
    Direction,
    Document,
    FaceRef,
    Feature,
    Operation,
    OperationKind,
    Placement,
    Plane,
    ProfileRef,
)
from stamp.core.profiles import ProfileCache
from stamp.core.rebuild import RebuildEngine
from stamp.core.refs import (
    face_center,
    face_normal_at,
    faces_of,
    make_face_ref,
    surface_kind,
)
from stamp.core.replace_part import plan_replacement, replace_part
from stamp.io.part_import import import_part
from stamp.io.profile_import import file_hash


def top_face_ref(part, z: float):
    """The upward-facing plane at height *z*, which is where artwork goes."""
    for face in faces_of(part.runtime):
        if surface_kind(face) != "plane":
            continue
        center = face_center(face)
        if abs(center[2] - z) < 1e-6 and face_normal_at(face, center)[2] > 0.9:
            return make_face_ref(face, (30.0, 20.0, z))
    pytest.fail(f"no upward face at z={z}")


def a_document(part, fixtures, ref, *, name="logo", depth=0.6):
    """A document as it is after a rebuild, with the anchor already resolved.

    The resolved plane is what "kept" and "moved" are measured against, so a
    document without one would report everything as kept no matter what happened.
    """
    from stamp.core.refs import resolve_anchor

    doc = Document(base=part)
    anchor = Anchor(kind=AnchorKind.FACE, face_ref=FaceRef.from_dict(ref.to_dict()))
    anchor.plane, _warnings = resolve_anchor(anchor, part.runtime)
    doc.add_feature(
        Feature(
            name=name,
            profile=ProfileRef(source_path=str(fixtures / "logo.svg"),
                               source_hash=file_hash(fixtures / "logo.svg")),
            placement=Placement(anchor=anchor),
            operation=Operation(kind=OperationKind.CUT, depth_mode=DepthMode.BLIND,
                                depth=depth, direction=Direction.INTO),
        )
    )
    return doc


@pytest.fixture
def bracket(fixtures):
    return import_part(fixtures / "bracket.step").part


@pytest.fixture
def rev_b(fixtures):
    return import_part(fixtures / "bracket_rev_b.step").part


class TestARevisedPart:
    """Holes move, the boss grows, a rib appears - the top face is still the top."""

    def test_the_stamp_keeps_its_place(self, bracket, rev_b, fixtures):
        doc = a_document(bracket, fixtures, top_face_ref(bracket, 8.0))
        before = doc.features[0].placement.anchor.plane

        report = replace_part(doc, rev_b)

        assert report.ok, [m.detail for m in report.lost]
        assert len(report.kept) == 1
        assert doc.base is rev_b
        after = doc.features[0].placement.anchor.plane
        assert before is not None and after is not None
        assert math.dist(before.origin, after.origin) < 0.05

    def test_the_part_still_rebuilds_afterwards(self, bracket, rev_b, fixtures):
        """The point of keeping the anchor is that the geometry still builds."""
        doc = a_document(bracket, fixtures, top_face_ref(bracket, 8.0))
        replace_part(doc, rev_b)

        result = RebuildEngine(ProfileCache().get).rebuild(doc)
        assert result.ok, result.errors
        assert result.geometry is not None

    def test_planning_changes_nothing(self, bracket, rev_b, fixtures):
        """A dry run has to be safe to call before asking the user anything."""
        doc = a_document(bracket, fixtures, top_face_ref(bracket, 8.0))
        before = doc.features[0].placement.anchor.face_ref.to_dict()

        report = plan_replacement(doc, rev_b)

        assert report.matches
        assert doc.base is bracket, "the dry run must not swap the part"
        assert doc.features[0].placement.anchor.face_ref.to_dict() == before


class TestAThickerPart:
    """The face the artwork sits on moved, so the artwork moves with it."""

    def test_the_stamp_follows_the_face_up(self, bracket, fixtures):
        thicker = import_part(fixtures / "bracket_thicker.step").part
        doc = a_document(bracket, fixtures, top_face_ref(bracket, 8.0))

        report = replace_part(doc, thicker)

        assert report.ok, [m.detail for m in report.lost]
        plane = doc.features[0].placement.anchor.plane
        assert plane is not None
        assert abs(plane.origin[2] - 12.0) < 1e-6, "it should sit on the new top face"

    def test_it_reports_the_move_rather_than_staying_silent(self, bracket, fixtures):
        thicker = import_part(fixtures / "bracket_thicker.step").part
        doc = a_document(bracket, fixtures, top_face_ref(bracket, 8.0))
        assert doc.features[0].placement.anchor.plane.origin[2] == pytest.approx(8.0)

        report = replace_part(doc, thicker)

        assert len(report.moved) == 1
        assert report.moved[0].moved_mm == pytest.approx(4.0, abs=0.01)

    def test_the_result_still_builds(self, bracket, fixtures):
        thicker = import_part(fixtures / "bracket_thicker.step").part
        doc = a_document(bracket, fixtures, top_face_ref(bracket, 8.0))
        replace_part(doc, thicker)
        result = RebuildEngine(ProfileCache().get).rebuild(doc)
        assert result.ok, result.errors


class TestAPartExportedFromElsewhere:
    """The same part, written from a different origin."""

    def test_the_offset_is_found_and_the_stamp_follows(self, bracket, fixtures):
        moved = import_part(fixtures / "bracket_moved.step").part
        doc = a_document(bracket, fixtures, top_face_ref(bracket, 8.0))

        report = replace_part(doc, moved)

        assert report.ok, [m.detail for m in report.lost]
        assert report.aligned
        assert report.alignment == pytest.approx((120.0, -45.0, 30.0), abs=1e-6)
        plane = doc.features[0].placement.anchor.plane
        assert plane is not None
        assert plane.origin[0] == pytest.approx(150.0, abs=0.5)
        assert plane.origin[2] == pytest.approx(38.0, abs=1e-6)

    def test_it_says_the_part_moved(self, bracket, fixtures):
        moved = import_part(fixtures / "bracket_moved.step").part
        doc = a_document(bracket, fixtures, top_face_ref(bracket, 8.0))
        report = replace_part(doc, moved)
        assert any("moved with it" in w for w in report.warnings), report.warnings

    def test_the_result_builds_at_the_new_place(self, bracket, fixtures):
        moved = import_part(fixtures / "bracket_moved.step").part
        doc = a_document(bracket, fixtures, top_face_ref(bracket, 8.0))
        replace_part(doc, moved)
        result = RebuildEngine(ProfileCache().get).rebuild(doc)
        assert result.ok, result.errors


class TestWhenThereIsNoMatch:
    """A face that is genuinely gone must be reported, not guessed at."""

    def test_a_cylindrical_anchor_on_a_bare_plate_is_lost(self, bracket, fixtures):
        plate = import_part(fixtures / "plate.step").part
        cylinder = None
        for face in faces_of(bracket.runtime):
            if surface_kind(face) == "cylinder":
                cylinder = make_face_ref(face, face_center(face))
                break
        assert cylinder is not None, "the bracket should have a cylindrical face"

        doc = a_document(bracket, fixtures, cylinder)
        report = replace_part(doc, plate)

        assert not report.ok
        assert len(report.lost) == 1
        assert "pick the face again" in report.lost[0].detail.lower()

    def test_the_feature_is_kept_so_the_work_is_not_thrown_away(self, bracket, fixtures):
        plate = import_part(fixtures / "plate.step").part
        cylinder = None
        for face in faces_of(bracket.runtime):
            if surface_kind(face) == "cylinder":
                cylinder = make_face_ref(face, face_center(face))
                break

        doc = a_document(bracket, fixtures, cylinder)
        stored = doc.features[0].placement.anchor.face_ref.to_dict()
        replace_part(doc, plate)

        assert len(doc.features) == 1, "a lost feature must never be deleted"
        assert doc.features[0].placement.anchor.face_ref.to_dict() == stored
        assert doc.base is plate

    def test_the_summary_says_what_needs_attention(self, bracket, fixtures):
        plate = import_part(fixtures / "plate.step").part
        cylinder = None
        for face in faces_of(bracket.runtime):
            if surface_kind(face) == "cylinder":
                cylinder = make_face_ref(face, face_center(face))
                break
        doc = a_document(bracket, fixtures, cylinder)
        report = replace_part(doc, plate)
        assert "need a face picked again" in report.summary()


class TestSeveralFeatures:
    def test_each_one_is_reported_separately(self, bracket, rev_b, fixtures):
        doc = a_document(bracket, fixtures, top_face_ref(bracket, 8.0), name="one")
        doc.add_feature(
            Feature(
                name="two",
                profile=ProfileRef(source_path=str(fixtures / "logo.svg"),
                                   source_hash=file_hash(fixtures / "logo.svg")),
                placement=Placement(
                    anchor=Anchor(
                        kind=AnchorKind.FACE,
                        face_ref=FaceRef.from_dict(
                            top_face_ref(bracket, 8.0).to_dict()
                        ),
                    )
                ),
                operation=Operation(kind=OperationKind.CUT, depth_mode=DepthMode.BLIND,
                                    depth=0.4, direction=Direction.INTO),
            )
        )

        report = replace_part(doc, rev_b)

        assert len(report.matches) == 2
        assert {m.name for m in report.matches} == {"one", "two"}
        assert report.ok


class TestMeshParts:
    """An STL revision has no faces to match, so the region is fitted again."""

    def test_a_stamp_on_a_mesh_survives_a_revision(self, fixtures):
        old = import_part(fixtures / "bracket.stl").part
        new = import_part(fixtures / "bracket_rev_b.stl").part

        doc = Document(base=old)
        doc.add_feature(
            Feature(
                name="logo",
                profile=ProfileRef(source_path=str(fixtures / "logo.svg"),
                                   source_hash=file_hash(fixtures / "logo.svg")),
                placement=Placement(anchor=Anchor(
                    kind=AnchorKind.MESH_REGION,
                    plane=Plane(origin=(30.0, 20.0, 8.0), normal=(0.0, 0.0, 1.0),
                                u_axis=(1.0, 0.0, 0.0)),
                )),
                operation=Operation(kind=OperationKind.CUT, depth_mode=DepthMode.BLIND,
                                    depth=0.5, direction=Direction.INTO),
            )
        )

        report = replace_part(doc, new)

        assert report.ok, [m.detail for m in report.lost]
        plane = doc.features[0].placement.anchor.plane
        assert plane is not None
        assert abs(plane.origin[2] - 8.0) < 0.2
        assert plane.normal[2] > 0.9


class TestEdgeCases:
    def test_replacing_with_nothing_is_refused(self, bracket, fixtures):
        from stamp.core.document import BasePart

        doc = a_document(bracket, fixtures, top_face_ref(bracket, 8.0))
        with pytest.raises(ValueError):
            replace_part(doc, BasePart())

    def test_a_document_with_no_artwork_is_fine(self, bracket, rev_b):
        doc = Document(base=bracket)
        report = replace_part(doc, rev_b)
        assert report.ok
        assert report.matches == []
        assert doc.base is rev_b
        assert "no artwork" in report.summary()
