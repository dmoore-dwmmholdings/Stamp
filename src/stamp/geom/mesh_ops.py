"""The manifold3d path - spec §2, §6.5.

The tool solid arrives here as B-rep, already filleted.  It is tessellated at the
last moment and handed to manifold3d for the boolean.  That ordering is the whole
point: rounding the top edge of a raised logo works on an STL part, because the
rounding happened in OpenCascade before the mesh ever saw the tool.

What does *not* work in mesh mode is blending into the base surface, because that
needs exact surface geometry.  :data:`BLEND_NOT_AVAILABLE` is the message the UI
shows instead of graying out a button with no explanation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from OCP.TopoDS import TopoDS_Shape

BLEND_NOT_AVAILABLE = (
    "Blending into the base surface needs exact surface geometry, which an STL does "
    "not have. You can still round the edges of the feature itself. To blend into "
    "the part, start from a STEP file."
)

#: Tessellation deflection presets, in mm, shared with the STL exporter (§9).
QUALITY_PRESETS: dict[str, float] = {
    "draft": 0.1,
    "normal": 0.02,
    "fine": 0.005,
}


@dataclass
class MeshBooleanResult:
    manifold: object
    warnings: list[str] = field(default_factory=list)


def shape_to_manifold(shape: TopoDS_Shape, deflection: float = 0.02, angle: float = 0.3):
    """Tessellate a B-rep tool solid and wrap it as a Manifold."""
    from manifold3d import Manifold, Mesh

    verts, tris = triangulate(shape, deflection, angle)
    if len(verts) == 0 or len(tris) == 0:
        raise ValueError("The tool solid produced no triangles.")
    return Manifold(
        Mesh(
            vert_properties=verts.astype(np.float32),
            tri_verts=tris.astype(np.uint32),
        )
    )


def triangulate(
    shape: TopoDS_Shape, deflection: float = 0.02, angle: float = 0.3
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(vertices, triangles)`` for a B-rep shape, welded across faces."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    BRepMesh_IncrementalMesh(shape, deflection, False, angle, True)

    all_verts: list[tuple[float, float, float]] = []
    all_tris: list[tuple[int, int, int]] = []

    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None:
            explorer.Next()
            continue

        transform = location.Transformation()
        offset = len(all_verts)
        for i in range(1, triangulation.NbNodes() + 1):
            p = triangulation.Node(i).Transformed(transform)
            all_verts.append((p.X(), p.Y(), p.Z()))

        reversed_face = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
        for i in range(1, triangulation.NbTriangles() + 1):
            a, b, c = triangulation.Triangle(i).Get()
            if reversed_face:
                a, c = c, a
            all_tris.append((a - 1 + offset, b - 1 + offset, c - 1 + offset))
        explorer.Next()

    if not all_verts:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)

    verts = np.array(all_verts, dtype=np.float64)
    tris = np.array(all_tris, dtype=np.int64)
    return weld(verts, tris)


def weld(
    verts: np.ndarray, tris: np.ndarray, tol: float = 1e-7
) -> tuple[np.ndarray, np.ndarray]:
    """Merge coincident vertices so the per-face triangulations form one closed mesh."""
    if len(verts) == 0:
        return verts, tris
    decimals = max(0, int(round(-np.log10(tol))))
    keys = np.round(verts, decimals)
    _, first_index, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    new_verts = verts[first_index]
    new_tris = inverse[tris]
    # Drop triangles that collapsed to a line when their corners merged.
    keep = (
        (new_tris[:, 0] != new_tris[:, 1])
        & (new_tris[:, 1] != new_tris[:, 2])
        & (new_tris[:, 0] != new_tris[:, 2])
    )
    return new_verts, new_tris[keep]


def boolean(base_manifold, tool_manifold, kind: str) -> MeshBooleanResult:
    """Fuse or cut in the mesh engine.  ``kind`` is ``"add"`` or ``"cut"``.

    The tool is applied one component at a time.  A five-character serial number is
    five disconnected solids, and a single union against all five at once leaves
    some of them unmerged - the same trap as handing OpenCascade a compound (see
    ``solid_ops._split_compound``).
    """
    if kind not in ("add", "cut"):
        raise ValueError(f"Unknown boolean kind {kind!r}")

    warnings: list[str] = []
    before = base_manifold.volume()

    pieces = tool_manifold.decompose() or [tool_manifold]
    result = base_manifold
    for piece in pieces:
        result = result + piece if kind == "add" else result - piece
        if result.is_empty():
            raise ValueError(
                "The boolean removed everything. Check the depth and the direction."
            )

    after = result.volume()
    if abs(after - before) < 1e-6:
        if kind == "cut":
            warnings.append(
                "This cut does not touch the part, so nothing was removed. "
                "Check the depth and the direction."
            )
        else:
            warnings.append("This feature adds nothing. It is already inside the part.")

    if kind == "add" and len(result.decompose()) > 1:
        warnings.append(
            "This feature is not connected to the part. The result is more than one "
            "body, which will not print or machine as one piece."
        )

    return MeshBooleanResult(manifold=result, warnings=warnings)


def to_trimesh(manifold):
    import trimesh

    mesh = manifold.to_mesh()
    return trimesh.Trimesh(
        vertices=np.asarray(mesh.vert_properties)[:, :3],
        faces=np.asarray(mesh.tri_verts),
        process=False,
    )


def decimate_for_display(manifold, target: int):
    """Reduce triangle count for the viewport only, keeping the full mesh for booleans."""
    mesh = to_trimesh(manifold)
    if len(mesh.faces) <= target:
        return mesh
    try:
        return mesh.simplify_quadric_decimation(target)
    except Exception:
        return mesh


__all__ = [
    "BLEND_NOT_AVAILABLE",
    "MeshBooleanResult",
    "QUALITY_PRESETS",
    "boolean",
    "decimate_for_display",
    "shape_to_manifold",
    "to_trimesh",
    "triangulate",
    "weld",
]
