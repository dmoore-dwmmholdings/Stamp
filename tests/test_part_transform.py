"""Whole-part mirror and precise part scale."""

from __future__ import annotations

import numpy as np
import pytest

from stamp.core.document import (
    SCHEMA_VERSION,
    Document,
    MirrorPlane,
    PartTransform,
)
from stamp.geom import mesh_ops, part_transform, solid_ops
from stamp.geom.part_transform import PartTransformError


def points(shape, deflection: float = 0.1) -> np.ndarray:
    verts, _ = mesh_ops.triangulate(shape, deflection)
    return verts


def sorted_points(shape) -> np.ndarray:
    return np.sort(points(shape), axis=0)


# ---------------------------------------------------------------- the data model


def test_identity_is_the_default():
    assert PartTransform().is_identity
    assert not PartTransform().mirrors
    assert PartTransform().suffix() == ""


def test_a_transform_round_trips_through_the_project_json():
    transform = PartTransform(mirror=MirrorPlane.XZ, scale=(1.5, 1.5, 1.5))
    assert PartTransform.from_dict(transform.to_dict()) == transform


def test_a_document_carries_its_transform():
    document = Document()
    document.transform = PartTransform(mirror=MirrorPlane.YZ, scale=(2.0, 2.0, 2.0))
    restored = Document.from_dict(document.to_dict())
    assert restored.transform == document.transform
    assert document.to_dict()["schema_version"] == SCHEMA_VERSION


def test_an_older_project_opens_with_an_identity_transform():
    older = {"schema_version": 4, "name": "Old", "features": []}
    assert Document.from_dict(older).transform.is_identity


def test_undo_restores_the_transform():
    document = Document()
    before = document.snapshot()
    document.transform = PartTransform(mirror=MirrorPlane.YZ)
    document.restore(before)
    assert document.transform.is_identity


def test_a_bad_scale_is_refused_with_a_reason():
    assert PartTransform(scale=(0.0, 1.0, 1.0)).validate()
    assert PartTransform(scale=(-2.0, -2.0, -2.0)).validate()
    assert PartTransform(scale=(1e9, 1e9, 1e9)).validate()
    assert not PartTransform(scale=(2.0, 2.0, 2.0)).validate()


def test_uniform_and_per_axis_scale_are_told_apart():
    assert PartTransform(scale=(2.0, 2.0, 2.0)).is_uniform_scale
    assert not PartTransform(scale=(2.0, 1.0, 1.0), uniform=False).is_uniform_scale
    # Claiming uniform while the factors differ is a bug, and it is reported.
    assert PartTransform(scale=(2.0, 1.0, 1.0), uniform=True).validate()


def test_scale_to_a_finished_dimension():
    assert PartTransform.factor_for(80.0, 100.0) == pytest.approx(1.25)
    assert PartTransform.factor_for(80.0, 40.0) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        PartTransform.factor_for(0.0, 10.0)
    with pytest.raises(ValueError):
        PartTransform.factor_for(80.0, 0.0)


def test_the_finished_size_is_reported_before_anything_is_built():
    transform = PartTransform(scale=(2.0, 0.5, 1.0), uniform=False)
    assert transform.size_from((80.0, 40.0, 14.0)) == pytest.approx((160.0, 20.0, 14.0))
    # A mirror changes the hand, never the size.
    assert PartTransform(mirror=MirrorPlane.YZ).size_from((80.0, 40.0, 14.0)) == pytest.approx(
        (80.0, 40.0, 14.0)
    )


def test_the_filename_suffix_says_what_was_done():
    assert PartTransform(mirror=MirrorPlane.YZ).suffix() == "mirrored"
    assert PartTransform(scale=(1.25, 1.25, 1.25)).suffix() == "125pct"
    assert PartTransform(mirror=MirrorPlane.XZ, scale=(2.0, 1.0, 1.0), uniform=False).suffix() == (
        "mirrored-scaled"
    )


# ------------------------------------------------------------------- solid mode


def test_mirroring_a_solid_keeps_its_volume_and_its_box(bracket_step):
    shape = bracket_step.runtime
    mirrored = part_transform.apply_solid(shape, PartTransform(mirror=MirrorPlane.YZ))
    assert solid_ops.volume(mirrored) == pytest.approx(solid_ops.volume(shape), rel=1e-9)
    assert points(mirrored).min(axis=0) == pytest.approx(points(shape).min(axis=0), abs=1e-6)
    assert points(mirrored).max(axis=0) == pytest.approx(points(shape).max(axis=0), abs=1e-6)


def test_mirroring_a_solid_actually_changes_the_hand(bracket_step):
    shape = bracket_step.runtime
    mirrored = part_transform.apply_solid(shape, PartTransform(mirror=MirrorPlane.YZ))
    # The box is the same, so only the point set proves the reflection happened.
    assert not np.allclose(sorted_points(shape), sorted_points(mirrored), atol=1e-6)


def test_mirroring_twice_gives_the_part_back(bracket_step):
    shape = bracket_step.runtime
    once = part_transform.apply_solid(shape, PartTransform(mirror=MirrorPlane.YZ))
    twice = part_transform.apply_solid(once, PartTransform(mirror=MirrorPlane.YZ))
    assert np.allclose(sorted_points(shape), sorted_points(twice), atol=1e-6)


def test_a_mirrored_solid_is_the_right_way_out(bracket_step):
    mirrored = part_transform.apply_solid(
        bracket_step.runtime, PartTransform(mirror=MirrorPlane.XZ)
    )
    assert solid_ops.volume(mirrored) > 0
    assert solid_ops.check_valid(mirrored)


@pytest.mark.parametrize("plane", [MirrorPlane.YZ, MirrorPlane.XZ, MirrorPlane.XY])
def test_every_mirror_plane_works(bracket_step, plane):
    mirrored = part_transform.apply_solid(bracket_step.runtime, PartTransform(mirror=plane))
    assert solid_ops.volume(mirrored) == pytest.approx(
        solid_ops.volume(bracket_step.runtime), rel=1e-9
    )


def test_a_uniform_scale_cubes_the_volume(bracket_step):
    shape = bracket_step.runtime
    scaled = part_transform.apply_solid(shape, PartTransform(scale=(2.0, 2.0, 2.0)))
    assert solid_ops.volume(scaled) == pytest.approx(8 * solid_ops.volume(shape), rel=1e-9)
    assert solid_ops.check_valid(scaled)


def test_a_uniform_scale_hits_the_typed_dimension(bracket_step):
    shape = bracket_step.runtime
    width = bracket_step.size[0]
    factor = PartTransform.factor_for(width, 100.0)
    scaled = part_transform.apply_solid(shape, PartTransform().with_uniform(factor))
    got = points(scaled)
    assert got.max(axis=0)[0] - got.min(axis=0)[0] == pytest.approx(100.0, abs=1e-6)


def test_a_per_axis_scale_stretches_one_axis_only(bracket_step):
    shape = bracket_step.runtime
    before = points(shape)
    scaled = part_transform.apply_solid(
        shape, PartTransform(scale=(2.0, 1.0, 1.0), uniform=False)
    )
    after = points(scaled)
    span_before = before.max(axis=0) - before.min(axis=0)
    span_after = after.max(axis=0) - after.min(axis=0)
    assert span_after[0] == pytest.approx(2 * span_before[0], rel=1e-6)
    assert span_after[1] == pytest.approx(span_before[1], rel=1e-6)
    assert span_after[2] == pytest.approx(span_before[2], rel=1e-6)
    assert solid_ops.check_valid(scaled)


def test_scale_and_mirror_together(bracket_step):
    shape = bracket_step.runtime
    both = part_transform.apply_solid(
        shape, PartTransform(mirror=MirrorPlane.YZ, scale=(2.0, 2.0, 2.0))
    )
    assert solid_ops.volume(both) == pytest.approx(8 * solid_ops.volume(shape), rel=1e-9)
    assert solid_ops.check_valid(both)


def test_the_part_stays_put_because_the_centre_is_its_own(bracket_step):
    """Scaling about the part's centre keeps the centre where it was."""
    shape = bracket_step.runtime
    before = points(shape)
    centre_before = (before.max(axis=0) + before.min(axis=0)) / 2
    scaled = part_transform.apply_solid(shape, PartTransform(scale=(3.0, 3.0, 3.0)))
    after = points(scaled)
    centre_after = (after.max(axis=0) + after.min(axis=0)) / 2
    assert centre_after == pytest.approx(centre_before, abs=1e-6)


# -------------------------------------------------------------------- mesh mode


def test_mirroring_a_mesh_keeps_it_the_right_way_out(bracket_stl):
    manifold = bracket_stl.runtime
    mirrored = part_transform.apply_mesh(manifold, PartTransform(mirror=MirrorPlane.YZ))
    # A negated axis without a winding flip gives a negative volume - inside out.
    assert mirrored.volume() > 0
    assert mirrored.volume() == pytest.approx(manifold.volume(), rel=1e-6)


def test_a_mirrored_mesh_is_still_watertight(bracket_stl):
    mirrored = part_transform.apply_mesh(bracket_stl.runtime, PartTransform(mirror=MirrorPlane.XY))
    assert mesh_ops.to_trimesh(mirrored).is_watertight


def test_mirroring_a_mesh_changes_the_hand(bracket_stl):
    manifold = bracket_stl.runtime
    mirrored = part_transform.apply_mesh(manifold, PartTransform(mirror=MirrorPlane.YZ))
    before = np.sort(np.asarray(manifold.to_mesh().vert_properties)[:, :3], axis=0)
    after = np.sort(np.asarray(mirrored.to_mesh().vert_properties)[:, :3], axis=0)
    assert not np.allclose(before, after, atol=1e-6)


def test_scaling_a_mesh_cubes_the_volume(bracket_stl):
    manifold = bracket_stl.runtime
    scaled = part_transform.apply_mesh(manifold, PartTransform(scale=(2.0, 2.0, 2.0)))
    assert scaled.volume() == pytest.approx(8 * manifold.volume(), rel=1e-6)


def test_a_per_axis_mesh_scale_stretches_one_axis_only(bracket_stl):
    manifold = bracket_stl.runtime
    scaled = part_transform.apply_mesh(
        manifold, PartTransform(scale=(2.0, 1.0, 1.0), uniform=False)
    )
    x0, y0, z0, x1, y1, z1 = manifold.bounding_box()
    sx0, sy0, sz0, sx1, sy1, sz1 = scaled.bounding_box()
    assert sx1 - sx0 == pytest.approx(2 * (x1 - x0), rel=1e-6)
    assert sy1 - sy0 == pytest.approx(y1 - y0, rel=1e-6)
    assert sz1 - sz0 == pytest.approx(z1 - z0, rel=1e-6)


# ----------------------------------------------------------------- the dispatch


def test_apply_returns_the_same_object_for_an_identity(bracket_step):
    shape = bracket_step.runtime
    assert part_transform.apply(shape, "solid", PartTransform()) is shape


def test_apply_refuses_a_bad_scale(bracket_step):
    with pytest.raises(PartTransformError):
        part_transform.apply(bracket_step.runtime, "solid", PartTransform(scale=(0.0, 0.0, 0.0)))


def test_apply_picks_the_mesh_path_for_a_mesh(bracket_stl):
    result = part_transform.apply(bracket_stl.runtime, "mesh", PartTransform(scale=(2.0, 2.0, 2.0)))
    assert result.volume() == pytest.approx(8 * bracket_stl.runtime.volume(), rel=1e-6)


def test_for_export_uses_the_documents_transform(bracket_step):
    document = Document(base=bracket_step)
    assert part_transform.for_export(document, bracket_step.runtime) is bracket_step.runtime
    document.transform = PartTransform(scale=(2.0, 2.0, 2.0))
    written = part_transform.for_export(document, bracket_step.runtime)
    assert solid_ops.volume(written) == pytest.approx(
        8 * solid_ops.volume(bracket_step.runtime), rel=1e-9
    )
