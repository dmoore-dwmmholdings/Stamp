"""Screenshot the main window inside the container.

    docker/shot.sh out.png [--tab 2] [--width 1600] [--height 1000]

Xvfb has no compositor and nothing to look at, so the picture comes from
QWidget.grab() rather than from the screen.  A part is loaded first when one is
given, because an empty window disables half the ribbon and a greyed-out
picture says nothing about how the colours read.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--tab", type=int, default=0)
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--ribbon-only", action="store_true")
    parser.add_argument("--part")
    parser.add_argument("--collapsed", action="store_true")
    parser.add_argument(
        "--icon-sheet", action="store_true",
        help="every icon on one page, large, for looking at them as a set",
    )
    parser.add_argument(
        "--dark", action="store_true",
        help="run against a dark desktop palette, which the ribbon has to follow",
    )
    parser.add_argument(
        "--stamp", metavar="SVG",
        help="place this artwork on the open part as a colour stamp, select it, "
             "and let the preview draw - for looking at the preview colours",
    )
    parser.add_argument(
        "--update-bar", action="store_true",
        help="show the update bar, for looking at it without a real release",
    )
    parser.add_argument(
        "--large", action="store_true",
        help="switch the ribbon to its roomy layout, after the window is up - "
             "which also proves the switch does not lose any buttons",
    )
    args = parser.parse_args()

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv[:1])

    if args.dark:
        _dark_palette(app)

    if args.icon_sheet:
        return _icon_sheet(Path(args.out))

    from stamp.ui.main_window import MainWindow

    window = MainWindow()
    window.interactive = False
    window.resize(args.width, args.height)
    window.show()
    for _ in range(20):
        app.processEvents()
    if args.part:
        window.open_part(Path(args.part))
        for _ in range(60):
            app.processEvents()
    if args.stamp:
        _place_stamp(window, app, Path(args.stamp))
    if args.update_bar:
        from stamp.update import Release

        window._on_update_found(
            Release(
                version="1.5.0",
                notes_url="https://github.com/dmoore-dwmmholdings/Stamp/releases",
                artifact=None,
                urgent=False,
            )
        )
    window.ribbon.setCurrentIndex(args.tab)
    if args.large:
        window.ribbon.set_dense(False)
    if args.collapsed:
        window.ribbon.set_collapsed(True)
    for _ in range(20):
        app.processEvents()

    target = window.ribbon if args.ribbon_only else window
    pixmap = target.grab()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(str(out))
    # Closed rather than left to the interpreter, so the worker threads are
    # shut down the way the real application shuts them down.
    window.close()
    app.processEvents()
    print(f"{out} {pixmap.width()}x{pixmap.height()}")
    return 0


def _place_stamp(window, app, artwork: Path) -> None:
    """Drop a colour stamp on the top face and wait for the rebuild."""
    from stamp.core.document import (
        Anchor,
        AnchorKind,
        DepthMode,
        Direction,
        Feature,
        Operation,
        OperationKind,
        Placement,
        ProfileRef,
    )
    from stamp.core.refs import FaceRef
    from stamp.io.profile_import import file_hash

    box = window.document.base.bbox
    feature = Feature(
        name=artwork.stem,
        profile=ProfileRef(source_path=str(artwork), source_hash=file_hash(artwork)),
        placement=Placement(anchor=Anchor(kind=AnchorKind.FACE, face_ref=FaceRef(
            point=((box[0] + box[3]) / 2, (box[1] + box[4]) / 2, box[5]),
            normal=(0.0, 0.0, 1.0), surface_type="plane"))),
        operation=Operation(kind=OperationKind.COLOR, depth_mode=DepthMode.BLIND,
                            depth=0.2, direction=Direction.INTO),
    )
    window.document.add_feature(feature)
    window.tree.set_document(window.document)
    window.tree.select_feature(feature.id)
    window.request_rebuild()
    for _ in range(400):
        app.processEvents()
    window._show_preview()
    for _ in range(60):
        app.processEvents()


def _dark_palette(app) -> None:
    """What a dark desktop hands Qt, near enough for looking at the chrome."""
    from PySide6.QtGui import QColor, QPalette

    role = QPalette.ColorRole
    palette = QPalette()
    for which, color in (
        (role.Window, "#2b2b2b"), (role.WindowText, "#e8e8e8"),
        (role.Base, "#232323"), (role.AlternateBase, "#2f2f2f"),
        (role.Text, "#e8e8e8"), (role.Button, "#2b2b2b"),
        (role.ButtonText, "#e8e8e8"), (role.Highlight, "#3d7eff"),
        (role.HighlightedText, "#ffffff"), (role.ToolTipBase, "#3a3a3a"),
        (role.ToolTipText, "#e8e8e8"),
    ):
        palette.setColor(which, QColor(color))
    app.setStyle("Fusion")
    app.setPalette(palette)


def _icon_sheet(out: Path) -> int:
    """Every icon on one page, at three sizes, on a light and a dark ground."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QPainter, QPixmap

    from stamp.ui import icons

    names = icons.names()
    columns = 6
    rows = -(-len(names) // columns)
    cell_w, cell_h = 190, 96
    width = columns * cell_w
    height = rows * cell_h * 2

    sheet = QPixmap(width, height)
    sheet.fill(QColor("#f7f7f7"))
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    for band, (ground, ink, accent) in enumerate(
        [("#f7f7f7", "#1c1c1c", "#1a73e8"), ("#333333", "#e6e6e6", "#5aa2ff")]
    ):
        top = band * rows * cell_h
        painter.fillRect(QRectF(0, top, width, rows * cell_h), QColor(ground))
        for index, name in enumerate(names):
            x = (index % columns) * cell_w
            y = top + (index // columns) * cell_h
            offset = 10
            for size in (22, 32, 48):
                pixmap = icons.pixmap(name, size, QColor(ink), QColor(accent), ratio=3)
                painter.drawPixmap(x + offset, y + 8, pixmap)
                offset += size + 12
            painter.setPen(QColor(ink))
            painter.drawText(
                QRectF(x + 6, y + 64, cell_w - 12, 20),
                Qt.AlignmentFlag.AlignLeft,
                name,
            )
    painter.end()
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(str(out))
    print(f"{out} {sheet.width()}x{sheet.height()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
