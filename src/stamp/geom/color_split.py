"""Split the rebuilt part into per-color bodies for multi-color printing - §9.

A slicer prints a second color only where there is a second body, so the result
is divided along feature boundaries:

* A raised feature becomes its own body: the part of the tool solid that survives
  into the result (``result ∩ tool``).  The base body loses that region
  (``result − tool``), so the two mate exactly with no overlap.
* An engraved feature becomes an inlay that fills the pocket flush with the
  surface: the volume the cut removed (``base ∩ tool − result``).
* A through cut stays a hole; refilling it in another color would defeat it.

The tool solid used here is the one the rebuild kept, before its own fillets and
chamfers.  Its small contact overlap reaches into the base, which anchors a raised
body and matches how the inlay seats.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stamp.core.document import DepthMode, Document, OperationKind
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
    document: Document, result: RebuildResult, *, deflection: float = 0.02
) -> ColorSplit:
    """Divide the rebuilt geometry into a base body and one body per feature."""
    if result.geometry is None:
        raise ColorSplitError("There is nothing to export. Rebuild first.")
    if document.base is None or document.base.runtime is None:
        raise ColorSplitError("There is no part loaded.")

    if result.mode == "solid":
        return _split_solid(document, result, deflection)
    return _split_mesh(document, result, deflection)


def _feature_of(document: Document, feature_id: str):
    return next((f for f in document.features if f.id == feature_id), None)


def _split_solid(document: Document, result: RebuildResult, deflection: float) -> ColorSplit:
    out = ColorSplit()
    final = result.geometry
    base_shape = final

    pieces: list[tuple[str, object]] = []
    for row in result.features:
        feature = _feature_of(document, row.feature_id)
        if feature is None or row.tool is None:
            continue
        if row.broken:
            out.warnings.append(f"{feature.name}: this feature is broken, so it was skipped.")
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
                    document.base.runtime, tool, "common", collect_history=False
                ).shape
                body = solid_ops.boolean(pocket, final, "cut", collect_history=False).shape
        except solid_ops.GeometryError as exc:
            out.warnings.append(f"{feature.name}: the color split failed ({exc}), skipped.")
            continue
        if solid_ops.volume(body) < MIN_BODY_VOLUME_MM3:
            out.warnings.append(
                f"{feature.name}: this feature leaves no printable volume, so it was skipped."
            )
            continue
        pieces.append((feature.name, body))

    verts, tris = mesh_ops.triangulate(base_shape, deflection)
    out.bodies.append(ColorBody(name="base", role="base", vertices=verts, triangles=tris))
    for name, body in pieces:
        verts, tris = mesh_ops.triangulate(body, deflection)
        out.bodies.append(ColorBody(name=name, role="feature", vertices=verts, triangles=tris))
    return out


def _split_mesh(document: Document, result: RebuildResult, deflection: float) -> ColorSplit:
    out = ColorSplit()
    final = result.geometry
    base_shape = final

    pieces = []
    for row in result.features:
        feature = _feature_of(document, row.feature_id)
        if feature is None or row.tool is None:
            continue
        if row.broken:
            out.warnings.append(f"{feature.name}: this feature is broken, so it was skipped.")
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
                pocket = mesh_ops.boolean(document.base.runtime, tool, "intersect").manifold
                body = mesh_ops.boolean(pocket, final, "cut").manifold
        except ValueError as exc:
            out.warnings.append(f"{feature.name}: the color split failed ({exc}), skipped.")
            continue
        if body.is_empty() or body.volume() < MIN_BODY_VOLUME_MM3:
            out.warnings.append(
                f"{feature.name}: this feature leaves no printable volume, so it was skipped."
            )
            continue
        pieces.append((feature.name, body))

    mesh = mesh_ops.to_trimesh(base_shape)
    out.bodies.append(
        ColorBody(name="base", role="base", vertices=mesh.vertices, triangles=mesh.faces)
    )
    for name, body in pieces:
        mesh = mesh_ops.to_trimesh(body)
        out.bodies.append(
            ColorBody(name=name, role="feature", vertices=mesh.vertices, triangles=mesh.faces)
        )
    return out


__all__ = ["ColorBody", "ColorSplit", "ColorSplitError", "split_for_color"]
