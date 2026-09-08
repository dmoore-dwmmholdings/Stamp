"""Non-interactive CSV batch stamping used by ``stamp batch`` and the UI."""

from __future__ import annotations

import csv
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from stamp.core.document import Document
from stamp.core.profiles import ProfileCache
from stamp.core.rebuild import RebuildEngine
from stamp.core.replace_part import replace_part
from stamp.geom import color_split, part_transform
from stamp.io import export as export_io
from stamp.io.part_import import import_part
from stamp.io.project import open_project

#: ``{{anything}}``.  A CSV column is whatever the person typed in the header
#: row - "serial-no", "Part Number" - and a pattern that only matched
#: identifiers left those placeholders in the part, unsubstituted and unreported.
_TOKEN = re.compile(r"\{\{([^{}]+)\}\}")

#: Suffixes that name a model format.  One of these that is not the format being
#: written is a mistake to correct; anything else is part of the name.
_MODEL_SUFFIXES = {".step", ".stp", ".stl", ".3mf"}


class BatchError(RuntimeError):
    pass


@dataclass
class BatchRow:
    index: int
    input: str
    output: str
    status: str
    detail: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class BatchReport:
    rows: list[BatchRow] = field(default_factory=list)
    stopped: bool = False

    def to_dict(self) -> dict:
        return {"stopped": self.stopped, "rows": [r.__dict__ for r in self.rows]}


def placeholders(document: Document) -> set[str]:
    """Every ``{{name}}`` the template asks the CSV to fill in."""
    found: set[str] = set()
    for feature in document.features:
        if feature.profile.text is not None:
            found.update(_TOKEN.findall(feature.profile.text.text))
        if feature.profile.code is not None:
            found.update(_TOKEN.findall(feature.profile.code.payload))
    return {name.strip() for name in found}


def _substitute(document: Document, values: dict[str, str]) -> None:
    # Headers and placeholders are typed by hand, so "{{ serial }}" and a
    # column called " serial" are the same column.
    values = {str(key).strip(): value for key, value in values.items()}

    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if key not in values:
            raise BatchError(f"CSV is missing a value for {{{{{key}}}}}.")
        return values[key]

    for feature in document.features:
        if feature.profile.text is not None:
            feature.profile.text.text = _TOKEN.sub(replace, feature.profile.text.text)
        if feature.profile.code is not None:
            feature.profile.code.payload = _TOKEN.sub(replace, feature.profile.code.payload)


def _output_name(value: str | None, fmt: str, output_dir: Path | None = None) -> Path:
    """The name a row writes to, decided once for the dry run and the real one.

    Both go through this, so a row the simulation calls ready is a row the run
    accepts.  *output_dir*, when it is known, adds the check that survives
    symlinks: the lexical one below cannot see where a link points.
    """
    output = (value or "").strip()
    if not output:
        raise BatchError("output is empty")
    path = Path(output)
    suffix = path.suffix.casefold()
    if suffix != "." + fmt:
        # A row that says part.txt gets STEP written into it either way, so the
        # name is made to say so.  A wrong model suffix is replaced; anything
        # else is part of the name - "PN-1234.5" must not lose its ".5".
        if suffix in _MODEL_SUFFIXES:
            path = path.with_suffix("." + fmt)
        else:
            path = Path(str(path) + "." + fmt)
    if path.is_absolute() or ".." in path.parts:
        raise BatchError("the output path must stay inside the chosen output folder")
    if path.name.casefold() == "stamp-batch-report.json" and len(path.parts) == 1:
        raise BatchError("output name is reserved for the batch report")
    if output_dir is not None:
        # A CSV is data, not permission to write outside the selected folder.
        root = Path(output_dir).resolve()
        if not (root / path).resolve().is_relative_to(root):
            raise BatchError("the output path must stay inside the chosen output folder")
    return path


def simulate_batch(
    template: str | Path,
    csv_path: str | Path,
    fmt: str,
    output_dir: str | Path | None = None,
) -> BatchReport:
    """Validate CSV substitutions and output collisions without importing or writing parts.

    Given the folder the real run would write to, it checks containment the same
    way that run does, thus a row called ready here is a row that gets written
    there.
    """
    fmt = fmt.lower().lstrip(".")
    if fmt not in {"step", "stl", "3mf"}:
        raise BatchError("Batch format must be step, stl, or 3mf.")
    # Project sources must be materialized to inspect a portable .stamp archive,
    # but never beside the user's template during a dry run.
    with tempfile.TemporaryDirectory(prefix="stamp-batch-sim-") as work:
        try:
            opened = open_project(template, work_dir=work)
        except Exception as exc:
            raise BatchError(f"Could not open template: {exc}") from exc
        if opened.missing:
            raise BatchError("The template has missing sources: " + ", ".join(opened.missing))
        if opened.document.base is None:
            raise BatchError("The template has no base part.")
        report = BatchReport()
        seen: set[str] = set()
        root = Path(output_dir) if output_dir is not None else None
        with Path(csv_path).open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not {"input", "output"}.issubset(reader.fieldnames):
                raise BatchError("CSV must have input and output columns.")
            columns = {(name or "").strip() for name in reader.fieldnames}
            # Said once, before the rows: a placeholder no column fills would
            # otherwise fail every row with the same message, and a CSV with no
            # rows at all would not mention it.
            unknown = sorted(placeholders(opened.document) - columns)
            if unknown:
                raise BatchError(
                    "The CSV has no column for "
                    + ", ".join("{{" + name + "}}" for name in unknown)
                    + "."
                )
            for index, values in enumerate(reader, start=2):
                try:
                    document = Document.from_dict(opened.document.to_dict())
                    _substitute(document, {key: value or "" for key, value in values.items()})
                    source = (values["input"] or "").strip()
                    if not source:
                        raise BatchError("input is empty")
                    output = _output_name(values["output"], fmt, root)
                    output_text = str(output)
                    if output_text.casefold() in seen:
                        raise BatchError(f"duplicate output {output_text!r}")
                    seen.add(output_text.casefold())
                    report.rows.append(BatchRow(index, source, output_text, "ready"))
                except Exception as exc:
                    report.rows.append(BatchRow(index, values.get("input", ""), values.get("output", ""), "failed", str(exc)))
        return report


def run_batch(template: str | Path, csv_path: str | Path, output_dir: str | Path, fmt: str) -> BatchReport:
    """Run rows in order and stop at the first failed rebuild/preflight/export."""
    template, csv_path, output_dir = Path(template), Path(csv_path), Path(output_dir)
    fmt = fmt.lower().lstrip(".")
    if fmt not in {"step", "stl", "3mf"}:
        raise BatchError("Batch format must be step, stl, or 3mf.")
    try:
        opened = open_project(template)
    except Exception as exc:
        raise BatchError(f"Could not open template: {exc}") from exc
    if opened.missing:
        raise BatchError("The template has missing sources: " + ", ".join(opened.missing))
    if opened.document.base is None:
        raise BatchError("The template has no base part.")
    # Replacement needs the original runtime shape to resolve the stored anchors.
    try:
        opened.document.base = import_part(opened.document.base.source_path).part
    except Exception as exc:
        raise BatchError(f"Could not import the template part: {exc}") from exc
    output_dir.mkdir(parents=True, exist_ok=True)
    report = BatchReport()
    seen_outputs: set[str] = set()
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"input", "output"}.issubset(reader.fieldnames):
            raise BatchError("CSV must have input and output columns.")
        for index, values in enumerate(reader, start=2):
            try:
                document = Document.from_dict(opened.document.to_dict())
                _substitute(document, {k: v or "" for k, v in values.items()})
                source_text = (values["input"] or "").strip()
                if not source_text:
                    raise BatchError("input is empty")
                output_name = _output_name(values["output"], fmt, output_dir)
                output_key = str(output_name).casefold()
                if output_key in seen_outputs:
                    raise BatchError(f"duplicate output {str(output_name)!r}")
                seen_outputs.add(output_key)
                source = Path(source_text)
                if not source.is_absolute():
                    source = csv_path.parent / source
                part = import_part(source).part
                replaced = replace_part(document, part)
                if not replaced.ok:
                    raise BatchError(replaced.summary())
                profiles = ProfileCache()
                engine = RebuildEngine(profiles.get)
                rebuilt = engine.rebuild(document)
                # Containment is checked in _output_name, against this folder.
                output = (output_dir / output_name).resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                preflight = export_io.preflight_export(document, rebuilt, fmt, output)
                preflight.require_ok()
                geometry = part_transform.for_export(document, rebuilt.geometry)
                if fmt == "step":
                    result = export_io.export_step(geometry, output)
                elif fmt == "stl":
                    result = export_io.export_stl(geometry, output, mode=document.base.mode)
                else:
                    # The same cache the rebuild used: the per-colour split needs
                    # to see the artwork again, and re-reading it per row is the
                    # one cost a batch of a hundred parts cannot afford.
                    split = color_split.split_for_color(
                        document, rebuilt, profiles=profiles
                    )
                    bodies = part_transform.transform_bodies(document, split.bodies)
                    result = export_io.export_3mf(bodies, output)
                    preflight.warnings.extend(split.warnings)
                report.rows.append(BatchRow(index, str(source), str(result.path), "ok", warnings=preflight.warnings))
            except Exception as exc:
                report.rows.append(BatchRow(index, values.get("input", ""), values.get("output", ""), "failed", str(exc)))
                report.stopped = True
                break
    report_path = output_dir / "stamp-batch-report.json"
    report_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return report
