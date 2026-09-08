"""Regressions in export and in the project file - spec §4.4, §9."""

from __future__ import annotations

import copy
import json
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from stamp.core.document import (
    Anchor,
    AnchorKind,
    DepthMode,
    Direction,
    Document,
    FaceRef,
    Feature,
    MirrorPlane,
    Operation,
    OperationKind,
    PartTransform,
    Placement,
    ProfileRef,
)
from stamp.core.profiles import ProfileCache
from stamp.core.rebuild import RebuildEngine
from stamp.core.refs import face_center, face_normal_at, faces_of, make_face_ref, surface_kind
from stamp.io import project as project_io
from stamp.io import with_extension
from stamp.io.export import ExportError, default_filename, export_for_quote, export_stl
from stamp.io.profile_import import file_hash
from stamp.io.project import ProjectError, open_project, save


def _top_ref(part):
    for face in faces_of(part.runtime):
        if surface_kind(face) != "plane":
            continue
        center = face_center(face)
        if abs(center[2] - 8.0) < 1e-6 and face_normal_at(face, center)[2] > 0.9:
            return make_face_ref(face, (30.0, 20.0, 8.0))
    pytest.fail("no top face found")


@pytest.fixture
def stamped(bracket_step, fixtures):
    """A bracket with the logo cut into its top face - a real, saveable project.

    The base is copied: several of these tests move the part out from under the
    project, and the fixture is shared for the whole session.
    """
    ref = _top_ref(bracket_step)
    document = Document(base=copy.copy(bracket_step), name="bracket")
    document.add_feature(
        Feature(
            name="Logo",
            profile=ProfileRef(
                source_path=str(fixtures / "logo.svg"),
                source_hash=file_hash(fixtures / "logo.svg"),
            ),
            placement=Placement(
                anchor=Anchor(
                    kind=AnchorKind.FACE, face_ref=FaceRef.from_dict(ref.to_dict())
                )
            ),
            operation=Operation(
                kind=OperationKind.CUT,
                depth_mode=DepthMode.BLIND,
                depth=0.6,
                direction=Direction.INTO,
            ),
        )
    )
    return document


class TestPackageFromTheCommandLine:
    """``stamp package`` runs with no window, and a PDF needs a Qt application."""

    def test_it_writes_a_zip_with_the_model_project_and_pdf(self, stamped, tmp_path):
        project = save(stamped, tmp_path / "job.stamp")
        out = tmp_path / "job.zip"
        done = subprocess.run(
            [
                sys.executable, "-m", "stamp.main", "package",
                "--project", str(project), "--output", str(out),
            ],
            capture_output=True,
            text=True,
            timeout=900,
        )
        assert done.returncode == 0, done.stderr
        assert out.exists()
        names = set(zipfile.ZipFile(out).namelist())
        assert any(n.endswith(".step") for n in names), names
        assert any(n.endswith(".stamp") for n in names), names
        assert "preflight.json" in names
        assert "production-summary.pdf" in names
        manifest = json.loads(zipfile.ZipFile(out).read("preflight.json"))
        assert manifest["project"] == "job"
        assert zipfile.ZipFile(out).read("production-summary.pdf").startswith(b"%PDF")


class TestNamesWithDotsInThem:
    """``Path.with_suffix`` replaces everything after the last dot."""

    def test_a_revision_number_is_not_eaten(self):
        assert with_extension("bracket v1.2", ".stamp").name == "bracket v1.2.stamp"

    def test_an_extension_already_there_is_left_alone(self):
        assert with_extension("job.zip", ".zip").name == "job.zip"
        assert with_extension("job.ZIP", ".zip").name == "job.ZIP"

    def test_saving_keeps_the_whole_name(self, stamped, tmp_path):
        written = save(stamped, tmp_path / "bracket v1.2")
        assert written.name == "bracket v1.2.stamp"
        assert written.exists()

    def test_a_preset_keeps_the_whole_name(self, stamped, tmp_path):
        from stamp.io.presets import save_preset

        written = save_preset(stamped.features[0], tmp_path / "logo v2.1")
        assert written.name == "logo v2.1.stamp-preset"
        assert written.exists()


class TestOpeningAProjectThatIsWrong:
    """Everything open_project can hit has to arrive as a ProjectError."""

    def test_a_newer_schema_is_named_not_raised_as_a_ValueError(self, stamped, tmp_path):
        path = save(stamped, tmp_path / "future.stamp")
        _rewrite_manifest(path, lambda m: m.update({"schema_version": 9999}))
        with pytest.raises(ProjectError) as caught:
            open_project(path)
        assert "newer version" in str(caught.value)

    def test_a_field_of_the_wrong_kind_is_named(self, stamped, tmp_path):
        path = save(stamped, tmp_path / "bad_field.stamp")
        _rewrite_manifest(path, lambda m: m.update({"features": [{"profile": 12}]}))
        with pytest.raises(ProjectError) as caught:
            open_project(path)
        assert "damaged" in str(caught.value)

    def test_a_read_only_folder_falls_back_to_a_temporary_work_dir(
        self, stamped, tmp_path
    ):
        folder = tmp_path / "readonly"
        folder.mkdir()
        path = save(stamped, folder / "locked.stamp")
        folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            opened = open_project(path)
        finally:
            folder.chmod(stat.S_IRWXU)
        assert opened.work_dir is not None
        assert not opened.work_dir.is_relative_to(folder)
        assert Path(opened.document.base.source_path).exists()


class TestSavingWithTheBaseGone:
    """A project saved without its part could never be opened again."""

    def test_a_moved_part_stops_the_save_and_names_the_file(self, stamped, tmp_path):
        missing = tmp_path / "gone" / "bracket.step"
        stamped.base.source_path = str(missing)
        with pytest.raises(ProjectError) as caught:
            save(stamped, tmp_path / "orphan.stamp")
        assert str(missing) in str(caught.value)
        assert not (tmp_path / "orphan.stamp").exists()

    def test_a_relinked_part_is_archived(self, stamped, tmp_path, fixtures):
        stamped.base.source_path = str(tmp_path / "gone" / "bracket.step")
        path = save(
            stamped, tmp_path / "relinked.stamp", base_path=str(fixtures / "bracket.step")
        )
        assert "base/part.step" in zipfile.ZipFile(path).namelist()

    def test_the_copy_already_in_the_project_carries_a_moved_source(
        self, stamped, tmp_path
    ):
        path = save(stamped, tmp_path / "carried.stamp")
        opened = open_project(path)
        # The source moves out from under the project after it was opened.
        opened.document.base.source_path = str(tmp_path / "elsewhere" / "bracket.step")
        again = save(opened.document, path)
        assert "base/part.step" in zipfile.ZipFile(again).namelist()

    def test_a_missing_base_is_named_when_opening(self, stamped, tmp_path):
        """A project mailed on without its part still has to open enough to relink."""
        path = save(stamped, tmp_path / "nobase.stamp")
        _strip_member(path, "base/part.step")
        _rewrite_manifest(
            path, lambda m: m["base"].update({"source_path": "/nowhere/bracket.step"})
        )
        opened = open_project(path, work_dir=tmp_path / "work")
        assert opened.missing_base == "/nowhere/bracket.step"
        assert "/nowhere/bracket.step" in opened.missing


class TestQuoteExportFollowsTheTransform:
    def test_the_note_and_the_names_carry_the_mirror_and_scale(
        self, bracket_step, tmp_path
    ):
        document = Document(base=bracket_step, name="bracket")
        document.transform = PartTransform(mirror=MirrorPlane.YZ, scale=(2.0, 2.0, 2.0))
        result = RebuildEngine(ProfileCache().get).rebuild(document)
        written = export_for_quote(
            result.geometry,
            tmp_path / "quote",
            "bracket",
            mode="solid",
            volume_mm3=result.volume,
            bbox=bracket_step.bbox,
            document=document,
        )
        tag = document.transform.suffix()
        assert tag
        assert all(tag in item.path.name for item in written)
        note = next(item for item in written if item.path.suffix == ".txt").path
        text = note.read_text(encoding="utf-8")
        # 80 x 40 x 14 doubled, not the untransformed size.
        assert "160.00 x 80.00 x 28.00 mm" in text
        assert "Mirror and scale:" in text

    def test_an_untransformed_part_is_named_as_it_always_was(
        self, bracket_step, tmp_path
    ):
        document = Document(base=bracket_step, name="bracket")
        written = export_for_quote(
            result_geometry(document), tmp_path / "quote", "bracket",
            mode="solid", bbox=bracket_step.bbox, document=document,
        )
        assert {item.path.name for item in written} == {
            default_filename("bracket", ext) for ext in ("step", "stl", "txt")
        }


def result_geometry(document):
    return RebuildEngine(ProfileCache().get).rebuild(document).geometry


class TestAFailedWriteLeavesNothingBehind:
    """A truncated STEP or STL where a good one was is the worst possible outcome."""

    def test_the_old_file_survives_a_failed_stl_write(self, bracket_stl, tmp_path, monkeypatch):
        from stamp.geom import mesh_ops

        target = tmp_path / "keep.stl"
        target.write_bytes(b"the good file")

        class BrokenMesh:
            is_watertight = True
            faces: list = []

            def export(self, path, file_type=None):
                Path(path).write_bytes(b"solid half-writ")
                raise OSError("the disk filled up")

        monkeypatch.setattr(mesh_ops, "to_trimesh", lambda _g: BrokenMesh())
        with pytest.raises(OSError):
            export_stl(object(), target, mode="mesh")

        assert target.read_bytes() == b"the good file"
        assert not list(tmp_path.glob("*.part"))

    def test_a_good_write_leaves_no_temporary_file(self, bracket_stl, tmp_path):
        document = Document(base=bracket_stl)
        geometry = result_geometry(document)
        out = export_stl(geometry, tmp_path / "good.stl", mode="mesh")
        assert out.path.exists()
        assert not list(tmp_path.glob("*.part"))

    def test_a_step_that_cannot_be_written_raises_and_leaves_nothing(
        self, bracket_step, tmp_path
    ):
        from stamp.io.export import export_step

        document = Document(base=bracket_step)
        geometry = result_geometry(document)
        target = tmp_path / "nowhere" / "out.step"
        with pytest.raises(ExportError):
            export_step(geometry, target)
        assert not target.exists()
        assert not list(tmp_path.glob("**/*.part"))


def _rewrite_manifest(path: Path, change) -> None:
    with zipfile.ZipFile(path) as archive:
        items = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(items[project_io.MANIFEST])
    change(manifest)
    items[project_io.MANIFEST] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in items.items():
            archive.writestr(name, data)


def _strip_member(path: Path, member: str) -> None:
    with zipfile.ZipFile(path) as archive:
        items = {name: archive.read(name) for name in archive.namelist()}
    items.pop(member, None)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in items.items():
            archive.writestr(name, data)
