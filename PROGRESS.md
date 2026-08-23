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
single test, and it passes. The suite is 263 tests, all passing, with a clean ruff
run. Headless (`QT_QPA_PLATFORM=offscreen`) 210 run and 53 skip — the skipped ones
need a real window and an OpenGL context.

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
| `geom/color_split.py` | One body per feature, for multi-color printing. |
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

## v0.2.0

- The window title now carries the version. `stamp.__version__` is the single
  source; a test keeps pyproject.toml and packaging/stamp.iss in agreement.
- A fillet or chamfer value that fails is corrected automatically: the rebuild's
  suggested value goes straight into the modifier and the part rebuilds with it.
  The "Use N" button in the properties panel is gone. Attempts are capped at
  three per modifier in case an estimated suggestion itself fails.
- Modifiers on "edges at the face" (EdgeRole.BOTTOM) were applied to the tool
  solid, which points them the wrong way: a chamfer at the base of a boss cut an
  undercut notch into the boss, and a chamfer on a pocket rim left an
  overhanging lip. They now run through the same post-boolean path as BLEND, so
  a base modifier flares outward and a rim modifier widens the mouth. On mesh
  parts they are unavailable, like BLEND.
- "Export 3MF" writes one body per feature plus the base for multi-color
  printing (`geom/color_split.py` + `export_3mf`). Raised features become
  `result ∩ tool`, engraved features become the inlay `(base ∩ tool) − result`,
  so the bodies mate exactly. The archive is a standard multi-object 3MF with
  basematerials display colors plus Bambu/Orca `Metadata/model_settings.config`
  extruder slots (base = 1, features = 2). Through cuts stay open on purpose.
- `solid_ops.boolean` gained the "common" kind and `mesh_ops.boolean` gained
  "intersect" for the color split.

The union leaves sliver edges about one contact overlap long stitched into the
joint ring, and one of them refuses any radius worth seeing — which fails the
whole build, because a fillet is one build for every edge. `find_blend_edges`
takes a `min_length` and drops them. Before the filter, a ring modifier on
bracket.step took minutes and failed; after it, 19-29 ms on plate.step and it
succeeds. Fine artwork on a busy face can still be genuinely too fine, and the
message says so.

## v0.2.1 - two bugs shipped in v0.2.0

The automatic fillet and chamfer value did nothing whenever the value asked for
was more than about sixty-four times too large. `_bisect_working_value` halved
between zero and the request in six steps, so its smallest probe was a
sixty-fourth of it. Two millimetres of fillet on 2.5 mm text - an ordinary
thing to type - never reached the 0.03 mm that works, returned nothing, and the
correction had nothing to apply. The search now descends from half the shortest
edge until a build succeeds, then closes in on the largest, so it finds a value
at any ratio. The v0.2.0 test passed because it used a value only ten times too
large, which bisection survives.

The 3MF exported colours that Bambu Studio did not read, and a config error.
Two causes:

1. `Metadata/model_settings.config` alone makes Bambu treat the file as one of
   its own projects. A project also carries `project_settings.config` and
   per-plate entries, and without them the load stops with a config error and
   falls back to geometry. Stamp no longer writes any vendor config.
2. Colours were written as core-spec `basematerials` with an object-level
   `pid`/`pindex`. Bambu's standard-3MF colour parser reads only the materials
   extension `<m:colorgroup>`, and it wants the reference on the triangles.
   Every triangle now carries `pid` and `p1`, and the object keeps its
   object-level reference for other readers.

lib3mf 2.5, the 3MF Consortium's own implementation, parses the result in
strict mode with no warnings and resolves every triangle to the right colour.
That is a spec check, not a Bambu check - nothing here has been opened in Bambu
Studio.

## v0.2.2 - why the automatic value still did nothing

Three separate causes, all found by driving the real window instead of the
model:

1. The value stored had more precision than the panel shows. The panel shows
   three decimals, so a stored 0.09375 appeared as 0.094 - and the next edit
   committed 0.094 back into the model, which is larger than the value that
   works, so it failed again. Three of those and the attempt cap stopped
   correcting for good, which is the "I have to type it in myself every time"
   the user reported. The value is now rounded *down* to three decimals before
   it is stored, so what is on screen is what is stored.

2. `_auto_apply_working_values` ran at the end of `_on_rebuild_finished`, after
   the slow-rebuild offer. That offer is a modal dialog, and a modal dialog runs
   a nested event loop: a later rebuild can finish inside it and re-enter the
   handler, and the outer call then acts on a result that has been replaced. A
   failing fillet search on fine artwork passes the ten-second mark easily, so
   this was reachable in the application and not in any test, because the tests
   run with `interactive` off. The correction now runs before anything that can
   open a dialog, and returns early if its result is no longer the current one.

3. The descent gave up before reaching the smallest useful value on large edge
   sets, so no value came back at all. The budget is now derived from the seed:
   enough halvings to walk it down to `MIN_USEFUL_VALUE`, capped by the build
   budget for the edge count.

The 3MF bodies were one build item each. A slicer moves what a build item
names, so orienting the part left the artwork behind on the plate. The bodies
are now components of a single object, and there is one item.

"The 3mf file has invalid config, load geometry data only" is not a fault in
the file. Bambu Studio says it for every 3MF that is not one of its own
projects - Fusion, FreeCAD and OnShape exports all produce it. Colours load
normally alongside it. Removing it would mean writing a full Bambu project,
which is the vendor path that broke v0.2.0.

## Why Bambu always says the config is invalid

`bbs_3mf.cpp` in BambuStudio sets `m_is_bbl_3mf` in exactly one place
(`_handle_end_metadata`): when `<metadata name="Application">` starts with
`"BambuStudio-"`. Nothing else sets it. Every 3MF from any other tool is
therefore "a 3mf from other vendor" and gets the notice - Fusion, FreeCAD and
OnShape exports all produce it.

The notice cannot be removed without claiming BambuStudio wrote the file, and
that claim would cost the colours. The per-triangle colour data is collected
under `if (!m_is_bbl_3mf && sub_object->geometry.triangle_colors.size() ==
triangles_count)` and handed out through `get_volume_color_data()`, which is
what the colour parsing window uses. Setting the flag skips that branch, so a
file that avoids the message arrives with no colours at all. The two are
mutually exclusive, and the colours are worth more than a notice.

The export dialog and the README now say so, so the message does not read as a
fault.

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
