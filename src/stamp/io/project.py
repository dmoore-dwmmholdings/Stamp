"""The .stamp project file - spec §4.4.

A zip archive that any unzip tool can open:

    manifest.json          the Document, serialized, with schema_version
    base/part.<ext>        a verbatim copy of the imported part
    profiles/<hash>.<ext>  verbatim copies of every imported profile
    thumbnail.png          512x512, for the recent-files list

Derived geometry is never stored.  Everything rebuilds from the sources plus the
manifest, so a project mailed to a shop still works, and a missing source is named
rather than guessed at.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from stamp.core.document import SCHEMA_VERSION, Document

MANIFEST = "manifest.json"
BASE_DIR = "base"
PROFILE_DIR = "profiles"
THUMBNAIL = "thumbnail.png"
EXTENSION = ".stamp"


class ProjectError(RuntimeError):
    """Saving or opening failed.  The message names the file and the problem."""


@dataclass
class OpenResult:
    document: Document
    #: Sources that were extracted from the archive, keyed by the manifest path.
    extracted: dict[str, str] = field(default_factory=dict)
    #: Sources that are missing and could not be recovered from the archive.
    missing: list[str] = field(default_factory=list)
    thumbnail: bytes | None = None
    work_dir: Path | None = None


def save(
    document: Document,
    path: str | Path,
    *,
    thumbnail: bytes | None = None,
    profile_paths: dict[str, str] | None = None,
) -> Path:
    """Write the project archive.

    *profile_paths* maps a feature's recorded ``source_path`` to where the file
    actually is now, which is how a relinked source gets archived correctly.
    """
    path = Path(path)
    if path.suffix.lower() != EXTENSION:
        path = path.with_suffix(EXTENSION)
    profile_paths = profile_paths or {}

    manifest = document.to_dict()
    manifest["schema_version"] = SCHEMA_VERSION

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            if document.base is not None and document.base.source_path:
                source = Path(document.base.source_path)
                if source.exists():
                    name = f"{BASE_DIR}/part{source.suffix.lower()}"
                    archive.write(source, name)
                    manifest["base"]["archive_path"] = name

            seen: set[str] = set()
            for feature, entry in zip(document.features, manifest["features"], strict=True):
                ref = feature.profile
                if not ref.source_path:
                    continue
                source = Path(profile_paths.get(ref.source_path, ref.source_path))
                name = f"{PROFILE_DIR}/{ref.source_hash}{source.suffix.lower()}"
                entry["profile"]["archive_path"] = name
                if name in seen:
                    continue
                if source.exists():
                    archive.write(source, name)
                    seen.add(name)

            archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
            if thumbnail:
                archive.writestr(THUMBNAIL, thumbnail)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise ProjectError(f"Stamp could not write {path.name}: {exc}") from exc

    tmp.replace(path)
    return path


def open_project(path: str | Path, work_dir: str | Path | None = None) -> OpenResult:
    """Read a project archive and extract its sources next to it.

    Sources are extracted so the importers can read real files.  A source that is
    absent from the archive *and* from its recorded path is reported by name, not
    guessed at (§10).
    """
    path = Path(path)
    if not path.exists():
        raise ProjectError(f"There is no file at {path}.")

    work = Path(work_dir) if work_dir else path.parent / f".{path.stem}_sources"
    work.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if MANIFEST not in names:
                raise ProjectError(
                    f"{path.name} is not a Stamp project - it has no {MANIFEST}."
                )
            manifest = json.loads(archive.read(MANIFEST))
            thumbnail = archive.read(THUMBNAIL) if THUMBNAIL in names else None

            document = Document.from_dict(manifest)
            extracted: dict[str, str] = {}
            missing: list[str] = []

            base_entry = manifest.get("base") or {}
            archive_path = base_entry.get("archive_path")
            if document.base is not None:
                target = _extract(archive, names, archive_path, work)
                if target:
                    extracted[document.base.source_path] = str(target)
                    document.base.source_path = str(target)
                elif not Path(document.base.source_path).exists():
                    missing.append(document.base.source_path)

            for feature, entry in zip(document.features, manifest.get("features", []), strict=False):
                archive_path = (entry.get("profile") or {}).get("archive_path")
                target = _extract(archive, names, archive_path, work)
                if target:
                    extracted[feature.profile.source_path] = str(target)
                    feature.profile.source_path = str(target)
                elif not Path(feature.profile.source_path).exists():
                    missing.append(feature.profile.source_path)
    except zipfile.BadZipFile as exc:
        raise ProjectError(f"{path.name} is not readable as a zip archive.") from exc
    except json.JSONDecodeError as exc:
        raise ProjectError(f"The manifest in {path.name} is damaged: {exc}") from exc

    document.name = path.stem
    return OpenResult(
        document=document,
        extracted=extracted,
        missing=sorted(set(missing)),
        thumbnail=thumbnail,
        work_dir=work,
    )


def _extract(archive: zipfile.ZipFile, names: set[str], member: str | None, work: Path):
    if not member or member not in names:
        return None
    target = work / Path(member).name
    with archive.open(member) as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return target


def read_thumbnail(path: str | Path) -> bytes | None:
    """Pull just the thumbnail out, for the recent-files list."""
    try:
        with zipfile.ZipFile(path) as archive:
            if THUMBNAIL in archive.namelist():
                return archive.read(THUMBNAIL)
    except Exception:
        return None
    return None


__all__ = [
    "EXTENSION",
    "OpenResult",
    "ProjectError",
    "open_project",
    "read_thumbnail",
    "save",
]
