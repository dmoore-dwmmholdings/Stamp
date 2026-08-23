"""STEP and STL export - spec §9.

STEP is solid mode only.  *STL in, STL out*: there is no mesh-to-solid
reconstruction, so a document that started from an STL cannot produce a STEP.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

from OCP.TopoDS import TopoDS_Shape

from stamp.geom import mesh_ops, solid_ops

STEP_SCHEMAS = {"AP242": "AP242DIS", "AP214": "AP214IS"}

#: Deflection presets shown in the STL dialog, in mm (§9).
STL_QUALITY = dict(mesh_ops.QUALITY_PRESETS)

MESH_MODE_NO_STEP = (
    "This project started from an STL, which is triangles and not exact surfaces. "
    "Stamp will not invent surfaces it does not have. Export STL, or start again "
    "from a STEP file if the shop needs one."
)


class ExportError(RuntimeError):
    """Export was refused.  The message says exactly why."""


@dataclass
class ExportResult:
    path: Path
    size_bytes: int = 0
    triangle_count: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def size_text(self) -> str:
        size = float(self.size_bytes)
        for unit in ("bytes", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"


def default_filename(project_name: str, extension: str) -> str:
    stamp = _dt.date.today().strftime("%Y%m%d")
    safe = "".join(c for c in project_name if c.isalnum() or c in "-_") or "stamp"
    return f"{safe}_{stamp}.{extension.lstrip('.')}"


# ------------------------------------------------------------------------ STEP


def export_step(
    shape: TopoDS_Shape,
    path: str | Path,
    *,
    schema: str = "AP242",
    simplify: bool = True,
    allow_invalid: bool = False,
) -> ExportResult:
    """Write a STEP file, refusing an invalid solid unless told otherwise."""
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    path = Path(path)
    warnings: list[str] = []

    if shape is None or shape.IsNull():
        raise ExportError("There is nothing to export.")
    if not solid_ops.check_valid(shape):
        if not allow_invalid:
            raise ExportError(
                "The result fails OpenCascade's validity check, so Stamp will not "
                "write it. A shop would not be able to use it. Export anyway only "
                "if you know the file is good."
            )
        warnings.append("This STEP was written from a solid that failed the validity check.")

    out_shape = shape
    if simplify:
        # Merging co-planar faces gives the shop a much cleaner file.  It is applied
        # only to the exported copy, because it invalidates face references (§3.1).
        try:
            out_shape = solid_ops.unify_same_domain(shape)
        except Exception:
            warnings.append("The co-planar face merge failed, so the raw solid was written.")
            out_shape = shape

    Interface_Static.SetCVal_s("write.step.unit", "MM")
    Interface_Static.SetCVal_s("write.step.schema", STEP_SCHEMAS.get(schema, "AP242DIS"))

    writer = STEPControl_Writer()
    writer.Transfer(out_shape, STEPControl_AsIs)
    status = writer.Write(str(path))
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise ExportError(f"Stamp could not write {path.name}. Check the folder and permissions.")

    return ExportResult(path=path, size_bytes=path.stat().st_size, warnings=warnings)


# ------------------------------------------------------------------------- STL


def export_stl(
    geometry: object,
    path: str | Path,
    *,
    mode: str = "solid",
    quality: str = "normal",
    deflection: float | None = None,
    ascii_format: bool = False,
) -> ExportResult:
    """Write an STL.  Solid mode tessellates; mesh mode writes the result directly."""
    path = Path(path)
    warnings: list[str] = []

    if mode == "mesh":
        mesh = mesh_ops.to_trimesh(geometry)
        if not mesh.is_watertight:
            warnings.append(
                "This mesh is not watertight. A slicer may refuse it or produce "
                "unexpected results."
            )
        mesh.export(path, file_type="stl_ascii" if ascii_format else "stl")
        return ExportResult(
            path=path,
            size_bytes=path.stat().st_size,
            triangle_count=int(len(mesh.faces)),
            warnings=warnings,
        )

    value = deflection if deflection is not None else STL_QUALITY.get(quality, 0.02)
    verts, tris = mesh_ops.triangulate(geometry, value)
    if len(tris) == 0:
        raise ExportError("There is nothing to export.")

    import trimesh

    mesh = trimesh.Trimesh(vertices=verts, faces=tris, process=False)
    if not mesh.is_watertight:
        warnings.append(
            "The tessellated result is not watertight. Try a finer quality setting."
        )
    mesh.export(path, file_type="stl_ascii" if ascii_format else "stl")
    return ExportResult(
        path=path,
        size_bytes=path.stat().st_size,
        triangle_count=int(len(tris)),
        warnings=warnings,
    )


def triangle_count_for(geometry: object, mode: str, deflection: float) -> int:
    """Live triangle count for the STL dialog, without writing anything."""
    if mode == "mesh":
        return int(len(mesh_ops.to_trimesh(geometry).faces))
    _, tris = mesh_ops.triangulate(geometry, deflection)
    return int(len(tris))


# -------------------------------------------------------------- export for quote


def export_for_quote(
    geometry: object,
    folder: str | Path,
    project_name: str,
    *,
    mode: str = "solid",
    screenshot: bytes | None = None,
    units: str = "mm",
    volume_mm3: float = 0.0,
    bbox: tuple[float, float, float, float, float, float] | None = None,
    quality: str = "normal",
) -> list[ExportResult]:
    """STEP + STL + a screenshot + a dimensions note, in one folder (§9).

    This is exactly what gets emailed to a machine shop.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    results: list[ExportResult] = []

    if mode == "solid":
        results.append(
            export_step(geometry, folder / default_filename(project_name, "step"))
        )
    results.append(
        export_stl(
            geometry,
            folder / default_filename(project_name, "stl"),
            mode=mode,
            quality=quality,
        )
    )

    if screenshot:
        png = folder / default_filename(project_name, "png")
        png.write_bytes(screenshot)
        results.append(ExportResult(path=png, size_bytes=png.stat().st_size))

    note = folder / default_filename(project_name, "txt")
    lines = [f"{project_name}", f"Exported {_dt.date.today().isoformat()}", ""]
    if bbox:
        x0, y0, z0, x1, y1, z1 = bbox
        lines.append(
            f"Bounding box: {x1 - x0:.2f} x {y1 - y0:.2f} x {z1 - z0:.2f} mm"
        )
    lines.append(f"Volume: {volume_mm3:.1f} mm3 ({volume_mm3 / 1000.0:.2f} cm3)")
    lines.append(f"Units: {units}")
    if mode == "mesh":
        lines.append("")
        lines.append("Source was a mesh, so there is no STEP file. " + MESH_MODE_NO_STEP)
    note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    results.append(ExportResult(path=note, size_bytes=note.stat().st_size))
    return results


__all__ = [
    "ExportError",
    "ExportResult",
    "MESH_MODE_NO_STEP",
    "STEP_SCHEMAS",
    "STL_QUALITY",
    "default_filename",
    "export_for_quote",
    "export_step",
    "export_stl",
    "triangle_count_for",
]


# ------------------------------------------------------------------------- 3MF


def export_3mf(
    bodies: list,
    path: str | Path,
    *,
    base_color: str = "#D8D8D8",
    feature_color: str = "#D62E2E",
) -> ExportResult:
    """Write a multi-body 3MF for multi-color printing.

    Every body becomes its own object in the file, colored through a standard
    ``basematerials`` group, and ``Metadata/model_settings.config`` assigns the
    base to filament slot 1 and every feature body to slot 2.  Bambu Studio and
    Orca read that assignment directly; any other 3MF consumer still gets a
    plain multi-object model and ignores the extra file.
    """
    import zipfile
    from xml.sax.saxutils import escape

    path = Path(path)
    if not bodies:
        raise ExportError("There is nothing to export.")
    total = sum(int(len(b.triangles)) for b in bodies)
    if total == 0:
        raise ExportError("There is nothing to export.")

    def color(value: str) -> str:
        value = value.strip()
        if not value.startswith("#"):
            value = "#" + value
        return value.upper()

    parts: list[str] = []
    parts.append(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        ' <resources>\n'
        '  <basematerials id="1">\n'
        f'   <base name="Base" displaycolor="{color(base_color)}"/>\n'
        f'   <base name="Feature" displaycolor="{color(feature_color)}"/>\n'
        '  </basematerials>\n'
    )
    config_rows: list[str] = []
    for index, body in enumerate(bodies):
        object_id = index + 2
        pindex = 0 if body.role == "base" else 1
        name = escape(body.name, {'"': "&quot;"})
        parts.append(
            f'  <object id="{object_id}" type="model" name="{name}" '
            f'pid="1" pindex="{pindex}">\n   <mesh>\n    <vertices>\n'
        )
        parts.append(
            "".join(
                f'     <vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>\n'
                for v in body.vertices
            )
        )
        parts.append("    </vertices>\n    <triangles>\n")
        parts.append(
            "".join(
                f'     <triangle v1="{t[0]}" v2="{t[1]}" v3="{t[2]}"/>\n'
                for t in body.triangles
            )
        )
        parts.append("    </triangles>\n   </mesh>\n  </object>\n")
        extruder = 1 if body.role == "base" else 2
        config_rows.append(
            f'  <object id="{object_id}">\n'
            f'    <metadata key="name" value="{name}"/>\n'
            f'    <metadata key="extruder" value="{extruder}"/>\n'
            f'  </object>\n'
        )
    parts.append(" </resources>\n <build>\n")
    for index in range(len(bodies)):
        parts.append(f'  <item objectid="{index + 2}"/>\n')
    parts.append(" </build>\n</model>\n")
    model_xml = "".join(parts)

    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        ' <Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        ' <Default Extension="model" '
        'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        ' <Default Extension="config" ContentType="text/xml"/>\n'
        "</Types>\n"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
        "</Relationships>\n"
    )
    model_settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<config>\n" + "".join(config_rows) + "</config>\n"
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("3D/3dmodel.model", model_xml)
        archive.writestr("Metadata/model_settings.config", model_settings)

    return ExportResult(path=path, size_bytes=path.stat().st_size, triangle_count=total)
