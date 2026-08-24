"""Portable single-feature presets and the per-user preset library."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from PySide6.QtCore import QStandardPaths

from stamp.core.document import Anchor, Feature

EXTENSION = ".stamp-preset"


def library_dir() -> Path:
    root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    folder = root / "presets"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_preset(feature: Feature, path: str | Path | None = None) -> Path:
    path = Path(path) if path else library_dir() / f"{feature.name}{EXTENSION}"
    if path.suffix != EXTENSION:
        path = path.with_suffix(EXTENSION)
    payload = feature.to_dict()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("feature.json", json.dumps(payload, indent=2))
        if feature.profile.source_path and Path(feature.profile.source_path).exists():
            source = Path(feature.profile.source_path)
            archive.write(source, "profile" + source.suffix.lower())
    return path


def load_preset(path: str | Path, extraction_dir: str | Path) -> Feature:
    with zipfile.ZipFile(path) as archive:
        feature = Feature.from_dict(json.loads(archive.read("feature.json")))
        assets = [name for name in archive.namelist() if name.startswith("profile.")]
        if assets:
            target = Path(extraction_dir) / Path(assets[0]).name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(assets[0]))
            feature.profile.source_path = str(target)
    feature = feature.copy_with_new_id()
    # A preset must be placed deliberately on its new part.
    feature.placement.anchor = Anchor()
    return feature


def list_presets() -> list[Path]:
    return sorted(library_dir().glob("*" + EXTENSION), key=lambda p: p.name.lower())


__all__ = ["EXTENSION", "library_dir", "list_presets", "load_preset", "save_preset"]
