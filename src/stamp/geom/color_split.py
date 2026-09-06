"""Split the rebuilt part into per-color bodies for multi-color printing - §9.

A slicer prints a second color only where there is a second body, so the result
is divided along feature boundaries:

* A raised feature becomes its own body: the part of the tool solid that survives
  into the result (``result ∩ tool``).  The base body loses that region
  (``result − tool``), so the two mate exactly with no overlap.
* An engraved feature becomes an inlay that fills the pocket flush with the
  surface: the volume the cut removed (``base ∩ tool − result``).
* A color stamp is that same inlay, and it is the whole point of the feature
  rather than a bonus: the recess is only a layer or two deep and exists so the
  slicer has somewhere to put a second color.  Filled, the artwork is flush with
  the face; unfilled, the part ships with an open recess, so a stamp that yields
  no body is reported in those words.
* A through cut stays a hole; refilling it in another color would defeat it.

The tool solid used here is the one the rebuild kept, before its own fillets and
chamfers.  Its small contact overlap reaches into the base, which anchors a raised
body and matches how the inlay seats.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stamp.core.document import (
    DepthMode,
    Document,
    OperationKind,
    PlacementMode,
)
from stamp.core.rebuild import RebuildResult
from stamp.geom import mesh_ops, solid_ops

#: Bodies smaller than this are noise from the boolean, not printable geometry.
MIN_BODY_VOLUME_MM3 = 1e-4


@dataclass
class ColorBody:
    """One printable body, already tessellated."""

    name: str
    role: str  # "base" or "feature"
    vertices: object  # (n, 3) float array
    triangles: object  # (m, 3) int array
    #: The colour this body is to be printed in, as ``#rrggbb``.  Empty means the
    #: caller's default for the role, which is what a single-colour feature gets.
    color: str = ""
    #: The artwork component this came from, when the feature was split by colour.
    component: str = ""

    @property
    def triangle_count(self) -> int:
        return int(len(self.triangles))


@dataclass
class ColorSplit:
    bodies: list[ColorBody] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def feature_count(self) -> int:
        return sum(1 for b in self.bodies if b.role == "feature")


class ColorSplitError(RuntimeError):
    """The split failed.  The message says why."""


def split_for_color(
    document: Document,
    result: RebuildResult,
    *,
    deflection: float = 0.02,
    profiles=None,
    part_index: int | None = None,
) -> ColorSplit:
    """Divide the rebuilt geometry into a base body and one body per feature.

    A feature whose artwork components have been given different colours divides
    further, one body per component.  *profiles* is the caller's profile cache,
    which the per-component split needs to see the artwork again; without one a
    private cache is used and the artwork is re-read.

    *part_index* narrows the whole thing to one part of an assembly: its own
    rebuilt geometry, its own original for the pocket to be measured against,
    and only the features that sit on it.  That is what "export just the lid"
    means, and doing it here rather than by trimming the finished bodies is what
    keeps the pocket arithmetic honest.
    """
    if document.base is None or document.base.runtime is None:
        raise ColorSplitError("There is no part loaded.")

    final = result.geometry
    original = document.base.runtime
    rows = list(result.features)
    if part_index is not None and part_index >= 0:
        piece = result.part(part_index)
        part = next((p for p in document.base.parts if p.index == part_index), None)
        if piece is None or piece.geometry is None or part is None:
            raise ColorSplitError("That part is not in the rebuilt result.")
        final = piece.geometry
        original = part.runtime if part.runtime is not None else original
        on_it = {f.id for f in document.features if f.part_index == part_index}
        rows = [r for r in rows if r.feature_id in on_it]
    if final is None:
        raise ColorSplitError("There is nothing to export. Rebuild first.")

    if profiles is None:
        from stamp.core.profiles import ProfileCache

        profiles = ProfileCache()
    if result.mode == "solid":
        return _split_solid(document, result, deflection, profiles, final, original, rows)
    return _split_mesh(document, result, deflection, profiles, final, original, rows)


def effective_colors(profile, feature) -> dict[str, str]:
    """What each artwork component actually prints in.

    What the user chose for it, and failing that the colour the artwork was
    drawn in.  Falling back to the source colour is the whole difference between
    a two-colour logo arriving as two filaments and arriving as one lump -
    nobody draws a logo in two colours and means one - and the panel still lets
    them say otherwise.

    One function because two places have to agree: what is exported, and what
    the preview shows.  A preview that lies about the colours is worse than a
    preview with no colours in it.
    """
    components = list(getattr(profile, "components", []) or [])
    return {c.key: feature.color_for(c.key, c.color or "") for c in components}


def divides_by_color(profile, feature) -> bool:
    """Whether this feature has more than one colour to divide into."""
    wanted = effective_colors(profile, feature)
    return len({value for value in wanted.values() if value}) > 1


def _feature_of(document: Document, feature_id: str):
    return next((f for f in document.features if f.id == feature_id), None)


def _skipped(feature, reason: str) -> str:
    """Say what a skipped feature costs, which is not the same for both kinds.

    A raised or engraved feature that yields no body simply prints in the base
    color.  A color stamp that yields no body leaves the recess it cut wide open,
    which is a defect in the part and not only a missing color.
    """
    if feature.operation.kind is OperationKind.COLOR:
        return (
            f"{feature.name}: {reason} A color stamp with no body leaves an open "
            f"recess in the face, so fix it or turn the feature off."
        )
    return f"{feature.name}: {reason}"


def _profile_for(profiles, feature) -> object | None:
    """The normalized artwork behind a feature, or None if it cannot be had.

    The split needs it only to divide a body between colours; a failure here
    costs the colour breakdown, never the export.
    """
    try:
        return profiles.get(feature.profile)
    except Exception:  # noqa: BLE001 - a missing source is reported elsewhere
        return None


def _split_by_component(
    document: Document, profiles, feature, row, body: object, *, mesh: bool,
    deflection: float, warnings: list[str],
) -> list[tuple[str, str, object]]:
    """Divide one feature body into ``(name, colour, geometry)`` per component.

    Returns a single entry when the feature is one colour, which is the usual
    case and the one that must stay exactly as cheap as it was.
    """
    if row.tool is None:
        return [(feature.name, "", body)]
    profile = _profile_for(profiles, feature)
    if profile is None or len(profile.components) < 2:
        return [(feature.name, "", body)]

    wanted = effective_colors(profile, feature)
    if not divides_by_color(profile, feature):
        return [(feature.name, "", body)]

    if feature.placement.mode is PlacementMode.WRAP:
        # The dividing masks are flat prisms of each component's footprint, and a
        # wrapped tool's footprint is bent around the face.  A flat prism would
        # cut the wrong pieces out of it, so the feature stays one colour and
        # says why rather than exporting something wrong in silence.
        warnings.append(
            f"{feature.name}: wrapped artwork cannot be split by colour, so it "
            f"exports in one colour. Place it flat to colour the pieces separately."
        )
        return [(feature.name, "", body)]

    from stamp.geom.tool_solid import component_prisms

    reach = max(_diagonal(document) * 4.0, 1.0)
    prisms = component_prisms(profile, feature.placement, row.tool, reach=reach)
    if len(prisms) < 2:
        return [(feature.name, "", body)]

    out: list[tuple[str, str, object]] = []
    for component in profile.components:
        prism = prisms.get(component.key)
        if prism is None:
            continue
        try:
            if mesh:
                piece = mesh_ops.boolean(
                    body, mesh_ops.shape_to_manifold(prism, deflection), "intersect"
                ).manifold
                empty = piece.is_empty() or piece.volume() < MIN_BODY_VOLUME_MM3
            else:
                piece = solid_ops.boolean(body, prism, "common", collect_history=False).shape
                empty = solid_ops.volume(piece) < MIN_BODY_VOLUME_MM3
        except (solid_ops.GeometryError, ValueError):
            continue
        if empty:
            continue
        out.append(
            (f"{feature.name} - {component.label}", wanted.get(component.key, ""), piece)
        )
    return out or [(feature.name, "", body)]


def _diagonal(document: Document) -> float:
    if document.base is None:
        return 100.0
    x0, y0, z0, x1, y1, z1 = document.base.bbox
    return max(1.0, ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5)


def _split_solid(
    document: Document, result: RebuildResult, deflection: float, profiles,
    final, original, rows,
) -> ColorSplit:
    out = ColorSplit()
    base_shape = final

    pieces: list[tuple[str, str, object]] = []
    for row in rows:
        feature = _feature_of(document, row.feature_id)
        if feature is None or row.tool is None:
            continue
        if row.broken:
            out.warnings.append(_skipped(feature, "this feature is broken, so it was skipped."))
            continue
        tool = row.tool.shape
        try:
            if feature.operation.kind is OperationKind.ADD:
                body = solid_ops.boolean(final, tool, "common", collect_history=False).shape
                base_shape = solid_ops.boolean(
                    base_shape, tool, "cut", collect_history=False
                ).shape
            else:
                if feature.operation.depth_mode is DepthMode.THROUGH_ALL:
                    out.warnings.append(
                        f"{feature.name}: a through cut stays open, so it has no "
                        f"second-color body."
                    )
                    continue
                pocket = solid_ops.boolean(
                    original, tool, "common", collect_history=False
                ).shape
                body = solid_ops.boolean(pocket, final, "cut", collect_history=False).shape
        except solid_ops.GeometryError as exc:
            out.warnings.append(_skipped(feature, f"the color split failed ({exc}), skipped."))
            continue
        if solid_ops.volume(body) < MIN_BODY_VOLUME_MM3:
            out.warnings.append(
                _skipped(feature, "this feature leaves no printable volume, so it was skipped.")
            )
            continue
        pieces.extend(
            _split_by_component(
                document, profiles, feature, row, body, mesh=False,
                deflection=deflection, warnings=out.warnings,
            )
        )

    verts, tris = mesh_ops.triangulate(base_shape, deflection)
    out.bodies.append(ColorBody(name="base", role="base", vertices=verts, triangles=tris))
    for name, color, body in pieces:
        verts, tris = mesh_ops.triangulate(body, deflection)
        out.bodies.append(
            ColorBody(
                name=name, role="feature", vertices=verts, triangles=tris, color=color
            )
        )
    return out


def _split_mesh(
    document: Document, result: RebuildResult, deflection: float, profiles,
    final, original, rows,
) -> ColorSplit:
    out = ColorSplit()
    base_shape = final

    pieces: list[tuple[str, str, object]] = []
    for row in rows:
        feature = _feature_of(document, row.feature_id)
        if feature is None or row.tool is None:
            continue
        if row.broken:
            out.warnings.append(_skipped(feature, "this feature is broken, so it was skipped."))
            continue
        try:
            tool = mesh_ops.shape_to_manifold(row.tool.shape, deflection)
            if feature.operation.kind is OperationKind.ADD:
                body = mesh_ops.boolean(final, tool, "intersect").manifold
                base_shape = mesh_ops.boolean(base_shape, tool, "cut").manifold
            else:
                if feature.operation.depth_mode is DepthMode.THROUGH_ALL:
                    out.warnings.append(
                        f"{feature.name}: a through cut stays open, so it has no "
                        f"second-color body."
                    )
                    continue
                pocket = mesh_ops.boolean(original, tool, "intersect").manifold
                body = mesh_ops.boolean(pocket, final, "cut").manifold
        except ValueError as exc:
            out.warnings.append(_skipped(feature, f"the color split failed ({exc}), skipped."))
            continue
        if body.is_empty() or body.volume() < MIN_BODY_VOLUME_MM3:
            out.warnings.append(
                _skipped(feature, "this feature leaves no printable volume, so it was skipped.")
            )
            continue
        pieces.extend(
            _split_by_component(
                document, profiles, feature, row, body, mesh=True,
                deflection=deflection, warnings=out.warnings,
            )
        )

    mesh = mesh_ops.to_trimesh(base_shape)
    out.bodies.append(
        ColorBody(name="base", role="base", vertices=mesh.vertices, triangles=mesh.faces)
    )
    for name, color, body in pieces:
        mesh = mesh_ops.to_trimesh(body)
        out.bodies.append(
            ColorBody(
                name=name, role="feature", vertices=mesh.vertices,
                triangles=mesh.faces, color=color,
            )
        )
    return out


__all__ = ["ColorBody", "ColorSplit", "ColorSplitError", "split_for_color"]
