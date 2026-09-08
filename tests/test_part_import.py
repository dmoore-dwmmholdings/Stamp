"""Regressions in the part importers - spec §5.1, §5.2."""

from __future__ import annotations

import pytest

from stamp.io.import_process import read_answer
from stamp.io.part_import import PartImportError, import_part


class TestInsideOutMesh:
    """An inverted mesh is watertight, so nothing else in the importer looks at it."""

    def test_an_inverted_stl_is_turned_the_right_way_out(self, fixtures):
        result = import_part(fixtures / "inverted.stl")
        part = result.part
        # 40 x 20 x 6 - a positive volume, not the whole of space minus the box.
        assert part.volume == pytest.approx(4800.0, rel=1e-6)
        assert part.watertight

    def test_it_says_so_rather_than_repairing_in_silence(self, fixtures):
        part = import_part(fixtures / "inverted.stl").part
        assert any("point" in w for w in part.warnings), part.warnings

    def test_the_runtime_solid_is_the_box_and_not_its_complement(self, fixtures):
        part = import_part(fixtures / "inverted.stl").part
        assert float(part.runtime.volume()) == pytest.approx(4800.0, rel=1e-4)

    def test_a_good_mesh_is_left_alone(self, fixtures):
        part = import_part(fixtures / "bracket.stl").part
        assert part.volume > 0
        assert not [w for w in part.warnings if "inward" in w]


class TestDeclaredMeshUnits:
    def test_a_3mf_in_inches_arrives_in_millimetres(self, fixtures):
        result = import_part(fixtures / "inch_box.3mf")
        x0, y0, z0, x1, y1, z1 = result.part.bbox
        assert (x1 - x0, y1 - y0, z1 - z0) == pytest.approx((50.8, 25.4, 6.35), abs=1e-6)
        assert result.part.unit_scale == pytest.approx(25.4)
        assert not result.units_ambiguous

    def test_a_3mf_that_says_millimetre_is_unchanged(self, fixtures):
        result = import_part(fixtures / "assembly.3mf")
        assert result.part.unit_scale == pytest.approx(1.0)
        assert not result.units_ambiguous
        x0, y0, z0, x1, y1, z1 = result.part.bbox
        assert (x1 - x0, y1 - y0) == pytest.approx((40.0, 20.0), abs=1e-6)

    def test_an_explicit_scale_still_wins_and_is_applied_once(self, fixtures):
        """A one-object scene hands its only piece back as the working mesh."""
        result = import_part(fixtures / "inch_box.3mf", unit_scale=10.0)
        x0, _y0, _z0, x1, _y1, _z1 = result.part.bbox
        assert x1 - x0 == pytest.approx(20.0, abs=1e-6)


class TestWorkerFailuresAreSurfaced:
    """A worker that died has an account of itself; the parent used to lose it."""

    def test_the_child_stderr_reaches_the_message(self, tmp_path):
        with pytest.raises(PartImportError) as caught:
            read_answer(
                tmp_path / "missing.json",
                detail="The importer crashed. It said:\nlibGL.so.1: cannot open",
            )
        assert "libGL.so.1" in str(caught.value)
        assert "run out of memory" not in str(caught.value)

    def test_without_a_detail_the_old_guess_still_stands(self, tmp_path):
        with pytest.raises(PartImportError) as caught:
            read_answer(tmp_path / "missing.json")
        assert "run out of memory" in str(caught.value)

    def test_a_dead_process_is_described_by_status_and_stderr(self, qapp):
        import sys

        from PySide6.QtCore import QProcess

        from stamp.ui.import_worker import _worker_failure

        process = QProcess()
        process.setProgram(sys.executable)
        process.setArguments(
            ["-c", "import sys; sys.stderr.write('boom happened'); sys.exit(3)"]
        )
        process.start()
        assert process.waitForFinished(20000)
        stderr = bytes(process.readAllStandardError()).decode()

        said = _worker_failure(process, stderr)
        assert "status 3" in said
        assert "boom happened" in said
