"""Unit handling. All internals are millimetres; units are a display concern only."""

from __future__ import annotations

MM_PER_INCH = 25.4

# Multiply a value in the named unit by this to get millimetres.
TO_MM: dict[str, float] = {
    "mm": 1.0,
    "cm": 10.0,
    "m": 1000.0,
    "um": 0.001,
    "in": MM_PER_INCH,
    "inch": MM_PER_INCH,
    "ft": MM_PER_INCH * 12.0,
    "px": MM_PER_INCH / 96.0,  # CSS reference pixel, 96 dpi
    "pt": MM_PER_INCH / 72.0,
    "pc": MM_PER_INCH / 6.0,
}

# STEP / IGES header unit names -> mm
STEP_UNIT_TO_MM = {
    "MILLI": 1.0,
    "CENTI": 10.0,
    "METRE": 1000.0,
    "INCH": MM_PER_INCH,
    "FOOT": MM_PER_INCH * 12.0,
    "MIL": MM_PER_INCH / 1000.0,
    "MICRO": 0.001,
}

# ezdxf $INSUNITS codes -> mm (0 means "unset"; caller must prompt)
DXF_INSUNITS_TO_MM: dict[int, float] = {
    1: MM_PER_INCH,
    2: MM_PER_INCH * 12.0,
    4: 1.0,
    5: 10.0,
    6: 1000.0,
    8: MM_PER_INCH / 1e6,
    9: MM_PER_INCH / 1000.0,
    10: MM_PER_INCH * 36.0,
    11: 1e-7,
    12: 1e-6,
    13: 0.001,
    14: 100.0,
    15: 10000.0,
}


def to_mm(value: float, unit: str) -> float:
    """Convert *value* expressed in *unit* to millimetres."""
    try:
        return value * TO_MM[unit.strip().lower()]
    except KeyError as exc:  # pragma: no cover - guarded by the UI
        raise ValueError(f"Unknown unit {unit!r}") from exc


def from_mm(value_mm: float, unit: str) -> float:
    """Convert *value_mm* (millimetres) into *unit*."""
    try:
        return value_mm / TO_MM[unit.strip().lower()]
    except KeyError as exc:  # pragma: no cover
        raise ValueError(f"Unknown unit {unit!r}") from exc


def format_length(value_mm: float, unit: str = "mm", decimals: int | None = None) -> str:
    """Format a millimetre length for display in *unit*."""
    v = from_mm(value_mm, unit)
    if decimals is None:
        decimals = 2 if unit == "mm" else 4
    return f"{v:.{decimals}f}"
