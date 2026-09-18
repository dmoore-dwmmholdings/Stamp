"""Photograph the 3D view without touching the machine you are on.

``docker/shot.sh`` grabs the window widget, which is why the 3D view comes out
black there, and driving the real app with the mouse takes the pointer and the
keyboard off whoever is using the computer.  This does neither.  The window is
built with ``WA_ShowWithoutActivating`` and parked off every screen, so it never
takes focus, and the picture comes from the OpenCascade view dumping its own
frame buffer - the real 3D view, artwork and all.

    uv run python tools/shot.py out.png --part tests/fixtures/bracket.step \\
        --profile "tests/fixtures/logo.svg" --place 30,20,8 --rotate 25,-35

``--place`` takes a point on the part in model coordinates: the face under it is
the face the artwork goes on, in mesh mode and solid mode alike.  ``--rotate``
may be repeated, and writes ``out.png``, ``out-2.png`` and so on, which is how
to tell whether something is visible from every side or only from the front.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def _point(text: str) -> tuple[float, float, float]:
    x, y, z = (float(v) for v in text.split(","))
    return x, y, z


def _pair(text: str) -> tuple[float, float]:
    yaw, pitch = (float(v) for v in text.split(","))
    return yaw, pitch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", help="where to write the PNG")
    parser.add_argument("--part", help="the part file to open")
    parser.add_argument("--profile", help="artwork to place on it")
    parser.add_argument("--place", type=_point, help="x,y,z on the face to place it on")
    parser.add_argument("--project", help="open a .stamp project instead of placing")
    parser.add_argument("--view", default="iso", help="top, front, iso, ...")
    parser.add_argument(
        "--rotate", type=_pair, action="append", default=[],
        help="yaw,pitch in degrees from the preset view; repeatable",
    )
    parser.add_argument("--size", default="1200x800", help="window size, WxH")
    parser.add_argument("--wait", type=float, default=90.0, help="seconds to allow")
    args = parser.parse_args(argv)
    if bool(args.part) == bool(args.project):
        parser.error("pass one of --part or --project")

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication([sys.argv[0]])

    from stamp.ui.main_window import MainWindow

    width, _, height = args.size.partition("x")
    window = MainWindow()
    window.interactive = False  # every dialog answers itself, nothing waits
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    window.setWindowFlag(Qt.WindowType.Tool, True)  # and no taskbar button
    window.resize(int(width), int(height))
    window.move(-4000, -4000)
    window.show()

    deadline = time.time() + args.wait

    def settle(seconds: float = 1.5) -> None:
        end = time.time() + seconds
        while time.time() < end or window.rebuilder.busy:
            app.processEvents()
            time.sleep(0.03)
            if time.time() > deadline:
                return

    settle(1.0)
    if args.project:
        window.open_project(Path(args.project))
    else:
        window.open_part(Path(args.part))
    settle(2.0)
    window.viewport.set_preset_view(args.view)
    window.viewport.fit_all()
    settle(0.8)

    if args.profile:
        if args.place is None:
            parser.error("--profile needs --place x,y,z")
        window.add_profile(Path(args.profile))
        _place(window, args.place)
        settle(2.5)

    out = Path(args.out)
    shots = [out] + [
        out.with_name(f"{out.stem}-{i + 2}{out.suffix}") for i in range(len(args.rotate))
    ]
    # Every shot is checked: a view that never started dumps nothing, and a run
    # that prints paths to files it did not write is worse than one that fails.
    written = [window.viewport.screenshot(str(shots[0]))]
    for turn, path in zip(args.rotate, shots[1:], strict=True):
        window.viewport.rotate_by(*turn)
        settle(0.6)
        written.append(window.viewport.screenshot(str(path)))

    for ok, path in zip(written, shots, strict=True):
        print(path if ok else f"{path}: the view could not be dumped")
    return 0 if all(written) else 1


def _place(window, point: tuple[float, float, float]) -> None:
    """Put the pending artwork on whatever the view shows at *point*.

    The face comes from the viewport's own pick, not from a search for a face
    whose centre shares the point's height: that is what a click does, so it
    lands on the face actually facing the camera there - a side face included -
    rather than on some other face at the same level.
    """
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopoDS import TopoDS
    from PySide6.QtCore import QPoint

    view = window.viewport.view
    x, y = view.Convert(*view.Project(*point))
    window.viewport.last_pick_position = QPoint(int(x), int(y))
    if window.document.base is not None and window.document.base.mode == "mesh":
        window._on_mesh_picked()
        return
    picked = window.viewport.pick_at(int(x), int(y))
    if picked is None or picked[0].ShapeType() != TopAbs_ShapeEnum.TopAbs_FACE:
        raise SystemExit(f"nothing to place on at {point}")
    shape, where = picked
    window._create_feature(
        window._pending_profile, TopoDS.Face_s(shape), (where.X(), where.Y(), where.Z())
    )


if __name__ == "__main__":
    raise SystemExit(main())
