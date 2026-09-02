"""Mirror and scale the finished part - what every export writes.

The transform is applied to the rebuilt result, on its way out, in both solid and
mesh mode.  The rebuild itself never sees it, which is the point: a sketch plane,
an anchor, a snap target and a drag handle all stay in the part's own
untransformed space no matter what the transform says, so editing a mirrored part
works exactly like editing an unmirrored one.

Both the mirror plane and the scale are centred on the *base part's* bounding-box
centre, not the rebuilt result's.  Artwork would otherwise move the centre, and a
mirrored copy would stop lining up with the original as features were added.

Two traps live here.

A mirror reverses orientation.  In OpenCascade a reflected shape comes back with
its faces pointing inward - a solid of negative volume that a shop's CAM system
reads as a hole in space.  :func:`apply_solid` checks the signed volume and
reverses the shape when it is negative.

manifold3d has the same problem with a different symptom: negating one coordinate
of every vertex flips the winding of every triangle, so the mesh is inside out.
The transform matrix path in manifold3d handles the flip itself, so
:func:`apply_mesh` uses it rather than touching vertices directly, and the result
is checked for a positive volume all the same.
"""

from __future__ import annotations

import numpy as np
from OCP.TopoDS import TopoDS_Shape

from stamp.core.document import PartTransform

__all__ = [
    "PartTransformError",
    "apply",
    "apply_mesh",
    "apply_solid",
    "bbox_center",
    "for_export",
    "transform_bodies",
]


class PartTransformError(RuntimeError):
    """The transform could not be applied.  The message says exactly why."""


def bbox_center(bbox: tuple[float, float, float, float, float, float]) -> tuple[float, float, float]:
    x0, y0, z0, x1, y1, z1 = bbox
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0)


def _matrix(transform: PartTransform, center: tuple[float, float, float]) -> np.ndarray:
    """The 4x4 the transform amounts to: scale about *center*, then mirror in it."""
    factors = np.array(transform.scale, dtype=float)
    if transform.mirrors:
        factors[transform.mirror.axis] *= -1.0
    matrix = np.eye(4)
    matrix[:3, :3] = np.diag(factors)
    matrix[:3, 3] = np.array(center, dtype=float) - factors * np.array(center, dtype=float)
    return matrix


def apply(geometry: object, mode: str, transform: PartTransform, *, bbox=None) -> object:
    """Transform *geometry* in whichever mode it is in.  Identity returns it as is."""
    if geometry is None or transform.is_identity:
        return geometry
    problems = transform.validate()
    if problems:
        raise PartTransformError("\n".join(problems))
    if mode == "mesh":
        return apply_mesh(geometry, transform, bbox=bbox)
    return apply_solid(geometry, transform, bbox=bbox)


# ------------------------------------------------------------------ solid mode


def apply_solid(shape: TopoDS_Shape, transform: PartTransform, *, bbox=None) -> TopoDS_Shape:
    """Mirror and scale a B-rep solid about its own bounding-box center."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform, BRepBuilderAPI_Transform
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Dir, gp_GTrsf, gp_Mat, gp_Pnt, gp_Trsf, gp_XYZ

    if shape is None or shape.IsNull():
        raise PartTransformError("There is nothing to transform.")

    center = bbox_center(bbox) if bbox is not None else _solid_bbox_center(shape)
    result = shape

    factor = transform.scale
    if factor != (1.0, 1.0, 1.0):
        if transform.is_uniform_scale:
            # A uniform scale is a gp_Trsf, which keeps exact surfaces exact:
            # a cylinder stays a cylinder.  gp_GTrsf would turn it into a B-spline.
            trsf = gp_Trsf()
            trsf.SetScale(gp_Pnt(*center), factor[0])
            result = BRepBuilderAPI_Transform(result, trsf, True).Shape()
        else:
            gtrsf = gp_GTrsf()
            gtrsf.SetVectorialPart(gp_Mat(factor[0], 0, 0, 0, factor[1], 0, 0, 0, factor[2]))
            gtrsf.SetTranslationPart(
                gp_XYZ(*(c - f * c for c, f in zip(center, factor, strict=True)))
            )
            builder = BRepBuilderAPI_GTransform(result, gtrsf, True)
            if not builder.IsDone():
                raise PartTransformError(
                    "OpenCascade could not scale this part by different amounts on "
                    "each axis. Try a uniform scale, or export STL instead."
                )
            result = builder.Shape()
            # A gp_GTrsf carries the source triangulation across unchanged, and that
            # triangulation no longer sits on the stretched surfaces - which makes
            # BRepCheck call the result invalid and the exporter refuse to write it.
            # Dropping it is enough; the exporter meshes again at its own quality.
            BRepTools.Clean_s(result)

    if transform.mirrors:
        axis = transform.mirror.axis
        direction = [0.0, 0.0, 0.0]
        direction[axis] = 1.0
        trsf = gp_Trsf()
        trsf.SetMirror(gp_Ax2(gp_Pnt(*center), gp_Dir(*direction)))
        result = BRepBuilderAPI_Transform(result, trsf, True).Shape()
        result = _fix_orientation(result)

    return result


def _solid_bbox_center(shape: TopoDS_Shape) -> tuple[float, float, float]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return bbox_center((x0, y0, z0, x1, y1, z1))


def _fix_orientation(shape: TopoDS_Shape) -> TopoDS_Shape:
    """Turn a reflected solid right side out.

    ``SetMirror`` reflects the geometry but leaves the face orientations alone, so
    the result encloses negative volume.  Reversing the shape swaps every face
    orientation back, which is what makes the solid usable downstream.
    """
    from stamp.geom import solid_ops

    if solid_ops.volume(shape) >= 0:
        return shape
    reversed_shape = shape.Reversed()
    if solid_ops.volume(reversed_shape) < 0:
        raise PartTransformError(
            "The mirrored solid came back inside out and Stamp could not correct "
            "it. Export STL, which has no orientation to get wrong."
        )
    return reversed_shape


# ------------------------------------------------------------------- mesh mode


def apply_mesh(manifold, transform: PartTransform, *, bbox=None):
    """Mirror and scale a manifold3d result about its own bounding-box center."""
    if manifold is None:
        raise PartTransformError("There is nothing to transform.")

    if bbox is not None:
        center = bbox_center(bbox)
    else:
        low, high = manifold.bounding_box()[:3], manifold.bounding_box()[3:]
        center = tuple((a + b) / 2.0 for a, b in zip(low, high, strict=True))

    matrix = _matrix(transform, center)
    # manifold3d takes the 3x4 affine part and flips triangle winding itself when
    # the determinant is negative, so a mirror comes back with correct normals.
    result = manifold.transform(matrix[:3, :])
    if result.volume() <= 0:
        raise PartTransformError(
            "The mirrored mesh came back inside out. This is a bug - please report "
            "the part that produced it."
        )
    return result


# ------------------------------------------------------------- what exports use


def for_export(document, geometry: object) -> object:
    """The geometry an exporter should write for *document*.

    Every export path goes through here, so a mirrored or scaled part is written
    the same way whether it left the desktop app, the batch runner or the quote
    package.  An identity transform costs nothing and returns the same object.
    """
    if geometry is None or document is None:
        return geometry
    transform = getattr(document, "transform", None)
    if transform is None or transform.is_identity:
        return geometry
    mode = document.base.mode if document.base else "solid"
    bbox = document.base.bbox if document.base else None
    return apply(geometry, mode, transform, bbox=bbox)


def transform_bodies(document, bodies: list) -> list:
    """The 3MF colour bodies, transformed the same way the solid would be.

    The bodies are already tessellated, so this is arithmetic on vertices - with
    one catch.  Negating one coordinate reverses the winding of every triangle,
    which turns the body inside out for a slicer.  A mirror therefore swaps two
    indices of every triangle to put the winding back.
    """
    from dataclasses import replace as _replace

    if not bodies or document is None:
        return bodies
    transform = getattr(document, "transform", None)
    if transform is None or transform.is_identity:
        return bodies
    problems = transform.validate()
    if problems:
        raise PartTransformError("\n".join(problems))

    bbox = document.base.bbox if document.base else None
    center = bbox_center(bbox) if bbox is not None else (0.0, 0.0, 0.0)
    matrix = _matrix(transform, center)
    linear, offset = matrix[:3, :3], matrix[:3, 3]

    out = []
    for body in bodies:
        verts = np.asarray(body.vertices, dtype=float) @ linear.T + offset
        tris = np.asarray(body.triangles)
        if transform.mirrors:
            tris = tris[:, [0, 2, 1]]
        out.append(_replace(body, vertices=verts, triangles=tris))
    return out
