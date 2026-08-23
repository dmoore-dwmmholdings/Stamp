"""Profile normalization - spec §5.5 and the §10 error table."""

from __future__ import annotations

import math

import pytest

from stamp.io.normalize import (
    IssueKind,
    point_in_polygon,
    polygon_area,
    self_intersections,
)
from stamp.io.profile_import import (
    ImportOptions,
    import_profile,
    parse_length,
    svg_user_unit_mm,
)


def face_area(profile) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    total = 0.0
    for f in profile.faces:
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, props)
        total += props.Mass()
    return total


class TestPrimitives:
    def test_polygon_area_sign(self):
        ccw = [(0, 0), (1, 0), (1, 1), (0, 1)]
        assert polygon_area(ccw) == pytest.approx(1.0)
        assert polygon_area(list(reversed(ccw))) == pytest.approx(-1.0)

    def test_point_in_polygon(self):
        square = [(0, 0), (10, 0), (10, 10), (0, 10)]
        assert point_in_polygon((5, 5), square)
        assert not point_in_polygon((15, 5), square)

    def test_self_intersection_finds_bow_tie(self):
        hits = self_intersections([(0, 0), (10, 10), (10, 0), (0, 10)])
        assert len(hits) == 1
        assert hits[0] == pytest.approx((5.0, 5.0))

    def test_clean_polygon_has_no_crossings(self):
        assert self_intersections([(0, 0), (10, 0), (10, 10), (0, 10)]) == []


class TestSvg:
    def test_physical_units_are_honored(self, fixtures):
        result = import_profile(fixtures / "logo.svg")
        assert result.unit_scale == pytest.approx(1.0)
        assert not result.units_ambiguous
        # svgelements resolves the viewBox transform to about seven significant
        # figures, so the imported size carries roughly half a micron of error on a
        # 36 mm profile.  That is upstream and far below any machining tolerance.
        assert result.profile.width == pytest.approx(36.0, abs=1e-3)
        assert result.profile.height == pytest.approx(16.0, abs=1e-3)

    def test_fill_rule_wins_over_containment(self, fixtures):
        """A filled circle inside a filled rectangle is material, not a hole.

        The circle covers ground the rectangle already covers, so the two resolve to
        one silhouette: the rectangle minus its own hole.  Were the circle treated as
        a hole by containment alone, the area would come out 78.5 mm2 smaller.
        """
        profile = import_profile(fixtures / "logo.svg").profile
        assert len(profile.faces) == 1
        material = 36 * 16 - 8 * 8
        assert face_area(profile) == pytest.approx(material, rel=1e-3)
        assert face_area(profile) != pytest.approx(material - math.pi * 25, rel=1e-3)

    def test_unitless_svg_is_read_at_96_dpi_and_flagged(self, fixtures):
        result = import_profile(fixtures / "unitless.svg")
        assert result.units_ambiguous
        assert result.unit_scale == pytest.approx(25.4 / 96.0)
        assert result.profile.width == pytest.approx(180 * 25.4 / 96.0, rel=1e-5)
        assert result.profile.issues_of(IssueKind.AMBIGUOUS_UNITS)

    def test_live_text_is_named_and_blocks(self, fixtures):
        profile = import_profile(fixtures / "live_text.svg").profile
        issues = profile.issues_of(IssueKind.LIVE_TEXT)
        assert issues and issues[0].blocking
        assert "outlines" in issues[0].message

    def test_stroke_only_is_detected_not_silently_empty(self, fixtures):
        profile = import_profile(fixtures / "stroke_only.svg").profile
        issues = profile.issues_of(IssueKind.NO_FILL)
        assert issues and issues[0].blocking
        assert issues[0].detail["suggested_width_mm"] == pytest.approx(1.0)

    def test_outline_strokes_gives_the_path_area(self, fixtures):
        profile = import_profile(
            fixtures / "stroke_only.svg", ImportOptions(outline_stroke_width=1.0)
        ).profile
        assert not profile.blocked
        assert len(profile.faces) == 1
        assert profile.width == pytest.approx(27.0, abs=0.01)
        assert profile.height == pytest.approx(1.0, abs=0.01)
        # a 26 mm long stadium of width 1: rectangle plus two half discs
        assert face_area(profile) == pytest.approx(26.0 + math.pi * 0.25, rel=0.01)

    def test_self_intersection_blocks_until_repaired(self, fixtures):
        profile = import_profile(fixtures / "self_intersecting.svg").profile
        issues = profile.issues_of(IssueKind.SELF_INTERSECTION)
        assert issues and issues[0].blocking
        assert issues[0].points

    def test_union_overlapping_repairs_a_bow_tie(self, fixtures):
        profile = import_profile(
            fixtures / "self_intersecting.svg", ImportOptions(union_overlapping=True)
        ).profile
        assert not profile.blocked
        assert len(profile.faces) == 2  # the two lobes
        assert face_area(profile) == pytest.approx(2 * 0.5 * 26 * 13, rel=0.01)

    def test_profile_is_centered_on_the_origin(self, fixtures):
        profile = import_profile(fixtures / "logo.svg").profile
        x0, y0, x1, y1 = profile.bbox
        assert (x0 + x1) / 2 == pytest.approx(0.0, abs=1e-9)
        assert (y0 + y1) / 2 == pytest.approx(0.0, abs=1e-9)


class TestDxf:
    def test_insunits_drives_the_scale(self, fixtures):
        result = import_profile(fixtures / "profile.dxf", ImportOptions(layers=["PROFILE"]))
        assert result.unit_scale == pytest.approx(1.0)
        assert not result.units_ambiguous

    def test_missing_insunits_is_flagged_not_guessed(self, fixtures):
        result = import_profile(fixtures / "no_units.dxf")
        assert result.units_ambiguous
        assert result.profile.issues_of(IssueKind.AMBIGUOUS_UNITS)

    def test_unit_override_rescales(self, fixtures):
        result = import_profile(fixtures / "no_units.dxf", ImportOptions(unit_override="in"))
        assert result.unit_scale == pytest.approx(25.4)
        assert result.profile.width == pytest.approx(25 * 25.4, rel=1e-5)

    def test_layer_filter_excludes_construction_geometry(self, fixtures):
        everything = import_profile(fixtures / "profile.dxf").profile
        just_profile = import_profile(
            fixtures / "profile.dxf", ImportOptions(layers=["PROFILE"])
        ).profile
        assert everything.blocked  # the construction line will not close
        assert not just_profile.blocked
        assert just_profile.width == pytest.approx(40.0, abs=1e-3)

    def test_bulge_becomes_a_real_arc(self, fixtures):
        profile = import_profile(
            fixtures / "profile.dxf", ImportOptions(layers=["PROFILE"])
        ).profile
        # bulge 0.5 over a 40 mm chord lifts the arc 10 mm above the straight edge
        assert profile.height == pytest.approx(30.0, abs=0.01)

    def test_open_loop_reports_its_gap(self, fixtures):
        profile = import_profile(fixtures / "open_loop.dxf").profile
        issues = profile.issues_of(IssueKind.OPEN_LOOP)
        assert issues and issues[0].blocking
        assert issues[0].detail["gap"] == pytest.approx(0.35, abs=1e-6)

    def test_auto_close_repairs_the_gap(self, fixtures):
        profile = import_profile(
            fixtures / "open_loop.dxf", ImportOptions(close_open_loops=True)
        ).profile
        assert not profile.blocked
        assert len(profile.faces) == 1

    def test_disjoint_loops_become_separate_faces(self, fixtures):
        profile = import_profile(fixtures / "serial.dxf").profile
        assert len(profile.faces) == 5
        # three of the five glyphs carry an interior hole
        expected = 5 * 50 - 3 * math.pi * 1.2**2
        assert face_area(profile) == pytest.approx(expected, rel=1e-3)


class TestLengths:
    @pytest.mark.parametrize(
        "text,expected",
        [("40mm", (40.0, "mm")), ("2in", (2.0, "in")), ("96", (96.0, "px")), ("1.5e2px", (150.0, "px"))],
    )
    def test_parse_length(self, text, expected):
        assert parse_length(text) == expected

    def test_percentage_is_not_a_length(self):
        assert parse_length("50%") is None

    def test_viewbox_scaling(self, fixtures):
        scale, unit, ambiguous = svg_user_unit_mm(fixtures / "logo.svg")
        assert scale == pytest.approx(1.0)
        assert unit == "mm"
        assert not ambiguous
