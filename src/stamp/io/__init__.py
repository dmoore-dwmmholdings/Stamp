"""File formats.  Reading and writing, and nothing about geometry."""

from __future__ import annotations

from pathlib import Path


def with_extension(path: str | Path, extension: str) -> Path:
    """*path* with *extension* on the end, without eating a name's own dots.

    ``Path.with_suffix`` replaces everything after the last dot, so a project
    called "bracket v1.2" was saved as "bracket v1.stamp" and a revision number
    quietly went missing.  An extension that is already there is left alone;
    anything else is appended.
    """
    path = Path(path)
    if path.suffix.lower() == extension.lower():
        return path
    return path.with_name(path.name + extension)


__all__ = ["with_extension"]
