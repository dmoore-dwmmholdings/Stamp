"""Swap the part for an updated file and keep the stamps where they were.

A part goes through revisions - a hole moves, a wall thickens, a boss appears -
and the artwork on it should not have to be placed again each time.  Nothing here
is new machinery: a feature already stores its anchor as geometry plus intent
rather than a face index (§8.2), precisely so it can be resolved against a shape
it has never seen.  This module points that resolver at a *different* part and
reports what happened.

Three outcomes per feature, and the difference matters to the user:

* **kept** - the anchor resolved and the sketch plane is where it was.
* **moved** - it resolved, but the face has shifted, so the artwork moved with
  it.  That is usually right, and always worth saying out loud.
* **lost** - nothing on the new part matches.  The feature is left exactly as it
  was, with its old anchor, so the rebuild reports it and the user re-picks a
  face.  Deleting someone's work because a file changed is never the answer.

An exported revision often has a different origin - the same part, moved.  Every
stored point would then miss by that offset, so a translation between the two
bounding-box centres is tried as well, and whichever pass places more features
wins.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from stamp.core.document import Anchor, AnchorKind, BasePart, Document, FaceRef, Plane
from stamp.core.refs import (
    ReferenceError,
    plane_from_face,
    resolve_face_ref,
)

#: Past this, the face is treated as having moved rather than stayed put, in mm.
MOVED_TOLERANCE_MM = 0.05

#: A translation is only worth applying if it is bigger than this, in mm.
MIN_ALIGNMENT_MM = 1e-6


@dataclass
class FeatureMatch:
    """What became of one feature's anchor."""

    feature_id: str
    name: str
    status: str  # "kept" | "moved" | "lost"
    score: float = 0.0
    moved_mm: float = 0.0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status != "lost"


@dataclass
class ReplaceReport:
    matches: list[FeatureMatch] = field(default_factory=list)
    #: Translation applied to every stored point before matching, in mm.
    alignment: tuple[float, float, float] = (0.0, 0.0, 0.0)
    warnings: list[str] = field(default_factory=list)

    @property
    def kept(self) -> list[FeatureMatch]:
        return [m for m in self.matches if m.status == "kept"]

    @property
    def moved(self) -> list[FeatureMatch]:
        return [m for m in self.matches if m.status == "moved"]

    @property
    def lost(self) -> list[FeatureMatch]:
        return [m for m in self.matches if m.status == "lost"]

    @property
    def aligned(self) -> bool:
        return any(abs(v) > MIN_ALIGNMENT_MM for v in self.alignment)

    @property
    def ok(self) -> bool:
        return not self.lost

    def summary(self) -> str:
        if not self.matches:
            return "The part was replaced. There was no artwork on it."
        parts = [f"{len(self.kept)} stayed put"]
        if self.moved:
            parts.append(f"{len(self.moved)} followed the face they sit on")
        if self.lost:
            parts.append(f"{len(self.lost)} need a face picked again")
        return "The part was replaced: " + ", ".join(parts) + "."


def _bbox_center(bbox) -> tuple[float, float, float]:
    x0, y0, z0, x1, y1, z1 = bbox
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0)


def alignment_between(old: BasePart, new: BasePart) -> tuple[float, float, float]:
    """How far the new part's middle sits from the old one's."""
    if not old.bbox or not new.bbox:
        return (0.0, 0.0, 0.0)
    a = _bbox_center(old.bbox)
    b = _bbox_center(new.bbox)
    return (b[0] - a[0], b[1] - a[1], b[2] - a[2])


def _shift(point, delta) -> tuple[float, float, float]:
    return (point[0] + delta[0], point[1] + delta[1], point[2] + delta[2])


def _shifted_ref(ref: FaceRef, delta) -> FaceRef:
    """A copy of *ref* whose stored points are moved by *delta*."""
    moved = FaceRef.from_dict(ref.to_dict())
    moved.point = _shift(ref.point, delta)
    if ref.bbox_center:
        moved.bbox_center = _shift(ref.bbox_center, delta)
    return moved


def _match_face_anchor(anchor: Anchor, shape, delta) -> tuple[Plane | None, float, str]:
    """Resolve a face anchor against *shape*, returning the plane it landed on."""
    ref = anchor.face_ref
    if ref is None:
        return None, 0.0, "This feature has no face reference."
    try:
        resolved = resolve_face_ref(_shifted_ref(ref, delta), shape)
    except ReferenceError as exc:
        return None, 0.0, str(exc)
    point = _shift(ref.point, delta)
    try:
        plane, _warnings = plane_from_face(resolved.face, point)
    except Exception as exc:  # a face that resolves but cannot give a plane
        return None, resolved.score, str(exc)
    detail = ""
    if resolved.ambiguous:
        detail = "Two faces matched equally well, so check where this one landed."
    return plane, resolved.score, detail


def _match_mesh_anchor(anchor: Anchor, manifold, delta) -> tuple[Plane | None, float, str]:
    """Re-fit a mesh region at the stored point on the new mesh."""
    import numpy as np

    from stamp.geom import mesh_regions

    stored = anchor.plane
    if stored is None:
        return None, 0.0, "This feature has no plane on the mesh."

    origin = _shift(stored.origin, delta)
    normal = stored.normal
    vertices, faces = mesh_regions.mesh_arrays(manifold)
    # Look from outside the surface, back along the normal, so the ray meets the
    # face the plane was fitted to rather than the far side of the part.
    diagonal = float(np.linalg.norm(vertices.max(axis=0) - vertices.min(axis=0))) or 1.0
    start = tuple(o + n * diagonal for o, n in zip(origin, normal, strict=True))
    region = mesh_regions.region_at(
        vertices, faces, start, tuple(-n for n in normal)
    )
    if region is None:
        return None, 0.0, "Nothing on the new mesh is under this feature."
    dot = sum(a * b for a, b in zip(region.plane.normal, normal, strict=True))
    if dot < 0.7:
        return None, 0.0, "The surface here faces a different way now."
    return region.plane, float(max(0.0, dot)), ""


def _classify(old_plane: Plane | None, new_plane: Plane) -> tuple[str, float]:
    if old_plane is None:
        return "kept", 0.0
    distance = math.dist(old_plane.origin, new_plane.origin)
    return ("moved" if distance > MOVED_TOLERANCE_MM else "kept", distance)


def _match_all(document: Document, new_part: BasePart, delta) -> ReplaceReport:
    report = ReplaceReport(alignment=tuple(delta))
    shape = new_part.runtime
    for feature in document.features:
        anchor = feature.placement.anchor
        old_plane = anchor.plane
        if anchor.kind is AnchorKind.DATUM:
            # A datum is absolute, so a new part cannot move it.
            report.matches.append(
                FeatureMatch(feature.id, feature.name, "kept", 1.0,
                             detail="On a datum plane, so the part does not affect it.")
            )
            continue

        if new_part.mode == "mesh" or anchor.kind is AnchorKind.MESH_REGION:
            plane, score, detail = _match_mesh_anchor(anchor, shape, delta)
        else:
            plane, score, detail = _match_face_anchor(anchor, shape, delta)

        if plane is None:
            report.matches.append(
                FeatureMatch(feature.id, feature.name, "lost", score, detail=detail)
            )
            continue
        status, distance = _classify(old_plane, plane)
        report.matches.append(
            FeatureMatch(feature.id, feature.name, status, score,
                         moved_mm=distance, detail=detail)
        )
    return report


def plan_replacement(document: Document, new_part: BasePart) -> ReplaceReport:
    """Work out what a replacement would do, without changing the document.

    Both a straight match and an aligned one are tried, and the better result is
    the one reported.  "Better" is more features placed first, then less movement,
    because a pass that keeps everything where it was beats one that drags the
    artwork across the part.
    """
    if document.base is None:
        raise ValueError("There is no part to replace.")
    if new_part.runtime is None:
        raise ValueError("The new part has no geometry.")

    straight = _match_all(document, new_part, (0.0, 0.0, 0.0))
    delta = alignment_between(document.base, new_part)
    if all(abs(v) <= MIN_ALIGNMENT_MM for v in delta):
        return straight

    aligned = _match_all(document, new_part, delta)

    def rank(report: ReplaceReport) -> tuple[int, float]:
        return (len(report.lost), sum(m.moved_mm for m in report.matches))

    best = min((straight, aligned), key=rank)
    if best is aligned:
        best.warnings.append(
            f"The new part sits {math.dist((0, 0, 0), delta):.2f} mm from where the "
            f"old one did, so the artwork was moved with it."
        )
    return best


def replace_part(document: Document, new_part: BasePart) -> ReplaceReport:
    """Put *new_part* in the document and re-anchor every feature to it.

    A feature that cannot be matched keeps the anchor it had.  The rebuild will
    report it, the feature tree will show it as broken, and the user can pick a
    face again - which is recoverable, where deleting it would not be.
    """
    report = plan_replacement(document, new_part)
    delta = report.alignment
    shape = new_part.runtime

    for feature in document.features:
        match = next((m for m in report.matches if m.feature_id == feature.id), None)
        if match is None or match.status == "lost":
            continue
        anchor = feature.placement.anchor
        if anchor.kind is AnchorKind.DATUM:
            continue
        if new_part.mode == "mesh" or anchor.kind is AnchorKind.MESH_REGION:
            plane, _score, _detail = _match_mesh_anchor(anchor, shape, delta)
            if plane is not None:
                anchor.plane = plane
            continue
        plane, _score, _detail = _match_face_anchor(anchor, shape, delta)
        if plane is None:
            continue
        anchor.plane = plane
        # Re-capture the reference against the new part, so the next replacement
        # starts from where the face is now rather than from where it once was.
        if anchor.face_ref is not None:
            try:
                resolved = resolve_face_ref(_shifted_ref(anchor.face_ref, delta), shape)
            except ReferenceError:
                continue
            from stamp.core.refs import make_face_ref

            anchor.face_ref = make_face_ref(
                resolved.face,
                _shift(anchor.face_ref.point, delta),
                origin_feature_id=anchor.face_ref.origin_feature_id,
            )

    document.base = new_part
    return report


__all__ = [
    "FeatureMatch",
    "MOVED_TOLERANCE_MM",
    "ReplaceReport",
    "alignment_between",
    "plan_replacement",
    "replace_part",
]
