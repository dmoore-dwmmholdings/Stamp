# Stamp progress

This file records the progress against the milestones in section 11 of the
specification. Update this file at the end of each work period.

## Environment

The uv tool is installed with pip. It is not on the PATH. Thus you must start it as
a Python module. The virtual environment is `.venv`, with Python 3.12.10.

To start the application:

```
python -m uv run stamp
python -m uv run stamp tests/fixtures/bracket.step
```

To start the test suite and the lint tool:

```
python -m uv run pytest
python -m uv run ruff check src tests
```

To make the test fixtures again, and to make the package:

```
python -m uv run python tests/make_fixtures.py
python -m uv run pyinstaller packaging/stamp.spec --noconfirm
```

## Milestones

| Number | Milestone | Condition |
|---|---|---|
| M0 | Skeleton and OCC viewport. | Completed |
| M1 | Import and preview. STEP, STL, SVG and DXF. | Completed |
| M2 | One completed feature, from face selection to export. | Completed |
| M3 | Placement with the mouse. Drag handles, snap, live preview. | Completed |
| M4 | Feature tree, modifiers, rebuild, project files. | Completed |
| M5 | Polish. Error messages, packages. | Completed for Windows |

Version 1 is complete. The test suite holds the seven steps of section 14 as one test,
and that test gives the correct result.

The test suite has 229 tests. All of them give the correct result. The lint tool finds
no errors.

## Modules

| Module | Function |
|---|---|
| `units.py` | Unit tables. All internal values are millimeters. |
| `core/document.py` | Document, BasePart, Feature. JSON only, no geometry. |
| `core/refs.py` | FaceRef and the sketch plane. Section 8. |
| `core/rebuild.py` | The rebuild engine and the cache. Section 6.6. |
| `core/profiles.py` | The profile cache. Section 5.5, step 7. |
| `core/snapping.py` | The snap targets. Section 6.2. |
| `io/normalize.py` | The shared back half of each import. Section 5.5. |
| `io/profile_import.py` | SVG, DXF and DWG. Sections 5.3 and 5.4. |
| `io/part_import.py` | STEP, IGES, BREP, STL, 3MF and OBJ. Sections 5.1 and 5.2. |
| `io/export.py` | STEP and STL export. Section 9. |
| `io/project.py` | The `.stamp` archive. Section 4.4. |
| `geom/tool_solid.py` | Profile plus placement plus operation. Section 6.3. |
| `geom/solid_ops.py` | OCC booleans, fillets and chamfers. Section 6.4. |
| `geom/mesh_ops.py` | The manifold3d path. Sections 2 and 6.5. |
| `geom/mesh_regions.py` | Face selection on a mesh part. Section 6.1. |
| `ui/viewport.py` | The OCC viewport widget. |
| `ui/main_window.py` | The window and all the commands. Section 7. |
| `ui/feature_tree.py` | The feature tree on the left. |
| `ui/properties.py` | The properties panel on the right. Section 6.2. |
| `ui/handles.py` | The drag handles and the snap logic. |
| `ui/dialogs.py` | The import and export prompts. |
| `ui/rebuild_worker.py` | The worker thread and the debounce. |
| `diagnostics.py` | The log, the crash dump and the crash flag. |
| `reporting.py` | The crash report and the bug report. |
| `io/text_profile.py` | Artwork from a text message. Section 5.3. |

## Results that are important to keep

The `WNT_Window` binding accepts a capsule, not an integer. `QWidget.winId()` gives an
integer in PySide6. The function `ctypes.pythonapi.PyCapsule_New` makes the capsule from
that integer. The function `ui/viewport.py::_handle_capsule` does this. This one step makes
the full viewport possible.

A compound that goes into `SetTools` as one entry is not fully processed. Only its first
solid goes into the boolean. Thus `solid_ops._split_compound` divides it first. The
same trap is in manifold3d, and `mesh_ops.boolean` divides the tool there with the same procedure.

The tool starts a small distance behind the sketch plane. The function
`tool_solid.contact_overlap_for` gives that distance. A contact that is fully coplanar is the
worst condition for the two boolean engines. It also causes `SectionEdges()` to give an
empty list, and those edges are the blend targets of section 6.4B.

Artwork elements frequently overlap. The function `normalize._resolve_overlaps` unions
such elements in 2D with the non-zero fill rule. Without this step, the extrude makes a
seam between two flat faces, and no fillet can go on that seam.

An anchor on a base face resolves against the initial part. It does not resolve against
the result that is not fully built. The method `_anchor_shape` of the rebuild engine does
this. Section 8 of the specification gives this rule, and a test shows why.

PySide6 keeps a `StrEnum` in a `QComboBox` as a plain string. The string comes back from
`currentData()`, thus the properties panel makes it into the enum again. The method
`to_dict` also accepts a plain string. Without the two steps, a change of the direction
stops the undo stack in the middle of an event.

A toolbar puts a widget in a `QWidgetAction` and shows it again at each layout. Thus you
must set the visibility on the `QWidgetAction`, not on the widget.

The viewport is a native window with `WA_PaintOnScreen`. Thus a Qt widget cannot put an
overlay on it. The drag handles are OCC point markers with a dimension in pixels. Thus
they keep the same dimension on the screen at each zoom.

Undo must have the condition from before the change. The properties panel changes the feature and
then reports it, but the tree reports first and changes after. Thus `_push_undo` puts the
baseline from the end of the last change on the stack. A timer that fires immediately gets the new
baseline after the current event.

Each feature keeps its position in the frame of its own sketch plane. Two features on one
flat surface can have different origins, because the origin is the point that the user
clicked. Thus the snap logic changes each position through world coordinates first.

The `trimesh` ray functions are not usable, because the `rtree` package is not a
dependency of Stamp. The module `geom/mesh_regions.py` thus has its own ray test in
numpy. It is short, and it is fast at these dimensions.

The region that is found on a mesh is fully coplanar with the part. Thus the highlight
goes up 0.05 mm along the plane normal. Without that step, the two surfaces compete for
the same depth, and you cannot see the highlight.

A `WNT_Window` that you make from a window handle starts at 640 x 480. Qt gives the native
window its correct dimensions after one turn of the event loop. Thus a `MustBeResized` at start
finds the initial dimensions, and the render fills only the lower left corner. The method
`viewport._sync_window_size` starts `DoResize` first, and a `QTimer` starts that method again
after the event loop turns. The first `fit_all` also uses the initial dimensions, thus the
same method does the fit again one time.

The enum `Quantity_TOC_RGB` reads linear RGB values, not sRGB values. The color
(0.13, 0.15, 0.19) given as linear RGB becomes GRAY43, which is a middle gray. Use
`Quantity_TOC_sRGB` to give the value that other tools show. You could not see the face
boundary lines until this change, because the color became lighter.

Face boundary lines show the contours of a shaded solid. Set them on the drawer of the object,
not on the default drawer of the context. The default drawer also puts lines on the previews
and on the drag handles.

A worker that stops because of a cancel gives no result. Thus it must report the cancel on a
signal of its own. Without that signal the controller keeps its busy flag, and it starts no
more rebuilds. One quick edit during a rebuild was sufficient to stop all rebuilds after it.
The geometry on the screen then stayed at the position of the last good rebuild, and only the
preview moved. Each request also has a generation number, because a cancel can come while the
request stays in the queue of the thread.

Qt gives the outline of each glyph through `QPainterPath`. Those outlines go into
`normalize_groups` as one group for each line. Containment in that group makes the counter
of an "o" a hole. One group for each contour is not correct, because each group is nested
on its own, and the counter then becomes material.

A `mailto:` link has no field for an attachment. Thus Stamp cannot attach the log to the
email. Stamp puts the full report on the clipboard, and the user pushes Ctrl+V. This is the
one method that operates with a mail application and with a mail page in a browser. The link
also has a limit of about 2 kB. That limit applies to the escaped link, not to the text.

The crash flag holds the process id. If that process continues to operate, the flag belongs
to a second window, not to a run that stopped. Without the process id, two windows at the
same time make an incorrect crash report.

A fillet is one build for all the edges of the selection. One edge that cannot accept the
value fails that build. Thus each edge gets the same value, or no edge gets a value. On
artwork with 1233 edges, no radius between 0.05 mm and 0.4 mm was possible, but 0.005 mm
was. The largest value a set accepts stays near the shortest edge in that set. The ratio
was between 0.57 and 1.05 in the measurements. Thus `_largest_working_value` does a search
when the count is not more than 250 edges. Above that count it makes an estimate from the
shortest edge. A search costs one complete fillet for each step, which was 92 seconds on
the largest set.

A fillet on part of the edges is possible. Groups of 40 edges, applied one after the other
to the shape that results, gave 22 percent of the edges. Groups of 20 gave 40 percent, and
a bisection gave 49 percent in 60 seconds. Stamp does not do this. Some letters with a
circular edge and some letters with a sharp edge look worse than all of them sharp.

## Times on the standard test

The part is `bracket.step` or `bracket.stl` with the `logo.svg` profile.

- Tool solid: 0.5 ms
- Fillet on 8 top edges: 25 ms
- OCC boolean: 15 ms
- Tessellation for the mesh path: 20 ms
- manifold3d boolean: 3 ms
- Rebuild from the cache: 0.1 ms
- Windows package: 311 MB

## Differences from the specification

The package root is `src/stamp/`, which is the default layout of uv. Section 13 shows `app/`.
The names of the modules in the package root agree with section 13.

## Work that is not done

1. The macOS and Linux packages are not made. Only Windows is. The PyInstaller
   specification has the code for the macOS bundle, but no machine here can test it.
2. DWG is best effort, which section 5.4 lets Stamp do. The ODA File Converter is not on this
   machine, thus that path has no test. Stamp finds the converter at start, and it shows
   the download address and a file picker if the converter is missing.
3. The items in the "later" list of section 11 are not started. Cylindrical wrap is the
   first of them.
