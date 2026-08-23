"""Generate the committed test fixtures.

Run with:  uv run python tests/make_fixtures.py

The spec asks for real, ugly files.  These are the synthetic baseline; the ugly
ones (traced logos with self-intersections, DXFs full of dimension layers, an STL
that is not quite watertight) are generated here too so the pipeline has something
to fail against without needing customer artwork in the repo.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def make_bracket_step(path: Path) -> None:
    """A plate with a boss and two holes - the standard part for manual testing."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    plate = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 80.0, 40.0, 8.0).Shape()

    boss = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(60, 20, 8), gp_Dir(0, 0, 1)), 9.0, 6.0
    ).Shape()
    shape = BRepAlgoAPI_Fuse(plate, boss).Shape()

    for cx, cy in ((12.0, 12.0), (12.0, 28.0)):
        hole = BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(cx, cy, -1), gp_Dir(0, 0, 1)), 3.0, 12.0
        ).Shape()
        shape = BRepAlgoAPI_Cut(shape, hole).Shape()

    _write_step(shape, path)


def make_plate_step(path: Path) -> None:
    """A bare 60x30x5 plate - the simplest possible solid-mode part."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    _write_step(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 60.0, 30.0, 5.0).Shape(), path)


def make_inch_plate_step(path: Path) -> None:
    """A plate authored in inches, to exercise header-unit scaling on import.

    The solid is built at its true millimetre size and written with the STEP unit set
    to INCH, so the file itself carries 2 x 1 x 0.25 INCH.  A correct import must
    bring it back as 50.8 x 25.4 x 6.35 mm.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 50.8, 25.4, 6.35).Shape()
    Interface_Static.SetCVal_s("write.step.unit", "INCH")
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    writer.Write(str(path))
    Interface_Static.SetCVal_s("write.step.unit", "MM")


def _write_step(shape, path: Path) -> None:
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    Interface_Static.SetCVal_s("write.step.unit", "MM")
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    writer.Write(str(path))


def make_bracket_stl(path: Path) -> None:
    """A watertight mesh part - the same shape as bracket.step, as triangles.

    The two primitives are unioned with manifold3d rather than concatenated, so the
    result is one closed shell and not two overlapping ones.
    """
    import numpy as np
    import trimesh
    from manifold3d import Manifold, Mesh

    def to_manifold(m):
        return Manifold(
            Mesh(
                vert_properties=np.asarray(m.vertices, dtype=np.float32),
                tri_verts=np.asarray(m.faces, dtype=np.uint32),
            )
        )

    plate = trimesh.creation.box(extents=(80.0, 40.0, 8.0))
    plate.apply_translation((40.0, 20.0, 4.0))
    boss = trimesh.creation.cylinder(radius=9.0, height=6.0, sections=48)
    boss.apply_translation((60.0, 20.0, 11.0))

    result = to_manifold(plate) + to_manifold(boss)
    for cx, cy in ((12.0, 12.0), (12.0, 28.0)):
        hole = trimesh.creation.cylinder(radius=3.0, height=12.0, sections=32)
        hole.apply_translation((cx, cy, 5.0))
        result = result - to_manifold(hole)

    out = result.to_mesh()
    trimesh.Trimesh(
        vertices=np.asarray(out.vert_properties)[:, :3],
        faces=np.asarray(out.tri_verts),
    ).export(path)


def make_leaky_stl(path: Path) -> None:
    """A mesh with a hole punched in it, for the not-watertight warning path."""
    import numpy as np
    import trimesh

    mesh = trimesh.creation.box(extents=(40.0, 20.0, 6.0))
    faces = np.delete(mesh.faces, [0, 1], axis=0)
    trimesh.Trimesh(vertices=mesh.vertices, faces=faces, process=False).export(path)


SVG_LOGO = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="20mm"
     viewBox="0 0 40 20">
  <path d="M 2 2 L 38 2 L 38 18 L 2 18 Z
           M 6 6 L 6 14 L 14 14 L 14 6 Z" fill="#202020" fill-rule="evenodd"/>
  <circle cx="26" cy="10" r="5" fill="#202020"/>
</svg>
"""

SVG_STROKE_ONLY = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="30mm" height="10mm"
     viewBox="0 0 30 10">
  <path d="M 2 5 L 28 5" fill="none" stroke="#000000" stroke-width="1"/>
</svg>
"""

SVG_LIVE_TEXT = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="60mm" height="15mm"
     viewBox="0 0 60 15">
  <text x="2" y="12" font-family="Arial" font-size="12">SN-0042</text>
</svg>
"""

SVG_UNITLESS = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">
  <rect x="10" y="10" width="180" height="80" fill="#000"/>
</svg>
"""

SVG_SELF_INTERSECTING = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="30mm" height="30mm"
     viewBox="0 0 30 30">
  <path d="M 2 2 L 28 28 L 28 2 L 2 28 Z" fill="#000"/>
</svg>
"""


def make_svgs() -> None:
    (FIXTURES / "logo.svg").write_text(SVG_LOGO, encoding="utf-8")
    (FIXTURES / "stroke_only.svg").write_text(SVG_STROKE_ONLY, encoding="utf-8")
    (FIXTURES / "live_text.svg").write_text(SVG_LIVE_TEXT, encoding="utf-8")
    (FIXTURES / "unitless.svg").write_text(SVG_UNITLESS, encoding="utf-8")
    (FIXTURES / "self_intersecting.svg").write_text(SVG_SELF_INTERSECTING, encoding="utf-8")


def make_dxf(path: Path) -> None:
    """A DXF with real geometry plus the construction junk a real one carries."""
    import ezdxf

    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4  # millimetres
    msp = doc.modelspace()

    doc.layers.add("PROFILE", color=7)
    doc.layers.add("DIMENSIONS", color=1)
    doc.layers.add("CONSTRUCTION", color=8)

    msp.add_lwpolyline(
        [(0, 0), (40, 0), (40, 20, 0.5), (0, 20)],
        format="xyb",
        close=True,
        dxfattribs={"layer": "PROFILE"},
    )
    msp.add_circle((10, 10), 4, dxfattribs={"layer": "PROFILE"})
    msp.add_ellipse((30, 10), major_axis=(5, 0), ratio=0.5, dxfattribs={"layer": "PROFILE"})

    msp.add_line((-10, -10), (60, -10), dxfattribs={"layer": "CONSTRUCTION"})
    msp.add_linear_dim(base=(0, -6), p1=(0, 0), p2=(40, 0), dxfattribs={"layer": "DIMENSIONS"})
    msp.add_text("40.00", dxfattribs={"layer": "DIMENSIONS", "height": 2.5}).set_placement((18, -5))

    doc.saveas(path)


def make_open_loop_dxf(path: Path) -> None:
    """A profile with a visible gap, for the open-loop repair path."""
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_line((0, 0), (20, 0))
    msp.add_line((20, 0), (20, 10))
    msp.add_line((20, 10), (0, 10))
    msp.add_line((0, 10), (0, 0.35))  # 0.35 mm gap - too wide for the default 0.01 tol
    doc.saveas(path)


def make_no_units_dxf(path: Path) -> None:
    """$INSUNITS unset, so the importer must prompt."""
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 0
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (25, 0), (25, 25), (0, 25)], close=True)
    doc.saveas(path)


def make_serial_dxf(path: Path) -> None:
    """Five disjoint outer loops - one feature, five faces."""
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for i in range(5):
        x = i * 7.0
        msp.add_lwpolyline(
            [(x, 0), (x + 5, 0), (x + 5, 10), (x, 10)], close=True
        )
        if i % 2 == 0:  # an interior hole in every other glyph
            msp.add_circle((x + 2.5, 5), 1.2)
    doc.saveas(path)


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    make_plate_step(FIXTURES / "plate.step")
    make_bracket_step(FIXTURES / "bracket.step")
    make_inch_plate_step(FIXTURES / "plate_inch.step")
    make_bracket_stl(FIXTURES / "bracket.stl")
    make_leaky_stl(FIXTURES / "leaky.stl")
    make_svgs()
    make_dxf(FIXTURES / "profile.dxf")
    make_open_loop_dxf(FIXTURES / "open_loop.dxf")
    make_no_units_dxf(FIXTURES / "no_units.dxf")
    make_serial_dxf(FIXTURES / "serial.dxf")
    for f in sorted(FIXTURES.iterdir()):
        print(f"{f.name:>26}  {f.stat().st_size:>8,} bytes")


if __name__ == "__main__":
    main()
