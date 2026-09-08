"""Regressions in the profile importers - spec §5.3, §5.4, §5.5."""

from __future__ import annotations

import pytest

from stamp.io.normalize import IssueKind
from stamp.io.profile_import import (
    ImportOptions,
    dxf_layers,
    import_profile,
    svg_scale,
)

MM_PER_PX = 25.4 / 96.0


def face_area(profile) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    total = 0.0
    for face in profile.faces:
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        total += props.Mass()
    return total


class TestSvgSizing:
    """ocpsvg applies the viewport transform itself.

    It parses at 25.4 ppi, so a document with a viewBox and a width in mm, cm or
    in comes back in millimetres already; everything else comes back in CSS
    pixels.  Multiplying either of those by the width-to-viewBox ratio a second
    time is what these files are here to catch - only one of them, whose viewBox
    equals its width in millimetres, ever imported at the right size.
    """

    @pytest.mark.parametrize(
        "name,width_mm,ambiguous",
        [
            # A physical width with a pixel viewBox - what Illustrator writes.
            ("size_mm_px_viewbox.svg", 100.0, False),
            ("size_mm_large_viewbox.svg", 100.0, False),
            ("size_inch_viewbox.svg", 50.8, False),
            # svgelements turns pt and pc into pixels before the viewport, so
            # they scale like pixels even though the size is not a guess.
            ("size_points_viewbox.svg", 50.8, False),
            ("size_px_viewbox.svg", 200 * MM_PER_PX, True),
            ("size_viewbox_only.svg", 400 * MM_PER_PX, True),
            ("size_bare.svg", 400 * MM_PER_PX, True),
            # No viewBox: one user unit is one CSS pixel, whatever the width says.
            ("size_mm_no_viewbox.svg", 100 * MM_PER_PX, False),
            # 100 x 50 drawn at scale(2) inside a 1000-unit viewBox of 100 mm.
            ("size_nested_transform.svg", 20.0, False),
        ],
    )
    def test_the_imported_width_is_the_width_the_file_declares(
        self, fixtures, name, width_mm, ambiguous
    ):
        result = import_profile(fixtures / name)
        assert result.profile.width == pytest.approx(width_mm, abs=1e-3)
        assert result.units_ambiguous is ambiguous

    def test_geometry_and_user_unit_scales_are_different_numbers(self, fixtures):
        """A physical document needs no geometry scale but its units are not mm."""
        scale = svg_scale(fixtures / "size_mm_large_viewbox.svg")
        assert scale.geometry == pytest.approx(1.0)
        assert scale.user_unit_mm == pytest.approx(0.1)
        assert scale.unit == "mm"

    def test_a_unit_override_still_says_what_one_user_unit_is(self, fixtures):
        """Overriding to mm makes the 400-unit viewBox 400 mm wide."""
        result = import_profile(
            fixtures / "size_px_viewbox.svg", ImportOptions(unit_override="mm")
        )
        assert result.profile.width == pytest.approx(400.0, abs=1e-3)
        assert not result.units_ambiguous

    def test_extra_scale_multiplies_the_finished_size(self, fixtures):
        result = import_profile(
            fixtures / "size_inch_viewbox.svg", ImportOptions(extra_scale=2.0)
        )
        assert result.profile.width == pytest.approx(101.6, abs=1e-3)


class TestDxfBlocks:
    """A block reference draws its block's geometry, so it has to be expanded."""

    def test_block_references_import_their_geometry(self, fixtures):
        result = import_profile(fixtures / "blocks.dxf")
        # Two pads, each with a tag block of its own, plus a plain circle.
        assert len(result.profile.faces) == 5
        assert result.profile.width == pytest.approx(63.0, abs=0.01)
        assert not result.profile.issues_of(IssueKind.EMPTY)

    def test_a_block_only_drawing_is_not_reported_as_empty(self, fixtures):
        result = import_profile(fixtures / "blocks.dxf", ImportOptions(layers=["PROFILE"]))
        assert len(result.profile.faces) == 5

    def test_the_layer_list_counts_geometry_inside_blocks(self, fixtures):
        # Everything is drawn on layer 0 inside the blocks, which takes the layer
        # the block was inserted on.
        assert dxf_layers(fixtures / "blocks.dxf") == ["PROFILE"]


class TestCrossingLoopsSurviveResolution:
    """A loop that crosses itself must not vanish because something else overlapped."""

    def test_a_bow_tie_is_still_reported_when_two_other_faces_overlap(self, fixtures):
        result = import_profile(fixtures / "crossing_with_overlap.svg")
        issues = result.profile.issues_of(IssueKind.SELF_INTERSECTION)
        assert issues and issues[0].blocking
        assert result.profile.blocked
        # The two overlapping squares still resolve to one silhouette.
        assert face_area(result.profile) == pytest.approx(700.0, rel=1e-3)

    def test_the_same_holds_when_the_overlap_is_between_components(self, fixtures):
        result = import_profile(fixtures / "crossing_two_color.svg")
        assert result.profile.issues_of(IssueKind.SELF_INTERSECTION)
        assert result.profile.blocked

    @pytest.mark.parametrize(
        "name", ["crossing_with_overlap.svg", "crossing_two_color.svg"]
    )
    def test_the_union_repair_can_still_reach_the_carried_loop(self, fixtures, name):
        result = import_profile(fixtures / name, ImportOptions(union_overlapping=True))
        assert not result.profile.blocked
        # 700 for the squares, plus the bow tie's two 56.25 mm2 lobes.
        assert face_area(result.profile) == pytest.approx(812.5, rel=1e-3)


class TestUnionKeepsHoles:
    def test_a_hole_wound_like_its_outer_loop_stays_a_hole(self, fixtures):
        """DXF has no fill rule, so both loops keep the winding they were drawn with.

        Handed to the non-zero rule as drawn, the hole counted as material.
        """
        plain = import_profile(fixtures / "ring_bowtie.dxf")
        assert plain.profile.issues_of(IssueKind.SELF_INTERSECTION)
        assert face_area(plain.profile) == pytest.approx(800.0, rel=1e-3)

        repaired = import_profile(
            fixtures / "ring_bowtie.dxf", ImportOptions(union_overlapping=True)
        )
        assert not repaired.profile.blocked
        assert face_area(repaired.profile) == pytest.approx(912.5, rel=1e-3)


class TestFontSubstitution:
    """Qt never refuses a family it does not have; it quietly uses another."""

    def test_a_font_that_is_not_installed_is_named(self, qapp):
        from stamp.core.document import TextSpec
        from stamp.io.text_profile import build_text_profile

        profile = build_text_profile(
            TextSpec(text="AB", family="Nonesuch Grotesk Fake", size_mm=10.0)
        )
        named = [
            issue
            for issue in profile.issues
            if issue.detail.get("requested_family") == "Nonesuch Grotesk Fake"
        ]
        assert named, "the substitution was not reported"
        assert not named[0].blocking
        assert named[0].detail["actual_family"]
        assert "not installed" in named[0].message

    def test_an_installed_font_raises_nothing(self, qapp):
        from PySide6.QtGui import QFontDatabase

        from stamp.core.document import TextSpec
        from stamp.io.text_profile import build_text_profile

        family = QFontDatabase.families()[0]
        profile = build_text_profile(TextSpec(text="AB", family=family, size_mm=10.0))
        assert not [i for i in profile.issues if i.detail.get("requested_family")]
