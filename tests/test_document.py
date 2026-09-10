"""Document model, undo stack, references, project round trip - §4, §8, §4.4."""

from __future__ import annotations

import pytest

from stamp.core.document import (
    Anchor,
    AnchorKind,
    Document,
    EdgeRole,
    EdgeSelector,
    Feature,
    Modifier,
    ModifierKind,
    Operation,
    OperationKind,
    Placement,
    ProfileRef,
    UndoStack,
)


def a_feature(name="Logo", path="logo.svg") -> Feature:
    return Feature(
        name=name,
        profile=ProfileRef(source_path=path, source_hash="abc123"),
        placement=Placement(offset_2d=(1.5, -2.5), rotation=30.0, scale=(2.0, 2.0)),
        operation=Operation(kind=OperationKind.ADD, depth=0.8),
        modifiers=[
            Modifier(kind=ModifierKind.FILLET, value=0.3,
                     target=EdgeSelector(role=EdgeRole.TOP))
        ],
    )


class TestSerialization:
    def test_feature_round_trip(self):
        original = a_feature()
        clone = Feature.from_dict(original.to_dict())
        assert clone.to_dict() == original.to_dict()

    def test_document_round_trip(self, bracket_step):
        doc = Document(base=bracket_step, name="bracket")
        doc.add_feature(a_feature())
        doc.add_feature(a_feature("Serial", "serial.dxf"))
        clone = Document.from_dict(doc.to_dict())
        assert clone.to_dict() == doc.to_dict()
        assert len(clone.features) == 2

    def test_geometry_is_never_serialized(self, bracket_step):
        doc = Document(base=bracket_step)
        assert "runtime" not in doc.to_dict()["base"]

    def test_a_newer_schema_is_refused_by_name(self):
        with pytest.raises(ValueError, match="newer version"):
            Document.from_dict({"schema_version": 99})

    def test_names_are_made_unique(self, bracket_step):
        doc = Document(base=bracket_step)
        doc.add_feature(a_feature("Logo"))
        doc.add_feature(a_feature("Logo"))
        doc.add_feature(a_feature("Logo"))
        assert [f.name for f in doc.features] == ["Logo", "Logo 2", "Logo 3"]

    def test_duplicate_gets_new_ids(self):
        original = a_feature()
        clone = original.copy_with_new_id()
        assert clone.id != original.id
        assert clone.modifiers[0].id != original.modifiers[0].id
        assert clone.operation.depth == original.operation.depth


class TestFeatureList:
    def test_reorder(self, bracket_step):
        doc = Document(base=bracket_step)
        a = doc.add_feature(a_feature("A"))
        doc.add_feature(a_feature("B"))
        doc.add_feature(a_feature("C"))
        doc.move_feature(a.id, 2)
        assert [f.name for f in doc.features] == ["B", "C", "A"]

    def test_remove(self, bracket_step):
        doc = Document(base=bracket_step)
        a = doc.add_feature(a_feature("A"))
        doc.add_feature(a_feature("B"))
        doc.remove_feature(a.id)
        assert [f.name for f in doc.features] == ["B"]


class TestUndo:
    def test_undo_and_redo(self, bracket_step):
        doc = Document(base=bracket_step)
        stack = UndoStack()

        stack.push("add feature", doc.snapshot())
        doc.add_feature(a_feature())
        assert len(doc.features) == 1

        snap = stack.undo(doc.snapshot())
        doc.restore(snap)
        assert len(doc.features) == 0

        snap = stack.redo(doc.snapshot())
        doc.restore(snap)
        assert len(doc.features) == 1

    def test_restore_keeps_the_live_geometry(self, bracket_step):
        doc = Document(base=bracket_step)
        runtime = doc.base.runtime
        stack = UndoStack()
        stack.push("edit", doc.snapshot())
        doc.add_feature(a_feature())
        doc.restore(stack.undo(doc.snapshot()))
        assert doc.base.runtime is runtime

    def test_a_new_edit_clears_the_redo_branch(self, bracket_step):
        doc = Document(base=bracket_step)
        stack = UndoStack()
        stack.push("one", doc.snapshot())
        doc.add_feature(a_feature())
        doc.restore(stack.undo(doc.snapshot()))
        assert stack.can_redo()
        stack.push("two", doc.snapshot())
        assert not stack.can_redo()

    def test_the_stack_is_capped(self, bracket_step):
        doc = Document(base=bracket_step)
        stack = UndoStack(limit=5)
        for i in range(20):
            stack.push(f"edit {i}", doc.snapshot())
        assert len(stack._undo) == 5


class TestAdoptingTheGeometry:
    """§4.4: the geometry is never in the file, so it is re-attached by hand.

    Every round trip through JSON - the undo stack, the copy the rebuild worker
    sends across the thread boundary, opening a project - drops it, and getting
    it back onto the right record is not as simple as assigning ``runtime``.
    """

    def test_it_takes_the_geometry_of_the_same_part(self, bracket_step):
        from stamp.core.document import BasePart

        copy = BasePart.from_dict(bracket_step.to_dict())
        assert copy.runtime is None
        assert copy.adopt_runtime(bracket_step) is True
        assert copy.runtime is bracket_step.runtime

    def test_it_refuses_the_geometry_of_a_different_part(self, bracket_step, bracket_stl):
        """A mesh is not the solid it was printed from, whatever the record says."""
        from stamp.core.document import BasePart

        copy = BasePart.from_dict(bracket_step.to_dict())
        assert copy.adopt_runtime(bracket_stl) is False
        assert copy.runtime is None

    def test_undo_across_a_replaced_part_does_not_take_the_new_geometry(
        self, bracket_step, fixtures
    ):
        """The record restored is the old part's; the geometry in hand is the new.

        Attaching it anyway left the document describing one part and holding
        another - old source path, old bounding box, new shape.
        """
        from stamp.core.replace_part import replace_part
        from stamp.io.part_import import import_part

        doc = Document(base=bracket_step)
        before = doc.snapshot()
        rev_b = import_part(fixtures / "bracket_rev_b.step").part
        replace_part(doc, rev_b)

        doc.restore(before)

        assert doc.base.source_hash == bracket_step.source_hash
        assert doc.base.runtime is not rev_b.runtime
        assert doc.base.runtime is None, "it needs reloading, not the wrong geometry"

    def test_undoing_an_ordinary_edit_still_keeps_the_geometry(self, bracket_step):
        doc = Document(base=bracket_step)
        before = doc.snapshot()
        doc.add_feature(a_feature())
        doc.restore(before)
        assert doc.base.runtime is bracket_step.runtime


class TestReferences:
    def test_a_face_ref_resolves_to_the_same_face(self, bracket_step):
        from stamp.core.refs import face_center, faces_of, make_face_ref, resolve_face_ref

        face = max(faces_of(bracket_step.runtime), key=lambda f: _area(f))
        ref = make_face_ref(face, face_center(face))
        resolved = resolve_face_ref(ref, bracket_step.runtime)
        assert resolved.face.IsSame(face)
        assert resolved.score > 0.9

    def test_a_reference_to_nothing_is_refused_by_name(self, bracket_step):
        from stamp.core.document import FaceRef
        from stamp.core.refs import ReferenceError, resolve_face_ref

        ref = FaceRef(point=(1e5, 1e5, 1e5), normal=(0, 0, 1), surface_type="plane",
                      area=1e9)
        with pytest.raises(ReferenceError, match="Pick the face again"):
            resolve_face_ref(ref, bracket_step.runtime)

    def test_an_unknown_surface_type_is_named(self, bracket_step):
        from stamp.core.document import FaceRef
        from stamp.core.refs import ReferenceError, resolve_face_ref

        ref = FaceRef(point=(0, 0, 0), normal=(0, 0, 1), surface_type="torus")
        with pytest.raises(ReferenceError, match="torus"):
            resolve_face_ref(ref, bracket_step.runtime)

    def test_the_sketch_plane_normal_points_outward(self, bracket_step):
        from stamp.core.refs import face_center, faces_of, plane_from_face, surface_kind

        for face in faces_of(bracket_step.runtime):
            if surface_kind(face) != "plane":
                continue
            center = face_center(face)
            if abs(center[2] - 8.0) < 1e-6:
                plane, warnings = plane_from_face(face, center)
                assert plane.normal[2] > 0.9
                assert not warnings
                return
        pytest.fail("no top face found")

    def test_a_curved_face_warns_but_still_gives_a_plane(self, bracket_step):
        from stamp.core.refs import face_center, faces_of, plane_from_face, surface_kind

        for face in faces_of(bracket_step.runtime):
            if surface_kind(face) == "cylinder":
                plane, warnings = plane_from_face(face, face_center(face))
                assert warnings and "cylinder" in warnings[0]
                return
        pytest.fail("no cylindrical face found")

    def test_datum_planes_resolve_without_a_part(self, bracket_step):
        from stamp.core.refs import resolve_anchor

        anchor = Anchor(kind=AnchorKind.DATUM, datum="XY", datum_offset=5.0)
        plane, warnings = resolve_anchor(anchor, bracket_step.runtime)
        assert plane.origin == (0.0, 0.0, 5.0)
        assert plane.normal == (0.0, 0.0, 1.0)
        assert not warnings


def _area(face):
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    return props.Mass()


class TestProjectFile:
    def test_round_trip(self, tmp_path, fixtures, bracket_step):
        from stamp.io import project

        doc = Document(base=bracket_step, name="bracket")
        feature = a_feature()
        feature.profile.source_path = str(fixtures / "logo.svg")
        doc.add_feature(feature)

        path = project.save(doc, tmp_path / "test.stamp", thumbnail=b"\x89PNG-fake")
        assert path.exists()

        result = project.open_project(path, work_dir=tmp_path / "sources")
        assert result.thumbnail == b"\x89PNG-fake"
        assert not result.missing
        assert len(result.document.features) == 1
        assert result.document.features[0].placement.rotation == 30.0
        # sources were extracted and the paths now point at real files
        from pathlib import Path

        assert Path(result.document.base.source_path).exists()
        assert Path(result.document.features[0].profile.source_path).exists()

    def test_the_archive_is_a_plain_zip(self, tmp_path, fixtures, bracket_step):
        import zipfile

        from stamp.io import project

        doc = Document(base=bracket_step, name="bracket")
        feature = a_feature()
        feature.profile.source_path = str(fixtures / "logo.svg")
        doc.add_feature(feature)
        path = project.save(doc, tmp_path / "test.stamp")

        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        assert "manifest.json" in names
        assert any(n.startswith("base/part") for n in names)
        assert any(n.startswith("profiles/") for n in names)

    def test_a_missing_source_is_named(self, tmp_path, bracket_step):
        from stamp.io import project

        doc = Document(base=bracket_step, name="bracket")
        feature = a_feature()
        feature.profile.source_path = str(tmp_path / "not_here.svg")
        doc.add_feature(feature)
        path = project.save(doc, tmp_path / "test.stamp")

        result = project.open_project(path, work_dir=tmp_path / "sources")
        assert str(tmp_path / "not_here.svg") in result.missing

    def test_a_file_that_is_not_a_project_is_refused(self, tmp_path):
        from stamp.io import project

        bogus = tmp_path / "bogus.stamp"
        bogus.write_bytes(b"not a zip at all")
        with pytest.raises(project.ProjectError):
            project.open_project(bogus)


class TestTheImportRepairIsRemembered:
    """§5.5: a repair the user accepted changes the artwork, so the file keeps it.

    "Close open loops" bridged the gaps in a DXF that would not otherwise build.
    The project recorded the file and the other import options but not that one,
    so re-importing on the next open - or on a duplicate, or in a batch - came
    back blocked, and a feature that was fine when it was saved was broken when
    it was reopened.
    """

    def test_it_survives_the_project_file(self, fixtures):
        ref = ProfileRef(source_path=str(fixtures / "open_loop.dxf"),
                         source_hash="abc123", close_open_loops=True)
        assert ProfileRef.from_dict(ref.to_dict()).close_open_loops is True

    def test_an_older_project_reads_as_no_repair(self):
        """Which is what those files were imported with, so nothing changes."""
        old = ProfileRef.from_dict({"source_path": "logo.svg", "source_hash": "abc123"})
        assert old.close_open_loops is False

    def test_it_is_part_of_the_cache_key(self):
        """Otherwise the repaired profile and the broken one share a slot."""
        plain = ProfileRef(source_path="a.dxf", source_hash="abc123")
        repaired = ProfileRef(source_path="a.dxf", source_hash="abc123",
                              close_open_loops=True)
        assert plain.cache_key != repaired.cache_key

    def test_the_reimported_profile_is_usable_again(self, fixtures):
        from stamp.core.profiles import ProfileCache
        from stamp.io.profile_import import ImportOptions, import_profile

        path = fixtures / "open_loop.dxf"
        repaired = import_profile(path, ImportOptions(close_open_loops=True))
        assert not repaired.profile.blocked, "the repair itself has to work"

        ref = ProfileRef(source_path=str(path), source_hash=repaired.source_hash,
                         close_open_loops=True)
        reopened = ProfileRef.from_dict(ref.to_dict())
        assert not ProfileCache().get(reopened).blocked

    def test_without_it_the_same_file_comes_back_blocked(self, fixtures):
        """The state the bug left every reopened project in."""
        from stamp.core.profiles import ProfileCache
        from stamp.io.normalize import IssueKind

        path = fixtures / "open_loop.dxf"
        ref = ProfileRef(source_path=str(path), source_hash="abc123")
        profile = ProfileCache().get(ref)
        assert profile.blocked
        assert any(i.kind is IssueKind.OPEN_LOOP for i in profile.issues)


class TestTheProfileCacheIsBounded:
    """Every keystroke in the text box is a new key (§5.5)."""

    def test_it_does_not_grow_without_end(self):
        from stamp.core.document import TextSpec
        from stamp.core.profiles import ProfileCache

        cache = ProfileCache(limit=8)
        for n in range(50):
            cache.put(ProfileRef(text=TextSpec(text="a" * n)), object())
        assert len(cache._cache) == 8

    def test_the_one_being_worked_on_is_the_one_kept(self):
        """LRU, not "throw the lot away": the live edit must stay a cache hit."""
        from stamp.core.document import TextSpec
        from stamp.core.profiles import ProfileCache

        cache = ProfileCache(limit=4)
        wanted = ProfileRef(text=TextSpec(text="keep me"))
        cache.put(wanted, "profile")
        for n in range(3):
            cache.put(ProfileRef(text=TextSpec(text=f"other {n}")), object())
            assert cache.get(wanted) == "profile"
        cache.put(ProfileRef(text=TextSpec(text="one more")), object())
        assert cache.get(wanted) == "profile"


class TestManufacturingWarnings:
    """The guidance has to be worth reading, so it must not cry wolf."""

    def test_a_through_cut_is_not_judged_on_a_depth_it_ignores(self):
        from stamp.core.document import DepthMode
        from stamp.core.inspection import inspect_feature

        feature = a_feature()
        feature.modifiers = []
        feature.operation = Operation(kind=OperationKind.CUT,
                                      depth_mode=DepthMode.THROUGH_ALL, depth=0.01)
        assert inspect_feature(Document(), feature) == []

    def test_nor_is_a_cut_to_a_face(self):
        from stamp.core.document import DepthMode
        from stamp.core.inspection import inspect_feature

        feature = a_feature()
        feature.modifiers = []
        feature.operation = Operation(kind=OperationKind.CUT,
                                      depth_mode=DepthMode.TO_FACE, depth=0.01)
        assert inspect_feature(Document(), feature) == []

    def test_a_blind_cut_still_is(self):
        from stamp.core.document import DepthMode
        from stamp.core.inspection import inspect_feature

        feature = a_feature()
        feature.modifiers = []
        feature.operation = Operation(kind=OperationKind.CUT,
                                      depth_mode=DepthMode.BLIND, depth=0.01)
        warnings = inspect_feature(Document(), feature)
        assert len(warnings) == 1
        assert "manufacturing limit" in warnings[0]

    def test_a_small_fillet_is_reported_once(self):
        """Below the limit is also below one and a half times it, which said so twice."""
        from stamp.core.inspection import inspect_feature

        feature = a_feature()
        feature.operation = Operation(kind=OperationKind.CUT, depth=1.0)
        feature.modifiers = [Modifier(kind=ModifierKind.FILLET, value=0.05)]
        warnings = inspect_feature(Document(), feature)
        assert len(warnings) == 1
        assert "detail limit" in warnings[0]

    def test_a_marginal_fillet_still_gets_the_softer_note(self):
        from stamp.core.inspection import inspect_feature

        feature = a_feature()
        feature.operation = Operation(kind=OperationKind.CUT, depth=1.0)
        feature.modifiers = [Modifier(kind=ModifierKind.FILLET, value=0.25)]
        warnings = inspect_feature(Document(), feature)
        assert len(warnings) == 1
        assert "cutter radius" in warnings[0]


class TestDiagnostics:
    def test_the_log_records_a_breadcrumb_and_an_error(self, tmp_path, monkeypatch):
        """A crash leaves no traceback, so the last breadcrumb has to name the step."""
        import importlib

        from stamp import diagnostics

        monkeypatch.setattr(diagnostics, "log_dir", lambda: tmp_path)
        monkeypatch.setattr(diagnostics, "_started", False)
        monkeypatch.setattr(diagnostics, "_log", importlib.import_module("logging").getLogger("stamp.test"))
        path = diagnostics.start()
        assert path is not None and path.exists()

        diagnostics.breadcrumb("modifier: kind=%s edges=%d", "FILLET", 7)
        try:
            raise ValueError("an example")
        except ValueError as exc:
            diagnostics.note_exception("unit test", exc)

        text = path.read_text(encoding="utf-8")
        assert "modifier: kind=FILLET edges=7" in text
        assert "an example" in text
