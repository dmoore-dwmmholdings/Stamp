"""Artwork made from a message - spec 5.3.

Font outlines need a Qt application, thus every test here asks for ``qapp``.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from stamp.core.document import (  # noqa: E402
    Anchor,
    AnchorKind,
    DepthMode,
    Direction,
    Document,
    EdgeRole,
    EdgeSelector,
    FaceRef,
    Feature,
    Modifier,
    ModifierKind,
    Operation,
    OperationKind,
    Placement,
    ProfileRef,
    TextAlign,
    TextSpec,
)
from stamp.core.profiles import ProfileCache  # noqa: E402
from stamp.core.rebuild import RebuildEngine  # noqa: E402
from stamp.core.refs import (  # noqa: E402
    face_center,
    face_normal_at,
    faces_of,
    make_face_ref,
    surface_kind,
)
from stamp.io.text_profile import build_text_profile  # noqa: E402


@pytest.fixture
def top_ref(bracket_step):
    for face in faces_of(bracket_step.runtime):
        if surface_kind(face) != "plane":
            continue
        center = face_center(face)
        if abs(center[2] - 8.0) < 1e-6 and face_normal_at(face, center)[2] > 0.9:
            return make_face_ref(face, (30.0, 20.0, 8.0))
    pytest.fail("no top face found")


def a_text_feature(spec, ref, *, kind, depth, direction, modifiers=()):
    return Feature(
        name="Text",
        profile=ProfileRef(text=spec),
        placement=Placement(
            anchor=Anchor(kind=AnchorKind.FACE, face_ref=FaceRef.from_dict(ref.to_dict()))
        ),
        operation=Operation(kind=kind, depth_mode=DepthMode.BLIND, depth=depth,
                            direction=direction),
        modifiers=list(modifiers),
    )


class TestTextProfile:
    def test_a_counter_becomes_a_hole(self, qapp):
        """The middle of an "O" is a hole, not material."""
        profile = build_text_profile(TextSpec(text="O", size_mm=10))
        assert len(profile.loops) == 2
        assert sum(1 for d in profile.depth if d % 2 == 1) == 1

    def test_separate_letters_stay_separate_faces(self, qapp):
        profile = build_text_profile(TextSpec(text="HI", size_mm=10))
        assert len(profile.faces) == 2

    def test_the_size_is_the_em_size_in_millimeters(self, qapp):
        small = build_text_profile(TextSpec(text="H", size_mm=5))
        large = build_text_profile(TextSpec(text="H", size_mm=10))
        assert large.height == pytest.approx(small.height * 2, rel=0.02)

    def test_bold_is_wider_than_regular(self, qapp):
        regular = build_text_profile(TextSpec(text="HH", size_mm=10))
        bold = build_text_profile(TextSpec(text="HH", size_mm=10, bold=True))
        assert bold.width > regular.width

    def test_underline_adds_geometry(self, qapp):
        plain = build_text_profile(TextSpec(text="ab", size_mm=10))
        lined = build_text_profile(TextSpec(text="ab", size_mm=10, underline=True))
        assert len(lined.loops) > len(plain.loops)

    def test_a_line_break_makes_a_second_line(self, qapp):
        one = build_text_profile(TextSpec(text="AA", size_mm=10))
        two = build_text_profile(TextSpec(text="AA\nAA", size_mm=10))
        assert two.height > one.height * 1.5

    def test_wrap_breaks_a_long_message(self, qapp):
        message = "the quick brown fox jumps over the lazy dog"
        wide = build_text_profile(TextSpec(text=message, size_mm=3))
        narrow = build_text_profile(TextSpec(text=message, size_mm=3, wrap_mm=20))
        assert narrow.width < wide.width
        assert narrow.height > wide.height

    def test_justify_fills_the_wrap_width(self, qapp):
        message = "the quick brown fox jumps over the lazy dog"
        ragged = build_text_profile(TextSpec(text=message, size_mm=3, wrap_mm=25))
        flush = build_text_profile(
            TextSpec(text=message, size_mm=3, wrap_mm=25, align=TextAlign.JUSTIFY)
        )
        assert flush.width >= ragged.width

    def test_letter_spacing_widens_the_message(self, qapp):
        tight = build_text_profile(TextSpec(text="HHH", size_mm=10))
        loose = build_text_profile(
            TextSpec(text="HHH", size_mm=10, letter_spacing=0.2)
        )
        assert loose.width > tight.width

    def test_line_spacing_raises_the_block(self, qapp):
        tight = build_text_profile(TextSpec(text="A\nA", size_mm=10))
        loose = build_text_profile(TextSpec(text="A\nA", size_mm=10, line_spacing=2.0))
        assert loose.height > tight.height

    def test_an_empty_message_is_blocking_and_says_so(self, qapp):
        profile = build_text_profile(TextSpec(text="   ", size_mm=10))
        assert profile.blocked
        assert "no message" in profile.issues[0].message


class TestTextRef:
    def test_the_spec_survives_a_round_trip(self, qapp):
        spec = TextSpec(text="SN-1", size_mm=4.5, bold=True, align=TextAlign.CENTER,
                        wrap_mm=30.0, letter_spacing=0.05, line_spacing=1.4)
        back = ProfileRef.from_dict(ProfileRef(text=spec).to_dict())
        assert back.is_text
        assert back.text == spec

    def test_the_cache_key_follows_the_message(self, qapp):
        one = ProfileRef(text=TextSpec(text="A"))
        two = ProfileRef(text=TextSpec(text="B"))
        assert one.cache_key != two.cache_key

    def test_the_cache_returns_a_profile_without_a_file(self, qapp):
        cache = ProfileCache()
        profile = cache.get(ProfileRef(text=TextSpec(text="AB", size_mm=8)))
        assert len(profile.faces) == 2


class TestTextFeature:
    def test_text_cuts_into_the_part(self, qapp, bracket_step, top_ref):
        engine = RebuildEngine(ProfileCache().get)
        doc = Document(base=bracket_step)
        doc.add_feature(a_text_feature(
            TextSpec(text="SN-0042", size_mm=6), top_ref,
            kind=OperationKind.CUT, depth=0.8, direction=Direction.INTO))
        result = engine.rebuild(doc)
        assert result.ok
        assert result.volume < bracket_step.volume

    def test_text_stands_out_of_the_part(self, qapp, bracket_step, top_ref):
        engine = RebuildEngine(ProfileCache().get)
        doc = Document(base=bracket_step)
        doc.add_feature(a_text_feature(
            TextSpec(text="STAMP", size_mm=8), top_ref,
            kind=OperationKind.ADD, depth=1.0, direction=Direction.OUT_OF))
        result = engine.rebuild(doc)
        assert result.ok
        assert result.volume > bracket_step.volume

    def test_a_fillet_rounds_the_top_of_raised_text(self, qapp, bracket_step, top_ref):
        engine = RebuildEngine(ProfileCache().get)
        doc = Document(base=bracket_step)
        doc.add_feature(a_text_feature(
            TextSpec(text="AB", size_mm=8), top_ref,
            kind=OperationKind.ADD, depth=1.0, direction=Direction.OUT_OF,
            modifiers=[Modifier(kind=ModifierKind.FILLET, value=0.12,
                                target=EdgeSelector(role=EdgeRole.TOP))]))
        result = engine.rebuild(doc)
        assert result.ok
        assert result.warnings == []

    def test_an_empty_message_reports_rather_than_crashes(
        self, qapp, bracket_step, top_ref
    ):
        engine = RebuildEngine(ProfileCache().get)
        doc = Document(base=bracket_step)
        doc.add_feature(a_text_feature(
            TextSpec(text="", size_mm=6), top_ref,
            kind=OperationKind.CUT, depth=0.5, direction=Direction.INTO))
        result = engine.rebuild(doc)
        assert not result.ok
        assert any("no message" in e for e in result.errors)

    def test_a_project_with_text_needs_no_artwork_file(
        self, qapp, bracket_step, top_ref, tmp_path
    ):
        """The message lives in the manifest, thus nothing can go missing."""
        from stamp.io.project import save

        doc = Document(base=bracket_step, name="text")
        doc.add_feature(a_text_feature(
            TextSpec(text="HI", size_mm=6), top_ref,
            kind=OperationKind.CUT, depth=0.5, direction=Direction.INTO))
        target = tmp_path / "text.stamp"
        save(doc, target)
        assert target.exists()
