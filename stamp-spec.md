# Stamp — Build-Ready Specification

**A desktop app for placing 2D vector artwork onto 3D parts as real geometry.**

Version 1.0 of this spec · Target: a working v1 in roughly 6–8 weeks of part-time work
Audience: a developer (human or AI coding agent) implementing from scratch

---

## 1. What this is

You have a 3D part (STEP or STL) and a 2D profile (SVG, DXF, or DWG) — a logo, a serial number,
a slot pattern, a keep-out cutout. You want the profile to become **actual geometry** on the part:
raised, recessed, or cut clean through, with rounded or chamfered edges, positioned by dragging it
around on the surface until it looks right. Then you export STEP for the machine shop and STL for
the printer.

That is the entire product. Everything below serves that one sentence.

### 1.1 Design principles

1. **One workflow, done well.** Pick a face → drop a profile on it → set depth → done. No sketcher,
   no constraint solver, no assembly, no history-of-history.
2. **Numbers are authoritative, the mouse is for approximation.** Every drag has a matching numeric
   field. The mouse gets you close; you type the number that matters.
3. **Nothing is destructive.** Every placed profile stays editable forever. Change the depth of the
   first feature after adding four more and the part rebuilds.
4. **Never lie about geometry.** If an operation cannot be done exactly, the app says so and refuses,
   rather than producing something that looks right and machines wrong.

### 1.2 Explicit non-goals for v1

- Full parametric sketching (lines, constraints, dimensions). Profiles come from files, period.
- Assemblies, multi-body management, materials, rendering.
- Editing the base part's original geometry beyond the features you add.
- Toolpath/CAM output, drawings, GD&T, BOMs.
- Cloud, accounts, collaboration, plugins.

---

## 2. The one hard technical decision, made up front

**STEP and STL are not the same kind of object, and the app must not pretend they are.**

- **STEP** is a B-rep solid: exact surfaces, real edges. OpenCascade can boolean it, fillet it,
  chamfer it, and export it back as STEP with no loss.
- **STL** is a triangle soup. Feeding it through OpenCascade's B-rep booleans is not a slow path,
  it is a broken one. Measured, on current OpenCascade 7.9: importing an 82,000-triangle STL via
  `StlAPI_Reader` produces a shape with 82,000 planar faces; sewing it into a shell takes **96
  seconds** and scales roughly quadratically; one boolean cut on the result takes another **16
  seconds**. The same cut in a mesh-native kernel takes **0.4 seconds**. Half-million-triangle files
  are simply not viable through OCC at all.

So the app keeps **two representations for the base part** and one for everything else:

| | Base part representation | Boolean engine | Fillet/chamfer against base | Export |
|---|---|---|---|---|
| **Solid mode** (STEP/IGES/BREP in) | OCC `TopoDS_Shape` | `BRepAlgoAPI_Fuse` / `_Cut` | Yes, exact | STEP + STL |
| **Mesh mode** (STL/3MF/OBJ in) | `manifold3d` mesh | `manifold3d` | No — see §6.5 | STL only |

**The tool solid is always B-rep.** The shape extruded from the profile — including its own fillets
and chamfers — is built in OpenCascade in both modes. In mesh mode it is tessellated at the last
moment and handed to manifold3d for the boolean. This matters because it means **rounding the top
edge of a raised logo works on an STL part**; only a *blend into the surrounding base surface*
requires B-rep. That is an honest, useful line, and the UI draws it explicitly (§6.5).

Consequence for the user, stated plainly in the app: *STL in → STL out. If you want a STEP for the
shop, start from a STEP.* There is no mesh-to-solid reconstruction in v1 and there should not be one
in v2 either.

---

## 3. Stack

Chosen for "simple and easy" as requested — meaning few moving parts, pip-installable, no build step,
and a permissive license end to end.

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.12** | The whole CAD ecosystem below is Python. 3.12 is inside every dependency's support range. |
| Geometry kernel | **OpenCascade via `cadquery-ocp-novtk` (7.9.3.x)** | The only pip-installable OCC binding. `pythonocc-core` is conda-only — its PyPI entry is an empty decade-old placeholder. The `-novtk` variant avoids a known VTK shared-library loading bug and drags in nothing you don't need. Apache-2.0. |
| Modeling helpers | **`build123d` 0.11.x** | Direct object algebra (`Solid`, `Face`, `Wire`, `Location`) rather than a fluent stack, which maps cleanly onto a feature tree you own. Apache-2.0. Drop to raw `OCP.*` freely — same wrapper underneath. |
| Type stubs | **`cadquery-ocp-stubs`** (dev only) | OCP ships no `.pyi`. Pin to the exact OCP version. Large quality-of-life win when calling raw OCC. |
| GUI | **PySide6** | LGPL, so the app can ship under a permissive license. (Note: PyQt6 is GPL-or-pay.) |
| 3D viewport | **OCC AIS/V3d embedded in a Qt widget** | Native OCC selection and highlighting of individual faces and edges — the entire reason to use AIS instead of a generic mesh viewer. OCP exposes `AIS_InteractiveContext`, `V3d_Viewer`, `OpenGl_GraphicDriver`, and the platform window wrappers (`Xw_Window` / `WNT_Window` / `Cocoa_Window`). |
| Mesh booleans | **`manifold3d` 3.5.x** | Robust, fast, watertight-preserving, pip-installable. Also the default engine inside `trimesh` 5.x, and its `CrossSection` gives 2D booleans + offsetting for free. |
| Mesh I/O + utility | **`trimesh` 5.x** | STL/3MF/OBJ read-write, repair, unit handling. |
| SVG import | **`ocpsvg` 0.6.x** (over `svgelements`) | Converts SVG paths straight to `TopoDS_Edge`/`Wire`/`Face`. This is the SVG→OCC bridge you would otherwise spend two weeks writing. `svgelements` is the only Python SVG parser that composes group transforms, `viewBox` mapping, and physical units correctly. |
| DXF import | **`ezdxf` 1.4.x** | `ezdxf.path.make_path()` converts LINE/ARC/CIRCLE/ELLIPSE/SPLINE/LWPOLYLINE-with-bulge to cubic Béziers. `ezdxf.path.winding_deconstruction` and `make_polygon_structure` solve hole/island nesting for you. |
| DWG import | **`ezdxf.addons.odafc`** (shell-out to ODA File Converter), pluggable | See §5.4. Not a hard v1 requirement. |
| Packaging | **`uv` + PyInstaller** | One-command dev setup; single-folder builds for Windows/macOS/Linux. |

### 3.1 Known stack gotchas — read before starting

- **The reference viewer implementation is CQ-editor, and it is PyQt5, not PySide.** Its
  `cq_editor/widgets/occt_widget.py` and `viewer.py` are the two files to study. Porting to PySide6
  is a mechanical rename — `pyqtSignal`→`Signal`, `pyqtSlot`→`Slot`, scoped enums
  (`Qt.WidgetAttribute.WA_NativeWindow`) — but budget a day for it, not an hour. OCP itself never
  imports Qt; the only coupling is one window handle.
- The widget must set `WA_NativeWindow | WA_PaintOnScreen | WA_NoSystemBackground` and override
  `paintEngine()` to return `None`, then pass `int(self.winId())` to the platform window wrapper.
- On Linux this path needs X11 or XWayland. Native Wayland is not handled by the CQ-editor widget.
  Document it; do not try to fix it in v1.
- OpenCascade boolean **history is collected by default**. `SetToFillHistory()` only *disables* it.
- `SimplifyResult()` on a boolean merges co-planar faces and will invalidate naive face references.
  Do not call it during modeling; call it once, optionally, at STEP export time.

---

## 4. Core concepts and data model

Three objects. That is the whole model.

### 4.1 Document

```
Document
  base: BasePart              # the imported 3D part, immutable
  features: [Feature]          # ordered list, applied in sequence
  units: "mm" | "in"           # display units; all internals are mm
  view_state: {...}            # camera, for reopening where you left off
```

### 4.2 BasePart

```
BasePart
  source_path: str             # original file, copied into the project archive
  source_hash: str
  mode: "solid" | "mesh"
  shape: TopoDS_Shape          # solid mode
  mesh: Manifold               # mesh mode
  unit_scale: float            # applied at import to normalize to mm
  bbox, volume, triangle_count # for the info panel
```

### 4.3 Feature

A Feature is a profile + a placement + an operation + modifiers. It is a pure data record; the
geometry is recomputed from it.

```
Feature
  id: uuid
  name: str                    # "Logo", "Serial cut" — user-editable
  enabled: bool                # checkbox in the tree; suppress without deleting

  profile:
    source_path: str           # SVG/DXF/DWG, copied into the archive
    source_hash: str
    loops: [Loop]              # normalized, cached; see §5.5
    native_units: str          # what the file claimed
    native_size_mm: (w, h)     # bbox at scale 1.0

  placement:
    anchor:                    # how the sketch plane is defined
      kind: "face" | "datum"
      face_ref: FaceRef        # see §8.2
      plane: {origin, normal, u_axis}   # resolved, cached
    offset_2d: (u, v)          # mm, within the sketch plane
    rotation: float            # degrees, CCW about the plane normal
    scale: (sx, sy)            # uniform by default; lock toggle
    mirror_u, mirror_v: bool
    lift: float                # mm along normal; start the extrude above/below the face

  operation:
    kind: "add" | "cut"
    depth_mode: "blind" | "through_all" | "to_face" | "symmetric"
    depth: float               # mm, used for "blind"/"symmetric"
    direction: "into" | "out_of"     # relative to the face's outward normal
    draft_angle: float         # degrees, 0 = straight walls

  modifiers: [Modifier]        # applied in order, to the tool solid or the result
```

```
Modifier
  kind: "fillet" | "chamfer"
  radius | distance: float
  angle: float                 # chamfer only, for asymmetric
  target: EdgeSelector         # see §8.3
```

### 4.4 Project file — `.stamp`

A zip archive. Openable with any unzip tool, diffable, and self-contained so a project mailed to a
shop still works.

```
project.stamp (zip)
  manifest.json          # the Document, serialized; schema_version field required
  base/part.step         # verbatim copy of the imported part
  profiles/<hash>.svg    # verbatim copies of every imported profile
  thumbnail.png          # 512×512, for the recent-files list
```

Never store derived geometry in the project file. Everything rebuilds from sources + manifest.
If a source is missing on open, the app says which file and offers to relink.

---

## 5. Import pipelines

### 5.1 STEP / IGES / BREP → solid mode

`STEPControl_Reader` → `TransferRoots()` → `OneShape()`. Then:

- Read the STEP length unit from the file header and scale to mm.
- If the result is a compound of several solids, ask the user which one to work on (or offer
  "treat as one body" if they don't intersect).
- Run `BRepCheck_Analyzer`. If invalid, attempt `ShapeFix_Shape` once, re-check, and warn if it
  still fails — but let the user proceed. Most real-world STEP is slightly dirty and still workable.
- IGES is surfaces, not solids: attempt sew-and-solidify, warn clearly if it doesn't close.

### 5.2 STL / 3MF / OBJ → mesh mode

`trimesh.load()` → `manifold3d.Manifold`. Then:

- **Do not** use `StlAPI_Reader`. If you only need to *display* an STL in the OCC viewer, use
  `RWStl.ReadFile_s` → `Poly_Triangulation` (essentially free, 0.03 s for 82k triangles) and hand
  the triangulation to `AIS_Shape` for display only.
- STL carries no units. Show a unit prompt on import with a size preview
  ("bounding box is 84 × 40 × 12 — is that mm or inches?"), defaulting to mm.
- Check watertightness. If not manifold, run `trimesh`'s repair pass; if it still isn't, warn that
  booleans may fail and let the user proceed.
- Above ~1M triangles, offer to decimate for display only, keeping the full mesh for booleans.

### 5.3 SVG → profile

Hand the file to `ocpsvg`, which resolves group transforms, `viewBox`, and physical units through
`svgelements` and returns OCC wires/faces directly.

Rules:
- **Units.** If the SVG declares physical units (`width="100mm"`), honor them. If it is unitless px,
  assume 96 dpi and show a "size" field pre-filled with the resulting mm, so the user can correct it
  in one edit.
- **Strokes are ignored. Only fills define geometry.** A stroked line with no fill has no area and
  cannot be extruded. Detect this case explicitly and offer *Outline strokes* — offset the path by
  half the stroke width using `manifold3d.CrossSection` with the right `JoinType` — rather than
  silently importing nothing.
- **Text must be converted to paths in the design tool first.** Detect `<text>` elements and say so
  by name: "This SVG contains live text. In Illustrator/Inkscape, convert text to outlines and
  re-export." Do not attempt font rasterization.
- Ignore gradients, masks, clip paths, animation, filters. Warn once if present.

### 5.4 DXF / DWG → profile

**DXF** via `ezdxf`:
- `doc.modelspace()`, filter to the entity types that carry geometry: LINE, ARC, CIRCLE, ELLIPSE,
  SPLINE, LWPOLYLINE, POLYLINE, plus HATCH boundaries.
- `ezdxf.path.make_path(entity)` for each → cubic Bézier `Path` objects. Bulges, splines, and
  elliptical arcs all come through correctly.
- Read `$INSUNITS` from the header for units; if it's 0/unset, prompt.
- **Layer filter.** Real DXFs have construction lines, dimensions, and title blocks. Show a layer
  list with checkboxes and a live preview; default to all visible non-defpoints layers.
- TEXT/MTEXT: same policy as SVG — tell the user to explode to geometry.

**DWG** — design this as a pluggable chain, and make it clear DWG is best-effort:
1. Try `ezdwg` (pure pip, Rust core, ezdxf-shaped API) if installed. It is new — 0.12.x as of
   mid-2026 — so treat it as "evaluate", not "depend on". Verify its output against a known DXF
   before trusting it.
2. Fall back to `ezdxf.addons.odafc`, shelling out to ODA File Converter (free, no registration,
   Windows/macOS/Linux). Detect it at startup; if absent, show a one-click link to the download and
   a path picker.
3. Fall back to a clear message: "Can't read DWG. Save as DXF from your CAD program and try again."

Never let DWG support block the v1 release.

### 5.5 Profile normalization — the shared back half

Everything above converges on one representation before it touches the 3D part.

```
Loop = { curves: [OCC edge], closed: bool, area: float, winding: "cw"|"ccw" }
Profile = { loops: [Loop], nesting: tree }
```

Steps, in order:

1. **Join.** Collect all curve segments; join endpoints within tolerance (default 0.01 mm, adjustable
   in a "Repair" popover). Build closed wires.
2. **Report open loops.** Any loop that won't close is highlighted in red in the preview with its gap
   size. Offer: auto-close with a straight segment / outline the stroke / discard this loop.
   Never silently drop geometry.
3. **Nest.** Determine containment by flattening to polylines and testing point-in-polygon. Build a
   nesting tree. Even depth = material, odd depth = hole. (`ezdxf.path.winding_deconstruction` and
   `make_polygon_structure` do this for DXF; implement the same logic once and use it for both.)
4. **Face-ify.** `BRepBuilderAPI_MakeFace` with the outer wire, then `Add()` each inner wire as a
   hole. Multiple disjoint outer loops become multiple faces in a compound — a five-character serial
   number is five faces, one feature.
5. **Self-intersection check.** Run `BRepCheck_Analyzer` on each face. Self-intersecting artwork
   (very common in traced logos) is flagged, with the intersection points shown, and the feature is
   blocked until fixed or the user chooses "union overlapping loops" (a 2D boolean via
   `manifold3d.CrossSection`).
6. **Normalize origin.** Translate so the profile bounding-box center is at (0,0). All placement math
   downstream is relative to that center, which is what makes "center it on this face" a one-click
   operation.
7. **Cache.** Store the normalized `Profile` keyed by source hash. Reimport is free.

---

## 6. Placing a profile — the core interaction

### 6.1 Defining the sketch plane

The user clicks a face on the part. That defines the plane.

**Planar face:**
- `origin` = the clicked point, projected onto the face's plane (not the centroid — the user clicked
  where they want it, and a "center on face" button covers the other case).
- `normal` = the face's outward normal, corrected for `TopAbs_REVERSED` orientation.
- `u_axis` = the direction of the longest straight edge of that face, projected into the plane. This
  is stable across rebuilds and usually matches what the user considers "along" the face. Fall back
  to global +X projected into the plane. A "Rotate 90°" button and a numeric rotation field handle
  the rest.

**Cylindrical / conical face (v1.5 — see §11):** *wrap mode.* Map the profile's (u, v) onto the
surface parameterization so the artwork follows the curvature — the case that matters for text on a
tube. The plane concept becomes a UV frame; everything downstream (offset, rotation, scale) still
works on (u, v). Extrude direction becomes the local radial normal.

**Any other curved face:** v1 refuses politely and offers to project onto the tangent plane at the
click point instead, with a warning that walls will be straight, not radial.

**Mesh mode:** there are no faces, only triangles. Clicking picks a triangle; the app grows a region
of connected triangles whose normals are within a tolerance (default 5°) and fits a plane to that
region by least squares. Show the detected region highlighted so the user can see what it locked
onto, with a tolerance slider. If the region is tiny (a single triangle on a curved surface), warn.

**Datum planes** as an escape hatch: global XY/XZ/YZ, or offset-from-face. Covers the case where no
suitable face exists.

### 6.2 The manipulation UI

Once the plane is set, the profile appears on the surface as a flat translucent overlay (a "decal"
preview — not yet a solid, so it is instant to redraw).

**Mouse, in the sketch plane:**

| Action | Result |
|---|---|
| Drag inside the profile | Translate in-plane |
| Drag a corner handle | Scale (uniform if the lock is on, which is the default) |
| Drag an edge handle | Scale one axis (lock off only) |
| Drag the rotation handle above the bbox | Rotate about the plane normal |
| Arrow keys | Nudge 0.1 mm; Shift+arrow 1 mm; Ctrl+arrow 0.01 mm |
| Shift while dragging | Constrain to the u or v axis |
| Alt while dragging | Ignore snapping |

Handles are drawn as a 2D screen-space overlay anchored to the projected bbox corners, so they are
always the same pixel size regardless of zoom. This is much easier than a true 3D gizmo and, on a
plane, it is exactly as expressive.

**Snapping** (toggleable, on by default), with a magenta highlight on the snapped target:
- Face center, face bounding-box corners and edge midpoints
- Any edge midpoint or endpoint on the part
- Cylinder axis projections — critical for centering a logo on a boss
- Grid, at a user-set pitch
- Alignment guides to previously placed features (so two labels line up)

**Numeric panel**, always visible, always live — the source of truth:

```
Position    U [  12.50 ] mm    V [  -4.00 ] mm      [Center on face]
Rotation    [   0.00 ] °                            [↺ 90°] [↻ 90°]
Size        W [  40.00 ] mm    H [  11.30 ] mm   🔒  [Fit to face]
Scale       [ 100.00 ] %                            [Reset]
Lift        [   0.00 ] mm
Mirror      [ ] horizontal  [ ] vertical
```

Editing W or H with the lock on updates the other and the scale %. This is how "make the logo exactly
40 mm wide" happens, and it is the single most-used control in the app.

### 6.3 The operation

```
Operation   ( ) Add material    (•) Cut material
Depth       (•) Blind [ 1.50 ] mm
            ( ) Through all
            ( ) To face…            [pick]
            ( ) Symmetric [ 1.50 ] mm
Direction   (•) Into the part   ( ) Out of the part
Draft       [ 0.00 ] °                  ⓘ positive = walls flare outward toward the opening
```

Implementation: build the tool solid by prism-extruding the profile face(s) along the plane normal
(`BRepPrimAPI_MakePrism`, or `BRepOffsetAPI_MakeDraft`/a lofted prism when draft ≠ 0). For
`through_all`, extrude to 1.5× the part's bounding-box diagonal in the chosen direction — cheap and
always sufficient. Then fuse or cut.

Live preview: render the tool solid semi-transparent (green for add, red for cut) as the user
adjusts depth, and only run the boolean on release or after a 250 ms idle.

### 6.4 Fillets and chamfers — solid mode

Two distinct targets, and the UI must keep them distinct because they behave differently:

**A. Edges of the feature itself** — the top edge of a raised logo, the bottom edge of a pocket.
These come from the profile's own curves, so they can be named deterministically (§8.3) and are
robust across rebuilds. Preset selectors:
`Top edges` · `Bottom edges` · `Side/vertical edges` · `All feature edges` · `Pick manually`.

**B. Blend into the base surface** — the fillet where a raised boss meets the wall it sits on.
These edges only exist after the boolean, so get them from the boolean's history:
`op.SectionEdges()` for the intersection curves, and `op.Generated(face)` for edges created from a
face. Accumulate across the feature chain with `BRepTools_History::Merge` when composing.

Apply with `BRepFilletAPI_MakeFillet` / `MakeChamfer`. **Fillets fail often** — radius larger than
the adjacent geometry allows, adjacent fillets overlapping, degenerate corners on sharp artwork
vertices. Handle it properly:

- Catch the failure, keep the pre-fillet result, mark the modifier with a warning badge in the tree.
- Show *which* edges failed, highlighted in the viewport.
- Suggest the largest radius that succeeds, found by bisection over ~6 attempts (fast enough to run
  on failure, too slow to run continuously).
- Never leave the user with a silently un-filleted model that they discover at the machine shop.

A note the UI should surface: sharp interior corners in artwork (the inside of a letter "V") cannot
accept a fillet larger than zero at the vertex itself. Recommend filleting the 2D profile instead —
`ezdxf.path` has `fillet`/`chamfer` helpers, and `manifold3d.CrossSection` offset-in-then-out gives
a clean rounded profile.

### 6.5 Fillets and chamfers — mesh mode

Because the tool solid is B-rep even in mesh mode, **target A works normally.** The feature is built
and filleted in OpenCascade, then tessellated (`BRepMesh_IncrementalMesh`, deflection from the
quality setting) and handed to manifold3d.

**Target B does not work** and the UI must say so, not gray out a button with no explanation:

> *Blending into the base surface needs exact surface geometry, which an STL doesn't have. You can
> still round the edges of the feature itself. To blend into the part, start from a STEP file.*

### 6.6 Rebuild

Linear and total. On any change:

```
result = base
for f in features where f.enabled:
    profile   = cache[f.profile.hash]
    plane     = resolve_anchor(f.placement.anchor, result)
    tool      = extrude(transform(profile, f.placement), f.operation)
    tool      = apply_modifiers(tool, f.modifiers, target="feature")
    result    = boolean(result, tool, f.operation.kind)
    result    = apply_modifiers(result, f.modifiers, target="blend")
```

Optimizations, in the order they become necessary — do not build them up front:
1. Cache the result after each feature. Editing feature N only recomputes N onward.
2. Debounce: 250 ms idle before rebuild, with the preview overlay covering the gap.
3. Run the rebuild on a worker thread; cancel and restart if input changes mid-flight. The GUI
   thread must never block on a boolean.
4. Show a progress indicator for anything over 500 ms, with a cancel button.

**Undo/redo** = a stack of serialized `Document` snapshots (they are tiny — JSON, no geometry).
Cap at 100. This is far simpler than command-pattern undo and, at this document size, indistinguishable.

---

## 7. UI layout

A single window. No modes, no ribbon, no floating palettes.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Stamp — bracket_v3.stamp                                         − □ ×  │
├────────────────┬─────────────────────────────────────┬───────────────────┤
│ FEATURES       │                                     │ PROPERTIES        │
│                │                                     │                   │
│ ▣ bracket_v3   │                                     │ ── Placement ──   │
│   (STEP·solid) │                                     │ U  [ 12.50 ] mm   │
│                │          3D VIEWPORT                │ V  [ -4.00 ] mm   │
│ ▣ ⬈ Logo       │                                     │ Rot [ 0.00 ] °    │
│   ▸ fillet 0.5 │        (OCC AIS view)               │ W  [ 40.00 ] mm   │
│ ▣ ⬊ Serial cut │                                     │ H  [ 11.30 ] 🔒   │
│ ☐ ⬊ Drain slot │                                     │ Lift[ 0.00 ] mm   │
│   ⚠ fillet     │                                     │                   │
│                │                                     │ ── Operation ──   │
│ [+ Add profile]│                                     │ (•) Add ( ) Cut   │
│                │                                     │ Depth [1.50] mm   │
│                │                                     │ Draft [0.00] °    │
│                ├─────────────────────────────────────┤                   │
│                │ ✓ Rebuilt in 340 ms · 48.2 cm³      │ [+ Fillet]        │
│                │ ⚠ Drain slot: fillet 2.0 failed     │ [+ Chamfer]       │
├────────────────┴─────────────────────────────────────┴───────────────────┤
│  Open  Save  │  Export STEP  Export STL  │  Units: mm ▾  │  Undo  Redo   │
└──────────────────────────────────────────────────────────────────────────┘
```

**Left — feature tree.** Base part pinned at top. Features in application order, drag to reorder,
checkbox to suppress, ⬈/⬊ icons for add/cut, ⚠ badge for anything that failed. Double-click renames.
Right-click: duplicate, mirror across a plane, delete.

**Center — viewport.** Standard orbit/pan/zoom (middle-drag orbit, shift+middle pan, wheel zoom — the
CAD convention). View cube or six preset-view buttons in the corner. Selection filter toggle:
faces / edges. Status bar underneath shows rebuild time, volume, mass if a density is set, and the
most recent warning.

**Right — properties.** Contextual to the tree selection. Empty state shows base part info: source
file, units, bounding box, triangle or face count, watertight status.

**Keyboard:** `Ctrl+Z/Y` undo/redo, `Ctrl+S` save, `Ctrl+D` duplicate feature, `Del` delete,
`F` frame selection, `Space` toggle the tool-solid preview, `1`–`6` preset views, `Esc` cancel drag.

### 7.1 The first-run flow

The app opens empty with two large buttons: **Open a part** and **Open a project**. Once a part is
loaded, a single **+ Add profile** button. Drag-and-drop works everywhere: drop a STEP on an empty
window to load a part, drop an SVG on a face to create a feature anchored right there. That last
gesture is the one to get right — it is the app in one motion.

---

## 8. Reference stability (the part that will bite you)

Features store references to topology. Topology changes when features are added or edited. This is
the classic topological-naming problem, and pretending it doesn't exist is how a feature-based app
becomes untrustworthy.

**Strategy: never store an index. Store geometry plus intent, and re-resolve on every rebuild.**

### 8.1 Rule

Every reference resolves at rebuild time against the *current* shape. If resolution is ambiguous or
fails, the feature is marked broken with a clear message and a "re-pick" button in the properties
panel — and the previous good result is retained so the user is never staring at a vanished part.

### 8.2 `FaceRef` — anchoring a feature to a face

Store all of:
- `point`: a 3D point on the face (where the user clicked)
- `normal`: the face normal there
- `surface_type` + parameters (plane equation, cylinder axis + radius, …)
- `area`, `bbox_center`
- `origin_feature_id`: which feature created this face, or `null` for the base part

Resolve by scoring every candidate face: surface type must match; then weight by distance from
`point`, normal agreement, and area ratio. Accept the best score above a threshold; if two faces tie
closely, mark it ambiguous rather than guessing.

**Key mitigation:** anchor to the *base part* wherever possible. Base faces don't move. Only allow
anchoring to a face created by an earlier feature when the user explicitly picks one, and warn in the
tree that the two are now coupled.

### 8.3 `EdgeSelector` — targeting fillets and chamfers

Two kinds, and the difference is why fillets on features are reliable while blends are fragile:

**Deterministic (preferred).** Feature edges are derived from the profile, so name them by
provenance: `(feature_id, loop_index, curve_index, role)` where role ∈ {top, bottom, side}. These are
recomputed identically every rebuild regardless of what else changed. All the presets in §6.4A use
this.

**Geometric (manual picks and blends).** Store a midpoint, a tangent direction, and a length. Resolve
by nearest-match with tolerance. If the edge moved more than a threshold or split into several edges,
resolve to the set of nearest edges and flag it for user confirmation.

Blend edges get the deterministic treatment where possible by tagging them from the boolean history
at creation time — `op.SectionEdges()` returns the intersection edges, and those can be associated
with the feature that created them and re-derived on rebuild rather than re-searched geometrically.

---

## 9. Export

**STEP** (solid mode only):
- `STEPControl_Writer`, `AP214IS` or `AP242` (offer both; AP242 is the modern default).
- Write the length unit explicitly as mm.
- Optionally run `SimplifyResult`/`ShapeUpgrade_UnifySameDomain` to merge co-planar faces — this
  produces much cleaner files for the shop. Offer it as a checkbox, default on, applied only to the
  exported copy.
- Validate with `BRepCheck_Analyzer` before writing and refuse to export an invalid solid without an
  explicit override.

**STL** (both modes):
- Solid mode: `BRepMesh_IncrementalMesh` with a deflection control exposed as three presets —
  Draft (0.1 mm), Normal (0.02 mm), Fine (0.005 mm) — plus a numeric override. Show the resulting
  triangle count live.
- Mesh mode: write the manifold result directly, no re-tessellation.
- Binary STL by default, ASCII on request.
- Verify watertightness before writing; warn if it isn't.

**3MF** (both modes), for multi-color printing:
- Split the rebuilt result along feature boundaries: a base body, plus one body per feature. Raised
  artwork is `result ∩ tool`; engraved artwork is the inlay the cut removed, `base ∩ tool − result`,
  which fills the pocket flush with the surface. A through cut stays open.
- Write the bodies as components of one object so the part moves in one piece on the plate, and put
  the colors on the triangles through the materials extension — an object-level `basematerials`
  group is ignored by Bambu Studio's color parser.
- Offer to leave the colors out entirely: separate named bodies with no color asks the slicer
  nothing on the way in.

**Color stamp** (`OperationKind.COLOR`), artwork that is color and nothing else:
- A third operation kind beside add and cut. It cuts a recess one printed layer deep (0.2 mm by
  default) and the 3MF export fills it back in as the second-color body, so the artwork prints flush
  with the face instead of standing proud of it. Base plus inlay equal the original part.
- Blind depth only — the export needs a floor to fill up to — so the depth-mode picker gives way to
  a single thickness. Below 0.08 mm there is no whole layer for the slicer to change color on, so
  preflight warns.
- Only 3MF carries the second body. STEP and STL get the bare recess, and preflight says so before
  writing one.

**Both:** default filename = `<project>_<yyyymmdd>.<ext>`. Remember the last export folder.
Show a completion toast with the file size and, for STL, the triangle count.

**Nice-to-have, cheap:** an "Export for quote" button that writes STEP + STL + a PNG screenshot +
a small text file listing dimensions and volume into one folder. That is exactly what gets emailed
to a shop, and it takes an afternoon to build.

---

## 10. Errors and edge cases

Every one of these needs a specific message. Generic "operation failed" dialogs are what make CAD
software miserable.

| Situation | Behavior |
|---|---|
| Profile has open loops | Highlight in red with gap sizes; offer auto-close / outline strokes / discard |
| Profile is stroke-only, no fill | Detect explicitly; offer "outline strokes at [width] mm" |
| SVG/DXF contains live text | Name it: "convert text to outlines and re-export" |
| Profile self-intersects | Show intersection points; offer 2D union; block until resolved |
| Profile larger than the face | Warn, allow it (a cut hanging off an edge is legitimate) |
| Cut doesn't intersect the part | Boolean returns the input unchanged — detect by volume comparison and warn |
| Add produces a disconnected body | Detect multiple solids in the result; warn (it will not print or machine as one piece) |
| Fillet radius too large | Suggest the largest working radius via bisection; keep the un-filleted result |
| Boolean fails outright | Retry once with a fuzzy tolerance (`SetFuzzyValue`, ~1e-4 × bbox diagonal); if it still fails, report and keep the previous result |
| STL not watertight | Attempt repair; warn; allow proceeding |
| Rebuild exceeds 10 s | Progress bar with cancel; offer to switch the viewport to draft tessellation |
| Source file missing on open | Name the file, offer relink, keep the rest of the project intact |
| Units ambiguous (STL, unitless SVG, DXF with `$INSUNITS`=0) | Prompt with a size preview; never guess silently |
| Feature reference unresolvable | Mark broken in the tree, keep the last good geometry, offer re-pick |

---

## 11. Milestones

Each milestone ends with something runnable. Estimates assume one developer working part-time.

**M0 — Skeleton (week 1).**
`uv` project, PySide6 window, the OCC AIS viewport widget ported from CQ-editor to PySide6, load a
STEP and orbit it. *Done when: a STEP file appears on screen and you can spin it.* This is the
highest-risk piece of the whole build — do it first, before anything else.

**M1 — Import and preview (week 2).**
STEP + STL loading with unit handling. SVG + DXF profile import through the normalization pipeline of
§5.5. Show the profile flat in the viewport. No placement yet.
*Done when: a logo appears floating in 3D next to the part, correctly sized in mm.*

**M2 — One feature, end to end (weeks 3–4).**
Face picking → sketch plane. Numeric placement panel. Blind extrude, add and cut. Boolean in both
solid and mesh modes. STEP + STL export.
*Done when: you can put a real logo on a real part and export a file a shop would accept.* This is
the minimum useful product — use it on an actual job before continuing.

**M3 — Direct manipulation (week 5).**
Screen-space drag handles for translate, rotate, scale. Snapping. Live tool-solid preview.
*Done when: placement feels like moving a sticker, not like typing coordinates.*

**M4 — Feature tree and modifiers (weeks 6–7).**
Multiple features, reorder, suppress, rebuild engine, undo/redo. Fillets and chamfers with the preset
selectors. Project save/load. Reference resolution per §8.
*Done when: you can reopen yesterday's project, change the first feature's depth, and everything
downstream rebuilds correctly.*

**M5 — Polish (week 8).**
The full error-message table from §10. Draft angle, through-all, to-face, symmetric. Recent files,
thumbnails, keyboard shortcuts, "Export for quote". PyInstaller builds for Windows and macOS.

**Later, in rough priority order:**
1. **Cylindrical wrap** — text and logos on tubes and bosses. The most-requested thing that v1 won't do.
2. Conformal projection onto arbitrary curved surfaces.
3. Feature patterns: linear, circular, mirror.
4. Batch mode: apply the same feature to a folder of parts (serialization — one part per number).
5. Variable-radius fillets.
6. A tiny built-in profile editor for quick rectangles, circles, and slots without leaving the app.

---

## 12. Risks, honestly stated

| Risk | Severity | Mitigation |
|---|---|---|
| OCC viewport widget doesn't come up on the target platform | **High** — it blocks everything | M0 exists precisely to find this out in week 1. Fallback: PyVista + `pyvistaqt`, accepting worse sub-shape selection. Second fallback for early dev: `ocp-vscode`, which runs standalone as a local server. |
| Topological naming breaks references on edit | **High** | §8. Anchor to base faces by default; deterministic feature-edge naming; degrade visibly, never silently. |
| Fillet failures on real artwork | **Medium** — common in practice | §6.4. Bisection to find a working radius; recommend 2D profile filleting for sharp corners. |
| Rebuild too slow on large meshes | **Medium** | manifold3d is fast; the risk is tessellating B-rep for every preview. Cache tessellations; preview with the tool solid only. |
| Real-world SVGs are messier than test files | **Medium** | The §5.5 repair pipeline is not optional polish — it is core. Test against actual customer logos early. |
| DWG support disappoints | **Low** | Pluggable chain, clearly best-effort, never blocks the release. |
| Scope creep toward "a real CAD program" | **Medium** | §1.2. When a feature request arrives, ask whether it serves "put artwork on a part." If not, it goes in §11's later list and probably never ships. |

---

## 13. Getting started

```bash
uv init stamp && cd stamp
uv add cadquery-ocp-novtk build123d PySide6 manifold3d trimesh ocpsvg ezdxf numpy
uv add --dev cadquery-ocp-stubs pytest pytest-qt ruff pyinstaller
```

Two things to do before writing application code:

1. **Prove the viewport.** Copy `occt_widget.py` and `viewer.py` from CQ-editor, port PyQt5 → PySide6,
   and get a STEP file orbiting in a window. If this doesn't work on your machine in a day, stop and
   reconsider the viewer choice before building anything on top of it.
2. **Prove the boolean, both modes.** A script that loads a STEP and an STL, extrudes a hard-coded
   rectangle into each, and writes the results. Time it. Those two numbers set every performance
   expectation in the rest of the project.

Suggested layout:

```
stamp/
  app/
    main.py              # entry point, QApplication
    ui/
      main_window.py
      viewport.py        # the OCC AIS widget
      feature_tree.py
      properties.py
      handles.py         # screen-space manipulation overlay
    core/
      document.py        # Document, BasePart, Feature dataclasses
      rebuild.py         # the rebuild engine
      refs.py            # FaceRef, EdgeSelector resolution
    io/
      part_import.py     # STEP/STL
      profile_import.py  # SVG/DXF/DWG → Profile
      normalize.py       # §5.5
      export.py
    geom/
      solid_ops.py       # OCC path
      mesh_ops.py        # manifold3d path
      tool_solid.py      # profile + placement + operation → TopoDS_Shape
  tests/
    fixtures/            # a handful of real parts and real logos, committed
```

Commit real, ugly test files early — a traced logo with self-intersections, a DXF full of dimension
layers, an STL that isn't quite watertight. The pipeline that handles those is the product; the one
that handles clean files is a demo.

---

## 14. Definition of done for v1

A person who has never used the app can, in under five minutes with no documentation:

1. Open a STEP file of a bracket.
2. Drop an SVG logo onto a flat face.
3. Size it to exactly 40 mm wide, center it, and rotate it 90°.
4. Emboss it 0.8 mm proud with a 0.3 mm fillet on the top edges.
5. Add a second feature that cuts a serial number 0.5 mm deep.
6. Export a STEP that opens cleanly in Fusion or SolidWorks, and an STL that slices without errors.
7. Reopen the project tomorrow, change the emboss to 1.2 mm, and watch it rebuild correctly.

If all seven work on a real part with real artwork, ship it.
