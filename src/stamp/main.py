"""Entry point.

    uv run stamp                    open empty
    uv run stamp bracket.step       open a part
    uv run stamp bracket_v3.stamp   open a project
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from stamp import __version__, diagnostics
from stamp.batch import BatchError, run_batch
from stamp.core.profiles import ProfileCache
from stamp.core.rebuild import RebuildEngine
from stamp.io import export as export_io
from stamp.io.import_process import WORKER_COMMAND, run_worker
from stamp.io.part_import import PART_EXTS, import_part
from stamp.io.project import open_project


def _offscreen_qt_application():
    """A Qt application with no display, for the command line.

    Writing a PDF goes through QPainter, which does not raise without a
    QGuiApplication - it aborts the interpreter - so ``stamp package`` could
    never produce a package at all.  The offscreen platform plugin ships with Qt
    on every system Stamp runs on and needs no display, no window server and no
    logged-in session, which is what a build machine has.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.instance() or QGuiApplication([sys.argv[0]])


def _package_resource(name: str):
    """A file from ``stamp/resources``, found the same way in a frozen build.

    ``__file__`` is ``_internal/main.py`` inside a one-folder bundle, so its
    parent is not the package directory and the icon was looked for in the wrong
    place on macOS and Linux.
    """
    from importlib.resources import as_file, files

    return as_file(files("stamp") / "resources" / name)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="stamp", add_help=False)
    parser.add_argument("command", nargs="?")
    parser.add_argument("--template")
    parser.add_argument("--csv")
    parser.add_argument("--output-dir")
    parser.add_argument("--format", choices=("step", "stl", "3mf"))
    parser.add_argument("--project")
    parser.add_argument("--output")
    args, _unknown = parser.parse_known_args(argv[1:])
    if args.command == WORKER_COMMAND:
        # A second copy of Stamp, started to read one part file and hand it back.
        # It must not touch Qt: the point of it is that this process has none of
        # OpenCascade's GIL-holding work to do.  See stamp.io.import_process.
        rest = [a for a in argv[2:] if not a.startswith("-")]
        if not rest:
            print(f"stamp {WORKER_COMMAND} needs a request file", file=sys.stderr)
            return 2
        return run_worker(rest[0])
    if args.command == "batch":
        if not all((args.template, args.csv, args.output_dir, args.format)):
            print("stamp batch requires --template, --csv, --output-dir, and --format", file=sys.stderr)
            return 2
        try:
            report = run_batch(args.template, args.csv, args.output_dir, args.format)
        except BatchError as exc:
            print(f"stamp batch: {exc}", file=sys.stderr)
            return 2
        for row in report.rows:
            print(f"{row.status}: {row.input} -> {row.output} {row.detail}".rstrip())
        return 1 if report.stopped else 0
    if args.command == "package":
        if not all((args.project, args.output)):
            print("stamp package requires --project and --output", file=sys.stderr)
            return 2
        try:
            opened = open_project(args.project)
            if opened.missing or opened.document.base is None:
                raise RuntimeError("The project has missing sources or no base part.")
            # Held, not discarded: the package writes a PDF, and QPdfWriter
            # aborts the process outright when no QGuiApplication exists.
            _qt = _offscreen_qt_application()  # noqa: F841
            base = opened.document.base
            opened.document.base = import_part(
                base.source_path, unit_scale=base.unit_scale
            ).part
            result = RebuildEngine(ProfileCache().get).rebuild(opened.document)
            written = export_io.export_job_package(
                opened.document, result.geometry, args.output, fmt=args.format, rebuild=result
            )
        except Exception as exc:
            print(f"stamp package: {exc}", file=sys.stderr)
            return 2
        print(written.path)
        return 0

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from stamp.ui.main_window import MainWindow

    diagnostics.start()

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(argv)
    app.setApplicationName("Stamp")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Stamp")

    with _package_resource("stamp.ico") as icon:
        if icon.exists():
            app.setWindowIcon(QIcon(str(icon)))

    window = MainWindow()
    window.show()

    for argument in argv[1:]:
        path = Path(argument)
        if not path.exists():
            continue
        if path.suffix.lower() == ".stamp":
            window.open_project(path)
        elif path.suffix.lower() in PART_EXTS:
            window.open_part(path)
        break

    # Ask about a crash only once the window is up, so the dialog has a parent.
    QTimer.singleShot(0, window.offer_crash_report)
    # And the update check after that, for the same reason and one more: it is
    # started here rather than in the window's constructor so that building a
    # window - which every UI test does - never makes a network call.
    QTimer.singleShot(0, window.begin_update_check)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
