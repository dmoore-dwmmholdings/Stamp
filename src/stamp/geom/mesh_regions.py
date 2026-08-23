"""Picking a sketch plane on a mesh part - spec §6.1, "Mesh mode".

An STL has no faces, only triangles.  Clicking picks one triangle; the app then
grows a region of connected triangles whose normals agree to within a tolerance and
fits a plane to that region by least squares.  The region is handed back so the
viewport can show what it locked onto, because the answer is a guess and the user
has to be able to see it.

Ray casting is done here rather than through ``trimesh.ray``, which needs ``rtree``.
A vectorized Moller-Trumbore over the triangle array is a few lines, has no extra
dependency, and is fast enough for the sizes involved.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from stamp.core.document import Plane

#: Default angle between triangle normals that still counts as the same region.
DEFAULT_TOLERANCE_DEG = 5.0

#: A region smaller than this is probably a single facet on a curved surface, and
#: the plane fitted to it means very little.
SMALL_REGION_TRIANGLES = 3

#: A region covering less than this share of the part is called small in the warning.
SMALL_REGION_AREA_FRACTION = 0.002


@dataclass
class Region:
    """A set of connected triangles and the plane fitted to them."""

    triangles: np.ndarray  # indices into the face array
    plane: Plane
    area: float
    point: tuple[float, float, float]
    flatness: float  # RMS distance of the region vertices from the fitted plane, mm
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return int(len(self.triangles))


def ray_triangle_hits(
    origin: np.ndarray,
    direction: np.ndarray,
    vertices: np.ndarray,
    faces: np.ndarray,
    *,
    epsilon: float = 1e-9,
) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized Moller-Trumbore.  Returns ``(face_indices, distances)``."""
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]

    edge1 = v1 - v0
    edge2 = v2 - v0
    pvec = np.cross(direction, edge2)
    det = np.einsum("ij,ij->i", edge1, pvec)

    parallel = np.abs(det) < epsilon
    safe_det = np.where(parallel, 1.0, det)
    inv_det = 1.0 / safe_det

    tvec = origin - v0
    u = np.einsum("ij,ij->i", tvec, pvec) * inv_det
    qvec = np.cross(tvec, edge1)
    v = np.einsum("j,ij->i", direction, qvec) * inv_det
    t = np.einsum("ij,ij->i", edge2, qvec) * inv_det

    hit = (~parallel) & (u >= 0.0) & (v >= 0.0) & (u + v <= 1.0) & (t > epsilon)
    indices = np.flatnonzero(hit)
    return indices, t[indices]


def pick_triangle(
    vertices: np.ndarray,
    faces: np.ndarray,
    origin: tuple[float, float, float],
    direction: tuple[float, float, float],
) -> tuple[int, tuple[float, float, float]] | None:
    """The nearest triangle a ray hits, with the hit point."""
    if len(faces) == 0:
        return None
    o = np.asarray(origin, dtype=np.float64)
    d = np.asarray(direction, dtype=np.float64)
    norm = np.linalg.norm(d)
    if norm < 1e-12:
        return None
    d = d / norm

    indices, distances = ray_triangle_hits(o, d, vertices, faces)
    if len(indices) == 0:
        return None
    nearest = int(np.argmin(distances))
    point = o + d * distances[nearest]
    return int(indices[nearest]), (float(point[0]), float(point[1]), float(point[2]))


def face_normals(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    normals = np.cross(v1 - v0, v2 - v0)
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    lengths[lengths == 0.0] = 1.0
    return normals / lengths


def face_areas(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    return np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1) / 2.0


def build_adjacency(faces: np.ndarray) -> dict[int, list[int]]:
    """Face adjacency across shared edges."""
    edge_map: dict[tuple[int, int], list[int]] = {}
    for index, face in enumerate(faces):
        a, b, c = int(face[0]), int(face[1]), int(face[2])
        for u, v in ((a, b), (b, c), (c, a)):
            key = (u, v) if u < v else (v, u)
            edge_map.setdefault(key, []).append(index)

    adjacency: dict[int, list[int]] = {}
    for shared in edge_map.values():
        if len(shared) < 2:
            continue
        for i in shared:
            for j in shared:
                if i != j:
                    adjacency.setdefault(i, []).append(j)
    return adjacency


def grow_region(
    seed: int,
    normals: np.ndarray,
    adjacency: dict[int, list[int]],
    tolerance_deg: float,
) -> np.ndarray:
    """Flood-fill outward while each neighbour's normal stays within tolerance.

    The comparison is against the *seed* normal, not the neighbour's.  Comparing to
    the neighbour would let a gently curved surface creep round a whole fillet one
    tolerable step at a time.
    """
    limit = math.cos(math.radians(max(tolerance_deg, 0.0)))
    seed_normal = normals[seed]

    seen = {seed}
    queue = deque([seed])
    while queue:
        current = queue.popleft()
        for neighbour in adjacency.get(current, ()):
            if neighbour in seen:
                continue
            if float(np.dot(normals[neighbour], seed_normal)) >= limit:
                seen.add(neighbour)
                queue.append(neighbour)
    return np.fromiter(sorted(seen), dtype=np.int64, count=len(seen))


def fit_plane(points: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """Least-squares plane through *points*.  Returns (centroid, normal, rms)."""
    centroid = points.mean(axis=0)
    centred = points - centroid
    # The smallest right singular vector is the direction of least variance, which
    # for a set of points on a plane is the plane normal.
    _u, _s, vt = np.linalg.svd(centred, full_matrices=False)
    normal = vt[-1]
    distances = centred @ normal
    rms = float(np.sqrt(np.mean(distances**2))) if len(points) else 0.0
    return centroid, normal, rms


def orthogonal_axis(normal: np.ndarray) -> np.ndarray:
    """A unit vector in the plane, from global +X where that is usable."""
    reference = np.array([1.0, 0.0, 0.0])
    if abs(float(np.dot(reference, normal))) > 0.9:
        reference = np.array([0.0, 1.0, 0.0])
    axis = reference - normal * float(np.dot(reference, normal))
    length = np.linalg.norm(axis)
    if length < 1e-9:
        return np.array([1.0, 0.0, 0.0])
    return axis / length


def region_at(
    vertices: np.ndarray,
    faces: np.ndarray,
    origin: tuple[float, float, float],
    direction: tuple[float, float, float],
    *,
    tolerance_deg: float = DEFAULT_TOLERANCE_DEG,
    adjacency: dict[int, list[int]] | None = None,
    normals: np.ndarray | None = None,
) -> Region | None:
    """Pick a triangle along a ray, grow its region, and fit a plane to it (§6.1)."""
    hit = pick_triangle(vertices, faces, origin, direction)
    if hit is None:
        return None
    seed, point = hit

    if normals is None:
        normals = face_normals(vertices, faces)
    if adjacency is None:
        adjacency = build_adjacency(faces)

    triangles = grow_region(seed, normals, adjacency, tolerance_deg)
    region_points = vertices[np.unique(faces[triangles].reshape(-1))]
    centroid, normal, rms = fit_plane(region_points)

    # Point the fitted normal the same way as the triangle the user actually clicked.
    if float(np.dot(normal, normals[seed])) < 0:
        normal = -normal

    areas = face_areas(vertices, faces)
    region_area = float(areas[triangles].sum())
    total_area = float(areas.sum()) or 1.0

    warnings: list[str] = []
    if len(triangles) <= SMALL_REGION_TRIANGLES:
        warnings.append(
            f"Only {len(triangles)} triangle"
            f"{'s' if len(triangles) != 1 else ''} matched, so this is probably a "
            f"curved surface rather than a flat one. Raise the tolerance, or expect "
            f"the plane to be a rough guess."
        )
    elif region_area / total_area < SMALL_REGION_AREA_FRACTION:
        warnings.append(
            "The flat region found here is very small. Check that it is the surface "
            "you meant, or raise the tolerance."
        )
    if rms > 0.05:
        warnings.append(
            f"The triangles in this region sit up to {rms:.2f} mm off the fitted "
            f"plane, so the surface is not truly flat."
        )

    axis = orthogonal_axis(normal)
    # Put the origin at the click point projected onto the fitted plane, which is
    # where the user pointed rather than the middle of whatever was found.
    click = np.asarray(point)
    projected = click - normal * float(np.dot(click - centroid, normal))

    plane = Plane(
        origin=(float(projected[0]), float(projected[1]), float(projected[2])),
        normal=(float(normal[0]), float(normal[1]), float(normal[2])),
        u_axis=(float(axis[0]), float(axis[1]), float(axis[2])),
    )
    return Region(
        triangles=triangles,
        plane=plane,
        area=region_area,
        point=point,
        flatness=rms,
        warnings=warnings,
    )


def region_shape(
    vertices: np.ndarray,
    faces: np.ndarray,
    triangles: np.ndarray,
    *,
    normal: tuple[float, float, float] | None = None,
    offset: float = 0.05,
):
    """The region as a displayable shape, so the user sees what was detected.

    The copy is lifted a hair off the surface along *normal*.  Without that it is
    exactly coplanar with the part and the two fight for the same depth, which
    leaves the highlight invisible in patches - the one thing it exists to avoid.
    """
    from OCP.gp import gp_Pnt
    from OCP.Poly import Poly_Triangle, Poly_Triangulation

    from stamp.io.part_import import triangulation_to_shape

    used = faces[triangles]
    unique, remapped = np.unique(used.reshape(-1), return_inverse=True)
    points = vertices[unique]
    tris = remapped.reshape(-1, 3)
    if normal is not None and offset:
        points = points + np.asarray(normal, dtype=np.float64) * offset

    triangulation = Poly_Triangulation(len(points), len(tris), False)
    for i, v in enumerate(points, start=1):
        triangulation.SetNode(i, gp_Pnt(float(v[0]), float(v[1]), float(v[2])))
    for i, t in enumerate(tris, start=1):
        triangulation.SetTriangle(
            i, Poly_Triangle(int(t[0]) + 1, int(t[1]) + 1, int(t[2]) + 1)
        )
    return triangulation_to_shape(triangulation)


def mesh_arrays(manifold) -> tuple[np.ndarray, np.ndarray]:
    """Vertices and triangles of a Manifold, as float64 / int64 arrays."""
    mesh = manifold.to_mesh()
    vertices = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    faces = np.asarray(mesh.tri_verts, dtype=np.int64)
    return vertices, faces


__all__ = [
    "DEFAULT_TOLERANCE_DEG",
    "Region",
    "build_adjacency",
    "face_areas",
    "face_normals",
    "fit_plane",
    "grow_region",
    "mesh_arrays",
    "pick_triangle",
    "ray_triangle_hits",
    "region_at",
    "region_shape",
]
