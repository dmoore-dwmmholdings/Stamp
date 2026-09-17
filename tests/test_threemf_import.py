"""The 3MF reader - spec §5.2.

Each package here is written by hand in the layout a slicer uses, because that
layout is what trimesh read wrongly: Bambu Studio put a cylinder in its own file
and placed it six times, and it arrived as six stacked copies in each place, so a
closed part came in open.  Every test is one way the object tree can be shaped.
"""

from __future__ import annotations

import zipfile

import numpy as np
import pytest

from stamp.io.part_import import PartImportError, import_part
from stamp.io.threemf_import import ThreeMFError, load_3mf

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"

#: A closed 10 mm cube with its corner at the origin, wound outward.
CUBE_VERTS = [
    (0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
    (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10),
]
CUBE_TRIS = [
    (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
    (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
]


def mesh_xml(verts=CUBE_VERTS, tris=CUBE_TRIS, offset=(0, 0, 0)) -> str:
    vertices = "".join(
        f'<vertex x="{x + offset[0]}" y="{y + offset[1]}" z="{z + offset[2]}"/>'
        for x, y, z in verts
    )
    triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return f"<mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh>"


def model_xml(resources: str, build: str = "", unit: str = "millimeter") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="{unit}" xmlns="{CORE}" xmlns:p="{PROD}" requiredextensions="p">'
        f"<resources>{resources}</resources><build>{build}</build></model>"
    )


def translate(x=0.0, y=0.0, z=0.0, scale=1.0, mirror_x=False) -> str:
    sx = -scale if mirror_x else scale
    return f"{sx} 0 0 0 {scale} 0 0 0 {scale} {x} {y} {z}"


CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    '<Default Extension="config" ContentType="text/xml"/>'
    "</Types>"
)


def write_package(path, parts: dict[str, str], root="3D/3dmodel.model"):
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Target="/{root}" Id="rel0"'
        ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", rels)
        for name, text in parts.items():
            archive.writestr(name, text)
        # The root model's own relationships name the other model parts, as a
        # slicer writes them - lib3mf will not follow a path without one.
        others = [n for n in parts if n.endswith(".model") and n != root]
        if others:
            targets = "".join(
                f'<Relationship Target="/{n}" Id="rel{i + 1}"'
                ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
                for i, n in enumerate(others)
            )
            folder, _, leaf = root.rpartition("/")
            archive.writestr(
                f"{folder}/_rels/{leaf}.rels",
                '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships'
                ' xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f"{targets}</Relationships>",
            )
    return path


def bodies(path):
    return load_3mf(path).dump()


class TestSharedObjects:
    def test_an_object_placed_six_times_is_one_copy_in_each_place(self, tmp_path):
        """The Bambu Studio layout that came in as open meshes."""
        cylinder = model_xml(f'<object id="9" type="model">{mesh_xml()}</object>')
        wrappers = "".join(
            f'<object id="{10 + i}" type="model"><components>'
            f'<component p:path="/3D/Objects/object_7.model" objectid="9"/>'
            "</components></object>"
            for i in range(6)
        )
        items = "".join(
            f'<item objectid="{10 + i}" transform="{translate(x=30 * i)}"/>' for i in range(6)
        )
        path = write_package(tmp_path / "shared.3mf", {
            "3D/3dmodel.model": model_xml(wrappers, items),
            "3D/Objects/object_7.model": cylinder,
        })

        meshes = bodies(path)
        assert len(meshes) == 6
        for i, mesh in enumerate(meshes):
            assert len(mesh.faces) == 12
            assert mesh.is_watertight
            assert mesh.volume == pytest.approx(1000.0)
            assert mesh.bounds[0][0] == pytest.approx(30 * i)

    def test_the_whole_part_imports_watertight(self, tmp_path):
        cube = model_xml(f'<object id="1" type="model">{mesh_xml()}</object>')
        wrappers = "".join(
            f'<object id="{2 + i}" type="model"><components>'
            f'<component p:path="/3D/Objects/cube.model" objectid="1"/>'
            "</components></object>"
            for i in range(3)
        )
        items = "".join(
            f'<item objectid="{2 + i}" transform="{translate(x=20 * i)}"/>' for i in range(3)
        )
        path = write_package(tmp_path / "part.3mf", {
            "3D/3dmodel.model": model_xml(wrappers, items),
            "3D/Objects/cube.model": cube,
        })

        part = import_part(path).part
        assert part.watertight
        assert part.volume == pytest.approx(3000.0)
        assert [p.triangle_count for p in part.parts] == [12, 12, 12]


class TestObjectIdentity:
    def test_ids_are_scoped_to_their_own_file(self, tmp_path):
        """Every Bambu object file numbers from 1, and so may the root model."""
        big = mesh_xml(verts=[(2 * x, 2 * y, 2 * z) for x, y, z in CUBE_VERTS])
        path = write_package(tmp_path / "ids.3mf", {
            "3D/3dmodel.model": model_xml(
                f'<object id="1" type="model">{big}</object>'
                '<object id="2" type="model"><components>'
                '<component p:path="/3D/Objects/a.model" objectid="1"/>'
                "</components></object>",
                '<item objectid="2"/>',
            ),
            "3D/Objects/a.model": model_xml(f'<object id="1" type="model">{mesh_xml()}</object>'),
        })
        (mesh,) = bodies(path)
        assert mesh.volume == pytest.approx(1000.0)

    def test_a_component_takes_only_the_object_it_names(self, tmp_path):
        path = write_package(tmp_path / "two.3mf", {
            "3D/3dmodel.model": model_xml(
                '<object id="5" type="model"><components>'
                '<component p:path="/3D/Objects/pair.model" objectid="2"/>'
                "</components></object>",
                '<item objectid="5"/>',
            ),
            "3D/Objects/pair.model": model_xml(
                f'<object id="1" type="model">{mesh_xml(offset=(100, 0, 0))}</object>'
                f'<object id="2" type="model">{mesh_xml()}</object>'
            ),
        })
        (mesh,) = bodies(path)
        assert len(mesh.faces) == 12
        assert mesh.bounds[1][0] == pytest.approx(10.0)

    def test_a_component_without_a_path_stays_in_its_own_file(self, tmp_path):
        path = write_package(tmp_path / "local.3mf", {
            "3D/3dmodel.model": model_xml(
                '<object id="3" type="model"><components>'
                '<component p:path="/3D/Objects/nest.model" objectid="2"/>'
                "</components></object>",
                '<item objectid="3"/>',
            ),
            "3D/Objects/nest.model": model_xml(
                f'<object id="1" type="model">{mesh_xml()}</object>'
                '<object id="2" type="model"><components>'
                '<component objectid="1"/></components></object>'
            ),
        })
        (mesh,) = bodies(path)
        assert mesh.volume == pytest.approx(1000.0)

    def test_an_unknown_object_is_refused_by_name(self, tmp_path):
        path = write_package(tmp_path / "missing.3mf", {
            "3D/3dmodel.model": model_xml(
                '<object id="3" type="model"><components>'
                '<component objectid="42"/></components></object>',
                '<item objectid="3"/>',
            ),
        })
        with pytest.raises(PartImportError, match="42"):
            import_part(path)

    def test_a_component_loop_is_refused(self, tmp_path):
        path = write_package(tmp_path / "loop.3mf", {
            "3D/3dmodel.model": model_xml(
                '<object id="1" type="model"><components><component objectid="2"/></components></object>'
                '<object id="2" type="model"><components><component objectid="1"/></components></object>',
                '<item objectid="1"/>',
            ),
        })
        with pytest.raises(ThreeMFError, match="loop"):
            load_3mf(path)

    def test_the_root_model_is_found_through_the_relationship(self, tmp_path):
        path = write_package(
            tmp_path / "renamed.3mf",
            {"3D/Main.model": model_xml(f'<object id="1">{mesh_xml()}</object>', '<item objectid="1"/>')},
            root="3D/Main.model",
        )
        (mesh,) = bodies(path)
        assert mesh.volume == pytest.approx(1000.0)


class TestTransforms:
    def test_transforms_compose_item_outermost(self, tmp_path):
        """Scale the cube by 2 in the component, then move it by 50 in the item."""
        path = write_package(tmp_path / "compose.3mf", {
            "3D/3dmodel.model": model_xml(
                f'<object id="1">{mesh_xml()}</object>'
                f'<object id="2"><components><component objectid="1" transform="{translate(scale=2)}"/>'
                "</components></object>",
                f'<item objectid="2" transform="{translate(x=50)}"/>',
            ),
        })
        (mesh,) = bodies(path)
        np.testing.assert_allclose(mesh.bounds, [[50, 0, 0], [70, 20, 20]])

    def test_a_mirrored_object_still_faces_out(self, tmp_path):
        path = write_package(tmp_path / "mirror.3mf", {
            "3D/3dmodel.model": model_xml(
                f'<object id="1">{mesh_xml()}</object>',
                f'<item objectid="1" transform="{translate(mirror_x=True)}"/>',
            ),
        })
        (mesh,) = bodies(path)
        assert mesh.volume == pytest.approx(1000.0)


class TestSlicerParts:
    def test_bambu_modifiers_are_not_geometry_and_names_are_kept(self, tmp_path):
        path = write_package(tmp_path / "bambu.3mf", {
            "3D/3dmodel.model": model_xml(
                '<object id="2"><components>'
                '<component p:path="/3D/Objects/o.model" objectid="1"/>'
                '<component p:path="/3D/Objects/o.model" objectid="3"/>'
                "</components></object>",
                '<item objectid="2"/>',
            ),
            "3D/Objects/o.model": model_xml(
                f'<object id="1">{mesh_xml()}</object>'
                f'<object id="3">{mesh_xml(offset=(2, 2, 2))}</object>'
            ),
            "Metadata/model_settings.config": (
                '<?xml version="1.0" encoding="UTF-8"?><config>'
                '<object id="2"><metadata key="name" value="Box - Lid"/>'
                '<part id="1" subtype="normal_part"/>'
                '<part id="3" subtype="modifier_part"/></object></config>'
            ),
        })
        (mesh,) = bodies(path)
        assert len(mesh.faces) == 12
        assert mesh.metadata["name"] == "Box - Lid"

    def test_prusa_modifier_volumes_are_not_geometry(self, tmp_path):
        both = mesh_xml(
            verts=CUBE_VERTS + [(x + 2, y + 2, z + 2) for x, y, z in CUBE_VERTS],
            tris=CUBE_TRIS + [(a + 8, b + 8, c + 8) for a, b, c in CUBE_TRIS],
        )
        path = write_package(tmp_path / "prusa.3mf", {
            "3D/3dmodel.model": model_xml(f'<object id="1">{both}</object>', '<item objectid="1"/>'),
            "Metadata/Slic3r_PE_model.config": (
                '<?xml version="1.0" encoding="UTF-8"?><config><object id="1">'
                '<metadata type="object" key="name" value="Bracket"/>'
                '<volume firstid="0" lastid="11"><metadata type="volume" key="volume_type" value="ModelPart"/></volume>'
                '<volume firstid="12" lastid="23"><metadata type="volume" key="volume_type" value="ParameterModifier"/></volume>'
                "</object></config>"
            ),
        })
        (mesh,) = bodies(path)
        assert mesh.volume == pytest.approx(1000.0)
        assert mesh.metadata["name"] == "Bracket"

    def test_repeated_names_are_numbered(self, tmp_path):
        path = write_package(tmp_path / "names.3mf", {
            "3D/3dmodel.model": model_xml(
                f'<object id="1" name="Clip">{mesh_xml()}</object>',
                f'<item objectid="1"/><item objectid="1" transform="{translate(x=20)}"/>',
            ),
        })
        assert [m.metadata["name"] for m in bodies(path)] == ["Clip", "Clip (2)"]


class TestProductionPaths:
    def test_a_build_item_can_place_an_object_from_another_file(self, tmp_path):
        path = write_package(tmp_path / "itempath.3mf", {
            "3D/3dmodel.model": model_xml(
                "", f'<item objectid="1" p:path="/3D/Objects/c.model" transform="{translate(x=5)}"/>'
            ),
            "3D/Objects/c.model": model_xml(f'<object id="1" name="Knob">{mesh_xml()}</object>'),
        })
        (mesh,) = bodies(path)
        assert mesh.volume == pytest.approx(1000.0)
        assert mesh.bounds[0][0] == pytest.approx(5.0)
        assert mesh.metadata["name"] == "Knob"

    def test_a_child_files_unit_is_ignored(self, tmp_path):
        """lib3mf reads a child's numbers in the root's unit, so Stamp does too.

        Checked against lib3mf 2.5: a 10-unit cube in a child marked inch, under a
        millimetre root, measures 10 mm.
        """
        path = write_package(tmp_path / "units.3mf", {
            "3D/3dmodel.model": model_xml(
                '<object id="2"><components>'
                '<component p:path="/3D/Objects/c.model" objectid="1"/>'
                "</components></object>",
                '<item objectid="2"/>',
            ),
            "3D/Objects/c.model": model_xml(f'<object id="1">{mesh_xml()}</object>', unit="inch"),
        })
        scene = load_3mf(path)
        (mesh,) = scene.dump()
        assert scene.metadata["units"] == "millimeter"
        np.testing.assert_allclose(mesh.bounds, [[0, 0, 0], [10, 10, 10]])


class TestDecompressionLimit:
    def test_a_part_that_expands_past_the_limit_is_refused(self, tmp_path, monkeypatch):
        from stamp.io import threemf_import

        monkeypatch.setattr(threemf_import, "MAX_PART_BYTES", 1024)
        big = mesh_xml(verts=CUBE_VERTS * 40, tris=CUBE_TRIS)
        path = write_package(tmp_path / "bomb.3mf", {
            "3D/3dmodel.model": model_xml(f'<object id="1">{big}</object>', '<item objectid="1"/>'),
        })
        with pytest.raises(ThreeMFError, match="more than"):
            load_3mf(path)

    def test_the_whole_package_shares_one_budget(self, tmp_path, monkeypatch):
        from stamp.io import threemf_import

        root = model_xml(
            '<object id="2"><components>'
            '<component p:path="/3D/Objects/c.model" objectid="1"/>'
            "</components></object>",
            '<item objectid="2"/>',
        )
        child = model_xml(f'<object id="1">{mesh_xml()}</object>')
        monkeypatch.setattr(threemf_import, "MAX_PACKAGE_BYTES", len(root) + len(child) // 2)
        path = write_package(tmp_path / "budget.3mf", {
            "3D/3dmodel.model": root, "3D/Objects/c.model": child,
        })
        with pytest.raises(ThreeMFError, match="more than"):
            load_3mf(path)
