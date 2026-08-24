"""Entry point.

    uv run stamp                    open empty
    uv run stamp bracket.step       open a part
    uv run stamp bracket_v3.stamp   open a project
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stamp import __version__, diagnostics
from stamp.batch import BatchError, run_batch
from stamp.io.part_import import PART_EXTS


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="stamp", add_help=False)
    parser.add_argument("command", nargs="?")
    parser.add_argument("--template")
    parser.add_argument("--csv")
    parser.add_argument("--output-dir")
    parser.add_argument("--format", choices=("step", "stl", "3mf"))
    args, _unknown = parser.parse_known_args(argv[1:])
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

    icon = Path(__file__).resolve().parent / "resources" / "stamp.ico"
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

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
