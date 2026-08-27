"""Portable single-feature presets and the per-user preset library."""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QStandardPaths

from stamp.core.document import Anchor, Feature

EXTENSION = ".stamp-preset"
METADATA = "preset.json"


@dataclass(frozen=True)
class PresetInfo:
    """Lightweight library data used without extracting a preset's artwork."""

    path: Path
    name: str
    tags: tuple[str, ...]
    summary: str


def _feature_tags(feature: Feature) -> tuple[str, ...]:
    """Give every preset useful search terms, including old preset archives."""
    tags = [str(feature.operation.kind)]
    if feature.profile.text is not None:
        tags.append("text")
    elif feature.profile.code is not None:
        tags.extend(("code", str(feature.profile.code.kind)))
    else:
        tags.append("profile")
    if feature.pattern is not None:
        tags.extend(("pattern", str(feature.pattern.kind)))
    if feature.modifiers:
        tags.append("modified")
    return tuple(dict.fromkeys(tag.replace("_", " ") for tag in tags))


def library_dir() -> Path:
    root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    folder = root / "presets"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_preset(
    feature: Feature, path: str | Path | None = None, *, tags: list[str] | tuple[str, ...] | None = None
) -> Path:
    path = Path(path) if path else library_dir() / f"{feature.name}{EXTENSION}"
    if path.suffix != EXTENSION:
        path = path.with_suffix(EXTENSION)
    payload = feature.to_dict()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("feature.json", json.dumps(payload, indent=2))
        library_tags = tags if tags is not None else _feature_tags(feature)
        archive.writestr(
            METADATA,
            json.dumps({"name": feature.name, "tags": list(dict.fromkeys(library_tags))}, indent=2),
        )
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


def preset_info(path: str | Path) -> PresetInfo:
    """Read a preset's catalog entry while keeping its profile inside the archive."""
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        feature = Feature.from_dict(json.loads(archive.read("feature.json")))
        metadata = json.loads(archive.read(METADATA)) if METADATA in archive.namelist() else {}
    name = str(metadata.get("name") or feature.name or path.stem)
    raw_tags = metadata.get("tags")
    tags = tuple(str(tag) for tag in raw_tags) if isinstance(raw_tags, list) else _feature_tags(feature)
    summary = f"{str(feature.operation.kind).replace('_', ' ')} · " + ", ".join(tags)
    return PresetInfo(path=path, name=name, tags=tags, summary=summary)


def list_preset_info() -> list[PresetInfo]:
    """List valid local-library presets, ignoring a damaged file rather than hiding the library."""
    info: list[PresetInfo] = []
    for path in list_presets():
        try:
            info.append(preset_info(path))
        except (OSError, ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile):
            continue
    return info


__all__ = [
    "EXTENSION", "PresetInfo", "library_dir", "list_preset_info", "list_presets", "load_preset",
    "preset_info", "save_preset",
]
