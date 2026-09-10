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

import hashlib
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from stamp.core.document import SCHEMA_VERSION, Document
from stamp.io import with_extension

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
    #: The base part's recorded path when that is one of the missing sources.
    #: Named separately because relinking it is a different action from
    #: relinking artwork, and because nothing opens without it.
    missing_base: str | None = None
    thumbnail: bytes | None = None
    work_dir: Path | None = None


def save(
    document: Document,
    path: str | Path,
    *,
    thumbnail: bytes | None = None,
    profile_paths: dict[str, str] | None = None,
    base_path: str | None = None,
) -> Path:
    """Write the project archive.

    *profile_paths* maps a feature's recorded ``source_path`` to where the file
    actually is now, which is how a relinked source gets archived correctly.
    *base_path* does the same for the base part.

    A project whose base part cannot be found is not written.  It used to be:
    the archive simply came out without ``base/part.*``, and the result could
    never be opened again - opening reported the base as missing, the import
    then failed, and the window showed nothing.  Better to refuse and name the
    file while the user still has it.
    """
    path = with_extension(path, EXTENSION)
    profile_paths = profile_paths or {}

    manifest = document.to_dict()
    manifest["schema_version"] = SCHEMA_VERSION

    base_source = None
    carried_base = None
    if document.base is not None and document.base.source_path:
        base_source = Path(base_path or document.base.source_path)
        if not base_source.exists():
            # The project may already hold a verbatim copy of the part, from
            # the archive it was opened from.  A source that has since moved is
            # then no reason to refuse the save.
            carried_base = _archived_base(path, document.base.source_hash)
            if carried_base is None:
                raise ProjectError(
                    f"Stamp cannot save {path.name}: the part it was built from is "
                    f"no longer at {base_source}. Relink it, then save again."
                )
            base_source = None

    tmp = path.with_name(path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            if carried_base is not None:
                name, data = carried_base
                archive.writestr(name, data)
                manifest["base"]["archive_path"] = name
            elif base_source is not None:
                name = f"{BASE_DIR}/part{base_source.suffix.lower()}"
                archive.write(base_source, name)
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


def _archived_base(path: Path, source_hash: str) -> tuple[str, bytes] | None:
    """The base part copy already inside the project at *path*, if there is one.

    Saving over a project that was opened from an archive can reuse the copy the
    archive carries, so a part file that has moved on disk since does not block
    the save.  The name is preserved so the manifest still points at it.

    Only the copy of *this* part will do.  Whatever is already at *path* may be
    a different project entirely - a "Save As" over an unrelated file - or the
    same project from before the part was replaced, and writing either one under
    the new manifest saves geometry the document is not describing, with nothing
    to say so on reopen.  The archived bytes are hashed the way
    :func:`~stamp.io.profile_import.file_hash` hashes the source, and anything
    that does not match is treated as no copy at all: the caller then asks for a
    relink, which is the truthful answer.
    """
    if not path.exists() or not source_hash:
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.startswith(f"{BASE_DIR}/part"):
                    data = archive.read(name)
                    if hashlib.sha256(data).hexdigest()[:32] != source_hash:
                        return None
                    return name, data
    except (OSError, zipfile.BadZipFile):
        return None
    return None


def _fallback_work_dir(path: Path) -> Path:
    """Somewhere writable to unpack the sources of a project we cannot write beside.

    One directory per archive rather than one per open, keyed on the archive's
    own path: a read-only project that is opened, closed and opened again used
    to leave a fresh ``stamp-sources-*`` behind on every open, and nothing ever
    removed them.  Reusing the directory also means the extracted sources are
    already there the second time.
    """
    key = hashlib.sha256(str(path.absolute()).encode("utf-8")).hexdigest()[:16]
    work = Path(tempfile.gettempdir()) / f"stamp-sources-{key}"
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Somebody else's directory of the same name, on a shared machine.
        return Path(tempfile.mkdtemp(prefix="stamp-sources-"))
    return work


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
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError:
        # A project on a read-only volume, a CD, or somebody else's share still
        # has to open.  The sources go somewhere writable instead.
        work = _fallback_work_dir(path)

    missing_base: str | None = None
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
                    missing_base = document.base.source_path
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
    except ProjectError:
        raise
    except ValueError as exc:
        # Document.from_dict says what it will not read - a newer schema, most
        # often.  Its wording is the message; only the file name is added.
        raise ProjectError(f"Stamp cannot open {path.name}. {exc}") from exc
    except (AttributeError, TypeError, KeyError, IndexError) as exc:
        raise ProjectError(
            f"The manifest in {path.name} is damaged: a field is not the kind of "
            f"value Stamp expects ({exc})."
        ) from exc
    except OSError as exc:
        raise ProjectError(f"Stamp could not read {path.name}: {exc}") from exc

    document.name = path.stem
    return OpenResult(
        document=document,
        extracted=extracted,
        missing=sorted(set(missing)),
        missing_base=missing_base,
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
