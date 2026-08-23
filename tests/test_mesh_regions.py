"""Picking a sketch plane on a mesh part - spec §6.1, mesh mode."""

from __future__ import annotations

import math

import numpy as np
import pytest

from stamp.geom import mesh_regions


@pytest.fixture(scope="module")
def box():
    """A 20 x 20 x 20 box centred on the origin, as vertices and triangles."""
    import trimesh

    mesh = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    return (
        np.asarray(mesh.vertices, dtype=np.float64),
        np.asarray(mesh.faces, dtype=np.int64),
    )


@pytest.fixture(scope="module")
def cylinder():
    import trimesh

    mesh = trimesh.creation.cylinder(radius=10.0, height=20.0, sections=64)
    return (
        np.asarray(mesh.vertices, dtype=np.float64),
        np.asarray(mesh.faces, dtype=np.int64),
    )


class TestRayCasting:
    def test_a_ray_finds_the_near_face(self, box):
        vertices, faces = box
        hit = mesh_regions.pick_triangle(vertices, faces, (0.0, 0.0, 50.0), (0.0, 0.0, -1.0))
        assert hit is not None
        _index, point = hit
        assert point[2] == pytest.approx(10.0)  # the top, not the bottom

    def test_a_ray_that_misses_reports_nothing(self, box):
        vertices, faces = box
        assert mesh_regions.pick_triangle(
            vertices, faces, (100.0, 100.0, 50.0), (0.0, 0.0, -1.0)
        ) is None

    def test_a_ray_from_below_finds_the_bottom(self, box):
        vertices, faces = box
        _index, point = mesh_regions.pick_triangle(
            vertices, faces, (0.0, 0.0, -50.0), (0.0, 0.0, 1.0)
        )
        assert point[2] == pytest.approx(-10.0)

    def test_a_ray_that_passes_over_the_box_hits_nothing(self, box):
        vertices, faces = box
        assert mesh_regions.pick_triangle(
            vertices, faces, (-50.0, 0.0, 50.0), (1.0, 0.0, 0.0)
        ) is None

    def test_a_ray_along_the_top_face_still_hits_the_side(self, box):
        """It grazes the top plane, so the far side face is a real hit."""
        vertices, faces = box
        hit = mesh_regions.pick_triangle(
            vertices, faces, (0.0, 0.0, 10.0), (1.0, 0.0, 0.0)
        )
        assert hit is not None
        assert hit[1][0] == pytest.approx(10.0)


class TestRegionGrowth:
    def test_a_box_face_grows_to_its_own_two_triangles(self, box):
        vertices, faces = box
        region = mesh_regions.region_at(
            vertices, faces, (0.0, 0.0, 50.0), (0.0, 0.0, -1.0)
        )
        assert region is not None
        assert region.count == 2  # a box face is two triangles
        assert region.area == pytest.approx(400.0)

    def test_the_fitted_plane_matches_the_face(self, box):
        vertices, faces = box
        region = mesh_regions.region_at(
            vertices, faces, (3.0, -2.0, 50.0), (0.0, 0.0, -1.0)
        )
        assert region.plane.normal == pytest.approx((0.0, 0.0, 1.0), abs=1e-9)
        assert region.plane.origin[2] == pytest.approx(10.0)
        # the origin is where the click landed, not the middle of the face
        assert region.plane.origin[0] == pytest.approx(3.0)
        assert region.plane.origin[1] == pytest.approx(-2.0)
        assert region.flatness == pytest.approx(0.0, abs=1e-9)

    def test_the_growth_does_not_creep_round_a_corner(self, box):
        """A 90 degree neighbour must never join at a 5 degree tolerance."""
        vertices, faces = box
        region = mesh_regions.region_at(
            vertices, faces, (0.0, 0.0, 50.0), (0.0, 0.0, -1.0), tolerance_deg=5.0
        )
        assert region.count == 2

    def test_no_tolerance_crosses_a_right_angle(self, box):
        """Even at the widest setting a 90 degree neighbour is a different surface.

        The growth compares each neighbour against the *seed* normal rather than
        against its own neighbour, so a gently curved surface cannot creep round a
        whole fillet one tolerable step at a time either.
        """
        vertices, faces = box
        region = mesh_regions.region_at(
            vertices, faces, (0.0, 0.0, 50.0), (0.0, 0.0, -1.0), tolerance_deg=89.0
        )
        assert region.count == 2

    def test_the_normal_points_the_way_the_click_came_from(self, box):
        vertices, faces = box
        top = mesh_regions.region_at(vertices, faces, (0.0, 0.0, 50.0), (0.0, 0.0, -1.0))
        bottom = mesh_regions.region_at(vertices, faces, (0.0, 0.0, -50.0), (0.0, 0.0, 1.0))
        assert top.plane.normal[2] > 0.9
        assert bottom.plane.normal[2] < -0.9

    def test_the_u_axis_lies_in_the_plane(self, box):
        vertices, faces = box
        region = mesh_regions.region_at(vertices, faces, (0.0, 0.0, 50.0), (0.0, 0.0, -1.0))
        dot = sum(a * b for a, b in zip(region.plane.normal, region.plane.u_axis, strict=True))
        assert dot == pytest.approx(0.0, abs=1e-9)
        length = math.sqrt(sum(a * a for a in region.plane.u_axis))
        assert length == pytest.approx(1.0)

    def test_a_curved_surface_warns(self, cylinder):
        """§6.1: if the region is tiny, say so rather than pretend it is a plane."""
        vertices, faces = cylinder
        region = mesh_regions.region_at(
            vertices, faces, (50.0, 0.0, 0.0), (-1.0, 0.0, 0.0), tolerance_deg=5.0
        )
        assert region is not None
        assert region.warnings
        assert "curved" in region.warnings[0] or "very small" in region.warnings[0]

    def test_a_bigger_tolerance_takes_more_of_a_cylinder(self, cylinder):
        vertices, faces = cylinder
        tight = mesh_regions.region_at(
            vertices, faces, (50.0, 0.0, 0.0), (-1.0, 0.0, 0.0), tolerance_deg=5.0
        )
        loose = mesh_regions.region_at(
            vertices, faces, (50.0, 0.0, 0.0), (-1.0, 0.0, 0.0), tolerance_deg=30.0
        )
        assert loose.count > tight.count
        # and the fit gets worse as more of the curve is taken in
        assert loose.flatness > tight.flatness


class TestPlaneFit:
    def test_a_flat_set_of_points_fits_exactly(self):
        points = np.array([[0.0, 0.0, 5.0], [1.0, 0.0, 5.0], [0.0, 1.0, 5.0], [2.0, 3.0, 5.0]])
        centroid, normal, rms = mesh_regions.fit_plane(points)
        assert centroid[2] == pytest.approx(5.0)
        assert abs(normal[2]) == pytest.approx(1.0)
        assert rms == pytest.approx(0.0, abs=1e-12)

    def test_noise_shows_up_in_the_residual(self):
        rng = np.random.default_rng(0)
        points = np.column_stack(
            [rng.uniform(-5, 5, 200), rng.uniform(-5, 5, 200), rng.normal(0, 0.1, 200)]
        )
        _centroid, normal, rms = mesh_regions.fit_plane(points)
        assert abs(normal[2]) > 0.99
        assert 0.05 < rms < 0.2


class TestOnTheRealPart:
    def test_the_top_of_the_bracket_is_found(self, bracket_stl):
        vertices, faces = mesh_regions.mesh_arrays(bracket_stl.runtime)
        region = mesh_regions.region_at(
            vertices, faces, (30.0, 20.0, 60.0), (0.0, 0.0, -1.0)
        )
        assert region is not None
        assert region.plane.normal == pytest.approx((0.0, 0.0, 1.0), abs=1e-6)
        assert region.plane.origin[2] == pytest.approx(8.0, abs=1e-6)
        assert not region.warnings  # a large genuinely flat face

    def test_the_boss_top_is_a_separate_region(self, bracket_stl):
        vertices, faces = mesh_regions.mesh_arrays(bracket_stl.runtime)
        plate = mesh_regions.region_at(
            vertices, faces, (30.0, 20.0, 60.0), (0.0, 0.0, -1.0)
        )
        boss = mesh_regions.region_at(
            vertices, faces, (60.0, 20.0, 60.0), (0.0, 0.0, -1.0)
        )
        assert boss.plane.origin[2] == pytest.approx(14.0, abs=1e-6)
        assert boss.area < plate.area

    def test_the_region_becomes_a_displayable_shape(self, bracket_stl):
        vertices, faces = mesh_regions.mesh_arrays(bracket_stl.runtime)
        region = mesh_regions.region_at(
            vertices, faces, (30.0, 20.0, 60.0), (0.0, 0.0, -1.0)
        )
        shape = mesh_regions.region_shape(vertices, faces, region.triangles)
        assert not shape.IsNull()

    def test_adjacency_is_built_once_and_reused(self, bracket_stl):
        vertices, faces = mesh_regions.mesh_arrays(bracket_stl.runtime)
        adjacency = mesh_regions.build_adjacency(faces)
        normals = mesh_regions.face_normals(vertices, faces)
        first = mesh_regions.region_at(
            vertices, faces, (30.0, 20.0, 60.0), (0.0, 0.0, -1.0),
            adjacency=adjacency, normals=normals,
        )
        second = mesh_regions.region_at(
            vertices, faces, (30.0, 20.0, 60.0), (0.0, 0.0, -1.0),
            adjacency=adjacency, normals=normals,
        )
        assert first.count == second.count
        assert first.plane.origin == pytest.approx(second.plane.origin)
