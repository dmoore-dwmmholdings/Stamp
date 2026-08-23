"""STEP / IGES / BREP / STL / 3MF / OBJ -> BasePart - spec §5.1, §5.2.

The two representations never mix.  A STEP file becomes a ``TopoDS_Shape`` and the
document runs in solid mode; an STL becomes a ``manifold3d.Manifold`` and the
document runs in mesh mode.  There is no mesh-to-solid reconstruction, in this
version or the next one.

On no account feed an STL through ``StlAPI_Reader``: it produces one planar face per
triangle, and sewing that is quadratic (§2).  For *display* of a mesh part,
:func:`mesh_triangulation` reads the file with ``RWStl`` straight into a
``Poly_Triangulation``, which is effectively free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shape

from stamp.core.document import BasePart
from stamp.io.profile_import import file_hash

SOLID_EXTS = {".step", ".stp", ".iges", ".igs", ".brep", ".brp"}
MESH_EXTS = {".stl", ".3mf", ".obj", ".ply", ".off"}
PART_EXTS = SOLID_EXTS | MESH_EXTS

#: Above this triangle count the viewport gets a decimated copy (§5.2).
DECIMATE_THRESHOLD = 1_000_000


class PartImportError(RuntimeError):
    """A part could not be read at all.  The message is shown to the user verbatim."""


@dataclass
class PartImportResult:
    part: BasePart
    #: Present when the file held several solids and the user must choose (§5.1).
    solids: list[TopoDS_Shape] = field(default_factory=list)
    #: True when the file carries no unit and the UI must prompt with a size preview.
    units_ambiguous: bool = False


def import_part(
    path: str | Path,
    *,
    unit_scale: float | None = None,
    solid_index: int | None = None,
    repair: bool = True,
) -> PartImportResult:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in SOLID_EXTS:
        return import_solid(path, solid_index=solid_index, repair=repair)
    if suffix in MESH_EXTS:
        return import_mesh(path, unit_scale=unit_scale)
    raise PartImportError(
        f"Stamp cannot read {suffix or 'this file'}. Open a STEP, IGES, BREP, STL, "
        f"3MF, or OBJ file."
    )


def mode_for(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in SOLID_EXTS:
        return "solid"
    if suffix in MESH_EXTS:
        return "mesh"
    raise PartImportError(f"Stamp cannot read {suffix or 'this file'}.")


# ----------------------------------------------------------------- solid mode


def import_solid(
    path: Path, *, solid_index: int | None = None, repair: bool = True
) -> PartImportResult:
    suffix = path.suffix.lower()
    warnings: list[str] = []

    if suffix in (".step", ".stp"):
        shape = _read_step(path)
    elif suffix in (".iges", ".igs"):
        shape, iges_warnings = _read_iges(path)
        warnings.extend(iges_warnings)
    else:
        shape = _read_brep(path)

    if shape is None or shape.IsNull():
        raise PartImportError(
            f"{path.name} contains no geometry that Stamp can read. "
            f"Check that the file exported correctly."
        )

    solids = _solids_of(shape)
    if len(solids) > 1:
        if solid_index is None:
            part = _describe_solid(shape, path, warnings, valid=True)
            return PartImportResult(part=part, solids=solids)
        shape = solids[solid_index]

    valid = BRepCheck_Analyzer(shape).IsValid()
    if not valid and repair:
        # Most real STEP is slightly dirty and still workable.  Try once, then let
        # the user proceed either way (§5.1).
        from OCP.ShapeFix import ShapeFix_Shape

        fixer = ShapeFix_Shape(shape)
        fixer.Perform()
        fixed = fixer.Shape()
        if not fixed.IsNull() and BRepCheck_Analyzer(fixed).IsValid():
            shape = fixed
            valid = True
            warnings.append("The part had small defects. Stamp repaired them on import.")
        else:
            warnings.append(
                "This part fails OpenCascade's validity check. Booleans on it can "
                "fail or give a wrong result. You can continue."
            )

    part = _describe_solid(shape, path, warnings, valid=valid)
    return PartImportResult(part=part)


def _read_step(path: Path) -> TopoDS_Shape | None:
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_Reader

    reader = STEPControl_Reader()
    status = reader.ReadFile(str(path))
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise PartImportError(f"{path.name} is not a STEP file that Stamp can read.")
    # OpenCascade reads the header length unit and scales to the system unit, which
    # is mm - so no explicit conversion is needed here.
    reader.TransferRoots()
    return reader.OneShape()


def _read_iges(path: Path) -> tuple[TopoDS_Shape | None, list[str]]:
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.IGESControl import IGESControl_Reader

    reader = IGESControl_Reader()
    status = reader.ReadFile(str(path))
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise PartImportError(f"{path.name} is not an IGES file that Stamp can read.")
    reader.TransferRoots()
    shape = reader.OneShape()

    # IGES is surfaces, not solids.  Sew and try to close it (§5.1).
    warnings: list[str] = []
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing

    sewing = BRepBuilderAPI_Sewing(1e-3)
    sewing.Add(shape)
    sewing.Perform()
    sewn = sewing.SewedShape()

    shells = _explore(sewn, TopAbs_ShapeEnum.TopAbs_SHELL)
    if len(shells) == 1:
        shell = TopoDS.Shell_s(shells[0])
        if shell.Closed():
            maker = BRepBuilderAPI_MakeSolid(shell)
            if maker.IsDone():
                return maker.Solid(), warnings
    warnings.append(
        "This IGES file is a set of surfaces that do not close into a solid. "
        "Cuts and fillets need a closed solid. Export STEP instead if you can."
    )
    return sewn, warnings


def _read_brep(path: Path) -> TopoDS_Shape:
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools

    shape = TopoDS_Shape()
    builder = BRep_Builder()
    if not BRepTools.Read_s(shape, str(path), builder):
        raise PartImportError(f"{path.name} is not a BREP file that Stamp can read.")
    return shape


def _explore(shape: TopoDS_Shape, kind: TopAbs_ShapeEnum) -> list[TopoDS_Shape]:
    out: list[TopoDS_Shape] = []
    explorer = TopExp_Explorer(shape, kind)
    while explorer.More():
        out.append(explorer.Current())
        explorer.Next()
    return out


def _solids_of(shape: TopoDS_Shape) -> list[TopoDS_Shape]:
    return [TopoDS.Solid_s(s) for s in _explore(shape, TopAbs_ShapeEnum.TopAbs_SOLID)]


def solids_intersect(solids: list[TopoDS_Shape]) -> bool:
    """True when any two solids overlap, so "treat as one body" would be wrong."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common

    for i in range(len(solids)):
        for j in range(i + 1, len(solids)):
            common = BRepAlgoAPI_Common(solids[i], solids[j])
            if common.IsDone() and _volume(common.Shape()) > 1e-6:
                return True
    return False


def _volume(shape: TopoDS_Shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()


def bounding_box(shape: TopoDS_Shape) -> tuple[float, float, float, float, float, float]:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return box.Get()


def _describe_solid(
    shape: TopoDS_Shape, path: Path, warnings: list[str], *, valid: bool
) -> BasePart:
    faces = _explore(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    return BasePart(
        source_path=str(path),
        source_hash=file_hash(path),
        mode="solid",
        unit_scale=1.0,
        bbox=bounding_box(shape),
        volume=_volume(shape),
        face_count=len(faces),
        triangle_count=0,
        watertight=True,
        valid=valid,
        warnings=warnings,
        runtime=shape,
    )


# ------------------------------------------------------------------ mesh mode


def import_mesh(path: Path, *, unit_scale: float | None = None) -> PartImportResult:
    import trimesh

    # process=True merges the duplicated vertices that STL stores per triangle.
    # Without it no edge is ever shared and nothing is ever watertight.
    try:
        loaded = trimesh.load(str(path), force="mesh", process=True)
    except ModuleNotFoundError as exc:
        # trimesh defers its per-format dependencies, thus a format it lists can
        # still fail here.  Say which package is absent, not "no module named".
        raise PartImportError(
            f"Stamp cannot read {path.name}. The {exc.name} package is not "
            f"installed. Install it, then open the file again."
        ) from exc
    except Exception as exc:
        raise PartImportError(f"Stamp cannot read {path.name}. {exc}") from exc
    if not isinstance(loaded, trimesh.Trimesh) or loaded.faces.shape[0] == 0:
        raise PartImportError(
            f"{path.name} contains no triangles that Stamp can read."
        )

    warnings: list[str] = []
    watertight = bool(loaded.is_watertight)
    if not watertight:
        # trimesh's repair pass, then re-test.  Never block on the result (§5.2).
        try:
            trimesh.repair.fill_holes(loaded)
            trimesh.repair.fix_normals(loaded)
            trimesh.repair.fix_winding(loaded)
        except Exception:
            pass
        watertight = bool(loaded.is_watertight)
        if watertight:
            warnings.append("This mesh had holes. Stamp repaired them on import.")
        else:
            warnings.append(
                "This mesh is not watertight, so booleans on it can fail. "
                "You can continue."
            )

    # STL carries no unit.  The caller shows a size preview and passes the answer
    # back as unit_scale; the default is mm (§5.2).
    ambiguous = unit_scale is None and path.suffix.lower() in (".stl", ".obj")
    scale = unit_scale if unit_scale is not None else 1.0
    if scale != 1.0:
        loaded.apply_scale(scale)

    manifold = _to_manifold(loaded)
    lo, hi = loaded.bounds
    return PartImportResult(
        part=BasePart(
            source_path=str(path),
            source_hash=file_hash(path),
            mode="mesh",
            unit_scale=scale,
            bbox=(float(lo[0]), float(lo[1]), float(lo[2]), float(hi[0]), float(hi[1]), float(hi[2])),
            volume=float(loaded.volume) if watertight else 0.0,
            triangle_count=int(loaded.faces.shape[0]),
            face_count=0,
            watertight=watertight,
            valid=watertight,
            warnings=warnings,
            runtime=manifold,
        ),
        units_ambiguous=ambiguous,
    )


def _to_manifold(mesh):
    import numpy as np
    from manifold3d import Manifold, Mesh

    verts = np.asarray(mesh.vertices, dtype=np.float32)
    tris = np.asarray(mesh.faces, dtype=np.uint32)
    return Manifold(Mesh(vert_properties=verts, tri_verts=tris))


def manifold_to_trimesh(manifold):
    import numpy as np
    import trimesh

    mesh = manifold.to_mesh()
    verts = np.asarray(mesh.vert_properties)[:, :3]
    tris = np.asarray(mesh.tri_verts)
    return trimesh.Trimesh(vertices=verts, faces=tris, process=False)


def mesh_triangulation(path: str | Path):
    """Read an STL straight into a ``Poly_Triangulation`` for display only.

    ``RWStl`` costs about 0.03 s for 82k triangles.  ``StlAPI_Reader`` on the same
    file produces 82k planar faces and is unusable (§5.2).
    """
    from OCP.RWStl import RWStl

    return RWStl.ReadFile_s(str(path))


def triangulation_to_shape(triangulation):
    """Wrap a ``Poly_Triangulation`` in a face so ``AIS_Shape`` can display it."""
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Face

    face = TopoDS_Face()
    builder = BRep_Builder()
    builder.MakeFace(face, triangulation)
    return face


def manifold_display_shape(manifold, *, decimate_to: int | None = None):
    """Turn the live mesh result into something the OCC viewer can draw."""
    import numpy as np
    from OCP.gp import gp_Pnt
    from OCP.Poly import Poly_Triangle, Poly_Triangulation

    mesh = manifold.to_mesh()
    verts = np.asarray(mesh.vert_properties)[:, :3]
    tris = np.asarray(mesh.tri_verts)
    if len(verts) == 0 or len(tris) == 0:
        raise ValueError("The mesh result is empty.")

    triangulation = Poly_Triangulation(len(verts), len(tris), False)
    for i, v in enumerate(verts, start=1):
        triangulation.SetNode(i, gp_Pnt(float(v[0]), float(v[1]), float(v[2])))
    for i, t in enumerate(tris, start=1):
        triangulation.SetTriangle(i, Poly_Triangle(int(t[0]) + 1, int(t[1]) + 1, int(t[2]) + 1))

    return triangulation_to_shape(triangulation)


def trimesh_display_shape(mesh):
    """Wrap a trimesh in a ``Poly_Triangulation`` for display."""
    import numpy as np
    from OCP.gp import gp_Pnt
    from OCP.Poly import Poly_Triangle, Poly_Triangulation

    verts = np.asarray(mesh.vertices)
    tris = np.asarray(mesh.faces)
    if len(verts) == 0 or len(tris) == 0:
        raise ValueError("The mesh is empty.")

    triangulation = Poly_Triangulation(len(verts), len(tris), False)
    for i, v in enumerate(verts, start=1):
        triangulation.SetNode(i, gp_Pnt(float(v[0]), float(v[1]), float(v[2])))
    for i, t in enumerate(tris, start=1):
        triangulation.SetTriangle(i, Poly_Triangle(int(t[0]) + 1, int(t[1]) + 1, int(t[2]) + 1))
    return triangulation_to_shape(triangulation)


__all__ = [
    "DECIMATE_THRESHOLD",
    "MESH_EXTS",
    "PART_EXTS",
    "PartImportError",
    "PartImportResult",
    "SOLID_EXTS",
    "bounding_box",
    "import_mesh",
    "import_part",
    "import_solid",
    "manifold_display_shape",
    "manifold_to_trimesh",
    "mesh_triangulation",
    "mode_for",
    "solids_intersect",
    "triangulation_to_shape",
    "trimesh_display_shape",
]
