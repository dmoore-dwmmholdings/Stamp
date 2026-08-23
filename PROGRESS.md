# Stamp progress

Tracks progress against the milestones in section 11 of the spec. Updated at the
end of each work session.

## Environment

uv was installed with pip and isn't on PATH, so run it as a module. The virtual
environment is `.venv` with Python 3.12.10.

Run the app:

```
python -m uv run stamp
python -m uv run stamp tests/fixtures/bracket.step
```

Tests and lint:

```
python -m uv run pytest
python -m uv run ruff check src tests
```

Regenerate fixtures / build the package:

```
python -m uv run python tests/make_fixtures.py
python -m uv run pyinstaller packaging/stamp.spec --noconfirm
```

## Milestones

| Number | Milestone | Status |
|---|---|---|
| M0 | Skeleton and OCC viewport. | Done |
| M1 | Import and preview: STEP, STL, SVG, DXF. | Done |
| M2 | One complete feature, from face selection to export. | Done |
| M3 | Mouse placement: drag handles, snapping, live preview. | Done |
| M4 | Feature tree, modifiers, rebuild, project files. | Done |
| M5 | Polish: error messages, packaging. | Done for Windows |

Version 1 is complete. The seven-step acceptance flow from section 14 exists as a
single test, and it passes. The suite is 229 tests, all passing, with a clean ruff
run.

## Modules

| Module | What it does |
|---|---|
| `units.py` | Unit tables. Everything internal is millimeters. |
| `core/document.py` | Document, BasePart, Feature. Pure JSON data, no geometry. |
| `core/refs.py` | FaceRef and the sketch plane. Section 8. |
| `core/rebuild.py` | The rebuild engine and its cache. Section 6.6. |
| `core/profiles.py` | The profile cache. Section 5.5, step 7. |
| `core/snapping.py` | Snap targets. Section 6.2. |
| `io/normalize.py` | The shared back half of every import. Section 5.5. |
| `io/profile_import.py` | SVG, DXF, and DWG. Sections 5.3 and 5.4. |
| `io/part_import.py` | STEP, IGES, BREP, STL, 3MF, and OBJ. Sections 5.1 and 5.2. |
| `io/export.py` | STEP and STL export. Section 9. |
| `io/project.py` | The `.stamp` archive. Section 4.4. |
| `io/text_profile.py` | Turns typed text into a profile. Section 5.3. |
| `geom/tool_solid.py` | Profile + placement + operation. Section 6.3. |
| `geom/solid_ops.py` | OCC booleans, fillets, chamfers. Section 6.4. |
| `geom/mesh_ops.py` | The manifold3d path. Sections 2 and 6.5. |
| `geom/mesh_regions.py` | Face selection on mesh parts. Section 6.1. |
| `ui/viewport.py` | The OCC viewport widget. |
| `ui/main_window.py` | The window and all commands. Section 7. |
| `ui/feature_tree.py` | The feature tree on the left. |
| `ui/properties.py` | The properties panel on the right. Section 6.2. |
| `ui/handles.py` | Drag handles and snap logic. |
| `ui/dialogs.py` | Import and export prompts. |
| `ui/rebuild_worker.py` | The worker thread and debounce. |
| `diagnostics.py` | Logging, crash dumps, the crash flag. |
| `reporting.py` | Crash and bug reports. |

## Findings worth keeping

The `WNT_Window` binding takes a capsule, not an integer. `QWidget.winId()` returns
an integer in PySide6; `ctypes.pythonapi.PyCapsule_New` wraps it
(`ui/viewport.py::_handle_capsule`). This one step is what makes the whole viewport
possible.

A compound passed to `SetTools` as a single entry is not fully processed — only its
first solid enters the boolean. `solid_ops._split_compound` splits it first. The
same trap exists in manifold3d, so `mesh_ops.boolean` splits the tool the same way.

The tool starts slightly sunk behind the sketch plane
(`tool_solid.contact_overlap_for` gives the distance). Perfectly coplanar contact is
the worst case for both boolean engines, and it also makes `SectionEdges()` return
an empty list — and those edges are the blend targets in section 6.4B.

Artwork elements overlap all the time. `normalize._resolve_overlaps` unions them in
2D with the non-zero fill rule. Skip this and the extrude produces a seam between
two flat faces that no fillet can attach to.

An anchor on a base face resolves against the original part, not against the
half-built result (`_anchor_shape` in the rebuild engine). Section 8 of the spec
states the rule, and a test shows why it matters.

PySide6 stores a `StrEnum` in a `QComboBox` as a plain string. `currentData()`
returns the string, so the properties panel converts it back to the enum, and
`to_dict` accepts plain strings too. Without both, changing the direction broke the
undo stack mid-event.

A toolbar wraps widgets in `QWidgetAction` and re-shows them on every relayout — set
visibility on the `QWidgetAction`, not the widget.

The viewport is a native window with `WA_PaintOnScreen`, so Qt widgets can't overlay
it. The drag handles are OCC point markers sized in pixels, which keeps them the
same size on screen at every zoom.

Undo needs the state from before the change. The properties panel mutates the
feature and then signals, but the tree signals first and mutates after — so
`_push_undo` pushes the baseline captured at the end of the previous change, and a
zero-delay timer captures the new baseline after the current event.

Each feature stores its position in its own sketch plane's frame. Two features on
the same flat surface can have different origins (the origin is wherever the user
clicked), so the snap logic converts through world coordinates first.

`trimesh`'s ray functions are unusable because `rtree` isn't a dependency, so
`geom/mesh_regions.py` carries its own numpy ray test. It's short, and fast at
these sizes.

A region found on a mesh is exactly coplanar with the part, so the highlight is
lifted 0.05 mm along the plane normal. Without that, the two surfaces z-fight and
the highlight is invisible.

A `WNT_Window` created from a window handle starts at 640 x 480, and Qt only gives
the native window its real size after one turn of the event loop — so a
`MustBeResized` at startup sees the wrong size and the render fills only the lower
left corner. `viewport._sync_window_size` calls `DoResize` immediately and again via
`QTimer` after the loop turns; the first `fit_all` has the same problem, so it too
runs a second time.

`Quantity_TOC_RGB` interprets values as linear RGB. The color (0.13, 0.15, 0.19)
fed in as linear comes out as GRAY43 — a medium gray. Use `Quantity_TOC_sRGB` to
get what other tools display. The face boundary lines were invisible until this was
fixed, because the background got lighter than intended.

Face boundary lines show the contours of a shaded solid. Set them on the object's
own drawer, not the context's default drawer — the default drawer also draws lines
on previews and drag handles.

A worker that stops due to a cancel produces no result, so it must signal the
cancel separately. Without that signal the controller's busy flag stuck and no
rebuild ever ran again — one quick edit during a rebuild was enough. On screen, the
geometry froze at the last good rebuild while only the preview moved. Each request
also carries a generation number, because a cancel can arrive while the request is
still queued in the thread.

Qt hands out glyph outlines through `QPainterPath`. Those outlines go into
`normalize_groups` as one group per line of text; containment within the group is
what turns the counter of an "o" into a hole. One group per contour is wrong — each
group nests independently, and the counter becomes solid material.

A `mailto:` link has no attachment field, so Stamp can't attach the log. Instead it
puts the full report on the clipboard and the user presses Ctrl+V — the one approach
that works with both mail apps and webmail. The link is also limited to roughly
2 kB, and the limit applies to the escaped URL, not the visible text.

The crash flag stores the process id. If that process is still alive, the flag
belongs to a second open window, not a crashed run. Without the pid, two windows
open at once produced a bogus crash report.

A fillet is one build across all selected edges: every edge takes the value or none
do. On artwork with 1233 edges, no radius between 0.05 mm and 0.4 mm worked, but
0.005 mm did. The largest value a set accepts tracks the shortest edge in the set
(ratio 0.57 to 1.05 in measurements), so `_largest_working_value` searches when the
selection is 250 edges or fewer and estimates from the shortest edge above that. A
search costs one full fillet per step — 92 seconds on the largest set.

Partial fillets are possible: groups of 40 edges applied sequentially to the
running result filleted 22% of the edges; groups of 20 got 40%; bisection got 49%
in 60 seconds. Stamp doesn't do it — a mix of rounded and sharp letters looks worse
than all sharp.

## Timings on the standard test

`bracket.step` / `bracket.stl` with `logo.svg`:

- Tool solid: 0.5 ms
- Fillet on 8 top edges: 25 ms
- OCC boolean: 15 ms
- Tessellation for the mesh path: 20 ms
- manifold3d boolean: 3 ms
- Rebuild from cache: 0.1 ms
- Windows package: 311 MB

## Deviations from the spec

The package root is `src/stamp/` (uv's default layout); section 13 says `app/`.
Module names inside the root match section 13.

## Not done

1. macOS and Linux packages. The PyInstaller spec has the macOS bundle code, but
   there's no machine here to test it.
2. DWG is best-effort, as section 5.4 allows. The ODA File Converter isn't on this
   machine, so that path is untested. Stamp detects the converter at startup and
   shows a download link and a file picker when it's missing.
3. Nothing from section 11's "later" list. Cylindrical wrap is first in line.
