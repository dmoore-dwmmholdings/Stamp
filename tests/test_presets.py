from __future__ import annotations

import json
import zipfile

import pytest

from stamp.core.document import Feature, Operation, OperationKind, ProfileRef, TextSpec
from stamp.io import presets
from stamp.io.presets import load_preset, preset_info, save_preset


def test_preset_catalog_metadata_is_searchable(tmp_path):
    feature = Feature(
        name="Serial plate",
        profile=ProfileRef(text=TextSpec(text="SN-{{serial}}")),
        operation=Operation(kind=OperationKind.ADD, depth=0.4),
    )
    path = save_preset(feature, tmp_path / "serial", tags=["production", "serial", "text"])

    info = preset_info(path)

    assert info.name == "Serial plate"
    assert info.tags == ("production", "serial", "text")
    assert "add" in info.summary


def test_old_preset_without_catalog_metadata_gets_inferred_tags(tmp_path):
    path = tmp_path / "legacy.stamp-preset"
    feature = Feature(name="Legacy QR", profile=ProfileRef(text=TextSpec(text="hello")))
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("feature.json", json.dumps(feature.to_dict()))

    info = preset_info(path)
    loaded = load_preset(path, tmp_path / "extracted")

    assert info.name == "Legacy QR"
    assert "text" in info.tags
    assert loaded.placement.anchor.face_ref is None
    assert loaded.placement.anchor.plane is None


class TestTheLibraryStampKeeps:
    """Saving a preset asks for a name, not for somewhere to put the file."""

    @pytest.fixture
    def library(self, tmp_path, monkeypatch):
        folder = tmp_path / "library"
        folder.mkdir()
        monkeypatch.setattr(presets, "library_dir", lambda: folder)
        return folder

    def test_a_name_is_all_it_takes(self, library):
        feature = Feature(name="Lid logo", profile=ProfileRef(text=TextSpec(text="hi")))

        written = presets.save_preset(feature, presets.library_path("Lid logo"))

        assert written.parent == library
        assert [info.name for info in presets.list_preset_info()] == ["Lid logo"]

    def test_a_name_a_file_system_would_refuse_is_still_saved(self, library):
        feature = Feature(name="A/B: test?", profile=ProfileRef(text=TextSpec(text="hi")))

        presets.save_preset(feature, presets.library_path(feature.name))

        assert [info.name for info in presets.list_preset_info()] == ["A/B: test?"]

    def test_saving_the_same_name_replaces_it(self, library):
        for depth in (0.4, 0.9):
            feature = Feature(
                name="Lid logo",
                profile=ProfileRef(text=TextSpec(text="hi")),
                operation=Operation(kind=OperationKind.CUT, depth=depth),
            )
            presets.save_preset(feature, presets.library_path("Lid logo"))

        assert len(presets.list_presets()) == 1
        again = presets.load_preset(presets.list_presets()[0], library / "out")
        assert again.operation.depth == 0.9

    def test_a_preset_can_be_thrown_away(self, library):
        feature = Feature(name="Lid logo", profile=ProfileRef(text=TextSpec(text="hi")))
        path = presets.save_preset(feature, presets.library_path("Lid logo"))

        presets.delete_preset(path)
        presets.delete_preset(path)  # already gone is not an error

        assert presets.list_presets() == []
