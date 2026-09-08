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


SVG_TWO_COLOR = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="20mm"
     viewBox="0 0 40 20">
  <circle cx="12" cy="10" r="6" fill="#000000"/>
  <rect x="24" y="5" width="10" height="10" fill="#ff0000"/>
</svg>
"""

SVG_BACKGROUND = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="20mm"
     viewBox="0 0 40 20">
  <rect x="0" y="0" width="40" height="20" fill="#ffffff"/>
  <circle cx="12" cy="10" r="6" fill="#000000"/>
  <rect x="24" y="5" width="10" height="10" fill="#ff0000"/>
</svg>
"""

#: A border, not a backdrop.  It surrounds the artwork and its bounding box
#: contains everything, but it leaves the middle empty, so it must survive.
SVG_BORDERED = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="20mm"
     viewBox="0 0 40 20">
  <path d="M 0 0 L 40 0 L 40 20 L 0 20 Z M 2 2 L 2 18 L 38 18 L 38 2 Z"
        fill="#0000ff" fill-rule="evenodd"/>
  <circle cx="20" cy="10" r="5" fill="#000000"/>
</svg>
"""


#: A backdrop the exporter already carved the drawing out of: one path, evenodd,
#: with the artwork as subpaths, and the artwork drawn again on top in its own
#: colours.  Its *material* is the page minus the drawing, so it covers well
#: under the 95% a backdrop is recognised by - and it used to survive as a solid
#: slab the size of the whole image.  Every logo traced from a bitmap looks like
#: this.
SVG_KNOCKOUT = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="20mm"
     viewBox="0 0 40 20">
  <path d="M 0 0 L 40 0 L 40 20 L 0 20 Z
           M 8 5 L 16 5 L 16 15 L 8 15 Z
           M 24 5 L 32 5 L 32 15 L 24 15 Z"
        fill="#ffffff" fill-rule="evenodd"/>
  <rect x="8" y="5" width="8" height="10" fill="#000000"/>
  <rect x="24" y="5" width="8" height="10" fill="#ff0000"/>
</svg>
"""


#: Sizing cases.  ocpsvg applies the width/viewBox viewport transform itself, so
#: what it hands back is millimetres for a document with a viewBox and a
#: physical width and CSS pixels for everything else.  Every one of these used to
#: import at the wrong size except the first, whose viewBox happens to equal its
#: width in millimetres.  Each shape fills its viewBox exactly, so the imported
#: width is the number the file says it is.
SVG_MM_PX_VIEWBOX = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm"
     viewBox="0 0 283.465 141.7325">
  <rect x="0" y="0" width="283.465" height="141.7325" fill="#000"/>
</svg>
"""

SVG_MM_LARGE_VIEWBOX = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm"
     viewBox="0 0 1000 500">
  <rect x="0" y="0" width="1000" height="500" fill="#000"/>
</svg>
"""

SVG_INCH_VIEWBOX = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="2in" height="1in" viewBox="0 0 192 96">
  <rect x="0" y="0" width="192" height="96" fill="#000"/>
</svg>
"""

SVG_POINTS_VIEWBOX = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="144pt" height="72pt" viewBox="0 0 192 96">
  <rect x="0" y="0" width="192" height="96" fill="#000"/>
</svg>
"""

SVG_PX_VIEWBOX = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 400 200">
  <rect x="0" y="0" width="400" height="200" fill="#000"/>
</svg>
"""

SVG_VIEWBOX_ONLY = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 200">
  <rect x="0" y="0" width="400" height="200" fill="#000"/>
</svg>
"""

#: Neither a width nor a viewBox: bare path coordinates, which are CSS pixels.
SVG_BARE = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg">
  <rect x="0" y="0" width="400" height="200" fill="#000"/>
</svg>
"""

#: A physical width with no viewBox.  One user unit is one CSS pixel whatever the
#: viewport says, which is how every browser and every editor draws it.
SVG_MM_NO_VIEWBOX = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm">
  <rect x="0" y="0" width="100" height="50" fill="#000"/>
</svg>
"""

#: A group transform inside a scaled viewport - the shape ocpsvg has to place,
#: and the case a viewBox ratio applied afterwards gets most badly wrong.
SVG_NESTED_TRANSFORM = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm"
     viewBox="0 0 1000 500">
  <g transform="translate(100,100) scale(2)">
    <rect x="0" y="0" width="100" height="50" fill="#000"/>
  </g>
</svg>
"""

#: Two overlapping squares and a bow tie, all one colour.  Resolving the overlap
#: used to take the bow tie with it - no face, no issue, nothing said.
SVG_CROSSING_WITH_OVERLAP = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="60mm" height="40mm" viewBox="0 0 60 40">
  <path d="M 0 0 L 20 0 L 20 20 L 0 20 Z" fill="#000000"/>
  <path d="M 10 10 L 30 10 L 30 30 L 10 30 Z" fill="#000000"/>
  <path d="M 40 0 L 55 15 L 55 0 L 40 15 Z" fill="#000000"/>
</svg>
"""

#: The same, in two colours, so the per-component resolution path runs instead.
SVG_CROSSING_TWO_COLOR = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="60mm" height="40mm" viewBox="0 0 60 40">
  <path d="M 0 0 L 20 0 L 20 20 L 0 20 Z" fill="#000000"/>
  <path d="M 10 10 L 30 10 L 30 30 L 10 30 Z" fill="#ff0000"/>
  <path d="M 40 0 L 55 15 L 55 0 L 40 15 Z" fill="#000000"/>
</svg>
"""


def make_assembly_3mf(path: Path) -> None:
    """Two separate boxes in one 3MF, which is what a slicer file looks like.

    Stamp used to flatten this into a single mesh on the way in, so there was no
    way to say which part a stamp went on or to export one of them.
    """
    import trimesh

    lid = trimesh.creation.box(extents=(40, 20, 4))
    lid.apply_translation((0, 0, 22))
    body = trimesh.creation.box(extents=(40, 20, 20))
    body.apply_translation((0, 0, 10))
    scene = trimesh.Scene()
    scene.add_geometry(body, geom_name="body")
    scene.add_geometry(lid, geom_name="lid")
    path.write_bytes(scene.export(file_type="3mf"))


SIZING_SVGS = {
    "size_mm_px_viewbox.svg": SVG_MM_PX_VIEWBOX,
    "size_mm_large_viewbox.svg": SVG_MM_LARGE_VIEWBOX,
    "size_inch_viewbox.svg": SVG_INCH_VIEWBOX,
    "size_points_viewbox.svg": SVG_POINTS_VIEWBOX,
    "size_px_viewbox.svg": SVG_PX_VIEWBOX,
    "size_viewbox_only.svg": SVG_VIEWBOX_ONLY,
    "size_bare.svg": SVG_BARE,
    "size_mm_no_viewbox.svg": SVG_MM_NO_VIEWBOX,
    "size_nested_transform.svg": SVG_NESTED_TRANSFORM,
}


def make_svgs() -> None:
    for name, text in SIZING_SVGS.items():
        (FIXTURES / name).write_text(text, encoding="utf-8")
    (FIXTURES / "crossing_with_overlap.svg").write_text(
        SVG_CROSSING_WITH_OVERLAP, encoding="utf-8"
    )
    (FIXTURES / "crossing_two_color.svg").write_text(
        SVG_CROSSING_TWO_COLOR, encoding="utf-8"
    )
    (FIXTURES / "logo.svg").write_text(SVG_LOGO, encoding="utf-8")
    (FIXTURES / "two_color.svg").write_text(SVG_TWO_COLOR, encoding="utf-8")
    (FIXTURES / "background.svg").write_text(SVG_BACKGROUND, encoding="utf-8")
    (FIXTURES / "bordered.svg").write_text(SVG_BORDERED, encoding="utf-8")
    (FIXTURES / "knockout.svg").write_text(SVG_KNOCKOUT, encoding="utf-8")
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


def make_bracket_rev_b(path: Path) -> None:
    """bracket.step, revised the way a real part gets revised.

    The top face - where artwork goes - keeps its plane, so a stamp on it should
    survive.  Everything around it changes: the holes move outward, the boss grows
    and moves, and a rib appears along one edge.
    """
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    plate = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 80.0, 40.0, 8.0).Shape()

    # The boss is bigger and has moved along the plate.
    boss = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(64, 20, 8), gp_Dir(0, 0, 1)), 11.0, 7.0
    ).Shape()
    shape = BRepAlgoAPI_Fuse(plate, boss).Shape()

    # A rib along the far edge, which is new topology the old part never had.
    rib = BRepPrimAPI_MakeBox(gp_Pnt(0, 36.0, 8.0), 80.0, 4.0, 5.0).Shape()
    shape = BRepAlgoAPI_Fuse(shape, rib).Shape()

    # The mounting holes moved outward and grew.
    for cx, cy in ((10.0, 8.0), (10.0, 32.0)):
        hole = BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(cx, cy, -1), gp_Dir(0, 0, 1)), 3.5, 12.0
        ).Shape()
        shape = BRepAlgoAPI_Cut(shape, hole).Shape()

    _write_step(shape, path)


def make_bracket_thicker(path: Path) -> None:
    """bracket.step with the plate 4 mm thicker.

    The top face is still the top face, but it is no longer at z = 8, so a stamp
    on it has to follow the face upward rather than stay at its old height.
    """
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    plate = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 80.0, 40.0, 12.0).Shape()
    boss = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(60, 20, 12), gp_Dir(0, 0, 1)), 9.0, 6.0
    ).Shape()
    shape = BRepAlgoAPI_Fuse(plate, boss).Shape()
    for cx, cy in ((12.0, 12.0), (12.0, 28.0)):
        hole = BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(cx, cy, -1), gp_Dir(0, 0, 1)), 3.0, 16.0
        ).Shape()
        shape = BRepAlgoAPI_Cut(shape, hole).Shape()
    _write_step(shape, path)


def make_bracket_moved(path: Path) -> None:
    """bracket.step exported from a different origin - the same part, moved.

    CAD tools re-export from wherever the model sits, so this is the common case
    that makes every stored point miss by a constant offset.
    """
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    dx, dy, dz = 120.0, -45.0, 30.0
    plate = BRepPrimAPI_MakeBox(gp_Pnt(dx, dy, dz), 80.0, 40.0, 8.0).Shape()
    boss = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(dx + 60, dy + 20, dz + 8), gp_Dir(0, 0, 1)), 9.0, 6.0
    ).Shape()
    shape = BRepAlgoAPI_Fuse(plate, boss).Shape()
    for cx, cy in ((12.0, 12.0), (12.0, 28.0)):
        hole = BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(dx + cx, dy + cy, dz - 1), gp_Dir(0, 0, 1)), 3.0, 12.0
        ).Shape()
        shape = BRepAlgoAPI_Cut(shape, hole).Shape()
    _write_step(shape, path)


def make_bracket_rev_b_stl(path: Path) -> None:
    """The revised bracket as a mesh, for the mesh-mode replacement path."""
    import tempfile

    from stamp.geom import mesh_ops

    with tempfile.TemporaryDirectory() as folder:
        step = Path(folder) / "rev_b.step"
        make_bracket_rev_b(step)
        from stamp.io.part_import import import_part

        part = import_part(step).part
        verts, tris = mesh_ops.triangulate(part.runtime, 0.05)

    import trimesh

    trimesh.Trimesh(vertices=verts, faces=tris, process=False).export(path)


def make_inverted_stl(path: Path) -> None:
    """A closed box with every triangle wound the wrong way round.

    It passes ``is_watertight`` exactly as a good mesh does, so the repair pass
    never looked at it; its volume is negative, which makes it the whole of space
    minus the box, and every boolean on it came out inside out.
    """
    import trimesh

    box = trimesh.creation.box(extents=(40.0, 20.0, 6.0))
    trimesh.Trimesh(
        vertices=box.vertices, faces=box.faces[:, ::-1], process=False
    ).export(path)


def make_blocks_dxf(path: Path) -> None:
    """A drawing whose geometry is all inside block references.

    Two inserts of a pad, one of which inserts a tag block of its own, plus a
    plain circle.  Block references carry no geometry themselves, so a reader
    that does not expand them imports the circle and nothing else - which is what
    Stamp did, without saying so.
    """
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    doc.layers.add("PROFILE", color=7)

    tag = doc.blocks.new(name="TAG")
    tag.add_lwpolyline([(0, 0), (4, 0), (4, 4), (0, 4)], close=True, dxfattribs={"layer": "0"})

    pad = doc.blocks.new(name="PAD")
    pad.add_lwpolyline(
        [(0, 0), (10, 0), (10, 6), (0, 6)], close=True, dxfattribs={"layer": "0"}
    )
    pad.add_blockref("TAG", (12, 0), dxfattribs={"layer": "0"})

    msp = doc.modelspace()
    msp.add_blockref("PAD", (0, 0), dxfattribs={"layer": "PROFILE"})
    msp.add_blockref("PAD", (30, 0), dxfattribs={"layer": "PROFILE"})
    msp.add_circle((60, 3), 3, dxfattribs={"layer": "PROFILE"})
    doc.saveas(path)


def make_ring_bowtie_dxf(path: Path) -> None:
    """A square with a hole wound the same way as its outer, plus a bow tie.

    DXF has no fill rule, so containment alone decides the hole and both loops
    keep the winding they were drawn with.  Handed to the non-zero fill rule as
    drawn, the hole fills in - so "union overlapping loops" used to hand back a
    solid square 100 mm2 too big.
    """
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (30, 0), (30, 30), (0, 30)], close=True)
    msp.add_lwpolyline([(10, 10), (20, 10), (20, 20), (10, 20)], close=True)
    msp.add_lwpolyline([(40, 0), (55, 15), (55, 0), (40, 15)], close=True)
    doc.saveas(path)


def make_inch_3mf(path: Path) -> None:
    """A 2 x 1 x 0.25 inch box in a 3MF that says ``unit="inch"``.

    trimesh reports the unit rather than applying it, so the box used to arrive
    as 2 x 1 x 0.25 *millimetres*.  The XML is written by hand because trimesh's
    3MF exporter has no way to set the attribute.
    """
    import zipfile

    verts = [
        (0, 0, 0), (2, 0, 0), (2, 1, 0), (0, 1, 0),
        (0, 0, 0.25), (2, 0, 0.25), (2, 1, 0.25), (0, 1, 0.25),
    ]
    tris = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    vertices = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)
    triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="inch" xml:lang="en-US"'
        ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        '<resources><object id="1" type="model"><mesh>'
        f"<vertices>{vertices}</vertices><triangles>{triangles}</triangles>"
        "</mesh></object></resources>"
        '<build><item objectid="1"/></build></model>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels"'
        ' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model"'
        ' ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships'
        ' xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel-1"'
        ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("3D/3dmodel.model", model)


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    make_plate_step(FIXTURES / "plate.step")
    make_bracket_step(FIXTURES / "bracket.step")
    make_inch_plate_step(FIXTURES / "plate_inch.step")
    make_bracket_stl(FIXTURES / "bracket.stl")
    make_bracket_rev_b(FIXTURES / "bracket_rev_b.step")
    make_bracket_thicker(FIXTURES / "bracket_thicker.step")
    make_bracket_moved(FIXTURES / "bracket_moved.step")
    make_bracket_rev_b_stl(FIXTURES / "bracket_rev_b.stl")
    make_leaky_stl(FIXTURES / "leaky.stl")
    make_inverted_stl(FIXTURES / "inverted.stl")
    make_assembly_3mf(FIXTURES / "assembly.3mf")
    make_inch_3mf(FIXTURES / "inch_box.3mf")
    make_svgs()
    make_dxf(FIXTURES / "profile.dxf")
    make_open_loop_dxf(FIXTURES / "open_loop.dxf")
    make_no_units_dxf(FIXTURES / "no_units.dxf")
    make_serial_dxf(FIXTURES / "serial.dxf")
    make_blocks_dxf(FIXTURES / "blocks.dxf")
    make_ring_bowtie_dxf(FIXTURES / "ring_bowtie.dxf")
    for f in sorted(FIXTURES.iterdir()):
        print(f"{f.name:>26}  {f.stat().st_size:>8,} bytes")


if __name__ == "__main__":
    main()
