"""Generated QR and Data Matrix artwork.

The encoder returns a binary module matrix.  Each dark module becomes a precise
square OCC face, so codes use exactly the same boolean and export path as any
other Stamp profile rather than becoming a bitmap or texture.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
from OCP.gp import gp_Pnt

from stamp.core.document import CodeKind, CodeSpec
from stamp.io.normalize import Issue, IssueKind, Profile, normalize_groups, union_overlapping


class CodeProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodeVerification:
    """Result of rasterizing a generated mark and decoding it again."""

    readable: bool
    detail: str = ""


def _matrix(spec: CodeSpec):
    try:
        import zxingcpp
    except ImportError as exc:  # pragma: no cover - dependency packaging guard
        raise CodeProfileError("The QR/Data Matrix encoder is not installed.") from exc
    if not spec.payload:
        raise CodeProfileError("A QR or Data Matrix feature needs a payload.")
    if spec.module_mm <= 0:
        raise CodeProfileError("Code module size must be more than zero.")
    fmt = zxingcpp.BarcodeFormat.QRCode if spec.kind is CodeKind.QR else zxingcpp.BarcodeFormat.DataMatrix
    kwargs = {"ec_level": spec.error_correction} if spec.kind is CodeKind.QR else {}
    try:
        barcode = zxingcpp.create_barcode(spec.payload, fmt, **kwargs)
        svg = barcode.to_svg(add_quiet_zones=False)
    except Exception as exc:
        raise CodeProfileError(f"Stamp could not encode this {spec.kind.replace('_', ' ')} payload: {exc}") from exc
    # The binding's Image buffer is not safe to iterate on every supported
    # platform. Its SVG is a lossless module grid. ZXing encodes every dark
    # rectangle as ``M x y h w v h h -w Z`` in its path data.
    match = re.search(r"<svg[^>]*\bwidth=\"([0-9.]+)\"[^>]*\bheight=\"([0-9.]+)\"", svg)
    if match is None:
        raise CodeProfileError("The code encoder returned SVG without dimensions.")
    columns, rows = int(float(match.group(1))), int(float(match.group(2)))
    matrix = [[False] * columns for _ in range(rows)]
    for x, y, width, height in re.findall(r"M(-?\d+) (-?\d+)h(-?\d+)v(-?\d+)h-?\d+Z", svg):
        x, y, width, height = int(x), int(y), int(width), int(height)
        for row in range(max(0, y), min(rows, y + height)):
            for column in range(max(0, x), min(columns, x + width)):
                matrix[row][column] = True
    return matrix


def _square(x: float, y: float, size: float):
    points = [(x, y), (x + size, y), (x + size, y + size), (x, y + size)]
    return [
        BRepBuilderAPI_MakeEdge(gp_Pnt(*points[index], 0.0), gp_Pnt(*points[(index + 1) % 4], 0.0)).Edge()
        for index in range(4)
    ]


def build_code_profile(spec: CodeSpec) -> Profile:
    """Generate a centered vector profile for *spec*."""
    matrix = _matrix(spec)
    if not matrix or not matrix[0]:
        return Profile(issues=[Issue(IssueKind.EMPTY, "The generated code has no modules.", blocking=True)])
    rows, cols = len(matrix), len(matrix[0])
    module = spec.module_mm
    origin_x, origin_y = -(cols * module) / 2.0, -(rows * module) / 2.0
    groups = []
    for row, values in enumerate(matrix):
        for column, dark in enumerate(values):
            if dark:
                groups.append(_square(origin_x + column * module, origin_y + (rows - row - 1) * module, module))
    if not groups:
        return Profile(issues=[Issue(IssueKind.EMPTY, "The generated code has no dark modules.", blocking=True)])
    profile = normalize_groups(groups, center=False, source_units="mm")
    # Adjacent dark modules share boundaries. Merge their 2D regions before the
    # solid is made so the shared edges do not look like self-intersections.
    if profile.issues_of(IssueKind.SELF_INTERSECTION):
        profile = union_overlapping(profile)
        profile.issues = [issue for issue in profile.issues if issue.kind is not IssueKind.SELF_INTERSECTION]
    return profile


def verify_code(spec: CodeSpec) -> CodeVerification:
    """Prove the generated module grid decodes to the intended payload.

    The test image includes the requested quiet zone and square modules. It is a
    digital verifier, not a substitute for physical-process qualification.
    """
    if spec.quiet_zone < 1:
        return CodeVerification(False, "the requested mark has no quiet zone")
    try:
        import numpy as np
        import zxingcpp

        matrix = _matrix(spec)
        if not matrix or not matrix[0]:
            return CodeVerification(False, "the generated code has no modules")
        scale = 12
        quiet = spec.quiet_zone * scale
        rows, columns = len(matrix), len(matrix[0])
        image = np.full((rows * scale + quiet * 2, columns * scale + quiet * 2), 255, dtype=np.uint8)
        for row, values in enumerate(matrix):
            for column, dark in enumerate(values):
                if dark:
                    image[quiet + row * scale:quiet + (row + 1) * scale,
                          quiet + column * scale:quiet + (column + 1) * scale] = 0
        fmt = zxingcpp.BarcodeFormat.QRCode if spec.kind is CodeKind.QR else zxingcpp.BarcodeFormat.DataMatrix
        decoded = zxingcpp.read_barcode(image, formats=fmt, is_pure=True)
        if decoded is None:
            return CodeVerification(False, "ZXing could not decode the generated module grid")
        if decoded.text != spec.payload:
            return CodeVerification(False, "decoded payload did not match the requested payload")
        return CodeVerification(True)
    except Exception as exc:
        return CodeVerification(False, str(exc))


__all__ = ["CodeProfileError", "CodeVerification", "build_code_profile", "verify_code"]
