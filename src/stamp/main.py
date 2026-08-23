"""Entry point.

    uv run stamp                    open empty
    uv run stamp bracket.step       open a part
    uv run stamp bracket_v3.stamp   open a project
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from stamp import __version__, diagnostics
from stamp.io.part_import import PART_EXTS
from stamp.ui.main_window import MainWindow


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
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
