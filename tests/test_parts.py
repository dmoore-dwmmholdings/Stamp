"""Multi-part files: keeping the parts, stamping one, exporting one.

A 3MF from a slicer is nearly always an assembly - a base, a lid, four copies of
a clip.  Stamp used to call ``trimesh.load(force="mesh")``, which flattens all of
that into one mesh before Stamp ever sees it, so there was no way to say which
part a stamp went on, no way to hide the rest, and no way to send one part to the
printer.
"""

from __future__ import annotations

import numpy as np
import pytest

from stamp.core.document import (
    Anchor,
    AnchorKind,
    DepthMode,
    Direction,
    Document,
    Feature,
    Operation,
    OperationKind,
    Placement,
    ProfileRef,
)
from stamp.core.profiles import ProfileCache
from stamp.core.rebuild import RebuildEngine
from stamp.geom import color_split, mesh_regions
from stamp.io.part_import import import_part, manifold_to_trimesh
from stamp.io.profile_import import file_hash


@pytest.fixture
def assembly(fixtures):
    return import_part(fixtures / "assembly.3mf").part


def _stamp_on(document, artwork, part_index, point, plane):
    feature = Feature(
        name="Logo",
        part_index=part_index,
        profile=ProfileRef(source_path=str(artwork), source_hash=file_hash(artwork)),
        placement=Placement(anchor=Anchor(
            kind=AnchorKind.MESH_REGION, mesh_seed=point,
            mesh_tolerance=25.0, plane=plane,
        )),
        operation=Operation(kind=OperationKind.COLOR, depth_mode=DepthMode.BLIND,
                            depth=0.4, direction=Direction.INTO),
    )
    document.add_feature(feature)
    return feature


def _pick_top(part, base):
    """The region a click on the top of *part* would produce."""
    mesh = manifold_to_trimesh(base.runtime)
    return mesh_regions.region_at(
        np.asarray(mesh.vertices), np.asarray(mesh.faces),
        (part.center[0], part.center[1], part.bbox[5] + 50.0), (0.0, 0.0, -1.0),
        tolerance_deg=25.0,
    )


class TestKeepingTheParts:
    def test_an_assembly_arrives_as_its_parts(self, assembly):
        assert [p.name for p in assembly.parts] == ["body", "lid"]
        assert all(p.triangle_count > 0 for p in assembly.parts)
        assert all(p.runtime is not None for p in assembly.parts)

    def test_the_working_geometry_is_still_the_whole_thing(self, assembly):
        """Picking, rebuilding and every boolean are unchanged by this."""
        assert assembly.runtime is not None
        assert assembly.triangle_count == sum(p.triangle_count for p in assembly.parts)

    def test_a_single_part_file_has_no_parts(self, fixtures):
        """One part is not an assembly, and must behave exactly as it always did."""
        part = import_part(fixtures / "bracket.stl").part
        assert part.parts == []

    def test_a_click_lands_on_the_part_it_is_over(self, assembly):
        assert assembly.part_at((0.0, 0.0, 22.0)).name == "lid"
        assert assembly.part_at((0.0, 0.0, 5.0)).name == "body"
        assert assembly.part_at((900.0, 0.0, 0.0)) is None

    def test_parts_survive_a_save_and_reopen(self, assembly):
        from stamp.core.document import BasePart

        again = BasePart.from_dict(assembly.to_dict())
        assert [p.name for p in again.parts] == ["body", "lid"]
        assert [p.visible for p in again.parts] == [True, True]


class TestRebuildingOnePart:
    def test_only_the_stamped_part_is_rebuilt(self, assembly, fixtures):
        """Stamping the lid must not do boolean work on the body.

        This is why doing it properly is cheaper than what it replaced, not
        dearer: an untouched part is passed straight through."""
        document = Document(base=assembly)
        lid = assembly.part_named("lid")
        region = _pick_top(lid, assembly)
        _stamp_on(document, fixtures / "two_color.svg", lid.index, region.point, region.plane)

        result = RebuildEngine(ProfileCache().get).rebuild(document)
        assert result.ok, result.errors
        assert [p.name for p in result.parts] == ["body", "lid"]
        assert result.part(lid.index).rebuilt is True
        assert result.part(assembly.part_named("body").index).rebuilt is False

    def test_the_untouched_part_comes_through_unchanged(self, assembly, fixtures):
        document = Document(base=assembly)
        lid = assembly.part_named("lid")
        body = assembly.part_named("body")
        region = _pick_top(lid, assembly)
        _stamp_on(document, fixtures / "two_color.svg", lid.index, region.point, region.plane)

        result = RebuildEngine(ProfileCache().get).rebuild(document)
        assert result.part(body.index).geometry is body.runtime

    def test_the_whole_thing_is_still_available(self, assembly, fixtures):
        """Every exporter has always been handed one geometry, and still is."""
        document = Document(base=assembly)
        lid = assembly.part_named("lid")
        region = _pick_top(lid, assembly)
        _stamp_on(document, fixtures / "two_color.svg", lid.index, region.point, region.plane)

        result = RebuildEngine(ProfileCache().get).rebuild(document)
        assert result.geometry is not None
        assert result.volume > 0

    def test_a_feature_on_no_particular_part_still_works(self, assembly, fixtures):
        """Everything placed before parts existed says -1, and means the lot."""
        document = Document(base=assembly)
        region = _pick_top(assembly.part_named("lid"), assembly)
        _stamp_on(document, fixtures / "two_color.svg", -1, region.point, region.plane)

        result = RebuildEngine(ProfileCache().get).rebuild(document)
        assert result.ok, result.errors
        assert result.geometry is not None

    def test_a_feature_on_a_part_that_is_gone_says_so(self, assembly, fixtures):
        """Rather than vanishing from the tree with no explanation."""
        document = Document(base=assembly)
        region = _pick_top(assembly.part_named("lid"), assembly)
        _stamp_on(document, fixtures / "two_color.svg", 99, region.point, region.plane)

        result = RebuildEngine(ProfileCache().get).rebuild(document)
        assert not result.ok
        assert any("not in this file" in e for e in result.errors)


class TestExportingOnePart:
    @pytest.fixture
    def stamped(self, assembly, fixtures):
        document = Document(base=assembly)
        lid = assembly.part_named("lid")
        region = _pick_top(lid, assembly)
        _stamp_on(document, fixtures / "two_color.svg", lid.index, region.point, region.plane)
        cache = ProfileCache()
        return document, RebuildEngine(cache.get).rebuild(document), cache, lid

    def test_one_part_carries_only_that_part(self, stamped):
        document, result, cache, lid = stamped
        whole = color_split.split_for_color(document, result, profiles=cache)
        alone = color_split.split_for_color(
            document, result, profiles=cache, part_index=lid.index
        )
        whole_base = next(b for b in whole.bodies if b.role == "base")
        alone_base = next(b for b in alone.bodies if b.role == "base")
        assert alone_base.triangle_count < whole_base.triangle_count

    def test_one_part_still_carries_its_colours(self, stamped):
        document, result, cache, lid = stamped
        alone = color_split.split_for_color(
            document, result, profiles=cache, part_index=lid.index
        )
        assert [b.color for b in alone.bodies if b.role == "feature"] == [
            "#000000", "#ff0000",
        ]

    def test_asking_for_a_part_that_is_not_there_is_an_error(self, stamped):
        document, result, cache, _lid = stamped
        with pytest.raises(color_split.ColorSplitError):
            color_split.split_for_color(
                document, result, profiles=cache, part_index=99
            )

    def test_it_writes_a_readable_3mf(self, stamped, tmp_path):
        """Verified by reading it back, because a slicer is the real consumer."""
        import trimesh

        from stamp.io import export as export_io

        document, result, cache, lid = stamped
        split = color_split.split_for_color(
            document, result, profiles=cache, part_index=lid.index
        )
        out = tmp_path / "lid.3mf"
        export_io.export_3mf(split.bodies, out)

        scene = trimesh.load(str(out))
        names = list(scene.geometry)
        assert "base" in names
        assert sum(1 for n in names if n.startswith("Logo")) == 2
        # Only the lid: the body sat below z=20 and must not be in here.
        lo, _hi = scene.bounds
        assert lo[2] > 15.0
