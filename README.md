<img src="stamp-logo.svg" width="110" align="right" alt="Stamp logo">

# Stamp

[![CI](https://github.com/dmoore-dwmmholdings/Stamp/actions/workflows/ci.yml/badge.svg)](https://github.com/dmoore-dwmmholdings/Stamp/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/dmoore-dwmmholdings/Stamp)](LICENSE)
[![Release](https://img.shields.io/github/v/release/dmoore-dwmmholdings/Stamp?include_prereleases)](https://github.com/dmoore-dwmmholdings/Stamp/releases)
[![Last commit](https://img.shields.io/github/last-commit/dmoore-dwmmholdings/Stamp)](https://github.com/dmoore-dwmmholdings/Stamp/commits/main)
[![Commits](https://img.shields.io/github/commit-activity/m/dmoore-dwmmholdings/Stamp)](https://github.com/dmoore-dwmmholdings/Stamp/commits/main)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Visits](https://hits.sh/github.com/dmoore-dwmmholdings/Stamp.svg?label=visits&color=6e7681)](https://hits.sh/github.com/dmoore-dwmmholdings/Stamp/)

Stamp is a desktop application. It puts 2D artwork onto 3D parts as geometry, not as an image.

You have a 3D part, which is a STEP file or an STL file. You also have a 2D profile,
which is an SVG, DXF or DWG file. The profile can be a logo, a serial number, a slot
pattern or a keep-out shape. Stamp makes that profile into geometry on the part. The
geometry can protrude, or go into the part, or cut through it. The edges can be
circular or have a chamfer. You put the profile in position with the mouse. Then you
type the accurate numbers. Then you export a STEP file for the machine shop and an
STL file for the printer.

## Install

```
uv sync
uv run stamp
```

You can also give a file on the command line:

```
uv run stamp tests/fixtures/bracket.step
uv run stamp my_project.stamp
```

## Workflow

1. Open a part.
2. Add a profile.
3. Click the face for the profile.
4. Set the dimensions and the position in the properties panel.
5. Set the depth and the operation.
6. Add a fillet or a chamfer.
7. Export.

## Text

Stamp makes artwork from a message. A file is not necessary.

1. Open a part.
2. Push Ctrl+T, or click "+ Add text".
3. Click the face for the text.
4. Type the message in the properties panel.
5. Set the font, the em size and the format.
6. Set the depth and the operation.

The em size of the font is in millimeters. Text accepts each operation and each
modifier that a logo accepts. Thus you can cut text into the part, or make it
protrude, and give it a fillet or a chamfer.

These controls set the format: the font, the em size, bold, italic, underline, the
alignment, the wrap width, the letter spacing and the line spacing. Justify is
possible only with a wrap width.

A text feature keeps the message in the project file. Thus no artwork file can go
missing.

## Fillets and chamfers

Stamp puts the fillet on all the edges of the selection in one operation. One edge
that cannot accept the radius stops the operation for all of them. Thus the radius
must suit the smallest detail of the artwork.

If the radius is too large, Stamp finds a radius that works. The message names it,
and a button adjacent to the value puts it in.

Detailed artwork has small edges. Thus it accepts only a small radius. To get a
larger radius, make the artwork larger or make it more simple.

A chamfer operates in the same manner. The distance has the same limit as the
radius.

## The two types of part

A STEP file is a solid with accurate surfaces. An STL file is a set of triangles.
Stamp does not mix the two.

| Input | Boolean engine | Blend into the part | Export |
|---|---|---|---|
| STEP, IGES, BREP | OpenCascade | Yes | STEP and STL |
| STL, 3MF, OBJ | manifold3d | No | STL only |

The tool solid is always a B-rep, in the two conditions. Thus a fillet on the top
edge of a logo that protrudes operates on an STL part. A blend into the surface
around it is different. It is only possible with accurate surfaces.

If you start with an STL, you get an STL. Stamp does not make surfaces that it does
not have. If a STEP file is necessary for the shop, start with a STEP file.

## What Stamp does not do

- Full parametric sketches. Profiles come from files.
- Assemblies, multiple bodies, materials.
- Changes to the initial geometry of the part.
- Toolpaths, drawings, GD&T.
- Cloud, accounts, plugins.

## Files

| Path | Content |
|---|---|
| `src/stamp/core/` | The document, the anchors and the rebuild engine. |
| `src/stamp/io/` | Import, normalization, export and the project archive. |
| `src/stamp/geom/` | The tool solid, the OCC path and the mesh path. |
| `src/stamp/ui/` | The window, the viewport, the tree and the panel. |
| `tests/` | The test suite and the fixtures. |
| `packaging/` | The PyInstaller specification. |

A `.stamp` project is a zip archive. It holds a manifest, a copy of the part, a copy
of each profile, and a thumbnail. It holds no geometry, because all of it rebuilds
from the sources. You can open it with any unzip tool.

## Reports

Stamp writes a log at each start. On Windows the log is in
`%LOCALAPPDATA%\Stamp\logs\stamp.log`.

If Stamp stops without warning, the next start shows a prompt. You can also click
"Report a crash" or "Report a bug" at any time.

Stamp writes the email for you. The email holds the details, the machine and the
last lines of the log. Examine the email, then send it.

Stamp sends no report on its own. An email link cannot carry a file, thus a full
copy of the report goes to a file. The email gives the name of that file.

## The Windows installer

```
python -m uv run python packaging/make_icons.py
python -m uv run pyinstaller packaging/stamp.spec --noconfirm --distpath build/dist --workpath build/work
ISCC.exe packaging/stamp.iss
```

The result is one file in `packaging/dist/`. It installs for one user, thus no
administrator password is necessary. It makes a shortcut in the Start menu, and it
can open `.stamp` files.

## Development

```
uv run pytest
uv run ruff check src tests
uv run python tests/make_fixtures.py
uv run pyinstaller packaging/stamp.spec --noconfirm
```

Each push to GitHub starts the lint tool and the test suite on Windows and on Linux.
The file `.github/workflows/ci.yml` controls this. The tests that show a window do
not run there, because the machines have no display.

The file `PROGRESS.md` records the condition of each milestone and the results that
are important to keep.

## Documentation rules

All prose in this repository obeys ASD-STE100 Simplified Technical English. The file
`ste-glossary.txt` holds the technical nouns and technical verbs of the project.

## License

The MIT license applies to Stamp. Refer to the file [`LICENSE`](LICENSE).
