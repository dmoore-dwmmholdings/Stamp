"""Traceability - a log file, a crash dump and a hook for uncaught errors.

A boolean or a fillet that fails inside OpenCascade can stop the process without
a Python traceback.  Thus ``faulthandler`` writes the stack of every thread into
the same log, and the rebuild engine writes a breadcrumb before each step.  The
last breadcrumb before a crash dump names the operation that stopped the process.

The log is at:

* Windows: ``%LOCALAPPDATA%\\Stamp\\logs\\stamp.log``
* macOS: ``~/Library/Logs/Stamp/stamp.log``
* Linux: ``$XDG_STATE_HOME/stamp/stamp.log``
"""

from __future__ import annotations

import faulthandler
import logging
import os
import platform
import sys
import threading
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

LOG_NAME = "stamp.log"

#: Made at start and removed at a clean exit.  It holds the process id, thus a
#: second window that runs at the same time is not mistaken for a crash.
RUNNING_NAME = "running.flag"

#: Bytes above which the log is moved to ``stamp.log.1`` at start.
ROTATE_BYTES = 2_000_000

#: The last lines, kept in memory as well as on disk.  A report must still carry
#: the recent history when the log file is gone - a disk that is full, a folder
#: that someone removed, a machine that denies the write.
RECENT_LIMIT = 400
_recent: deque[str] = deque(maxlen=RECENT_LIMIT)


class _RecentHandler(logging.Handler):
    """Keeps formatted records in memory, so a report never has nothing to say."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _recent.append(self.format(record))
        except Exception:
            pass


def recent_lines() -> list[str]:
    """The lines this run has logged, newest last."""
    return list(_recent)


_log = logging.getLogger("stamp")
_stream = None
_started = False
_log_path: Path | None = None
_crashed_before = False

#: Set by the window, so an uncaught error can be shown and not only written.
error_reporter = None


def log_dir() -> Path:
    """The directory for the log, per platform."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "Stamp" / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "Stamp"
    state = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(state) / "stamp"


def log_path() -> Path | None:
    return _log_path


def _pid_alive(pid: int) -> bool:
    """Is that process still running?"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        QUERY = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(QUERY, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def previous_run_crashed() -> bool:
    """Did the last run stop without a clean exit?"""
    return _crashed_before


def previous_log() -> Path | None:
    """The log of the run before this one, if it was kept."""
    if _log_path is None:
        return None
    rotated = _log_path.with_suffix(_log_path.suffix + ".prev")
    return rotated if rotated.exists() else None


def mark_clean_exit() -> None:
    """Record that this run finished on purpose."""
    if _log_path is None:
        return
    try:
        flag = _log_path.parent / RUNNING_NAME
        # Only clear our own flag.  Another window that is still open owns its.
        if flag.exists():
            owner = flag.read_text(encoding="utf-8").strip()
            if owner in ("", str(os.getpid())):
                flag.unlink(missing_ok=True)
        _log.info("Stamp stops cleanly.")
    except Exception:
        pass


def start() -> Path | None:
    """Open the log, arm the crash dump and catch uncaught errors.

    Never stops the application.  If the log cannot be opened, the application
    continues without it.
    """
    global _stream, _started, _log_path, _crashed_before
    if _started:
        return _log_path

    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / LOG_NAME
        if path.exists() and path.stat().st_size > ROTATE_BYTES:
            backup = path.with_suffix(path.suffix + ".1")
            backup.unlink(missing_ok=True)
            path.rename(backup)
        flag = directory / RUNNING_NAME
        owner = 0
        if flag.exists():
            try:
                owner = int(flag.read_text(encoding="utf-8").strip() or 0)
            except Exception:
                owner = 0
        # A flag whose process is still alive belongs to another window, not to
        # a run that crashed.
        if flag.exists() and not _pid_alive(owner):
            _crashed_before = True
            # Keep that run's log, because this run appends to the same file.
            try:
                previous = path.with_suffix(path.suffix + ".prev")
                previous.unlink(missing_ok=True)
                if path.exists():
                    import shutil

                    shutil.copy2(path, previous)
            except Exception:
                pass
        flag.write_text(str(os.getpid()), encoding="utf-8")
        _stream = open(path, "a", encoding="utf-8", buffering=1)
        _log_path = path
    except Exception:  # a read-only home must not stop the application
        _stream = None
        _log_path = None

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr), _RecentHandler()]
    if _stream is not None:
        handlers.append(logging.StreamHandler(_stream))
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(threadName)-12s %(message)s"
    )
    for handler in handlers:
        handler.setFormatter(formatter)
        _log.addHandler(handler)
    _log.setLevel(logging.INFO)
    _log.propagate = False

    if _stream is not None:
        try:
            faulthandler.enable(file=_stream, all_threads=True)
        except Exception:
            faulthandler.enable(all_threads=True)
    else:
        faulthandler.enable(all_threads=True)

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook

    _started = True
    _log.info("=" * 72)
    _log.info("Stamp starts. %s", datetime.now().isoformat(timespec="seconds"))
    _log.info("python %s on %s", sys.version.split()[0], platform.platform())
    _log.info("log %s", _log_path)
    if _crashed_before:
        _log.warning("The run before this one did not stop cleanly.")
    return _log_path


def breadcrumb(message: str, *args: object) -> None:
    """Record the step that starts now.

    A crash dump that follows this line names the step that stopped the process.
    """
    if _started:
        _log.info(message, *args)


def note_exception(where: str, exc: BaseException) -> None:
    """Record an error that the application caught and dealt with."""
    if _started:
        _log.error("%s: %s: %s", where, type(exc).__name__, exc)
        _log.error("".join(traceback.format_exception(exc)).rstrip())


def _excepthook(kind, value, tb) -> None:
    text = "".join(traceback.format_exception(kind, value, tb)).rstrip()
    if _started:
        _log.error("Uncaught error.\n%s", text)
    else:
        sys.__stderr__.write(text + "\n")
    if error_reporter is not None:
        try:
            error_reporter(value)
        except Exception:
            pass


def _thread_excepthook(args) -> None:
    if args.exc_type is SystemExit:
        return
    text = "".join(
        traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
    ).rstrip()
    if _started:
        _log.error("Uncaught error in thread %s.\n%s", args.thread and args.thread.name, text)
    else:
        sys.__stderr__.write(text + "\n")


__all__ = [
    "breadcrumb",
    "log_dir",
    "log_path",
    "mark_clean_exit",
    "note_exception",
    "recent_lines",
    "previous_log",
    "previous_run_crashed",
    "start",
]
