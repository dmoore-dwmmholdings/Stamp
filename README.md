<p align="center">
  <img src="stamp-logo.svg" width="140" alt="Stamp logo">
</p>

<h1 align="center">Stamp</h1>

<p align="center">
  <a href="https://github.com/dmoore-dwmmholdings/Stamp/actions/workflows/ci.yml"><img src="https://github.com/dmoore-dwmmholdings/Stamp/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/dmoore-dwmmholdings/Stamp" alt="License"></a>
  <a href="https://github.com/dmoore-dwmmholdings/Stamp/releases"><img src="https://img.shields.io/github/v/release/dmoore-dwmmholdings/Stamp?include_prereleases" alt="Release"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python 3.12+"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff"></a>
  <a href="https://hits.sh/github.com/dmoore-dwmmholdings/Stamp/"><img src="https://hits.sh/github.com/dmoore-dwmmholdings/Stamp.svg?label=visits&color=6e7681" alt="Visits"></a>
</p>

Stamp is a desktop app that puts 2D artwork onto 3D parts as real geometry — not a
decal, not a texture.

You bring a 3D part (STEP or STL) and a 2D profile (SVG, DXF, or DWG): a logo, a
serial number, a slot pattern, a keep-out shape. Stamp turns that profile into
geometry on the part — raised, engraved, or cut clean through, with fillets or
chamfers on the edges if you want them. Drag it into place with the mouse, type
exact numbers where it matters, then export STEP for the machine shop and STL for
the printer.

## Install

Download the latest build for your platform from the
[releases page](https://github.com/dmoore-dwmmholdings/Stamp/releases). On Windows,
run `Stamp-x.y.z-Setup.exe`; it installs for the current user only, so no
administrator password is needed, adds a Start menu shortcut, and registers the
`.stamp` file type. On macOS, open the DMG matching your Mac (`arm64` for Apple
silicon, `x86_64` for Intel) and drag `Stamp.app` onto the Applications folder.
The matching PKG is also available if you prefer macOS Installer to perform the
copy. The app is not notarized, so macOS may require Control-clicking it and
choosing Open on the first launch.

On other platforms, or if you'd rather run from source:

```
uv sync
uv run stamp
```

You can also pass a file on the command line:

```
uv run stamp tests/fixtures/bracket.step
uv run stamp my_project.stamp
```

## Batch stamping

Save a `.stamp` project as the template, then apply it to a CSV of parts from
**Batch stamp** or the command line:

```
uv run stamp batch --template template.stamp --csv jobs.csv --output-dir output --format step
```

The CSV needs `input` and `output` columns.  Other columns substitute into text
features using `{{column}}`, which makes serial-number runs reproducible.  Stamp
stops at the first failed row and writes `stamp-batch-report.json` beside the
output files.

## Export preflight

Every export is checked before Stamp writes a file. Invalid rebuilds and formats
that cannot represent the source part are blocked; smaller manufacturing risks
are presented as warnings so you can make the final call.

## The workflow

1. Open a part.
2. Add a profile.
3. Click the face it belongs on.
4. Dial in the size and position in the properties panel.
5. Pick the depth and the operation.
6. Add a fillet or chamfer if you want one.
7. Export.

## Wrap and patterns

On a solid STEP/BREP part, select a feature and choose **Wrap cylinder/cone** in
the placement panel to make its profile follow a cylindrical or conical face.
Stamp refuses mesh parts, other curved surfaces, and artwork that crosses the
face seam rather than making approximate geometry.

The same panel also turns a seed feature into an editable linear, circular, or
mirror pattern. The pattern remains one feature-tree item: edit its text,
profile, or operation once and every generated instance rebuilds.

## Text

No artwork file? Type it instead.

1. Open a part.
2. Press Ctrl+T, or click "+ Add text".
3. Click the face the text belongs on.
4. Type your text in the properties panel.
5. Pick the font, size, and formatting.
6. Pick the depth and the operation.

Font size is the em size in millimeters. Text supports everything a logo does — cut
it into the part or raise it out, fillet or chamfer the edges. Formatting covers
font, size, bold, italic, underline, alignment, wrap width, letter spacing, and line
spacing (justify needs a wrap width).

A text feature stores the text itself in the project file, so there is no artwork
file to lose.

## QR and Data Matrix

Choose **+ Add code** to create an editable QR or Data Matrix mark directly in
Stamp.  The code is vector geometry: module size, operation, depth, placement,
patterns, and modifiers work exactly as they do for imported artwork.  In a batch
template, code payloads accept `{{column}}` substitutions just like text, making
serialised jobs reproducible.

Keep the code's module size above your project's manufacturing limit and leave a
quiet zone around it. Stamp warns about marks that are likely to be too fine to
read after manufacture, and decodes its generated module grid again before export
warnings are produced.

## Production handoff

**Production proof** writes a standalone PDF with the current viewport image, part
facts, every mark's operation and depth, manufacturing identifiers, and preflight
warnings. Use it as the review sheet before sending a job to production.

**Manufacturing limits** includes starting rulesets for laser engraving, CNC
engraving, embossing, resin printing, and FDM printing. They are conservative
warnings, not CAM instructions; adjust the three limits for your machine.

Select **Inspect** beneath the viewport to see the rebuilt mark's measured
three-dimensional envelope and a line to its nearest host-face edge or hole. The
line turns red when it misses the configured clearance. On cylindrical and conical
faces, the normal colored preview is a curved-face proof: it follows the actual
wrapped solid, not a flat decal.

Replacing a part produces a per-mark revision comparison: each mark is called out
as kept, moved by a measured distance, or needing a new host face. Before a batch
run, Stamp simulates every CSV row to catch substitutions, duplicate names, and
unsafe output paths. Inspection also flags modifier values that may fall below a
practical cutter radius; these are toolpath hints, not generated CAM.

Use **Export job package** (or `stamp package --project job.stamp --output job.zip`)
to create a portable ZIP with the model, self-contained Stamp project, PDF
production summary, preflight JSON, and thumbnail. Feature metadata — identifier,
process, material, color, and notes — appears in the summary.

For repeat work, save a selected feature as a **stamp preset** and insert it into
another project. Presets carry their profile, operation, modifiers, and metadata,
then prompt you to pick the new host face.

## Reference placement

On solid parts, **Align stamp to edge** uses a selected edge as the stamp's origin
and horizontal direction. **Create datum from stamp** saves a named standalone
plane; **Place stamp on datum** reuses it (or the global XY/XZ/YZ planes). These
references are stored as geometry rather than face or edge numbers, so Stamp can
resolve them again after a rebuild or revised part import.

## Fillets and chamfers

A fillet applies to every edge of the selection in one operation, and one edge that
can't take the radius fails it for all of them — so the radius has to suit the
smallest detail in the artwork.

If the radius is too large, Stamp finds the largest one that works, applies it
automatically, and tells you what it did.

Detailed artwork has short edges and can only take a small radius. For a bigger
radius, make the artwork bigger or simpler.

Chamfers work the same way, and the distance has the same limit as the radius.

Modifiers aimed at the edges where a feature meets the part act on the joined
result: at the base of a raised feature they flare outward into the surface, and
on the rim of a pocket they widen the mouth.

## Multi-color printing

"Export 3MF" writes the part as separate bodies: the base, plus one body per
feature. Raised artwork becomes its own solid, and engraved artwork becomes an
inlay that fills the pocket flush with the surface — so a color printer can put
the artwork in a second filament. Through cuts stay open.

Set the colors to the filaments you actually print with. A slicer creates a new
filament for every color it doesn't recognize, so colors that don't match yours
arrive as extra entries you have to change back. Stamp remembers what you chose,
so this is a one-time setup. You can also turn the colors off entirely: the file
then carries no color, no slicer asks anything on the way in, and you pick the
filament for each part yourself, as with any other 3MF.

When colors are on, the file is a standard 3MF. Each body carries its color on every triangle,
through the 3MF materials extension — which is what Bambu Studio's color parser
actually reads. Open it in Bambu Studio or Orca and a color parsing window
appears; the colors map to filament slots in the order they are written, the
base first and the artwork second. Other slicers see a plain multi-object 3MF
and let you assign a material per body.

Bambu Studio reports "The 3mf file has invalid config, load geometry data only"
when it opens the file. That is expected, and the colors still arrive. Bambu
treats a 3MF as its own project only when the file claims `Application` starts
with `BambuStudio-`, and it reads per-triangle colors **only** from files it did
not write (`bbs_3mf.cpp`, the `!m_is_bbl_3mf` branch). A file that avoids the
message would therefore lose its colors, so Stamp writes an honest third-party
3MF and keeps them.

## Updating the part

A part gets revised. "Replace part" swaps in the newer file and leaves the
artwork where you put it — Stamp re-resolves each stamp's anchor against the new
geometry rather than by face number, so moved holes, a thicker plate or new
features elsewhere don't disturb it.

Afterwards it tells you what happened to each stamp: the ones that stayed put,
the ones that followed the face they sit on (a thicker plate carries its
engraving up with the top face), and the ones it could not place. Nothing is
ever deleted — a stamp that cannot be matched keeps what it had and waits for
you to pick a face again.

If the new file was exported from a different origin, Stamp notices the offset
between the two parts and moves the artwork with it.

## The two kinds of part

A STEP file is a solid with exact surfaces. An STL is a bag of triangles. Stamp
never pretends one is the other.

| Input | Boolean engine | Blend into the part | Export |
|---|---|---|---|
| STEP, IGES, BREP | OpenCascade | Yes | STEP and STL |
| STL, 3MF, OBJ | manifold3d | No | STL only |

The tool solid is always a B-rep in both modes, which is why a fillet on the top
edge of a raised logo works even on an STL part. Blending into the surrounding
surface is different — that needs exact surfaces.

STL in means STL out. Stamp won't invent surfaces it doesn't have, so if the shop
needs a STEP file, start from a STEP file.

## What Stamp doesn't do

- Full parametric sketching — profiles come from files.
- Assemblies, multiple bodies, materials.
- Editing the part's original geometry.
- Toolpaths, drawings, GD&T.
- Cloud, accounts, plugins.

## Project layout

| Path | Content |
|---|---|
| `src/stamp/core/` | The document, anchors, and the rebuild engine. |
| `src/stamp/io/` | Import, normalization, export, and the project archive. |
| `src/stamp/geom/` | The tool solid, the OCC path, and the mesh path. |
| `src/stamp/ui/` | The window, viewport, feature tree, and properties panel. |
| `tests/` | The test suite and its fixtures. |
| `packaging/` | The PyInstaller spec and the installer script. |

A `.stamp` project is a plain zip archive: a manifest, a copy of the part, a copy of
each profile, and a thumbnail. No geometry is stored — everything rebuilds from the
sources. Any unzip tool can open one.

## Logs and crash reports

Stamp writes a log on every start. On Windows it lives at
`%LOCALAPPDATA%\Stamp\logs\stamp.log`.

If Stamp crashes, the next start offers to report it, and "Report a crash" /
"Report a bug" are available any time. Stamp drafts the email for you — details,
machine info, the last lines of the log — so all you do is look it over and send it.
Nothing is ever sent automatically. Since a `mailto:` link can't carry an
attachment, the full report also goes to a file, and the email names it.

## Building release applications

```
python -m uv run python packaging/make_icons.py
python -m uv run pyinstaller packaging/stamp.spec --noconfirm --distpath build/dist --workpath build/work
ISCC.exe packaging/stamp.iss
```

On macOS, build the application bundle plus a drag-to-Applications DMG and a
standard Installer package:

```
uv run python packaging/make_icons.py
uv run pyinstaller packaging/stamp.spec --noconfirm --distpath build/dist --workpath build/work
mkdir -p build/dmg
ditto build/dist/Stamp.app build/dmg/Stamp.app
ln -s /Applications build/dmg/Applications
hdiutil create -volname Stamp -srcfolder build/dmg -ov -format UDZO Stamp-x.y.z-macos-arm64.dmg
pkgbuild --component build/dist/Stamp.app --install-location /Applications Stamp-x.y.z-macos-arm64.pkg
```

Use `macos-x86_64` in the filename when building on an Intel Mac. Tagged releases
build and attach the Windows installer plus macOS DMG and PKG installers for Intel
and Apple silicon.

## Development

```
uv run pytest
uv run ruff check src tests
uv run python tests/make_fixtures.py
```

Every push runs lint and the test suite on Windows and Linux via GitHub Actions
(`.github/workflows/ci.yml`). Tests that need a real window and OpenGL are skipped
there — the runners are headless.

`PROGRESS.md` tracks milestone status and records the hard-won findings worth
keeping.

## License

MIT — see [`LICENSE`](LICENSE).
