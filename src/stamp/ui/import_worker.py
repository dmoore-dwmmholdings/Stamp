"""Running a part import in a second process, with the window still alive.

The window stays responsive because the work is genuinely elsewhere: see
:mod:`stamp.io.import_process` for why a thread cannot do this.  The caller does
not have to change shape for it.  :func:`import_part_for_ui` blocks the way
``import_part`` blocks, but it blocks in a local event loop, so the window keeps
painting, the progress dialog animates, and Cancel kills the process outright.

Small files are still read in process.  Starting a second interpreter and
loading OpenCascade into it costs a couple of seconds, which is worth paying
against a minute and absurd against a tenth of one.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QEventLoop, QProcess, QProcessEnvironment, Qt, QTimer
from PySide6.QtWidgets import QApplication, QProgressDialog, QWidget

from stamp import diagnostics
from stamp.io.import_process import (
    WORKER_COMMAND,
    ImportJob,
    phase_file,
    read_answer,
)
from stamp.io.part_import import PartImportResult, import_part

#: Files smaller than this are read in process.  A 12 MB STEP lands well inside
#: a second, where starting the subprocess alone costs longer than that.
SUBPROCESS_ABOVE_BYTES = 12 * 1024 * 1024

#: How long to wait for a cancelled worker to go quietly before killing it.
TERMINATE_GRACE_MS = 2000

#: How often to read the phase the child says it is in, in ms.
PHASE_POLL_MS = 150


class ImportCancelled(RuntimeError):
    """The user stopped the import.  Callers treat this as "do nothing"."""


def import_part_for_ui(
    parent: QWidget | None,
    path: str | Path,
    *,
    unit_scale: float | None = None,
    solid_index: int | None = None,
    repair: bool = True,
    draft: bool = False,
) -> PartImportResult:
    """Import *path*, in another process when the file is big enough to warrant it.

    Raises :class:`PartImportError` as the in-process importer does, and
    :class:`ImportCancelled` when the user stops it.
    """
    path = Path(path)
    if _size_of(path) < SUBPROCESS_ABOVE_BYTES or not _worker_command():
        return import_part(
            path, unit_scale=unit_scale, solid_index=solid_index, repair=repair
        )

    work = Path(tempfile.mkdtemp(prefix="stamp-import-"))
    try:
        return _run_worker(
            parent,
            work,
            ImportJob(
                path=str(path),
                out_dir=str(work),
                unit_scale=unit_scale,
                solid_index=solid_index,
                repair=repair,
                draft=draft,
            ),
            title=path.name,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run_worker(
    parent: QWidget | None, work: Path, job: ImportJob, *, title: str
) -> PartImportResult:
    request = work / "request.json"
    request.write_text(json.dumps(job.to_dict()), encoding="utf-8")

    program, arguments = _worker_command()
    process = QProcess()
    process.setProgram(program)
    process.setArguments([*arguments, str(request)])
    process.setProcessEnvironment(_worker_environment())

    dialog = QProgressDialog(f"Opening {title}", "Stop", 0, 0, parent)
    dialog.setWindowTitle("Opening a part")
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)

    phase_path = phase_file(request)
    loop = QEventLoop()
    # "done" is not redundant with the loop.  A worker that dies at once - a
    # broken build, a missing library - finishes before the loop is running, and
    # quitting a loop that has not started does nothing at all: exec() would then
    # wait for an event that has already been and gone.  That hung the window
    # forever.  The flag is what the wait below actually tests.
    state = {"cancelled": False, "done": False, "phase": ""}

    def finish() -> None:
        state["done"] = True
        loop.quit()

    def show_phase() -> None:
        try:
            phase = phase_path.read_text(encoding="utf-8").strip()
        except OSError:
            return  # not written yet, or caught mid-write
        if phase and phase != state["phase"]:
            state["phase"] = phase
            dialog.setLabelText(f"{phase} - {title}")

    def on_cancel() -> None:
        # There is no interrupting OpenCascade mid-call, so the process is killed
        # rather than asked.  Nothing of the part has been handed over yet, so
        # there is nothing half-built to clean up.
        state["cancelled"] = True
        process.terminate()
        if not process.waitForFinished(TERMINATE_GRACE_MS):
            process.kill()
        finish()

    poll = QTimer()
    poll.setInterval(PHASE_POLL_MS)
    poll.timeout.connect(show_phase)
    process.finished.connect(lambda *_: finish())
    process.errorOccurred.connect(lambda *_: finish())
    dialog.canceled.connect(on_cancel)

    process.start()
    if not process.waitForStarted(5000):
        dialog.canceled.disconnect(on_cancel)
        dialog.close()
        # Falling back keeps a broken worker from being a broken program.
        diagnostics.breadcrumb("import worker would not start; reading in process")
        return import_part(
            job.path,
            unit_scale=job.unit_scale,
            solid_index=job.solid_index,
            repair=job.repair,
        )

    dialog.show()
    poll.start()
    if not state["done"]:
        loop.exec()
    poll.stop()
    # Disconnect before closing.  QProgressDialog treats being closed as being
    # cancelled and emits canceled() either way, so closing it on the way out
    # reported every successful import as one the user had stopped.
    dialog.canceled.disconnect(on_cancel)
    dialog.close()
    QApplication.processEvents()

    if state["cancelled"]:
        raise ImportCancelled(f"Opening {title} was stopped.")
    return read_answer(work / "request.answer.json")


def _worker_environment() -> QProcessEnvironment:
    """The child's environment, with this Stamp findable in it.

    Started from an installed package the child imports ``stamp`` on its own.
    Started from a source tree it would not, so the package root goes on
    PYTHONPATH: the worker has to be the same Stamp as the window, whichever way
    the window was launched.
    """
    environment = QProcessEnvironment.systemEnvironment()
    if getattr(sys, "frozen", False):
        return environment  # the bundle carries its own imports; PYTHONPATH only confuses it
    root = str(Path(__file__).resolve().parents[2])
    existing = environment.value("PYTHONPATH", "")
    environment.insert(
        "PYTHONPATH", f"{root}{os.pathsep}{existing}" if existing else root
    )
    return environment


def _worker_command() -> tuple[str, list[str]] | None:
    """How to start a second copy of Stamp as an import worker.

    Frozen, that is the application itself with a command word.  From source it
    is the interpreter running the same entry point.  Anything else - an
    embedded interpreter, a stripped build - returns None and the caller reads
    the file in process instead of failing.
    """
    executable = sys.executable
    if not executable:
        return None
    if getattr(sys, "frozen", False):
        return executable, [WORKER_COMMAND]
    return executable, ["-m", "stamp.main", WORKER_COMMAND]


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


__all__ = [
    "SUBPROCESS_ABOVE_BYTES",
    "ImportCancelled",
    "import_part_for_ui",
]
