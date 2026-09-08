# Stamp progress

Tracks progress against the milestones in section 11 of the spec. Updated at the
end of each work session.

## Environment

The virtual environment is `.venv` with Python 3.12. Where `uv` was installed
with pip and is not on PATH, `python -m uv` does for `uv` throughout.

Run the app:

```
uv run stamp
uv run stamp tests/fixtures/bracket.step
```

Tests and lint:

```
uv run pytest
uv run ruff check src tests
```

Regenerate fixtures / build the package:

```
uv run python tests/make_fixtures.py
uv run pyinstaller packaging/stamp.spec --noconfirm
```

## Milestones

| Number | Milestone | Status |
|---|---|---|
| M0 | Skeleton and OCC viewport. | Done |
| M1 | Import and preview: STEP, STL, SVG, DXF. | Done |
| M2 | One complete feature, from face selection to export. | Done |
| M3 | Mouse placement: drag handles, snapping, live preview. | Done |
| M4 | Feature tree, modifiers, rebuild, project files. | Done |
| M5 | Polish: error messages, packaging. | Done |

Version 1 is complete. The seven-step acceptance flow from section 14 exists as a
single test, and it passes. The suite is about 800 tests (plus one marked
`opens_email`, deselected by default), all passing, with a clean ruff run. Under
`QT_QPA_PLATFORM=offscreen` the whole suite runs and 68 of them skip — 66 in
`test_ui.py` and the two acceptance tests, the ones that need a real window with
an OpenGL context. That is what CI does; `docker/` runs those 68 as well.

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
| `io/profile_import.py` | SVG, DXF, and DWG (DWG through the external ODA File Converter). Sections 5.3 and 5.4. |
| `io/part_import.py` | STEP, IGES, BREP, STL, 3MF, and OBJ. Sections 5.1 and 5.2. |
| `io/export.py` | STEP and STL export. Section 9. |
| `io/project.py` | The `.stamp` archive. Section 4.4. |
| `io/text_profile.py` | Turns typed text into a profile. Section 5.3. |
| `geom/tool_solid.py` | Profile + placement + operation. Section 6.3. |
| `geom/solid_ops.py` | OCC booleans, fillets, chamfers. Section 6.4. |
| `geom/mesh_ops.py` | The manifold3d path. Sections 2 and 6.5. |
| `geom/mesh_regions.py` | Face selection on mesh parts. Section 6.1. |
| `geom/color_split.py` | One body per feature and per colour, for multi-color printing. |
| `geom/part_transform.py` | Whole-part mirror and scale, applied on the way out. |
| `ui/viewport.py` | The OCC viewport widget. |
| `ui/main_window.py` | The window and all commands. Section 7. |
| `ui/feature_tree.py` | The feature tree on the left. |
| `ui/properties.py` | The properties panel on the right. Section 6.2. |
| `ui/handles.py` | Drag handles and snap logic. |
| `ui/dialogs.py` | Import and export prompts. |
| `ui/rebuild_worker.py` | The worker thread and debounce. |
| `ui/ribbon.py` | The command ribbon and its two sizes. Section 7. |
| `ui/icons.py` | The drawn icon set the ribbon uses. |
| `ui/update_bar.py` | The strip that says a newer Stamp exists. |
| `ui/update_worker.py` | The update check and download, off the GUI thread. |
| `update.py` | The signed release manifest, and verifying what it names. |
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

## v0.3.0

`core/replace_part.py` swaps the part for a newer file and re-anchors the
artwork. There is no new matching machinery in it: §8.2 already stores an anchor
as geometry plus intent rather than a face index, so `resolve_face_ref` can be
pointed at a shape it has never seen. The module adds the reporting and the
recovery around it.

Each feature comes back kept, moved or lost. Lost means the anchor is left
exactly as it was so the rebuild flags it and the user picks a face again -
deleting the feature would not be recoverable. A revision exported from a
different origin is handled by trying a second pass with the two bounding-box
centres aligned, and keeping whichever pass places more features and moves them
less.

Mesh parts have no faces to score, so a mesh anchor is re-fitted with
`mesh_regions.region_at`, firing the ray from outside the stored plane back
along its normal so it meets the near surface rather than the far one.

The 3MF export no longer forces its colours on anyone. Bambu makes a new
filament for every colour string it does not have (`bbs_3mf.cpp`, the loop over
`m_group_id_to_color`), so the old defaults arrived as unwanted white and red
entries next to the filaments actually loaded. The colours are remembered
between exports, and they can be left out altogether - the parts stay separate
and named, and no colour parsing window appears at all.

A color stamp is the third operation kind (`OperationKind.COLOR`). It is a cut
everywhere except the 3MF export, which fills its recess back in, so the code
that asks "does this take material away?" reads `operation.removes_material`
rather than comparing against `CUT` - that comparison was in three places and
each one would have treated a stamp as an add.

The geometry needed nothing new. `color_split` already turned an engraving into
an inlay that fills its pocket flush, so a stamp is that path with a layer-thin
blind depth and a name that says what it is for. What the feature actually
bought was the constraint and the warnings: blind depth only (every other mode
leaves no floor to fill up to), a thickness reset when a deep engraving is
switched over, a preflight line when the thickness is under one printed layer,
and another when STEP or STL is asked for a mark that only 3MF can fill. A
stamp that yields no body is reported in different words from a plain feature
that yields none, because the part ships with an open recess rather than simply
a missing color.

`SCHEMA_VERSION` went to 4. A project holding a color stamp opened by an older
build would rebuild it as a plain engraving and export a part with a hole in the
face, so the version refuses instead.

## v1.2.0

A part now carries a `PartTransform`: a mirror across one of its own principal
planes, and a scale that is either uniform or one factor per axis. The point of
it is the handed pair - export the part, turn the mirror on, export again, and
the shop has a left and a right from one project.

The transform runs on the finished geometry, on its way out, not as a stage of
the rebuild. That one decision is what makes the feature cheap. Sketch planes,
anchors, snap targets and the drag handles all stay in the part's own
untransformed space, so nothing in the feature chain needed to learn about it and
editing a mirrored part is editing an ordinary part. `part_transform.for_export`
is the single chokepoint every exporter goes through - STEP, STL, 3MF, quote
files, job package, `stamp batch`.

Both the mirror plane and the scale centre on the *base part's* bounding box, not
the rebuilt result's. Artwork would otherwise move the centre, and a mirrored copy
would stop lining up with the original as features were added.

Scaling to a size is the number people actually have: type the finished X, Y or Z
and Stamp solves the factor. `PartTransform.factor_for` is that solve, and both
the panel and the modal dialog use it, in the units on screen.

The suggested filename carries what was done - `-mirrored`, `-125pct` - so a
second export never lands on top of the first. Every handoff record repeats it in
words (`export.transform_summary`), because a shop holding one file cannot tell
which hand or which size it received from the geometry.

### Findings worth keeping

A non-uniform `gp_GTrsf` carries the source triangulation across unchanged, and
that triangulation no longer sits on the stretched surfaces - so `BRepCheck` calls
the result invalid and the STEP exporter refuses to write it. The failure only
appears once something has meshed the part, which in the app is always and in a
fresh script is never. `BRepTools.Clean_s` on the result is the whole fix.

`SetMirror` on a solid came back with a positive volume on every part tested, so
OpenCascade is handling the face orientations itself. The signed-volume check and
the `Reversed()` fallback stay in, because a reflected solid that encloses
negative volume is read as a hole in space by a CAM system, and that is not a
thing to find out at the shop.

manifold3d's `transform` flips triangle winding itself when the determinant of the
matrix is negative. Negating vertex coordinates by hand does not, and gives a mesh
that is inside out with a watertight test that still passes. The 3MF colour bodies
are already tessellated, so those *do* swap two indices of every triangle by hand.

`QPdfWriter` aborts the process when no `QGuiApplication` exists. Not an
exception - the interpreter dies, and a pytest run dies with it. Both PDF paths
check first and raise `ExportError`.

`Document.restore` was putting back the name, units, view state, features and base
part, and silently dropping the inspection settings and the datums. Undo has been
losing those since they were added; the transform would have been the third.

## Opening and drawing a converted mesh

A 3MF run through a mesh-to-STEP converter arrives as one flat face per
triangle. The test file here is 81,920 of them, in a 274 MB STEP. Opening it
froze the window for about a minute and then drew it as a black smudge.

### The importer runs in another process

OpenCascade holds the GIL for the whole of every call it is given. Measured:
`TransferRoots` on that file runs for 35 seconds and the main thread gets one
scheduling slot in all of it. So no worker *thread* can keep the window alive
during an import, and none ever could - the rebuild engine's thread has the same
limit. Only a worker *process* can.

`stamp.io.import_process` is both halves. The child reads the file, meshes the
result for display, and writes it out as binary BREP beside a small JSON
description; the parent reads the BREP back. A `TopoDS_Shape` has no pickle and
a mesh that size has no business in a pipe, so everything crossing between them
is a file. The child is started as `Stamp import-worker <request>` - an argv
word rather than a module path, because a PyInstaller build has no interpreter
to call and `sys.executable` is Stamp.exe itself.

The parent's cost is the BREP read, 1.4 s, and the triangulation comes across
with it so nothing meshes again. Everything else - 10 s of parsing, 35 s of
building, 10 s of validity checking, 7 s of meshing, and a 6 GB memory peak -
happens somewhere that cannot freeze anything, under a progress dialog naming
the phase it is in. Cancel kills the process outright, which is the only way to
stop OpenCascade mid-call.

Files under 12 MB are still read in process. Starting a second interpreter and
loading OpenCascade into it costs a couple of seconds, which is worth paying
against a minute and absurd against a tenth of one.

### The viewport stopped repeating itself

Nothing it did was wrong at ten faces and ruinous at eighty thousand except how
often it ran and how much of it there was.

Redisplaying an unchanged shape is now free. Switching selection mode,
reselecting a feature and toggling the preview all handed the viewport the shape
already on screen, and rebuilding that presentation cost three seconds.

Selection is computed on the first click rather than on display. It costs about
as much as drawing does and most of what gets displayed is never picked on. On a
converted mesh hover highlighting is skipped entirely - picking out one triangle
tells the user nothing, and it is not worth a second of frozen mouse to say it.
Face boundary edges are skipped for the same reason: they outline every triangle,
which costs a second to build and draws the part as a black smudge.

### Measured on the 81,920-face file

| | Before | After |
|---|---|---|
| Open the part, window frozen for | 66 s | 3 s |
| Open the part, wall clock | 66 s | 76 s |
| Rebuild redisplay | 3.3 s | 10 ms |
| Change selection mode | 2.2 s | 9 ms |
| Mouse move over the part | 5.8 ms each | 0.03 ms each |
| First click on the part | 30 ms | 1.2 s |

Two of those rows are trades, and both are deliberate. The wall clock is ten
seconds longer because a second interpreter has to start and the part has to be
written out and read back; the window is usable for all but three of those
seconds instead of none of them. And the selection nobody had asked for yet is
now built when they ask for it, under a busy cursor.

The importer also says what it sees: a STEP with that many faces is a converted
mesh, and Stamp now says so and points at the original 3MF, which goes through
the mesh path and needs none of this.

## Artwork components, and the backdrop that swallowed the drawing

An SVG now divides by fill colour. Every black path is one component, every red
path another; a feature can give any of them a colour of its own, and the export
then splits that feature into one body per colour with a filament slot each.
Grouping by colour rather than by element is what keeps it usable - a detailed
logo has hundreds of paths and three colours, and it is the three the user picks
from.

The same change fixed a bug that had been eating artwork. Overlapping elements
were resolved by unioning the whole profile into one silhouette, which is right
for two shapes of one colour and badly wrong across colours: a filled backdrop
and the drawing on top of it wind the same way, so their union is just the
backdrop. A logo with a white page rect behind it - which is most of what comes
out of an exporter - imported as a plain rectangle the size of its own page:

    nobg.svg  ->  loops=2 faces=2   bbox 28.00 x 12.00   (the artwork)
    bg.svg    ->  loops=1 faces=1   bbox 40.00 x 20.00   (the page)

Resolution is now per component, and between components it is the painter's
rule: whatever the source draws later is on top and comes out of everything
under it. So the pieces never overlap, and a body divided between them is
divided rather than counted twice.

A backdrop is still not wanted, so it is detected and left out, with a message
naming it and a switch to keep it. It is recognised by shape rather than by
colour or position: something covering nearly all of its own bounding box with
every other component inside it. Holes are subtracted when measuring that, which
is what tells a backdrop from a border - a border's outlines add up to more than
its box, but the material it covers is a thin frame, and the first version of
the check called every border a backdrop.

Detection runs before resolution rather than after. Afterwards the backdrop has
had the artwork carved out of it and no longer looks like one.

## The ribbon

Thirty commands never fitted on the bottom toolbar. Qt's answer was to move the
overflow into a chevron menu that shut again at every layout pass, so Export
STEP - which had no shortcut either - had no working path at all, and the menu
bar exists because of it.

They are now on a ribbon: four tabs, each holding captioned groups, each tab
scrolling sideways rather than hiding anything. The icons started as characters
painted into pixmaps, because Stamp ships no icon set; see the third pass below
for why that had to go. Buttons deliberately do not use `setDefaultAction`: that
keeps a button's text tied to the action's, which overwrote every short caption
with the full command name and left a row reading "Align... edge" and "Set
st...vertex". Enabled and checked state is wired through by hand instead, so the
window still enables one object and both views follow.

## The ribbon, second pass, and moving the view

The first ribbon was 126 px tall, which is a lot of chrome to look at while
working on a part. Buttons went from 78x64 to 66x53 with 17 px glyphs, margins
tightened, and the whole thing now collapses to its 26 px tab strip on a
double-click or Ctrl+F1. Open, it is 99 px.

Getting the camera somewhere was worse than the ribbon: a combo box, two clicks
to a standard view, and no way at all to rotate by hand. Four things were added,
and OpenCascade supplied most of them:

* **A navigation cube**, `AIS_ViewCube`, top right. Click a face, edge or corner
  and the camera swings there. Its animation is driven from a timer rather than
  OCC's own fixed loop, which blocks inside itself for the length of every swing.
  A click on it is navigation, not selection, so it is intercepted before the
  pick reaches the document.
* **Arrow keys** orbit 15° a press, Shift 90°, about the centre of what is on
  screen rather than about the eye - turning the eye is looking around, and what
  an arrow key should do is turn the part.
* **Roll**, Alt with left or right, through `SetTwist`. On the window rather than
  the viewport so it answers wherever the focus is; the arrows stay on the
  viewport, because taking them window-wide would break every spin box in the
  properties panel.
* **Normal to face**, Ctrl+8, which points the camera down the normal of the face
  the selected stamp is anchored to.

## The ribbon, third pass: making it look like one

It had the shape of a ribbon and none of the finish, and one of the reasons was
a plain bug. The emoji stand-in for an icon set fell back to painting the glyph
in a fixed `#d8d8d8`, which is white on white against a light desktop theme: on
Windows out of the box, the ribbon's icons were not there at all. The screenshot
that showed it is the whole argument for `docker/shot.sh`.

Three things changed.

**A drawn icon set**, `stamp/ui/icons.py`. Thirty-five icons on one 24-unit grid
at one stroke weight, in two colours taken from the palette - the outline in the
window's text colour, an accent for the part that carries the verb. Emoji could
never be a set: some render in colour, some as a box with a hex number in it,
and none of them share an optical size. Commands that belong together wear the
same badge - a plus for every "add this", a down arrow for every "write this
out" - and the badge's ring and its inner mark are *erased* rather than painted
in the background colour, because the button behind them changes colour on
hover, on press and when checked, and a hole is right against all four.

**A theme mixed from the palette**, not hard-coded. The body sits a shade off
the strip above it and the window below; the current tab carries the accent and
an underline; every button has a hover, a pressed and a checked state, because
those states are what tell you a rectangle is a button before you click it. All
of it is computed from `QPalette`, so it follows a light or a dark desktop, and
the menu bar is painted from the same mix so the top of the window reads as one
surface rather than as two programs stacked. `--dark` on the screenshot tool is
there to check the other end of that.

**Two button sizes and a quick-access bar.** A group whose commands are all one
size has no shape. The command you reach for is large; the ones beside it stack
three to a column, small, label alongside - which is also how thirty commands
fit across a laptop screen. Save, undo and redo also sit level with the menus,
because they are wanted from whichever tab you happen to be on.

## The ribbon, fourth pass: it was still too tall

100 px of chrome, and the complaint was the same as the second pass. Making a
ribbon *look* right and making it *cost* little are different problems, and the
third pass only solved the first.

So it now opens **compact**: one row of small buttons per tab, no captions under
the groups, 56 px including the tab strip - against 102 for the roomy
arrangement. That is 46 px back to the 3D view on every window, and nothing is
lost from a tab; only the labels move from under the icon to beside it. "Large
ribbon buttons" in the View menu brings the roomy layout back and is remembered.

The group lays itself out twice over rather than being built twice. Buttons are
made once and re-dressed - object name, button style, icon size, caption - and
the group swaps in a freshly built row. That matters because each button carries
live connections to its action; rebuilding them would leave those behind, and
the action would end up driving a widget that no longer exists.

The trap in doing it that way is that **re-parenting a widget hides it**. Qt's
own documentation says so and it is easy to read past: a button moved into the
new row comes back invisible unless it is shown again, so the first working
version of the switch emptied the ribbon. `_move()` takes the widget out of its
old layout, puts it in the new one, and shows it, in that order.

Two traps worth recording.

`ribbon.findChildren(QToolButton)` is not the list of commands. A `QTabBar`
keeps two scroller buttons of its own, they have no icon, and a test that
asserts every button has one fails on them. `command_buttons()` returns the
registered ones.

And the test for "clicking a button runs the command" used to click Batch. The
first line of `batch_stamp` is a `QFileDialog`, which is modal, does not consult
`interactive`, and under Xvfb never comes back - so the container run stopped
dead two thirds of the way through the suite with no failure and no output,
which reads exactly like a slow test. It clicks Fit to window now. Anything that
opens a file dialog needs an `interactive` guard before a test may click it.

## Updating itself

An updater downloads an executable and runs it. That is the exact shape of a
supply-chain attack, so the design question was never "how do we fetch a file" -
it was "what has to be true before anything is allowed to run".

The answer is a signed manifest. Every release publishes `latest.json` listing
the version and one artifact per platform with its SHA-256, plus an Ed25519
signature over its exact bytes. Stamp verifies the signature *before parsing the
JSON* - parsing attacker-controlled input before checking who wrote it is doing
work on their behalf - and then checks the downloaded installer against the hash
in that manifest before running it. A file that does not match is deleted rather
than kept. The private key exists only as a GitHub Actions secret; the public
half is compiled into `stamp/update.py`.

TLS was not enough on its own. It says the bytes came from GitHub, not that they
are the bytes we published, and it has nothing to say about a release that was
replaced or a machine with an extra root certificate.

With no key compiled in, the whole feature reports itself unconfigured and does
nothing. That is deliberate: a version check that trusts whatever it is handed
is worse than no version check, so it refuses rather than degrading quietly. The
release workflow does the same in reverse - no `STAMP_RELEASE_KEY` secret, no
manifest published, and nobody is offered the release.

Four decisions that are about the app rather than the crypto:

* **A bar, not a dialog.** An update is never urgent enough to interrupt
  somebody halfway through placing a stamp on a face, and a modal box at start-up
  trains people to dismiss modal boxes.
* **Never over unsaved work, never a silent restart.** Closing to install and
  losing somebody's part is the one unforgivable bug in this feature, so the
  install path asks, refuses while a rebuild is running, and offers "install when
  I quit" - which is the moment nobody is waiting on Stamp.
* **A failed automatic check says nothing.** A laptop on a train is not the
  user's problem to be told about. A check they *asked* for reports either way.
* **The check starts from `main.py`, not the window's constructor.** Every UI
  test builds a window, and building a window must never make a network call.

Two things that bit while writing it. The Windows install being per-user is what
makes the whole thing possible without a UAC prompt - `{autopf}` under
`PrivilegesRequired=lowest` is `%LOCALAPPDATA%\Programs`, which the user can
already write to. And Inno deliberately skips its "start the application" entry
in a silent install, so Stamp asks for the restart with a switch of its own,
`/RELAUNCH=1`, read by a `Check` in the script.

`update.py` has no Qt in it, which is what lets the tests sign real manifests
with a real key, serve them over `file://`, and exercise the verification Stamp
actually performs rather than a stand-in for it - including the forged manifest,
the tampered installer, and the attempted downgrade.

## Assemblies, and colours that were already in the file

Three things a real job needed, and the finding behind each.

**A two-colour logo exported as one lump.** `_split_by_component` began with
`if not colors`, so a feature was only divided by colour when the user had
assigned one to each component by hand. The colours were sitting in the SVG the
whole time. A component's colour now falls back to the colour the artwork was
drawn in - nobody draws a logo in two colours and means one - and what the user
picks still wins. One function, `effective_colors`, answers this for both the
export and the preview, because a preview that lies about the colours is worse
than a preview with no colours in it.

**The colour-stamp preview was one flat blue.** A colour stamp is a layer or two
deep, so the translucent solid over it is almost flush with the face and says
very little about where the artwork actually is. The decal is now drawn one
shape per component in its printing colour. `component_footprints` is the first
two lines of `component_prisms` pulled out - placement transform, no extrusion,
no boolean - because it redraws constantly and the 2D footprint is all it needs.

**Multi-part 3MFs arrived as one blob.** `import_mesh` called
`trimesh.load(force="mesh")`, which flattens a scene before Stamp sees it. Now
the parts are kept, named and measured. Use `scene.dump()` and not
`scene.geometry`: the second is the mesh *library*, where four copies of one clip
are a single entry carrying none of the transforms that put them where they are -
two instances came out with identical bounding boxes, which is no use for telling
them apart.

Two approaches were measured and rejected before the third:

| | 4-part, 346k tris | 11-part, 957k tris |
|---|---|---|
| `decompose()` the rebuilt result | 4 pieces, 66 ms | **267 pieces, 6752 ms** |
| `compose()` the parts | 0 ms | 7572 ms (broken mesh) |

`decompose` is out twice over: seven seconds on every rebuild, and it returns
shells rather than parts - a part is not one connected lump. `compose` is free on
a sound mesh and only slow on a file that is not watertight, which is a repair
cost, so that is what puts the assembly back together at the end.

So each part is rebuilt on its own, with only the features anchored to it, and a
part with nothing on it is passed straight through untouched. That makes this
**cheaper** than what it replaced rather than dearer: stamping one bracket of a
four-part file did boolean work on 346k triangles and now does it on 16k.

The one thing given up is the per-feature cache on that path. Its key is the
whole document's feature list and a part rebuilds only its own, so the key would
not mean what it says - and not touching untouched parts skips far more work than
the cache ever did. The single-part path keeps the cache and is untouched, which
is why every existing test still passes unchanged.

`part_at` picks the *smallest* box containing a clicked point. Boxes overlap in a
printed assembly - a lid sits inside the envelope of the box it closes - and the
smaller box is the more specific answer.

## Testing without a desktop

Many tests build a real window, so a local run takes over the screen.
`QT_QPA_PLATFORM=offscreen` is the way out for all but 68 of them: those need a
real GL surface for the OCC viewport and skip themselves, and the rest — the text
tests included — run offscreen. That is what CI does. Note that
`tests/test_acceptance.py` builds a real window too, so `--ignore=tests/test_ui.py`
alone is not enough to keep a run off the screen; set the platform as well.

`docker/` runs the whole suite, those 68 included, under Xvfb with Mesa's
software renderer instead. Two things
cost an hour between them and are worth recording. `python:3.12-slim` has no
glib, so PySide6 would not import - and the failure showed up as a container
sitting at 0% CPU rather than as an error. And `uv run` without `--no-sync`
re-resolves the project at every start, reaches for the network, and hangs there
with no output, which looks exactly like a wedged test suite and is not one.

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

## What a release is allowed to publish

A signed feed is only worth the care taken over what gets signed, and four ways
of getting that wrong were found in one pass and are worth recording.

A manual run of the release workflow builds `main` but names everything after
the tag somebody typed, so the manifest said one version over the bytes of
another. `make_manifest.py` also took `sorted(glob("*-Setup.exe"))[0]`, which on
a tag that had been built twice signed whichever installer sorted first. The
workflow now refuses a run whose tag, `src/stamp/__init__.py` and
`packaging/stamp.iss` disagree, and the manifest is built from the installers
whose names carry that version - never from whatever is in the folder.

The manifest job ran `if: always()` over `gh release download ... || true`, so a
failed installer job still published a signed feed, over whatever a previous run
had left on that tag. It is gated on success now, and refuses to write a
manifest with a platform missing unless `--allow-missing` says so.

"Install when I quit" left a verified installer at a fixed path in `%TEMP%` for
as long as the user kept working. Verified that morning is not verified now, so
the download lands in a directory made fresh for it and the hash is checked
again immediately before the installer is started.

`STAMP_UPDATE_FEED` and `STAMP_UPDATE_PUBLIC_KEY` are what let the tests stand
up a real signed feed, and they were honoured in shipped builds too - anything
that could set an environment variable could point Stamp at its own feed signed
with its own key, and both signature checks would pass. They are honoured only
when not frozen now, `file://` included.

Packaging builds on every platform Stamp claims: `.github/workflows/release.yml`
compiles the Windows Setup with Inno, and builds a DMG and a PKG for each of the
two macOS architectures; `packaging/build_installer.py` makes the Linux tarball.
None of it is signed - see below.

## Not done

1. DWG is best-effort, as section 5.4 allows. The ODA File Converter isn't on this
   machine, so that path is untested. Stamp detects the converter at startup and
   shows a download link and a file picker when it's missing.
2. Nothing from section 11's "later" list.
3. Code signing. Nothing is signed with an Authenticode certificate or notarised
   for macOS, so SmartScreen warns on every install and Gatekeeper quarantines a
   downloaded bundle. The update manifest's Ed25519 signature covers *what Stamp
   installs*; it does nothing about what the operating system thinks of the
   installer. Until there is a Developer ID, the update bar on macOS links to the
   release page rather than installing anything.
4. No release key has been generated yet, so update checking is switched off in
   every build shipped so far: `RELEASE_PUBLIC_KEY` is empty, Stamp refuses to
   read a manifest it cannot verify, and Help → Check for updates says as much.
   See "Signing releases, once" in the README. The release workflow warns on a
   tag build when the key is still empty.
