"""Off-thread rebuilds with a debounce - spec §6.6, optimizations 2 to 4.

The GUI thread must never block on a boolean.  Every rebuild runs on a worker
thread; a request that arrives while one is in flight cancels it and starts again
with the newer document.

The document is snapshotted (JSON, no geometry) before it crosses the thread
boundary, so the user can keep editing while the worker runs.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from stamp.core.document import Document
from stamp.core.rebuild import Cancelled, RebuildEngine, RebuildResult

#: Idle time before a rebuild starts, in ms (§6.6 optimization 2).
DEBOUNCE_MS = 250

#: A rebuild slower than this shows a progress indicator (§6.6 optimization 4).
PROGRESS_AFTER_MS = 500


class _Worker(QObject):
    """Runs one rebuild per request and always reports the outcome.

    Every request carries a generation number.  A cancel names the last
    generation to throw away, so a cancel that arrives while the request is
    still in the thread queue is not lost.  The worker reports back on every
    path, cancellation included, because the controller clears its busy flag
    from those signals.
    """

    finished = Signal(object, int)  # RebuildResult, generation
    failed = Signal(str, int)
    cancelled = Signal(int)
    progress = Signal(int, int, str)

    def __init__(self, engine: RebuildEngine) -> None:
        super().__init__()
        self._engine = engine
        self._lock = threading.Lock()
        self._cancel_through = 0

    def cancel_through(self, generation: int) -> None:
        """Throw away every generation up to and including *generation*."""
        with self._lock:
            self._cancel_through = max(self._cancel_through, generation)

    def _is_cancelled(self, generation: int) -> bool:
        with self._lock:
            return generation <= self._cancel_through

    @Slot(object, int)
    def run(self, document: Document, generation: int) -> None:
        if self._is_cancelled(generation):
            self.cancelled.emit(generation)
            return
        try:
            result = self._engine.rebuild(
                document,
                should_cancel=lambda: self._is_cancelled(generation),
                progress=lambda i, n, name: self.progress.emit(i, n, name),
            )
        except Cancelled:
            self.cancelled.emit(generation)
            return
        except Exception as exc:  # never let a worker exception kill the app
            self.failed.emit(str(exc), generation)
            return
        if self._is_cancelled(generation):
            self.cancelled.emit(generation)
            return
        self.finished.emit(result, generation)


class RebuildController(QObject):
    """Debounces requests, runs them on a worker thread, reports the result.

    Signals
    -------
    started
        A rebuild actually began (after the debounce).
    finished
        ``RebuildResult`` - the geometry is ready.
    failed
        A message, when the rebuild raised rather than reporting per feature.
    progress
        ``(index, total, feature_name)``.
    busy_changed
        ``bool`` - drives the progress indicator and the cancel button.
    """

    started = Signal()
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)
    busy_changed = Signal(bool)

    _request = Signal(object, int)

    def __init__(self, engine: RebuildEngine, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._pending: Document | None = None
        self._busy = False
        #: Every dispatch gets a number, so a late reply can be recognized.
        self._generation = 0
        self._running = 0

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._dispatch)

        self._thread = QThread()
        self._worker = _Worker(engine)
        self._worker.moveToThread(self._thread)
        self._request.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.progress.connect(self.progress)
        self._thread.start()

    # ------------------------------------------------------------------ public

    @property
    def busy(self) -> bool:
        return self._busy

    def request(self, document: Document, *, immediate: bool = False) -> None:
        """Ask for a rebuild.  Coalesces with anything already waiting."""
        self._pending = Document.from_dict(document.to_dict())
        if document.base is not None and self._pending.base is not None:
            # The runtime geometry is shared, not copied - it is immutable.
            self._pending.base.runtime = document.base.runtime
        if immediate:
            self._timer.stop()
            self._dispatch()
        else:
            self._timer.start()

    def cancel(self) -> None:
        self._timer.stop()
        self._pending = None
        self._worker.cancel_through(self._running)
        self._set_busy(False)

    def shutdown(self) -> None:
        self.cancel()
        self._thread.quit()
        self._thread.wait(3000)

    # ----------------------------------------------------------------- private

    def _dispatch(self) -> None:
        if self._pending is None:
            return
        if self._busy:
            # Cancel what is in flight and wait.  The worker reports the
            # cancellation, and _on_cancelled dispatches the pending document.
            self._worker.cancel_through(self._running)
            return
        document, self._pending = self._pending, None
        self._generation += 1
        self._running = self._generation
        self._set_busy(True)
        self.started.emit()
        self._request.emit(document, self._running)

    def _on_finished(self, result: RebuildResult, generation: int) -> None:
        if generation != self._running:
            return  # a reply from a rebuild that was already thrown away
        self._set_busy(False)
        self.finished.emit(result)
        if self._pending is not None:
            self._dispatch()

    def _on_failed(self, message: str, generation: int) -> None:
        if generation != self._running:
            return
        self._set_busy(False)
        self.failed.emit(message)
        if self._pending is not None:
            self._dispatch()

    def _on_cancelled(self, generation: int) -> None:
        if generation != self._running:
            return
        self._set_busy(False)
        if self._pending is not None:
            self._dispatch()

    def _set_busy(self, value: bool) -> None:
        if value != self._busy:
            self._busy = value
            self.busy_changed.emit(value)


__all__ = ["DEBOUNCE_MS", "PROGRESS_AFTER_MS", "RebuildController"]
