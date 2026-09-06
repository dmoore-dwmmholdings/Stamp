"""Artwork components and their colours - spec §5.5, §9.

An SVG is grouped by fill colour, so a piece of the drawing can be singled out
and printed in a colour of its own.  Resolving the pieces separately is also
what stopped a filled backdrop from swallowing the artwork.
"""

from __future__ import annotations

import pytest

from stamp.core.document import (
    Anchor,
    AnchorKind,
    DepthMode,
    Direction,
    Document,
    FaceRef,
    Feature,
    Operation,
    OperationKind,
    Placement,
    ProfileRef,
)
from stamp.core.profiles import ProfileCache
from stamp.core.rebuild import RebuildEngine
from stamp.io.profile_import import ImportOptions, file_hash, import_profile


@pytest.fixture
def top_face():
    return FaceRef(point=(40.0, 20.0, 14.0), normal=(0.0, 0.0, 1.0), surface_type="plane")


def a_stamp(source, face, colors=None) -> Feature:
    feature = Feature(
        name="Logo",
        profile=ProfileRef(source_path=str(source), source_hash=file_hash(source)),
        placement=Placement(anchor=Anchor(kind=AnchorKind.FACE, face_ref=face)),
        operation=Operation(
            kind=OperationKind.COLOR, depth_mode=DepthMode.BLIND, depth=0.2,
            direction=Direction.INTO,
        ),
    )
    feature.component_colors = dict(colors or {})
    return feature


class TestComponents:
    def test_an_svg_divides_by_fill_colour(self, fixtures):
        profile = import_profile(fixtures / "two_color.svg").profile
        assert [c.key for c in profile.components] == ["#000000", "#ff0000"]
        assert [c.label for c in profile.components] == ["Black", "Red"]
        assert all(profile.faces_of(c.key) for c in profile.components)

    def test_one_colour_stays_one_component(self, fixtures):
        """The single-component path is the old behaviour and must not change."""
        profile = import_profile(fixtures / "logo.svg").profile
        assert len(profile.components) == 1
        assert profile.faces_of(profile.components[0].key) == profile.faces

    def test_a_component_knows_how_many_shapes_it_holds(self, fixtures):
        profile = import_profile(fixtures / "two_color.svg").profile
        assert [c.element_count for c in profile.components] == [1, 1]


class TestTheBackgroundLayer:
    """The commonest reason an SVG arrives looking like a plain rectangle."""

    def test_a_filled_backdrop_is_left_out(self, fixtures):
        plain = import_profile(fixtures / "two_color.svg").profile
        result = import_profile(fixtures / "background.svg")

        assert result.dropped_background is not None
        assert result.dropped_background.key == "#ffffff"
        # What is left is the artwork, at the artwork's own size - not the page.
        assert result.profile.width == pytest.approx(plain.width)
        assert result.profile.height == pytest.approx(plain.height)
        assert [c.key for c in result.profile.components] == ["#000000", "#ff0000"]

    def test_it_says_so_rather_than_silently_dropping_it(self, fixtures):
        result = import_profile(fixtures / "background.svg")
        message = " ".join(str(i) for i in result.profile.issues)
        assert "backdrop" in message
        assert "Keep the background layer" in message

    def test_keeping_it_puts_it_back(self, fixtures):
        result = import_profile(
            fixtures / "background.svg", ImportOptions(keep_background=True)
        )
        assert result.dropped_background is None
        assert len(result.profile.components) == 3
        assert result.profile.width == pytest.approx(40.0)

    def test_a_backdrop_the_drawing_is_cut_out_of_is_still_a_backdrop(self, fixtures):
        """Traced logos arrive like this: one path, evenodd, the artwork as
        subpaths, and the artwork drawn again on top in its own colours.

        Its material is the page *minus* the drawing - 80% of its own box here,
        under the 95% a backdrop is recognised by - so it used to survive, and
        the profile came out as a solid slab the size of the whole image."""
        result = import_profile(fixtures / "knockout.svg")

        assert result.dropped_background is not None
        assert result.dropped_background.key == "#ffffff"
        assert [c.key for c in result.profile.components] == ["#000000", "#ff0000"]
        # The artwork's own size, 8..32 by 5..15, not the 40x20 page.
        assert result.profile.width == pytest.approx(24.0)
        assert result.profile.height == pytest.approx(10.0)

    def test_a_border_is_not_a_backdrop(self, fixtures):
        """It contains everything and its box is the whole drawing - but it is
        hollow, so it covers a fraction of that box and must survive."""
        result = import_profile(fixtures / "bordered.svg")
        assert result.dropped_background is None
        assert [c.key for c in result.profile.components] == ["#0000ff", "#000000"]

    def test_the_option_is_part_of_the_cache_key(self):
        """Keeping the backdrop changes the geometry, so it cannot hit the cache."""
        kept = ProfileRef(source_path="a.svg", source_hash="h", keep_background=True)
        dropped = ProfileRef(source_path="a.svg", source_hash="h")
        assert kept.cache_key != dropped.cache_key


class TestPaintOrder:
    @staticmethod
    def _area(face) -> float:
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps

        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        return props.Mass()

    def test_a_later_element_is_taken_out_of_an_earlier_one(self, fixtures):
        """Whatever the source draws last is on top.

        So the components never overlap, and a body divided between them is
        divided rather than counted twice.  A blanket union across the whole
        profile is what made a backdrop swallow the drawing.
        """
        profile = import_profile(
            fixtures / "background.svg", ImportOptions(keep_background=True)
        ).profile
        backdrop = profile.faces_of("#ffffff")
        assert len(backdrop) == 1

        page = 40.0 * 20.0
        circle = 3.14159 * 6.0 * 6.0
        square = 10.0 * 10.0
        assert self._area(backdrop[0]) == pytest.approx(page - circle - square, rel=0.02)

    def test_the_pieces_on_top_keep_their_whole_area(self, fixtures):
        profile = import_profile(
            fixtures / "background.svg", ImportOptions(keep_background=True)
        ).profile
        assert self._area(profile.faces_of("#ff0000")[0]) == pytest.approx(100.0, rel=0.01)


class TestColorsOnAFeature:
    def test_a_feature_starts_with_no_colours_of_its_own(self, fixtures, top_face):
        feature = a_stamp(fixtures / "two_color.svg", top_face)
        assert feature.component_colors == {}
        assert feature.color_for("#000000", "#ffffff") == "#ffffff"

    def test_a_colour_survives_a_round_trip(self, fixtures, top_face):
        feature = a_stamp(fixtures / "two_color.svg", top_face, {"#ff0000": "#e4002b"})
        again = Feature.from_dict(feature.to_dict())
        assert again.component_colors == {"#ff0000": "#e4002b"}
        assert again.color_for("#ff0000", "#000000") == "#e4002b"

    def test_a_split_gives_one_body_per_colour(self, bracket_step, fixtures, top_face):
        from stamp.geom import color_split

        document = Document(base=bracket_step)
        document.add_feature(
            a_stamp(fixtures / "two_color.svg", top_face,
                    {"#000000": "#101010", "#ff0000": "#e4002b"})
        )
        cache = ProfileCache()
        result = RebuildEngine(cache.get).rebuild(document)
        assert result.ok

        split = color_split.split_for_color(document, result, profiles=cache)
        assert [b.role for b in split.bodies] == ["base", "feature", "feature"]
        assert [b.color for b in split.bodies[1:]] == ["#101010", "#e4002b"]
        assert all(b.triangle_count > 0 for b in split.bodies)

    def test_the_artwork_own_colours_are_the_default(self, bracket_step, fixtures, top_face):
        """A two-colour logo arrives as two filaments without being asked to.

        The colours are right there in the file.  Requiring the user to assign
        them first meant a green-and-black logo exported as one lump, which is
        never what anybody drew."""
        from stamp.geom import color_split

        document = Document(base=bracket_step)
        document.add_feature(a_stamp(fixtures / "two_color.svg", top_face))  # nothing assigned
        cache = ProfileCache()
        result = RebuildEngine(cache.get).rebuild(document)
        assert result.ok

        split = color_split.split_for_color(document, result, profiles=cache)
        assert [b.role for b in split.bodies] == ["base", "feature", "feature"]
        assert [b.color for b in split.bodies[1:]] == ["#000000", "#ff0000"]

    def test_what_the_user_chose_still_wins(self, bracket_step, fixtures, top_face):
        from stamp.geom import color_split

        document = Document(base=bracket_step)
        document.add_feature(
            a_stamp(fixtures / "two_color.svg", top_face, {"#ff0000": "#e4002b"})
        )
        cache = ProfileCache()
        result = RebuildEngine(cache.get).rebuild(document)
        split = color_split.split_for_color(document, result, profiles=cache)
        # The one they named, and the source colour for the one they did not.
        assert [b.color for b in split.bodies[1:]] == ["#000000", "#e4002b"]

    def test_a_source_with_no_colours_is_still_one_body(self, bracket_step, fixtures, top_face):
        """A DXF has no notion of colour, so there is nothing to fall back to."""
        from stamp.geom.color_split import divides_by_color
        from stamp.io.profile_import import import_profile

        profile = import_profile(fixtures / "profile.dxf").profile
        feature = a_stamp(fixtures / "profile.dxf", top_face)
        assert not divides_by_color(profile, feature)

    def test_the_preview_and_the_export_agree_on_the_colours(self, fixtures, top_face):
        """Two callers, one rule.  A preview that lies about the colours is
        worse than a preview with no colours in it."""
        from stamp.geom.color_split import effective_colors
        from stamp.io.profile_import import import_profile

        profile = import_profile(fixtures / "two_color.svg").profile
        feature = a_stamp(fixtures / "two_color.svg", top_face, {"#ff0000": "#e4002b"})
        assert effective_colors(profile, feature) == {
            "#000000": "#000000", "#ff0000": "#e4002b",
        }

    def test_wrapped_artwork_says_it_cannot_be_split(self, bracket_step, fixtures, top_face):
        """The masks are flat prisms; a wrapped footprint is bent round the face."""
        from stamp.core.document import PlacementMode
        from stamp.geom import color_split

        document = Document(base=bracket_step)
        feature = a_stamp(fixtures / "two_color.svg", top_face,
                          {"#000000": "#101010", "#ff0000": "#e4002b"})
        feature.placement.mode = PlacementMode.WRAP
        document.add_feature(feature)

        cache = ProfileCache()
        result = RebuildEngine(cache.get).rebuild(document)
        split = color_split.split_for_color(document, result, profiles=cache)
        if any(b.role == "feature" for b in split.bodies):
            assert sum(b.role == "feature" for b in split.bodies) == 1
            assert any("cannot be split by colour" in w for w in split.warnings)

    def test_one_colour_is_still_one_body(self, bracket_step, fixtures, top_face):
        """Saying the same thing twice is not a split."""
        from stamp.geom import color_split

        document = Document(base=bracket_step)
        document.add_feature(
            a_stamp(fixtures / "two_color.svg", top_face,
                    {"#000000": "#101010", "#ff0000": "#101010"})
        )
        cache = ProfileCache()
        result = RebuildEngine(cache.get).rebuild(document)
        split = color_split.split_for_color(document, result, profiles=cache)
        assert [b.role for b in split.bodies] == ["base", "feature"]


class TestTheThreeMfPalette:
    def _bodies(self, colors):
        from stamp.geom.color_split import ColorBody

        out = [ColorBody(name="base", role="base", vertices=[(0, 0, 0)], triangles=[(0, 0, 0)])]
        for index, color in enumerate(colors):
            out.append(
                ColorBody(
                    name=f"f{index}", role="feature", vertices=[(0, 0, 0)],
                    triangles=[(0, 0, 0)], color=color,
                )
            )
        return out

    def _model(self, bodies, tmp_path):
        import zipfile

        from stamp.io import export as export_io

        target = tmp_path / "out.3mf"
        export_io.export_3mf(bodies, target)
        with zipfile.ZipFile(target) as archive:
            return archive.read("3D/3dmodel.model").decode()

    def test_each_colour_gets_its_own_slot(self, tmp_path):
        model = self._model(self._bodies(["#101010", "#e4002b"]), tmp_path)
        group = model[model.index("<m:colorgroup"):model.index("</m:colorgroup>")]
        assert group.count("<m:color ") == 3  # the base plus the two
        assert "#101010FF" in group
        assert "#E4002BFF" in group

    def test_a_colour_used_twice_is_written_once(self, tmp_path):
        model = self._model(self._bodies(["#101010", "#101010"]), tmp_path)
        group = model[model.index("<m:colorgroup"):model.index("</m:colorgroup>")]
        assert group.count("<m:color ") == 2

    def test_a_colour_nothing_uses_is_not_written(self, tmp_path):
        """Every spare entry is a filament the user has to dismiss on the way in."""
        model = self._model(self._bodies(["#101010", "#e4002b"]), tmp_path)
        group = model[model.index("<m:colorgroup"):model.index("</m:colorgroup>")]
        assert "C8A24A" not in group  # the default feature colour, unused here
