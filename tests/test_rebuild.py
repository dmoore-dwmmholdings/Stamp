"""The rebuild engine and export - spec §6.6, §9, §14."""

from __future__ import annotations

import pytest

from stamp.core.document import (
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
)
from stamp.core.profiles import ProfileCache
from stamp.core.rebuild import RebuildEngine
from stamp.core.refs import face_center, face_normal_at, faces_of, make_face_ref, surface_kind
from stamp.io.profile_import import file_hash

LOGO_AREA = 36 * 16 - 8 * 8


@pytest.fixture
def top_ref(bracket_step):
    for face in faces_of(bracket_step.runtime):
        if surface_kind(face) != "plane":
            continue
        center = face_center(face)
        if abs(center[2] - 8.0) < 1e-6 and face_normal_at(face, center)[2] > 0.9:
            return make_face_ref(face, (30.0, 20.0, 8.0))
    pytest.fail("no top face found")


def a_feature(name, source, ref, *, kind, depth, direction, modifiers=()) -> Feature:
    return Feature(
        name=name,
        profile=ProfileRef(source_path=str(source), source_hash=file_hash(source)),
        placement=Placement(
            anchor=Anchor(kind=AnchorKind.FACE, face_ref=FaceRef.from_dict(ref.to_dict()))
        ),
        operation=Operation(kind=kind, depth_mode=DepthMode.BLIND, depth=depth,
                            direction=direction),
        modifiers=list(modifiers),
    )


@pytest.fixture
def engine():
    return RebuildEngine(ProfileCache().get)


class TestRebuild:
    def test_an_empty_document_returns_the_base(self, bracket_step, engine):
        doc = Document(base=bracket_step)
        result = engine.rebuild(doc)
        assert result.ok
        assert result.volume == pytest.approx(bracket_step.volume)

    def test_one_add_feature(self, bracket_step, engine, fixtures, top_ref):
        doc = Document(base=bracket_step)
        doc.add_feature(a_feature("Logo", fixtures / "logo.svg", top_ref,
                                  kind=OperationKind.ADD, depth=0.8,
                                  direction=Direction.OUT_OF))
        result = engine.rebuild(doc)
        assert result.ok
        assert result.volume - bracket_step.volume == pytest.approx(LOGO_AREA * 0.8, rel=0.05)

    def test_two_features_apply_in_order(self, bracket_step, engine, fixtures, top_ref):
        doc = Document(base=bracket_step)
        doc.add_feature(a_feature("Logo", fixtures / "logo.svg", top_ref,
                                  kind=OperationKind.ADD, depth=0.8,
                                  direction=Direction.OUT_OF))
        serial = a_feature("Serial", fixtures / "serial.dxf", top_ref,
                           kind=OperationKind.CUT, depth=0.5, direction=Direction.INTO)
        serial.placement.offset_2d = (0.0, -14.0)
        serial.placement.scale = (0.5, 0.5)
        doc.add_feature(serial)

        result = engine.rebuild(doc)
        assert result.ok
        assert len(result.features) == 2
        assert not result.errors

    def test_editing_the_first_feature_rebuilds_the_rest(
        self, bracket_step, engine, fixtures, top_ref
    ):
        """§14 step 7: change the emboss depth and everything downstream follows."""
        doc = Document(base=bracket_step)
        logo = a_feature("Logo", fixtures / "logo.svg", top_ref,
                         kind=OperationKind.ADD, depth=0.8, direction=Direction.OUT_OF)
        doc.add_feature(logo)
        serial = a_feature("Serial", fixtures / "serial.dxf", top_ref,
                           kind=OperationKind.CUT, depth=0.5, direction=Direction.INTO)
        serial.placement.offset_2d = (0.0, -14.0)
        serial.placement.scale = (0.5, 0.5)
        doc.add_feature(serial)

        before = engine.rebuild(doc).volume
        logo.operation.depth = 1.2
        after = engine.rebuild(doc).volume
        assert after - before == pytest.approx(LOGO_AREA * 0.4, rel=0.05)

    def test_an_unchanged_rebuild_hits_the_cache(self, bracket_step, engine, fixtures, top_ref):
        doc = Document(base=bracket_step)
        doc.add_feature(a_feature("Logo", fixtures / "logo.svg", top_ref,
                                  kind=OperationKind.ADD, depth=0.8,
                                  direction=Direction.OUT_OF))
        first = engine.rebuild(doc)
        second = engine.rebuild(doc)
        assert second.volume == pytest.approx(first.volume)
        assert second.duration_ms < first.duration_ms

    def test_suppressing_a_feature_removes_its_effect(
        self, bracket_step, engine, fixtures, top_ref
    ):
        doc = Document(base=bracket_step)
        logo = a_feature("Logo", fixtures / "logo.svg", top_ref,
                         kind=OperationKind.ADD, depth=0.8, direction=Direction.OUT_OF)
        doc.add_feature(logo)
        with_logo = engine.rebuild(doc).volume
        logo.enabled = False
        without = engine.rebuild(doc).volume
        assert without == pytest.approx(bracket_step.volume)
        assert with_logo > without

    def test_a_broken_reference_keeps_the_rest(self, bracket_step, engine, fixtures, top_ref):
        doc = Document(base=bracket_step)
        doc.add_feature(a_feature("Logo", fixtures / "logo.svg", top_ref,
                                  kind=OperationKind.ADD, depth=0.8,
                                  direction=Direction.OUT_OF))
        broken = a_feature("Broken", fixtures / "logo.svg", top_ref,
                           kind=OperationKind.CUT, depth=0.5, direction=Direction.INTO)
        broken.placement.anchor.face_ref.point = (1e5, 1e5, 1e5)
        broken.placement.anchor.face_ref.area = 1e9
        doc.add_feature(broken)

        result = engine.rebuild(doc)
        assert not result.ok
        assert len(result.errors) == 1
        assert "Broken" in result.errors[0]
        # the good feature still applied, so the geometry is not just the base part
        assert result.volume > bracket_step.volume

    def test_a_blocked_profile_is_reported_not_extruded(
        self, bracket_step, engine, fixtures, top_ref
    ):
        doc = Document(base=bracket_step)
        doc.add_feature(a_feature("Live text", fixtures / "live_text.svg", top_ref,
                                  kind=OperationKind.CUT, depth=0.5,
                                  direction=Direction.INTO))
        result = engine.rebuild(doc)
        assert not result.ok
        assert "outlines" in result.errors[0]

    def test_anchors_resolve_against_the_base_not_the_result(
        self, bracket_step, engine, fixtures, top_ref
    ):
        """§8.2: a later feature must not silently latch onto an earlier one's face."""
        doc = Document(base=bracket_step)
        doc.add_feature(a_feature("Logo", fixtures / "logo.svg", top_ref,
                                  kind=OperationKind.ADD, depth=3.0,
                                  direction=Direction.OUT_OF))
        cut = a_feature("Serial", fixtures / "serial.dxf", top_ref,
                        kind=OperationKind.CUT, depth=0.5, direction=Direction.INTO)
        cut.placement.offset_2d = (0.0, -14.0)
        cut.placement.scale = (0.5, 0.5)
        doc.add_feature(cut)

        result = engine.rebuild(doc)
        assert result.ok
        # The cut sits on the plate at z=8, not on the logo top at z=11, so it
        # actually removes material.
        assert not any("does not touch" in w for w in result.warnings)

    def test_a_fillet_that_fails_warns_and_keeps_the_part(
        self, bracket_step, engine, fixtures, top_ref
    ):
        doc = Document(base=bracket_step)
        doc.add_feature(a_feature(
            "Logo", fixtures / "logo.svg", top_ref,
            kind=OperationKind.ADD, depth=0.8, direction=Direction.OUT_OF,
            modifiers=[Modifier(kind=ModifierKind.FILLET, value=9.0,
                                target=EdgeSelector(role=EdgeRole.TOP))],
        ))
        result = engine.rebuild(doc)
        assert result.ok  # a failed fillet is a warning, not a broken feature
        assert any("too large for this artwork" in w for w in result.warnings)
        assert result.volume > bracket_step.volume


class TestMeshRebuild:
    def test_mesh_mode_matches_solid_mode(
        self, bracket_stl, bracket_step, engine, fixtures, top_ref
    ):
        solid_doc = Document(base=bracket_step)
        solid_doc.add_feature(a_feature("Logo", fixtures / "logo.svg", top_ref,
                                        kind=OperationKind.ADD, depth=0.8,
                                        direction=Direction.OUT_OF))
        solid_volume = engine.rebuild(solid_doc).volume

        mesh_doc = Document(base=bracket_stl)
        mesh_feature = a_feature("Logo", fixtures / "logo.svg", top_ref,
                                 kind=OperationKind.ADD, depth=0.8,
                                 direction=Direction.OUT_OF)
        mesh_feature.placement.anchor.plane = solid_doc.features[0].placement.anchor.plane
        mesh_doc.add_feature(mesh_feature)

        mesh_engine = RebuildEngine(ProfileCache().get)
        mesh_result = mesh_engine.rebuild(mesh_doc)
        assert mesh_result.ok
        assert mesh_result.volume == pytest.approx(solid_volume, rel=0.01)

    def test_a_blend_in_mesh_mode_says_why_it_cannot(
        self, bracket_stl, bracket_step, engine, fixtures, top_ref
    ):
        solid_doc = Document(base=bracket_step)
        solid_doc.add_feature(a_feature("Logo", fixtures / "logo.svg", top_ref,
                                        kind=OperationKind.ADD, depth=0.8,
                                        direction=Direction.OUT_OF))
        engine.rebuild(solid_doc)

        doc = Document(base=bracket_stl)
        feature = a_feature(
            "Logo", fixtures / "logo.svg", top_ref,
            kind=OperationKind.ADD, depth=0.8, direction=Direction.OUT_OF,
            modifiers=[Modifier(kind=ModifierKind.FILLET, value=0.4,
                                target=EdgeSelector(role=EdgeRole.BLEND))],
        )
        feature.placement.anchor.plane = solid_doc.features[0].placement.anchor.plane
        doc.add_feature(feature)

        result = RebuildEngine(ProfileCache().get).rebuild(doc)
        assert any("start from a STEP file" in w for w in result.warnings)


class TestExport:
    def test_step_round_trips(self, bracket_step, engine, fixtures, top_ref, tmp_path):
        from stamp.io.export import export_step
        from stamp.io.part_import import import_part

        doc = Document(base=bracket_step)
        doc.add_feature(a_feature("Logo", fixtures / "logo.svg", top_ref,
                                  kind=OperationKind.ADD, depth=0.8,
                                  direction=Direction.OUT_OF))
        result = engine.rebuild(doc)

        out = export_step(result.geometry, tmp_path / "out.step")
        assert out.path.exists() and out.size_bytes > 0

        reread = import_part(out.path).part
        assert reread.volume == pytest.approx(result.volume, rel=1e-6)

    def test_stl_from_solid_mode(self, bracket_step, engine, tmp_path):
        from stamp.io.export import export_stl

        doc = Document(base=bracket_step)
        result = engine.rebuild(doc)
        out = export_stl(result.geometry, tmp_path / "out.stl", mode="solid", quality="normal")
        assert out.triangle_count > 0
        assert out.path.stat().st_size > 0

    def test_finer_quality_gives_more_triangles(self, bracket_step, engine, tmp_path):
        from stamp.io.export import export_stl

        doc = Document(base=bracket_step)
        geometry = engine.rebuild(doc).geometry
        draft = export_stl(geometry, tmp_path / "draft.stl", mode="solid", quality="draft")
        fine = export_stl(geometry, tmp_path / "fine.stl", mode="solid", quality="fine")
        assert fine.triangle_count > draft.triangle_count

    def test_stl_from_mesh_mode(self, bracket_stl, tmp_path):
        from stamp.io.export import export_stl

        doc = Document(base=bracket_stl)
        result = RebuildEngine(ProfileCache().get).rebuild(doc)
        out = export_stl(result.geometry, tmp_path / "out.stl", mode="mesh")
        assert out.triangle_count == bracket_stl.triangle_count

    def test_export_for_quote_writes_the_whole_folder(
        self, bracket_step, engine, tmp_path
    ):
        from stamp.io.export import export_for_quote

        doc = Document(base=bracket_step, name="bracket")
        result = engine.rebuild(doc)
        written = export_for_quote(
            result.geometry, tmp_path / "quote", "bracket", mode="solid",
            screenshot=b"\x89PNG-fake", volume_mm3=result.volume, bbox=bracket_step.bbox,
        )
        suffixes = {p.path.suffix for p in written}
        assert suffixes == {".step", ".stl", ".png", ".txt"}
        note = next(p for p in written if p.path.suffix == ".txt")
        assert "Volume" in note.path.read_text(encoding="utf-8")

    def test_default_filename_carries_the_date(self):
        import datetime

        from stamp.io.export import default_filename

        name = default_filename("bracket v3", "step")
        assert name.startswith("bracketv3_")
        assert datetime.date.today().strftime("%Y%m%d") in name
        assert name.endswith(".step")


class TestModifierReach:
    """A modifier on a through cut acts on tool edges that miss the part."""

    def _doc(self, part, fixtures, ref, *, mode, role, depth=2.0):
        doc = Document(base=part)
        doc.add_feature(
            a_feature(
                "Logo", fixtures / "logo.svg", ref,
                kind=OperationKind.CUT, depth=depth, direction=Direction.INTO,
                modifiers=[Modifier(kind=ModifierKind.FILLET, value=0.5,
                                    target=EdgeSelector(role=role))],
            )
        )
        doc.features[0].operation.depth_mode = mode
        return doc

    def test_a_blind_cut_rounds_without_complaint(self, bracket_step, engine,
                                                  fixtures, top_ref):
        doc = self._doc(bracket_step, fixtures, top_ref,
                        mode=DepthMode.BLIND, role=EdgeRole.TOP)
        result = engine.rebuild(doc)
        assert result.ok
        assert result.warnings == []

    def test_a_through_cut_says_the_edges_miss_the_part(self, bracket_step, engine,
                                                        fixtures, top_ref):
        """The tool runs past the part, so its end caps round nothing visible."""
        doc = self._doc(bracket_step, fixtures, top_ref,
                        mode=DepthMode.THROUGH_ALL, role=EdgeRole.TOP)
        result = engine.rebuild(doc)
        assert result.ok
        assert any("not in the part" in w for w in result.warnings), result.warnings

    def test_side_edges_of_a_through_cut_are_not_flagged(self, bracket_step, engine,
                                                         fixtures, top_ref):
        """A side edge is longer than the part but still crosses it."""
        doc = self._doc(bracket_step, fixtures, top_ref,
                        mode=DepthMode.THROUGH_ALL, role=EdgeRole.SIDE)
        result = engine.rebuild(doc)
        assert result.ok
        assert not any("not in the part" in w for w in result.warnings), result.warnings

    def test_a_value_that_is_too_large_is_reported(self, bracket_step, engine,
                                                   fixtures, top_ref):
        doc = self._doc(bracket_step, fixtures, top_ref,
                        mode=DepthMode.BLIND, role=EdgeRole.TOP)
        doc.features[0].modifiers[0].value = 50.0
        result = engine.rebuild(doc)
        assert any("too large" in w for w in result.warnings), result.warnings


class TestModifierValueThatWorks:
    """A fillet is all-or-nothing, so the value must suit the smallest detail.

    Dense artwork is slow to rebuild, thus the two rebuilds these tests need are
    done once for the whole class rather than once for each test.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def outcome(bracket_step):
        """Rebuild once with a value that is too large, then with the offer."""
        from stamp.core.document import TextSpec
        from stamp.core.refs import (
            face_center,
            face_normal_at,
            faces_of,
            make_face_ref,
            surface_kind,
        )

        ref = None
        for face in faces_of(bracket_step.runtime):
            if surface_kind(face) != "plane":
                continue
            center = face_center(face)
            if abs(center[2] - 8.0) < 1e-6 and face_normal_at(face, center)[2] > 0.9:
                ref = make_face_ref(face, (30.0, 20.0, 8.0))
        assert ref is not None

        spec = TextSpec(text="ABCDEFG abcdefg 012345", size_mm=2.5, wrap_mm=40)
        feature = Feature(
            name="mark",
            profile=ProfileRef(text=spec),
            placement=Placement(
                anchor=Anchor(kind=AnchorKind.FACE, face_ref=FaceRef.from_dict(ref.to_dict()))
            ),
            operation=Operation(kind=OperationKind.CUT, depth_mode=DepthMode.BLIND,
                                depth=0.5, direction=Direction.INTO),
            modifiers=[Modifier(kind=ModifierKind.FILLET, value=0.5,
                                target=EdgeSelector(role=EdgeRole.TOP))],
        )
        engine = RebuildEngine(ProfileCache().get)
        doc = Document(base=bracket_step)
        doc.add_feature(feature)
        too_large = engine.rebuild(doc)

        row = too_large.result_for(feature.id)
        suggested = row.suggested_values.get(feature.modifiers[0].id)
        with_offer = None
        if suggested is not None:
            feature.modifiers[0].value = suggested
            engine.invalidate()
            with_offer = engine.rebuild(doc)
        return too_large, suggested, with_offer

    def test_a_value_too_large_names_one_that_works(self, qapp, outcome):
        """The message must name the problem, not only say no."""
        too_large, suggested, _ = outcome
        assert too_large.ok
        assert too_large.warnings, "a fillet that does nothing must say so"
        message = too_large.warnings[0]
        assert "smallest detail" in message
        if suggested is not None:
            assert "too large for this artwork" in message
            assert "works on every edge" in message or "largest that works" in message
        else:
            # Some platform fonts render this text too fine for any radius; the
            # warning must say so instead of offering a value.
            assert "too fine" in message

    def test_the_value_that_works_is_offered_to_the_panel(self, qapp, outcome):
        _too_large, suggested, _ = outcome
        if suggested is None:
            pytest.skip("this platform's font renders the text too fine for any radius")
        assert suggested > 0

    def test_the_offered_value_really_works(self, qapp, outcome):
        """The number in the message has to give a clean rebuild when it is used."""
        _too_large, suggested, with_offer = outcome
        if suggested is None:
            pytest.skip("this platform's font renders the text too fine for any radius")
        assert with_offer is not None
        assert with_offer.ok
        assert with_offer.warnings == [], with_offer.warnings

    def test_simple_artwork_is_untouched(self, bracket_step, engine, fixtures, top_ref):
        """The ordinary case must not pay for the failure path."""
        doc = Document(base=bracket_step)
        doc.add_feature(a_feature("Logo", fixtures / "logo.svg", top_ref,
                                  kind=OperationKind.CUT, depth=0.6,
                                  direction=Direction.INTO,
                                  modifiers=[Modifier(kind=ModifierKind.FILLET, value=0.3,
                                                      target=EdgeSelector(role=EdgeRole.TOP))]))
        result = engine.rebuild(doc)
        assert result.ok
        assert result.warnings == []
