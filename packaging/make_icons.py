"""Make the application icons from ``stamp-logo.svg``.

    python -m uv run python packaging/make_icons.py

The artwork has a wide margin of background around the mark.  An icon at 16 px
cannot spend half its width on a margin, thus the script measures where the mark
actually is and crops to it before it scales.

It writes:

* ``packaging/assets/stamp.ico``  - Windows, every size in one file
* ``packaging/assets/stamp.png``  - 512 px, for Linux and for the documents
* ``packaging/assets/stamp_256.png`` - for the installer
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "stamp-logo.svg"
ASSETS = Path(__file__).resolve().parent / "assets"
#: The application loads its icon from inside the package at run time.
RESOURCES = ROOT / "src" / "stamp" / "resources"

#: Sizes Windows asks for.  16 and 32 are the ones a person sees most.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

#: Space kept around the mark, as a fraction of the mark.
MARGIN = 0.06

#: Resolution used to measure and to crop.
WORK = 1024


def _render(path: Path, size: int):
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(str(path))
    if not renderer.isValid():
        raise SystemExit(f"{path} is not an SVG that Qt can read.")
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image


def _content_box(image) -> tuple[int, int, int, int]:
    """The box that holds everything that is not the background color."""
    width, height = image.width(), image.height()
    background = image.pixelColor(0, 0)
    left, top, right, bottom = width, height, -1, -1
    for y in range(height):
        for x in range(width):
            c = image.pixelColor(x, y)
            if c.alpha() == 0:
                continue
            difference = (
                abs(c.red() - background.red())
                + abs(c.green() - background.green())
                + abs(c.blue() - background.blue())
            )
            if difference > 24:
                left = min(left, x)
                right = max(right, x)
                top = min(top, y)
                bottom = max(bottom, y)
    if right < 0:
        return 0, 0, width, height
    return left, top, right - left + 1, bottom - top + 1


def _cropped_square(image):
    """Crop to the mark, keep it square, and keep the background behind it."""
    from PySide6.QtCore import QRect

    x, y, w, h = _content_box(image)
    side = int(max(w, h) * (1.0 + 2 * MARGIN))
    cx, cy = x + w / 2.0, y + h / 2.0
    left = int(round(cx - side / 2.0))
    top = int(round(cy - side / 2.0))

    # Stay inside the source, so the crop never invents transparent corners.
    left = max(0, min(left, image.width() - 1))
    top = max(0, min(top, image.height() - 1))
    side = min(side, image.width() - left, image.height() - top)
    return image.copy(QRect(left, top, side, side))


def _png_bytes(image, size: int) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, Qt

    scaled = image.scaled(
        size,
        size,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    scaled.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def _write_ico(image, target: Path, sizes=ICO_SIZES) -> None:
    """Assemble a multi-size .ico.  Every entry is a PNG, which Windows accepts."""
    payloads = [(size, _png_bytes(image, size)) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(payloads))
    offset = len(header) + 16 * len(payloads)
    directory, blobs = b"", b""
    for size, blob in payloads:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(blob),
            offset,
        )
        blobs += blob
        offset += len(blob)
    target.write_bytes(header + directory + blobs)


def main() -> int:
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication(sys.argv)  # noqa: F841 - Qt needs one for rendering
    if not SOURCE.exists():
        raise SystemExit(f"{SOURCE} is missing.")
    ASSETS.mkdir(parents=True, exist_ok=True)

    full = _render(SOURCE, WORK)
    box = _content_box(full)
    cropped = _cropped_square(full)
    print(f"mark found at {box}, cropped to {cropped.width()} px")

    _write_ico(cropped, ASSETS / "stamp.ico")
    cropped.scaled(512, 512).save(str(ASSETS / "stamp.png"), "PNG")
    (ASSETS / "stamp_256.png").write_bytes(_png_bytes(cropped, 256))
    for name in ("stamp.ico", "stamp.png", "stamp_256.png"):
        print(f"  wrote {(ASSETS / name).relative_to(ROOT)}")

    RESOURCES.mkdir(parents=True, exist_ok=True)
    _write_ico(cropped, RESOURCES / "stamp.ico")
    (RESOURCES / "stamp.png").write_bytes(_png_bytes(cropped, 512))
    for name in ("stamp.ico", "stamp.png"):
        print(f"  wrote {(RESOURCES / name).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
