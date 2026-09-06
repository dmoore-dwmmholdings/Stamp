"""Reading a part in a second process - spec §5.1, §5.2.

OpenCascade holds the GIL, so a thread cannot keep the window alive during an
import and a process has to.  These cover the two halves meeting: what the
child writes is what the parent reads, failures included.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stamp.io.import_process import ImportJob, read_answer, run_worker
from stamp.io.part_import import PartImportError, face_count


def _job(fixture: Path, out: Path, **kw) -> Path:
    request = out / "request.json"
    request.write_text(
        json.dumps(ImportJob(path=str(fixture), out_dir=str(out), **kw).to_dict()),
        encoding="utf-8",
    )
    return request


class TestTheChild:
    def test_a_solid_survives_the_crossing(self, fixtures, tmp_path):
        request = _job(fixtures / "bracket.step", tmp_path)
        assert run_worker(request) == 0

        result = read_answer(tmp_path / "request.answer.json")
        assert result.part.mode == "solid"
        assert face_count(result.part.runtime) == result.part.face_count
        assert result.part.volume > 0

    def test_the_triangulation_crosses_with_it(self, fixtures, tmp_path):
        """That is the point of the binary format - the parent never meshes again."""
        from OCP.StdPrs import StdPrs_ToolTriangulatedShape

        run_worker(_job(fixtures / "bracket.step", tmp_path))
        result = read_answer(tmp_path / "request.answer.json")
        assert StdPrs_ToolTriangulatedShape.IsTriangulated_s(result.part.runtime)

    def test_a_mesh_survives_it_too(self, fixtures, tmp_path):
        run_worker(_job(fixtures / "bracket.stl", tmp_path))
        result = read_answer(tmp_path / "request.answer.json")
        assert result.part.mode == "mesh"
        assert result.part.runtime.num_tri() > 0

    def test_it_names_the_phase_it_is_in(self, fixtures, tmp_path):
        """In a file, not on stdout - the shipped build is windowed and has none."""
        from stamp.io.import_process import phase_file

        seen: list[str] = []
        request = _job(fixtures / "bracket.step", tmp_path)
        run_worker(request)
        seen.append(phase_file(request).read_text(encoding="utf-8"))
        assert seen[-1] == "Handing it over"

    def test_naming_the_phase_never_costs_the_import(self, fixtures, tmp_path, monkeypatch):
        """A read-only temp directory is a lost label, not a lost part."""
        from stamp.io import import_process

        def refuse(*_args, **_kw):
            raise OSError("no writing here")

        request = _job(fixtures / "bracket.step", tmp_path)
        real = Path.write_text

        def write_text(self, *args, **kw):
            if self.name.endswith(import_process.PHASE_SUFFIX):
                refuse()
            return real(self, *args, **kw)

        monkeypatch.setattr(Path, "write_text", write_text)
        assert run_worker(request) == 0

    def test_a_file_it_cannot_read_comes_back_as_a_message(self, tmp_path):
        bad = tmp_path / "not-a-part.step"
        bad.write_text("this is not STEP", encoding="utf-8")
        assert run_worker(_job(bad, tmp_path)) == 1

        with pytest.raises(PartImportError) as raised:
            read_answer(tmp_path / "request.answer.json")
        assert "not a STEP file" in str(raised.value)

    def test_an_unreadable_request_still_produces_an_answer(self, tmp_path):
        """A parent waiting on the answer file must never wait on a dead process."""
        request = tmp_path / "request.json"
        request.write_text("{not json", encoding="utf-8")

        assert run_worker(request) == 2
        assert (tmp_path / "request.answer.json").exists()
        with pytest.raises(PartImportError):
            read_answer(tmp_path / "request.answer.json")


class TestTheParent:
    def test_no_answer_at_all_says_so_in_words(self, tmp_path):
        with pytest.raises(PartImportError) as raised:
            read_answer(tmp_path / "nothing.json")
        assert "stopped before it produced anything" in str(raised.value)

    def test_a_small_file_is_read_without_starting_a_process(
        self, fixtures, monkeypatch
    ):
        """Starting an interpreter costs longer than the whole import does."""
        from stamp.ui import import_worker

        def fail(*_args, **_kw):
            raise AssertionError("a subprocess was started for a small file")

        monkeypatch.setattr(import_worker, "_run_worker", fail)
        result = import_worker.import_part_for_ui(None, fixtures / "bracket.step")
        assert result.part.face_count > 0

    def test_the_frozen_build_re_runs_itself(self, monkeypatch):
        """There is no interpreter to call in a PyInstaller build."""
        from stamp.io.import_process import WORKER_COMMAND
        from stamp.ui.import_worker import _worker_command

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", r"C:\Program Files\Stamp\Stamp.exe")
        program, arguments = _worker_command()
        assert program.endswith("Stamp.exe")
        assert arguments == [WORKER_COMMAND]

    def test_from_source_it_runs_the_same_entry_point(self, monkeypatch):
        from stamp.io.import_process import WORKER_COMMAND
        from stamp.ui.import_worker import _worker_command

        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "executable", r"C:\venv\python.exe")
        _program, arguments = _worker_command()
        assert arguments == ["-m", "stamp.main", WORKER_COMMAND]


class TestDrivingTheWorker:
    """The parent half, with a real subprocess and a real progress dialog."""

    #: A worker that is this Stamp, started the ordinary way.
    CHILD = (
        "import sys;"
        "from stamp.io.import_process import run_worker;"
        "sys.exit(run_worker(sys.argv[1]))"
    )

    @pytest.fixture
    def always_subprocess(self, monkeypatch, qtbot):
        from stamp.ui import import_worker

        monkeypatch.setattr(import_worker, "SUBPROCESS_ABOVE_BYTES", 0)
        return import_worker

    def test_a_part_comes_back_through_the_process(self, always_subprocess, fixtures, monkeypatch):
        monkeypatch.setattr(
            always_subprocess, "_worker_command", lambda: (sys.executable, ["-c", self.CHILD])
        )
        result = always_subprocess.import_part_for_ui(None, fixtures / "bracket.step")
        assert result.part.face_count > 0
        assert result.part.volume > 0

    def test_finishing_is_not_mistaken_for_being_cancelled(
        self, always_subprocess, fixtures, monkeypatch
    ):
        """QProgressDialog emits canceled() when closed, however it came to be closed.

        Closing it on the way out therefore reported every successful import as
        one the user had stopped, and the part never reached the document.
        """
        from stamp.ui.import_worker import ImportCancelled

        monkeypatch.setattr(
            always_subprocess, "_worker_command", lambda: (sys.executable, ["-c", self.CHILD])
        )
        try:
            always_subprocess.import_part_for_ui(None, fixtures / "bracket.step")
        except ImportCancelled:  # pragma: no cover - the bug this covers
            pytest.fail("a finished import was reported as cancelled")

    def test_a_worker_that_dies_at_once_does_not_hang_the_window(
        self, always_subprocess, fixtures, monkeypatch
    ):
        """It finishes before the wait starts, and quitting a loop that is not
        running does nothing - so the wait has to test a flag, not an event."""
        monkeypatch.setattr(
            always_subprocess, "_worker_command", lambda: (sys.executable, ["-c", "pass"])
        )
        with pytest.raises(PartImportError):
            always_subprocess.import_part_for_ui(None, fixtures / "bracket.step")


class TestTheCommandLine:
    def test_the_worker_command_never_reaches_the_gui(self, fixtures, tmp_path):
        """main() has to dispatch it before it so much as imports Qt."""
        from stamp.main import main

        request = _job(fixtures / "bracket.step", tmp_path)
        assert main(["stamp", "import-worker", str(request)]) == 0
        assert (tmp_path / "request.answer.json").exists()

    def test_it_refuses_to_run_with_nothing_to_read(self):
        from stamp.main import main

        assert main(["stamp", "import-worker"]) == 2
