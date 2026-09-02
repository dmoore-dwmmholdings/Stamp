"""STEP and STL export - spec §9.

STEP is solid mode only.  *STL in, STL out*: there is no mesh-to-solid
reconstruction, so a document that started from an STL cannot produce a STEP.
"""

from __future__ import annotations

import base64
import datetime as _dt
import html
import json
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from OCP.TopoDS import TopoDS_Shape

from stamp.core.document import COLOR_STAMP_MIN_DEPTH, OperationKind
from stamp.core.inspection import inspect_document
from stamp.geom import mesh_ops, solid_ops

if TYPE_CHECKING:
    from stamp.core.document import Document
    from stamp.core.rebuild import RebuildResult

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
class PreflightReport:
    """A format-neutral export decision shared by the UI and batch runner."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def require_ok(self) -> None:
        if self.errors:
            raise ExportError("\n".join(self.errors))


def preflight_export(
    document: Document, rebuild: RebuildResult | None, fmt: str, path: str | Path | None = None
) -> PreflightReport:
    """Check conditions that must be known before any exporter writes a file."""
    report = PreflightReport()
    fmt = fmt.lower().lstrip(".")
    if document.base is None or document.base.runtime is None:
        report.errors.append("There is no part to export.")
        return report
    if rebuild is None or rebuild.geometry is None:
        report.errors.append("Rebuild the part before exporting.")
        return report
    if not rebuild.ok:
        report.errors.extend(rebuild.errors or ["One or more features could not be rebuilt."])
    if fmt == "step" and document.base.mode != "solid":
        report.errors.append(MESH_MODE_NO_STEP)
    if fmt not in {"step", "stl", "3mf"}:
        report.errors.append(f"Stamp cannot export the {fmt!r} format.")
    if path is not None:
        target = Path(path)
        if not target.name:
            report.errors.append("Choose a file name for the export.")
        elif not target.parent.exists():
            report.errors.append("The export folder does not exist.")
        elif not target.parent.is_dir():
            report.errors.append("The export location is not a folder.")
    if document.base.warnings:
        report.warnings.extend(document.base.warnings)
    report.warnings.extend(inspect_document(document))
    for feature in document.features:
        for modifier in feature.modifiers:
            if modifier.enabled and modifier.value < 0.05:
                report.warnings.append(
                    f"{feature.name}: {modifier.label} is very small and may not survive manufacturing."
                )
    for row in (rebuild.features if rebuild else []):
        report.warnings.extend(row.warnings)
        if row.suggested_values:
            report.warnings.append("One or more modifier values were reduced to rebuild safely.")
    if fmt == "3mf":
        report.warnings.append("3MF exports separate bodies; verify material assignment in your slicer.")
    report.warnings.extend(_color_stamp_warnings(document, fmt))
    report.errors.extend(_transform_errors(document))
    report.warnings.extend(_transform_warnings(document))
    return report


def _transform_errors(document: Document) -> list[str]:
    return list(document.transform.validate())


def _transform_warnings(document: Document) -> list[str]:
    """What a mirrored or scaled export needs said before it is written.

    The transform runs after the features, so every dimension in the project -
    artwork size, engraving depth, fillet radius - is multiplied along with the
    part.  That is usually what a user scaling a whole part wants, and always
    something they should be told rather than left to measure.
    """
    transform = document.transform
    if transform.is_identity:
        return []
    out: list[str] = []
    if transform.mirrors:
        out.append(
            "This part is mirrored on the way out, so text and artwork read "
            "backwards in the exported file. That is right for a die or a "
            "handed pair, and wrong if you wanted a readable mark."
        )
    if transform.scale != (1.0, 1.0, 1.0):
        factors = transform.scale
        shown = (
            f"{factors[0] * 100:g}%"
            if transform.is_uniform_scale
            else " x ".join(f"{f:g}" for f in factors)
        )
        out.append(
            f"This part is scaled {shown} on the way out. Artwork sizes, "
            f"engraving depths, fillet and chamfer radii are all multiplied with "
            f"it - the numbers in the panel are the ones before scaling."
        )
        if not transform.is_uniform_scale and document.base is not None:
            if document.base.mode == "solid":
                out.append(
                    "A different scale on each axis turns holes into ellipses and "
                    "fillets into variable-radius blends. Check the result before "
                    "sending it to a shop."
                )
        smallest = min(transform.scale)
        thinned = [
            f.name for f in document.features
            if f.enabled and f.operation.kind is OperationKind.COLOR
            and f.operation.depth * smallest < COLOR_STAMP_MIN_DEPTH
        ]
        if thinned:
            out.append(
                f"{', '.join(thinned)}: after scaling, the color stamp is thinner "
                f"than one printed layer on most machines, so the color may not "
                f"appear."
            )
    return out


def _color_stamp_warnings(document: Document, fmt: str) -> list[str]:
    """What a color stamp needs said before it is written - §9.

    A stamp is a recess the 3MF export fills back in.  Only 3MF carries the second
    body, so every other format writes the recess and nothing else, and the user
    should hear that from Stamp rather than discover it on the printer.
    """
    stamps = [
        f for f in document.features
        if f.enabled and f.operation.kind is OperationKind.COLOR
    ]
    if not stamps:
        return []
    out = [
        f"{f.name}: {f.operation.depth:.3f} mm is thinner than one printed layer on "
        f"most machines, so the color may not appear. Use "
        f"{COLOR_STAMP_MIN_DEPTH} mm or more."
        for f in stamps
        if f.operation.depth < COLOR_STAMP_MIN_DEPTH
    ]
    if fmt in {"step", "stl"}:
        names = ", ".join(f.name for f in stamps)
        out.append(
            f"{names}: a color stamp is color, and {fmt.upper()} carries none. It is "
            f"written as the shallow recess it cuts. Export 3MF to have it filled "
            f"back in and printed flush."
        )
    return out


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


def default_filename(project_name: str, extension: str, *, suffix: str = "") -> str:
    """The suggested name for an export.

    *suffix* names what the part transform did - ``mirrored``, ``125pct`` - so a
    mirrored copy never lands on top of the original in the same folder.
    """
    stamp = _dt.date.today().strftime("%Y%m%d")
    safe = "".join(c for c in project_name if c.isalnum() or c in "-_") or "stamp"
    tail = f"-{suffix}" if suffix else ""
    return f"{safe}{tail}_{stamp}.{extension.lstrip('.')}"


NO_QT_FOR_PDF = (
    "Writing a PDF needs the desktop application. Run this from Stamp rather than "
    "from a script with no Qt application."
)


def _require_qt_application() -> None:
    """QPdfWriter aborts the process outright when no QGuiApplication exists.

    That is not an exception a caller can catch - the interpreter dies - so the
    check has to happen before the writer is built.
    """
    try:
        from PySide6.QtGui import QGuiApplication
    except Exception as exc:  # pragma: no cover - PySide6 is a hard dependency
        raise ExportError(NO_QT_FOR_PDF) from exc
    if QGuiApplication.instance() is None:
        raise ExportError(NO_QT_FOR_PDF)


def transform_summary(document) -> str:
    """One line describing the part transform, for a handoff record.

    A shop holding a mirrored or scaled file has no way to tell from the geometry
    alone which hand or which size it received, so every record Stamp writes says
    it in words.
    """
    transform = getattr(document, "transform", None)
    if transform is None or transform.is_identity:
        return "As modelled"
    said = []
    if transform.mirrors:
        said.append(transform.mirror.label.split(" (")[0].replace("Mirror ", "Mirrored "))
    if transform.scale != (1.0, 1.0, 1.0):
        if transform.is_uniform_scale:
            said.append(f"scaled to {transform.scale[0] * 100:g}%")
        else:
            said.append(
                "scaled " + " x ".join(f"{f:g}" for f in transform.scale) + " (X, Y, Z)"
            )
    line = ", ".join(said)
    if document.base is not None:
        dx, dy, dz = transform.size_from(document.base.size)
        line += f" - finished {dx:.2f} x {dy:.2f} x {dz:.2f} mm"
    return line[0].upper() + line[1:]


def _proof_html(document, warnings: list[str], screenshot: bytes | None) -> str:
    """The readable handoff record shared by standalone and package PDF exports."""
    image = ""
    if screenshot:
        encoded = base64.b64encode(screenshot).decode("ascii")
        image = f'<p><img src="data:image/png;base64,{encoded}" width="480"></p>'
    part = document.base
    part_lines = []
    if part is not None:
        part_lines = [
            ("Source", Path(part.source_path).name or "embedded project part"),
            ("Kind", "Solid" if part.mode == "solid" else "Mesh"),
            ("Bounding box", " × ".join(f"{value:.2f}" for value in part.bbox) + " mm"),
            ("Mirror and scale", transform_summary(document)),
        ]
    rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in values) + "</tr>"
        for feature in document.features
        for values in [
            (
                feature.name,
                feature.operation.label,
                f"{feature.operation.depth:.2f} mm",
                feature.metadata.identifier,
                feature.metadata.process,
            )
        ]
    ) or "<tr><td colspan=\"5\">No marks are defined.</td></tr>"
    facts = "".join(
        f"<tr><th>{html.escape(caption)}</th><td>{html.escape(value)}</td></tr>"
        for caption, value in part_lines
    )
    warning_rows = "".join(f"<li>{html.escape(warning)}</li>" for warning in warnings)
    if not warning_rows:
        warning_rows = "<li>No preflight warnings.</li>"
    return f"""
        <h1>Production proof — {html.escape(document.name)}</h1>
        <p>Generated {html.escape(_dt.datetime.now().strftime('%Y-%m-%d %H:%M'))}.</p>
        {image}
        <h2>Part</h2><table>{facts}</table>
        <h2>Marks</h2>
        <table border=\"1\" cellspacing=\"0\" cellpadding=\"5\">
          <tr><th>Name</th><th>Operation</th><th>Depth</th><th>ID</th><th>Process</th></tr>{rows}
        </table>
        <h2>Preflight</h2><ul>{warning_rows}</ul>
    """


def export_proof_sheet(
    document: Document, path: str | Path, *, screenshot: bytes | None = None,
    rebuild: RebuildResult | None = None,
) -> ExportResult:
    """Write a standalone PDF production proof with the current thumbnail and marks."""
    if document.base is None:
        raise ExportError("There is no part to document.")
    fmt = "step" if document.base.mode == "solid" else "stl"
    report = preflight_export(document, rebuild, fmt, path)
    report.require_ok()
    path = Path(path).with_suffix(".pdf")
    _require_qt_application()
    try:
        from PySide6.QtCore import QSizeF
        from PySide6.QtGui import QPageSize, QPdfWriter, QTextDocument

        writer = QPdfWriter(str(path))
        writer.setPageSize(QPageSize(QSizeF(210, 297), QPageSize.Unit.Millimeter))
        proof = QTextDocument()
        proof.setHtml(_proof_html(document, report.warnings, screenshot))
        proof.print_(writer)
    except Exception as exc:
        raise ExportError(f"Stamp could not create the production proof PDF: {exc}") from exc
    return ExportResult(path=path, size_bytes=path.stat().st_size, warnings=report.warnings)


def export_job_package(document, geometry: object, path: str | Path, *, fmt: str | None = None,
                       screenshot: bytes | None = None, rebuild=None) -> ExportResult:
    """Write a portable production handoff ZIP without changing the open project."""
    from stamp.io import project as project_io

    if document.base is None:
        raise ExportError("There is no part to package.")
    fmt = (fmt or ("step" if document.base.mode == "solid" else "stl")).lower().lstrip(".")
    report = preflight_export(document, rebuild, fmt, path)
    report.require_ok()
    path = Path(path)
    if path.suffix.lower() != ".zip":
        path = path.with_suffix(".zip")
    with tempfile.TemporaryDirectory(prefix="stamp-package-") as temp_name:
        temp = Path(temp_name)
        model = temp / default_filename(
            document.name, fmt, suffix=document.transform.suffix()
        )
        if fmt == "step":
            export_step(geometry, model)
        elif fmt == "stl":
            export_stl(geometry, model, mode=document.base.mode)
        else:
            raise ExportError("Job packages currently support STEP or STL models.")
        project_path = project_io.save(document, temp / f"{document.name}.stamp", thumbnail=screenshot)
        manifest = {
            "project": document.name, "generated": _dt.datetime.now(tz=_dt.UTC).isoformat(),
            "format": fmt, "warnings": report.warnings,
            "transform": document.transform.to_dict(),
            "transform_summary": transform_summary(document),
            "features": [{"name": f.name, "metadata": f.metadata.to_dict()} for f in document.features],
        }
        (temp / "preflight.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        summary = temp / "production-summary.pdf"
        lines = [
            f"<h1>{document.name}</h1>",
            f"<p>Model: {model.name}</p>",
            f"<p>Mirror and scale: {html.escape(transform_summary(document))}</p>",
            "<h2>Features</h2><ul>",
        ]
        for feature in document.features:
            meta = feature.metadata
            lines.append(f"<li><b>{feature.name}</b> {meta.identifier} {meta.process}<br>{meta.notes}</li>")
        lines.append("</ul><h2>Warnings</h2><ul>" + "".join(f"<li>{w}</li>" for w in report.warnings) + "</ul>")
        _require_qt_application()
        try:
            from PySide6.QtCore import QSizeF
            from PySide6.QtGui import QPageSize, QPdfWriter, QTextDocument

            writer = QPdfWriter(str(summary))
            writer.setPageSize(QPageSize(QSizeF(210, 297), QPageSize.Unit.Millimeter))
            report_doc = QTextDocument()
            report_doc.setHtml("\n".join(lines))
            report_doc.print_(writer)
        except Exception as exc:
            raise ExportError(f"Stamp could not create the production PDF: {exc}") from exc
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in (model, project_path, temp / "preflight.json", summary):
                archive.write(item, item.name)
            if screenshot:
                image = temp / "thumbnail.png"
                image.write_bytes(screenshot)
                archive.write(image, image.name)
    return ExportResult(path=path, size_bytes=path.stat().st_size, warnings=report.warnings)


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
    "PreflightReport",
    "MESH_MODE_NO_STEP",
    "STEP_SCHEMAS",
    "STL_QUALITY",
    "default_filename",
    "export_for_quote",
    "export_job_package",
    "export_step",
    "export_stl",
    "preflight_export",
    "triangle_count_for",
]


# ------------------------------------------------------------------------- 3MF

#: The 3MF materials and properties extension.  Bambu Studio's standard-3MF
#: colour parser reads colours only from this extension's ``colorgroup``, and it
#: wants the reference on the triangles themselves.  An object-level
#: ``basematerials`` group - the obvious reading of the core spec - is ignored,
#: and the model arrives in one colour.
MATERIAL_NS = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"

#: A Bambu Studio *project* carries ``Metadata/model_settings.config`` next to a
#: ``project_settings.config`` and per-plate entries.  Writing the first without
#: the rest makes Bambu take the project path and stop on the missing pieces,
#: which loses the colours and shows a config error.  Stamp writes a plain
#: standard 3MF instead.  Do not add a vendor config here without the whole set.


def export_3mf(
    bodies: list,
    path: str | Path,
    *,
    base_color: str = "#2B2B2B",
    feature_color: str = "#C8A24A",
    write_colors: bool = True,
) -> ExportResult:
    """Write a multi-body 3MF, optionally with a colour on every triangle.

    The bodies are components of one object, so the part moves in one piece.

    With *write_colors* every triangle points at one entry of a colour group, and
    Bambu Studio and Orca offer to map those groups to filament slots on import,
    in the order written here: the base first, the features second.  A slicer
    makes a new filament for every colour it does not already have, so colours
    that do not match the ones loaded arrive as extra entries the user has to
    undo - which is why the caller asks rather than assuming.

    Without it the file carries no colour at all.  The parts are still separate
    and still named, so a filament can be given to each by hand, and nothing is
    asked on the way in.
    """
    import zipfile
    from xml.sax.saxutils import quoteattr

    path = Path(path)
    if not bodies:
        raise ExportError("There is nothing to export.")
    total = sum(int(len(b.triangles)) for b in bodies)
    if total == 0:
        raise ExportError("There is nothing to export.")

    def color(value: str) -> str:
        text = str(value).strip().lstrip("#").upper()
        if len(text) == 3:
            text = "".join(c * 2 for c in text)
        if len(text) == 6:
            text += "FF"  # the extension wants RGBA
        if len(text) != 8 or any(c not in "0123456789ABCDEF" for c in text):
            raise ExportError(f"{value!r} is not a colour Stamp can write.")
        return "#" + text

    if write_colors:
        header = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<model unit="millimeter" xml:lang="en-US"\n'
            ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"\n'
            f' xmlns:m="{MATERIAL_NS}">\n'
            " <resources>\n"
            '  <m:colorgroup id="1">\n'
            f'   <m:color color="{color(base_color)}"/>\n'
            f'   <m:color color="{color(feature_color)}"/>\n'
            "  </m:colorgroup>\n"
        )
    else:
        # No colour group at all: nothing for a slicer to ask about on the way in.
        header = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<model unit="millimeter" xml:lang="en-US"\n'
            ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
            " <resources>\n"
        )

    parts: list[str] = [header]
    for index, body in enumerate(bodies):
        object_id = index + 2
        pindex = 0 if body.role == "base" else 1
        colored = f' pid="1" pindex="{pindex}"' if write_colors else ""
        parts.append(
            f'  <object id="{object_id}" type="model" name={quoteattr(body.name)}'
            f'{colored}>\n   <mesh>\n    <vertices>\n'
        )
        parts.append(
            "".join(
                f'     <vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>\n'
                for v in body.vertices
            )
        )
        parts.append("    </vertices>\n    <triangles>\n")
        if write_colors:
            parts.append(
                "".join(
                    f'     <triangle v1="{t[0]}" v2="{t[1]}" v3="{t[2]}"'
                    f' pid="1" p1="{pindex}"/>\n'
                    for t in body.triangles
                )
            )
        else:
            parts.append(
                "".join(
                    f'     <triangle v1="{t[0]}" v2="{t[1]}" v3="{t[2]}"/>\n'
                    for t in body.triangles
                )
            )
        parts.append("    </triangles>\n   </mesh>\n  </object>\n")

    # The bodies go on the plate as one object built from components, not as one
    # item each.  A slicer moves and turns whatever a build item names, so
    # separate items leave the artwork behind when the part is oriented.
    assembly_id = len(bodies) + 2
    parts.append(f'  <object id="{assembly_id}" type="model" name="Stamp part">\n')
    parts.append("   <components>\n")
    for index in range(len(bodies)):
        parts.append(
            f'    <component objectid="{index + 2}"'
            f' transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'
        )
    parts.append("   </components>\n  </object>\n")
    parts.append(
        " </resources>\n <build>\n"
        f'  <item objectid="{assembly_id}"'
        ' transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'
        " </build>\n</model>\n"
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        ' <Default Extension="rels"'
        ' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        ' <Default Extension="model"'
        ' ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        "</Types>\n"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships'
        ' xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        ' <Relationship Target="/3D/3dmodel.model" Id="rel-1"'
        ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
        "</Relationships>\n"
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("3D/3dmodel.model", "".join(parts))

    return ExportResult(path=path, size_bytes=path.stat().st_size, triangle_count=total)
