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
