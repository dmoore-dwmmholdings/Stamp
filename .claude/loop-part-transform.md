# Loop plan: part mirror + precise part scale

Branch: feature/part-mirror-and-scale. Target version: 1.2.0.

## What ships

1. **Part mirror.** Mirror the whole part (base + all features) across a principal
   plane (YZ / XZ / XY) of the part bounding box, to make a handed pair (left/right
   bracket). Works in solid mode and mesh mode. The mirrored copy is exportable in
   every format the original supports: STEP, STL, 3MF, job package, proof sheet,
   batch.
2. **Precise part scale.** A numeric scale on the part: uniform percentage or
   factor, per-axis factors, and "scale to a target dimension" (type the finished
   X/Y/Z size and Stamp solves the factor). Non-uniform scale is refused where the
   geometry cannot survive it, with a message that says why.

## Design decisions (hold these unless a real problem forces a change)

- One new dataclass, `PartTransform`, on `Document` (next to BasePart), serialized
  in the project JSON. Bump `SCHEMA_VERSION`. Identity transform must round-trip
  through old-format projects unchanged (backward compatibility is a release bar).
- The transform is applied **after** the feature rebuild, as the last stage of the
  result, so feature anchors, snapping, handles and the sketch planes all keep
  working in unscaled, unmirrored part space. Do not re-anchor features.
- Solid: `gp_Trsf.SetMirror` for mirror, `gp_Trsf.SetScale` for uniform,
  `gp_GTrsf` + `BRepBuilderAPI_GTransform` for non-uniform. A mirror reverses
  orientation - confirm the result is a valid, correctly oriented solid
  (`solid_ops.check_valid`, positive volume) and fix it if not.
- Mesh: negate the axis and flip triangle winding for mirror; multiply vertices for
  scale. A mirror with unflipped winding gives inside-out normals - test for it.
- Modifier values (fillet/chamfer radii) are in part units. Scaling the part scales
  them geometrically because the transform runs after the modifiers - state this in
  README and do not silently rescale stored numbers.
- Export of the mirrored copy: an explicit "Export mirrored copy" path that writes
  both hands where the user asks for it, with a filename suffix. Filenames must not
  collide with the original.

## Definition of done

- Tests for: solid mirror handedness (volume preserved, chirality actually flipped),
  mesh mirror winding, uniform + per-axis scale, scale-to-dimension solve,
  project round-trip at the new schema version, old project opens unchanged,
  export of both hands in STEP/STL/3MF, preflight behaviour on a scaled part.
- `python -m uv run pytest` green; `python -m uv run ruff check src tests` clean.
- UI reachable: properties/part panel controls plus menu commands, and the UI test
  file covers them.
- README documents both features and their limits; PROGRESS.md gets a v1.2.0
  section written in the existing voice (what works, why, findings worth keeping).
- Version 1.2.0 in pyproject.toml, src/stamp/__init__.py, packaging/stamp.iss.

## Status log (newest last, append each iteration)

- [init] Branch created. Nothing implemented yet. Next: read core/document.py,
  core/rebuild.py, io/export.py, io/project.py, ui/main_window.py, ui/properties.py
  and write the PartTransform dataclass + rebuild application stage first.
- [iter 1, 2026-09-01] Design changed from the sketch above: the transform is applied
  on the way *out*, not as a rebuild stage. `part_transform.for_export(document,
  geometry)` is the single chokepoint every exporter goes through, so the rebuild
  cache, anchors, snapping and handles never see it. Landed:
  `core/document.MirrorPlane` + `PartTransform` (validate, factor_for, size_from,
  suffix), `Document.transform`, SCHEMA_VERSION 5, `Document.restore` now restores
  inspection/datums/transform (it dropped all three before), `geom/part_transform.py`
  for solid + mesh + 3MF colour bodies, `default_filename(suffix=)`, and wiring in
  main_window (STEP/STL/3MF/quote/job package) and batch. 31 new tests in
  tests/test_part_transform.py; suite 315 passed, ruff clean.
  Finding worth keeping: a non-uniform `gp_GTrsf` carries the source triangulation
  across unchanged, so BRepCheck calls the stretched result invalid and the exporter
  refuses it. `BRepTools.Clean_s` on the result fixes it.
  Next: the UI. A "Part" group in the properties panel (mirror plane combo, uniform
  scale % / per-axis factors, three "finished size" fields that solve the factor,
  reset), a Part menu with mirror commands, and a viewport toggle that previews the
  transformed result read-only. Then preflight warnings for a scaled part, README
  and PROGRESS.
