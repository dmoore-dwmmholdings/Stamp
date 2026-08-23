"""The document model - spec §4.

Three objects: Document, BasePart, Feature.  Everything here is a pure data record
that round-trips through JSON.  No geometry is stored; geometry is recomputed from
these records by :mod:`stamp.core.rebuild`.

The runtime geometry that *is* held in memory (the imported shape or mesh, the
normalized profile) hangs off :class:`BasePart.runtime` and :class:`Profile.runtime`
and is deliberately excluded from serialization - see spec §4.4, "never store derived
geometry in the project file".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Literal

SCHEMA_VERSION = 1

Units = Literal["mm", "in"]
Mode = Literal["solid", "mesh"]


def new_id() -> str:
    return str(uuid.uuid4())


class OperationKind(StrEnum):
    ADD = "add"
    CUT = "cut"


class DepthMode(StrEnum):
    BLIND = "blind"
    THROUGH_ALL = "through_all"
    TO_FACE = "to_face"
    SYMMETRIC = "symmetric"


class Direction(StrEnum):
    INTO = "into"
    OUT_OF = "out_of"


class ModifierKind(StrEnum):
    FILLET = "fillet"
    CHAMFER = "chamfer"


class EdgeRole(StrEnum):
    """Which edges of a feature a preset selector targets - spec §6.4A."""

    TOP = "top"
    BOTTOM = "bottom"
    SIDE = "side"
    ALL = "all"
    MANUAL = "manual"
    BLEND = "blend"  # §6.4B - the intersection with the base surface


class AnchorKind(StrEnum):
    FACE = "face"
    DATUM = "datum"
    #: A plane fitted to a region of triangles on a mesh part (§6.1, mesh mode).
    MESH_REGION = "mesh_region"


# --------------------------------------------------------------------------- refs


@dataclass
class FaceRef:
    """A durable reference to a face - spec §8.2.

    Never an index.  Geometry plus intent, re-resolved on every rebuild.
    """

    point: tuple[float, float, float]
    normal: tuple[float, float, float]
    surface_type: str = "plane"
    surface_params: dict[str, Any] = field(default_factory=dict)
    area: float = 0.0
    bbox_center: tuple[float, float, float] = (0.0, 0.0, 0.0)
    origin_feature_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "point": list(self.point),
            "normal": list(self.normal),
            "surface_type": self.surface_type,
            "surface_params": dict(self.surface_params),
            "area": self.area,
            "bbox_center": list(self.bbox_center),
            "origin_feature_id": self.origin_feature_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FaceRef:
        return cls(
            point=tuple(d["point"]),
            normal=tuple(d["normal"]),
            surface_type=d.get("surface_type", "plane"),
            surface_params=dict(d.get("surface_params", {})),
            area=float(d.get("area", 0.0)),
            bbox_center=tuple(d.get("bbox_center", (0.0, 0.0, 0.0))),
            origin_feature_id=d.get("origin_feature_id"),
        )


@dataclass
class EdgeSelector:
    """Targets edges for a fillet or chamfer - spec §8.3.

    ``role`` drives the deterministic path (edges derived from the profile, named by
    provenance).  ``picks`` carries the geometric fallback for manual selections:
    each entry is ``{"midpoint": [x, y, z], "tangent": [x, y, z], "length": float}``.
    """

    role: EdgeRole = EdgeRole.ALL
    picks: list[dict[str, Any]] = field(default_factory=list)
    loop_indices: list[int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": str(self.role),
            "picks": [dict(p) for p in self.picks],
            "loop_indices": self.loop_indices,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EdgeSelector:
        return cls(
            role=EdgeRole(d.get("role", "all")),
            picks=[dict(p) for p in d.get("picks", [])],
            loop_indices=d.get("loop_indices"),
        )


# ---------------------------------------------------------------------- feature


@dataclass
class Plane:
    """A resolved sketch plane.  Cached on the anchor; recomputed every rebuild."""

    origin: tuple[float, float, float]
    normal: tuple[float, float, float]
    u_axis: tuple[float, float, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": list(self.origin),
            "normal": list(self.normal),
            "u_axis": list(self.u_axis),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Plane:
        return cls(
            origin=tuple(d["origin"]),
            normal=tuple(d["normal"]),
            u_axis=tuple(d["u_axis"]),
        )


@dataclass
class Anchor:
    kind: AnchorKind = AnchorKind.FACE
    face_ref: FaceRef | None = None
    datum: str | None = None  # "XY" | "XZ" | "YZ" when kind is DATUM
    datum_offset: float = 0.0
    #: Where the user clicked on the mesh, and the angle that grew the region.
    #: Kept so the region can be found again if the tolerance is changed.
    mesh_seed: tuple[float, float, float] | None = None
    mesh_tolerance: float = 5.0
    plane: Plane | None = None  # resolved, cached

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "face_ref": self.face_ref.to_dict() if self.face_ref else None,
            "datum": self.datum,
            "datum_offset": self.datum_offset,
            "mesh_seed": list(self.mesh_seed) if self.mesh_seed else None,
            "mesh_tolerance": self.mesh_tolerance,
            "plane": self.plane.to_dict() if self.plane else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Anchor:
        return cls(
            kind=AnchorKind(d.get("kind", "face")),
            face_ref=FaceRef.from_dict(d["face_ref"]) if d.get("face_ref") else None,
            datum=d.get("datum"),
            datum_offset=float(d.get("datum_offset", 0.0)),
            mesh_seed=tuple(d["mesh_seed"]) if d.get("mesh_seed") else None,
            mesh_tolerance=float(d.get("mesh_tolerance", 5.0)),
            plane=Plane.from_dict(d["plane"]) if d.get("plane") else None,
        )


class TextAlign(StrEnum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    JUSTIFY = "justify"


@dataclass
class TextSpec:
    """Artwork that Stamp makes from a message, not from a file - spec 5.3.

    The message becomes contours in the same pipeline as an SVG, thus a text
    feature accepts every operation and every modifier that a logo accepts.
    """

    text: str = ""
    family: str = "Arial"
    #: The em size of the font, in millimeters.
    size_mm: float = 10.0
    bold: bool = False
    italic: bool = False
    underline: bool = False
    align: TextAlign = TextAlign.LEFT
    #: Break lines wider than this, in millimeters.  None keeps one line per
    #: line of the message.
    wrap_mm: float | None = None
    #: Extra space between characters, as a fraction of the em size.
    letter_spacing: float = 0.0
    #: Distance between baselines, as a multiple of the natural line height.
    line_spacing: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "family": self.family,
            "size_mm": self.size_mm,
            "bold": self.bold,
            "italic": self.italic,
            "underline": self.underline,
            "align": str(self.align),
            "wrap_mm": self.wrap_mm,
            "letter_spacing": self.letter_spacing,
            "line_spacing": self.line_spacing,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TextSpec:
        return cls(
            text=d.get("text", ""),
            family=d.get("family", "Arial"),
            size_mm=float(d.get("size_mm", 10.0)),
            bold=bool(d.get("bold", False)),
            italic=bool(d.get("italic", False)),
            underline=bool(d.get("underline", False)),
            align=TextAlign(d.get("align", "left")),
            wrap_mm=(float(d["wrap_mm"]) if d.get("wrap_mm") else None),
            letter_spacing=float(d.get("letter_spacing", 0.0)),
            line_spacing=float(d.get("line_spacing", 1.0)),
        )

    @property
    def key(self) -> tuple:
        return (
            self.text, self.family, self.size_mm, self.bold, self.italic,
            self.underline, str(self.align), self.wrap_mm,
            self.letter_spacing, self.line_spacing,
        )


@dataclass
class Placement:
    """Where the profile sits in the sketch plane - spec §4.3."""

    anchor: Anchor = field(default_factory=Anchor)
    offset_2d: tuple[float, float] = (0.0, 0.0)
    rotation: float = 0.0
    scale: tuple[float, float] = (1.0, 1.0)
    uniform_scale: bool = True
    mirror_u: bool = False
    mirror_v: bool = False
    lift: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor": self.anchor.to_dict(),
            "offset_2d": list(self.offset_2d),
            "rotation": self.rotation,
            "scale": list(self.scale),
            "uniform_scale": self.uniform_scale,
            "mirror_u": self.mirror_u,
            "mirror_v": self.mirror_v,
            "lift": self.lift,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Placement:
        return cls(
            anchor=Anchor.from_dict(d.get("anchor", {})),
            offset_2d=tuple(d.get("offset_2d", (0.0, 0.0))),
            rotation=float(d.get("rotation", 0.0)),
            scale=tuple(d.get("scale", (1.0, 1.0))),
            uniform_scale=bool(d.get("uniform_scale", True)),
            mirror_u=bool(d.get("mirror_u", False)),
            mirror_v=bool(d.get("mirror_v", False)),
            lift=float(d.get("lift", 0.0)),
        )


@dataclass
class Operation:
    kind: OperationKind = OperationKind.CUT
    depth_mode: DepthMode = DepthMode.BLIND
    depth: float = 1.0
    direction: Direction = Direction.INTO
    draft_angle: float = 0.0
    to_face_ref: FaceRef | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "depth_mode": str(self.depth_mode),
            "depth": self.depth,
            "direction": str(self.direction),
            "draft_angle": self.draft_angle,
            "to_face_ref": self.to_face_ref.to_dict() if self.to_face_ref else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Operation:
        return cls(
            kind=OperationKind(d.get("kind", "cut")),
            depth_mode=DepthMode(d.get("depth_mode", "blind")),
            depth=float(d.get("depth", 1.0)),
            direction=Direction(d.get("direction", "into")),
            draft_angle=float(d.get("draft_angle", 0.0)),
            to_face_ref=FaceRef.from_dict(d["to_face_ref"]) if d.get("to_face_ref") else None,
        )


@dataclass
class Modifier:
    kind: ModifierKind = ModifierKind.FILLET
    value: float = 0.5  # radius for fillet, distance for chamfer
    angle: float = 45.0  # chamfer only, for the asymmetric case
    target: EdgeSelector = field(default_factory=EdgeSelector)
    id: str = field(default_factory=new_id)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": str(self.kind),
            "value": self.value,
            "angle": self.angle,
            "target": self.target.to_dict(),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Modifier:
        return cls(
            id=d.get("id", new_id()),
            kind=ModifierKind(d.get("kind", "fillet")),
            value=float(d.get("value", 0.5)),
            angle=float(d.get("angle", 45.0)),
            target=EdgeSelector.from_dict(d.get("target", {})),
            enabled=bool(d.get("enabled", True)),
        )

    @property
    def label(self) -> str:
        unit = "R" if self.kind is ModifierKind.FILLET else ""
        return f"{str(self.kind)} {unit}{self.value:g}"


@dataclass
class ProfileRef:
    """The profile side of a feature - a source file plus its cached normalization."""

    source_path: str = ""
    source_hash: str = ""
    native_units: str = "mm"
    native_size_mm: tuple[float, float] = (0.0, 0.0)
    # Import-time options that change the normalized result, so they are part of the key.
    layers: list[str] | None = None  # DXF layer filter
    outline_strokes: float | None = None  # stroke width in mm, when outlining
    unit_scale: float = 1.0  # extra scale the user applied at import
    join_tolerance: float = 0.01
    union_overlapping: bool = False
    #: Set instead of *source_path* when the artwork is a message, not a file.
    text: TextSpec | None = None

    @property
    def is_text(self) -> bool:
        return self.text is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text.to_dict() if self.text else None,
            "source_path": self.source_path,
            "source_hash": self.source_hash,
            "native_units": self.native_units,
            "native_size_mm": list(self.native_size_mm),
            "layers": self.layers,
            "outline_strokes": self.outline_strokes,
            "unit_scale": self.unit_scale,
            "join_tolerance": self.join_tolerance,
            "union_overlapping": self.union_overlapping,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProfileRef:
        return cls(
            source_path=d.get("source_path", ""),
            source_hash=d.get("source_hash", ""),
            native_units=d.get("native_units", "mm"),
            native_size_mm=tuple(d.get("native_size_mm", (0.0, 0.0))),
            layers=d.get("layers"),
            outline_strokes=d.get("outline_strokes"),
            unit_scale=float(d.get("unit_scale", 1.0)),
            join_tolerance=float(d.get("join_tolerance", 0.01)),
            union_overlapping=bool(d.get("union_overlapping", False)),
            text=TextSpec.from_dict(d["text"]) if d.get("text") else None,
        )

    @property
    def cache_key(self) -> tuple:
        return (
            self.text.key if self.text else None,
            self.source_hash,
            tuple(self.layers) if self.layers else None,
            self.outline_strokes,
            self.unit_scale,
            self.join_tolerance,
            self.union_overlapping,
        )


@dataclass
class Feature:
    """A profile + a placement + an operation + modifiers - spec §4.3."""

    id: str = field(default_factory=new_id)
    name: str = "Feature"
    enabled: bool = True
    profile: ProfileRef = field(default_factory=ProfileRef)
    placement: Placement = field(default_factory=Placement)
    operation: Operation = field(default_factory=Operation)
    modifiers: list[Modifier] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "profile": self.profile.to_dict(),
            "placement": self.placement.to_dict(),
            "operation": self.operation.to_dict(),
            "modifiers": [m.to_dict() for m in self.modifiers],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Feature:
        return cls(
            id=d.get("id", new_id()),
            name=d.get("name", "Feature"),
            enabled=bool(d.get("enabled", True)),
            profile=ProfileRef.from_dict(d.get("profile", {})),
            placement=Placement.from_dict(d.get("placement", {})),
            operation=Operation.from_dict(d.get("operation", {})),
            modifiers=[Modifier.from_dict(m) for m in d.get("modifiers", [])],
        )

    def copy_with_new_id(self, name: str | None = None) -> Feature:
        clone = Feature.from_dict(self.to_dict())
        clone.id = new_id()
        for m in clone.modifiers:
            m.id = new_id()
        clone.name = name or f"{self.name} copy"
        return clone


# -------------------------------------------------------------------- base part


@dataclass
class BasePart:
    """The imported 3D part - immutable once loaded, spec §4.2.

    ``runtime`` holds the live ``TopoDS_Shape`` or ``manifold3d.Manifold`` and is
    never serialized.
    """

    source_path: str = ""
    source_hash: str = ""
    mode: Mode = "solid"
    unit_scale: float = 1.0
    bbox: tuple[float, float, float, float, float, float] = (0, 0, 0, 0, 0, 0)
    volume: float = 0.0
    triangle_count: int = 0
    face_count: int = 0
    watertight: bool = True
    valid: bool = True
    warnings: list[str] = field(default_factory=list)
    runtime: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_hash": self.source_hash,
            "mode": self.mode,
            "unit_scale": self.unit_scale,
            "bbox": list(self.bbox),
            "volume": self.volume,
            "triangle_count": self.triangle_count,
            "face_count": self.face_count,
            "watertight": self.watertight,
            "valid": self.valid,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BasePart:
        return cls(
            source_path=d.get("source_path", ""),
            source_hash=d.get("source_hash", ""),
            mode=d.get("mode", "solid"),
            unit_scale=float(d.get("unit_scale", 1.0)),
            bbox=tuple(d.get("bbox", (0, 0, 0, 0, 0, 0))),
            volume=float(d.get("volume", 0.0)),
            triangle_count=int(d.get("triangle_count", 0)),
            face_count=int(d.get("face_count", 0)),
            watertight=bool(d.get("watertight", True)),
            valid=bool(d.get("valid", True)),
            warnings=list(d.get("warnings", [])),
        )

    @property
    def size(self) -> tuple[float, float, float]:
        x0, y0, z0, x1, y1, z1 = self.bbox
        return (x1 - x0, y1 - y0, z1 - z0)

    @property
    def diagonal(self) -> float:
        dx, dy, dz = self.size
        return (dx * dx + dy * dy + dz * dz) ** 0.5


# --------------------------------------------------------------------- document


@dataclass
class Document:
    base: BasePart | None = None
    features: list[Feature] = field(default_factory=list)
    units: Units = "mm"
    view_state: dict[str, Any] = field(default_factory=dict)
    name: str = "Untitled"

    # ------------------------------------------------------------ serialization

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "units": self.units,
            "view_state": dict(self.view_state),
            "base": self.base.to_dict() if self.base else None,
            "features": [f.to_dict() for f in self.features],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Document:
        version = int(d.get("schema_version", 0))
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"This project was written by a newer version of Stamp "
                f"(schema {version}, this build reads {SCHEMA_VERSION})."
            )
        return cls(
            name=d.get("name", "Untitled"),
            units=d.get("units", "mm"),
            view_state=dict(d.get("view_state", {})),
            base=BasePart.from_dict(d["base"]) if d.get("base") else None,
            features=[Feature.from_dict(f) for f in d.get("features", [])],
        )

    # ------------------------------------------------------------------ editing

    def feature_by_id(self, feature_id: str) -> Feature | None:
        for f in self.features:
            if f.id == feature_id:
                return f
        return None

    def index_of(self, feature_id: str) -> int:
        for i, f in enumerate(self.features):
            if f.id == feature_id:
                return i
        raise KeyError(feature_id)

    def add_feature(self, feature: Feature, index: int | None = None) -> Feature:
        feature.name = self._unique_name(feature.name)
        if index is None:
            self.features.append(feature)
        else:
            self.features.insert(index, feature)
        return feature

    def remove_feature(self, feature_id: str) -> None:
        self.features = [f for f in self.features if f.id != feature_id]

    def move_feature(self, feature_id: str, new_index: int) -> None:
        i = self.index_of(feature_id)
        f = self.features.pop(i)
        self.features.insert(max(0, min(new_index, len(self.features))), f)

    def _unique_name(self, name: str) -> str:
        taken = {f.name for f in self.features}
        if name not in taken:
            return name
        n = 2
        while f"{name} {n}" in taken:
            n += 1
        return f"{name} {n}"

    def snapshot(self) -> dict[str, Any]:
        """A deep, cheap copy for the undo stack - JSON only, no geometry."""
        return self.to_dict()

    def restore(self, snap: dict[str, Any]) -> None:
        runtime = self.base.runtime if self.base else None
        other = Document.from_dict(snap)
        self.name = other.name
        self.units = other.units
        self.view_state = other.view_state
        self.features = other.features
        if other.base is not None:
            other.base.runtime = runtime
        self.base = other.base


class UndoStack:
    """Serialized document snapshots - spec §6.6.  Capped, and cheap because the
    snapshots hold no geometry."""

    def __init__(self, limit: int = 100) -> None:
        self.limit = limit
        self._undo: list[tuple[str, dict[str, Any]]] = []
        self._redo: list[tuple[str, dict[str, Any]]] = []

    def push(self, label: str, snapshot: dict[str, Any]) -> None:
        self._undo.append((label, snapshot))
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo_label(self) -> str | None:
        return self._undo[-1][0] if self._undo else None

    def redo_label(self) -> str | None:
        return self._redo[-1][0] if self._redo else None

    def undo(self, current: dict[str, Any]) -> dict[str, Any] | None:
        if not self._undo:
            return None
        label, snap = self._undo.pop()
        self._redo.append((label, current))
        return snap

    def redo(self, current: dict[str, Any]) -> dict[str, Any] | None:
        if not self._redo:
            return None
        label, snap = self._redo.pop()
        self._undo.append((label, current))
        return snap

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()


__all__ = [
    "SCHEMA_VERSION",
    "AnchorKind",
    "Anchor",
    "BasePart",
    "DepthMode",
    "Direction",
    "Document",
    "EdgeRole",
    "EdgeSelector",
    "FaceRef",
    "Feature",
    "Modifier",
    "ModifierKind",
    "Operation",
    "OperationKind",
    "Placement",
    "Plane",
    "ProfileRef",
    "UndoStack",
    "new_id",
    "replace",
]
