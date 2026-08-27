from __future__ import annotations

import json
import zipfile

from stamp.core.document import Feature, Operation, OperationKind, ProfileRef, TextSpec
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
