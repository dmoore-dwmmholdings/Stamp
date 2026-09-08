"""The update check and download, off the GUI thread.

Same shape as :mod:`stamp.ui.rebuild_worker`: a plain QObject doing the work on
a QThread, and a controller the window talks to.  Nothing here knows what an
update *means* - that is all in :mod:`stamp.update`, which has no Qt in it and
can be tested without a window.

The one rule worth stating: a version check is a network call, and a network
call on the GUI thread is a frozen window on a hotel wifi.  Every path through
here reports back with a signal, failures included, because the controller
clears its own busy flag from those.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from stamp import update


class _Worker(QObject):
    """Runs one check or one download at a time."""

    found = Signal(object)        # update.Release
    up_to_date = Signal()
    failed = Signal(str)
    progress = Signal(int, int)   # bytes done, bytes total (0 when unknown)
    ready = Signal(str)           # path to the verified installer

    def __init__(self) -> None:
        super().__init__()
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot(str)
    def check(self, current: str) -> None:
        try:
            release = update.check(current)
        except update.UpdateError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a check must never take the app down
            self.failed.emit(f"The update check did not finish: {exc}")
            return
        if release is None:
            self.up_to_date.emit()
        else:
            self.found.emit(release)

    @Slot(object, str)
    def fetch(self, release, into: str) -> None:
        self._cancel.clear()
        artifact = getattr(release, "artifact", None)
        if artifact is None:
            self.failed.emit("There is no installer for this machine.")
            return
        try:
            path = update.download(
                artifact,
                Path(into) if into else None,
                progress=lambda done, total: self.progress.emit(done, total),
                cancelled=self._cancel.is_set,
            )
        except update.UpdateError as exc:
            if str(exc) != "cancelled":
                self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"The download did not finish: {exc}")
            return
        self.ready.emit(str(path))


class UpdateController(QObject):
    """What the window talks to.  Owns the thread and the worker."""

    found = Signal(object)
    up_to_date = Signal()
    failed = Signal(str)
    progress = Signal(int, int)
    ready = Signal(str)

    _check_requested = Signal(str)
    _fetch_requested = Signal(object, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = QThread()
        self._thread.setObjectName("stamp-update")
        self._worker = _Worker()
        self._worker.moveToThread(self._thread)

        self._check_requested.connect(self._worker.check)
        self._fetch_requested.connect(self._worker.fetch)
        for name in ("found", "up_to_date", "failed", "progress", "ready"):
            getattr(self._worker, name).connect(getattr(self, name))

    @property
    def running(self) -> bool:
        """Whether the worker thread has been started at all."""
        return self._thread.isRunning()

    def _ensure_running(self) -> None:
        """Start the thread on the first request, not in the constructor.

        Most windows never check for an update - every test builds one and none
        of them should - and a thread nobody asked for is a thread that has to
        be shut down correctly on a path nobody exercises.
        """
        if not self._thread.isRunning():
            self._thread.start()

    def check(self, current: str, after_ms: int = 0) -> None:
        """Ask whether there is a newer Stamp.

        *after_ms* keeps the check off the opening seconds: a start is the one
        moment the user is waiting on, and nothing here is urgent.
        """
        self._ensure_running()
        if after_ms > 0:
            QTimer.singleShot(after_ms, lambda: self._check_requested.emit(current))
        else:
            self._check_requested.emit(current)

    def fetch(self, release, into: Path | None = None) -> None:
        self._ensure_running()
        self._fetch_requested.emit(release, str(into) if into is not None else "")

    def cancel(self) -> None:
        self._worker.cancel()

    def shutdown(self) -> None:
        if not self._thread.isRunning():
            return
        self._worker.cancel()
        self._thread.quit()
        # A download blocks in a socket read, so this can take a moment.  It is
        # bounded rather than infinite: a wedged connection must not stop Stamp
        # from closing.
        if not self._thread.wait(3000):
            self._thread.terminate()
            self._thread.wait(1000)


__all__ = ["UpdateController"]
