"""The normalized-profile cache - spec §5.5 step 7.

Reimport is free: a profile is keyed by its source hash plus every import option that
changes the result, so re-opening a project or duplicating a feature never re-parses
the artwork.
"""

from __future__ import annotations

from pathlib import Path

from stamp.core.document import ProfileRef
from stamp.io.normalize import Profile
from stamp.io.profile_import import ImportOptions, import_profile


class MissingSource(FileNotFoundError):
    """The artwork file a feature points at is gone.  Names it, offers a relink."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"The profile file is missing: {path}. Relink it, or put the file back."
        )
        self.path = path


class ProfileCache:
    def __init__(self) -> None:
        self._cache: dict[tuple, Profile] = {}
        #: Relinked paths, keyed by the original path recorded in the project.
        self.relinks: dict[str, str] = {}

    def clear(self) -> None:
        self._cache.clear()

    def options_for(self, ref: ProfileRef) -> ImportOptions:
        return ImportOptions(
            join_tolerance=ref.join_tolerance,
            close_open_loops=False,
            union_overlapping=ref.union_overlapping,
            outline_stroke_width=ref.outline_strokes,
            layers=ref.layers,
            extra_scale=ref.unit_scale,
        )

    def path_for(self, ref: ProfileRef) -> Path:
        if ref.is_text:
            raise MissingSource("a text feature has no file")
        path = Path(self.relinks.get(ref.source_path, ref.source_path))
        if not path.exists():
            raise MissingSource(ref.source_path)
        return path

    def get(self, ref: ProfileRef) -> Profile:
        key = ref.cache_key
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if ref.is_text:
            # Built from the message, not read from a file.  The key holds every
            # setting of the message, thus a change to it misses the cache.
            from stamp.io.text_profile import build_text_profile

            profile = build_text_profile(ref.text)
            self._cache[key] = profile
            return profile
        result = import_profile(self.path_for(ref), self.options_for(ref))
        self._cache[key] = result.profile
        return result.profile

    def put(self, ref: ProfileRef, profile: Profile) -> None:
        """Seed the cache from an import the UI already did, so it is not repeated."""
        self._cache[ref.cache_key] = profile

    def relink(self, original: str, new_path: str) -> None:
        self.relinks[original] = new_path
        self.clear()


__all__ = ["MissingSource", "ProfileCache"]
