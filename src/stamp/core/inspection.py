"""Conservative, format-neutral manufacturing guidance."""

from __future__ import annotations

from dataclasses import dataclass

from stamp.core.document import Document, Feature, InspectionSettings, OperationKind

# Conservative starting points, in millimetres.  They are editable after selection.
MANUFACTURING_RULESETS: dict[str, tuple[float, float, float]] = {
    "Laser engraving": (0.10, 0.05, 0.25),
    "CNC engraving": (0.30, 0.15, 0.75),
    "Embossing": (0.40, 0.30, 1.00),
    "Resin printing": (0.20, 0.15, 0.40),
    "FDM printing": (0.45, 0.25, 0.80),
}


def apply_ruleset(settings: InspectionSettings, name: str) -> None:
    """Apply a named process baseline; manual settings remain available afterwards."""
    detail, depth, clearance = MANUFACTURING_RULESETS[name]
    settings.min_detail_mm = detail
    settings.min_depth_mm = depth
    settings.min_clearance_mm = clearance


def settings_for(document: Document, feature: Feature) -> InspectionSettings:
    return feature.inspection or document.inspection


@dataclass(frozen=True)
class ClearanceMeasurement:
    """The exact shortest clearance from a placement origin to a face boundary."""

    distance_mm: float
    origin: tuple[float, float, float]
    boundary: tuple[float, float, float]


@dataclass(frozen=True)
class FeatureDimensions:
    """Axis-aligned dimensions of a rebuilt feature tool, in millimetres.

    This is deliberately an inspection envelope rather than a nominal artwork
    dimension: it includes draft, rounded edges, and the real curved-wrap result.
    """

    width_mm: float
    height_mm: float
    depth_mm: float


def anchor_clearance_measurement(document: Document, feature: Feature) -> ClearanceMeasurement | None:
    """Distance from a feature's placement origin to its host-face boundaries.

    A face's loop includes outer boundaries and holes, so this also catches marks
    placed too close to a drilled opening. Mesh anchors deliberately do not offer
    this exact check.
    """
    if document.base is None or document.base.mode != "solid" or feature.placement.anchor.face_ref is None:
        return None
    try:
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape
        from OCP.gp import gp_Pnt
        from OCP.TopAbs import TopAbs_ShapeEnum
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS

        from stamp.core.refs import resolve_anchor, resolve_face_ref

        shape = document.base.runtime
        plane, _ = resolve_anchor(feature.placement.anchor, shape, document.datums)
        face = resolve_face_ref(feature.placement.anchor.face_ref, shape).face
        u, normal = plane.u_axis, plane.normal
        v = (
            normal[1] * u[2] - normal[2] * u[1],
            normal[2] * u[0] - normal[0] * u[2],
            normal[0] * u[1] - normal[1] * u[0],
        )
        offset_u, offset_v = feature.placement.offset_2d
        point = tuple(
            origin + u_axis * offset_u + v_axis * offset_v
            for origin, u_axis, v_axis in zip(plane.origin, u, v, strict=True)
        )
        vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
        nearest: ClearanceMeasurement | None = None
        explorer = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
        while explorer.More():
            distance = BRepExtrema_DistShapeShape(vertex, TopoDS.Edge_s(explorer.Current()))
            if distance.IsDone() and distance.NbSolution() > 0:
                value = float(distance.Value())
                if nearest is None or value < nearest.distance_mm:
                    boundary = distance.PointOnShape2(1)
                    nearest = ClearanceMeasurement(
                        value, point, (boundary.X(), boundary.Y(), boundary.Z())
                    )
            explorer.Next()
        return nearest
    except Exception:
        return None


def anchor_clearance(document: Document, feature: Feature) -> float | None:
    """Distance from a feature placement origin to its host-face boundaries."""
    measurement = anchor_clearance_measurement(document, feature)
    return measurement.distance_mm if measurement is not None else None


def feature_dimensions(shape) -> FeatureDimensions | None:
    """Measure the actual rebuilt tool's axis-aligned inspection envelope."""
    try:
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib

        box = Bnd_Box()
        BRepBndLib.Add_s(shape, box)
        if box.IsVoid():
            return None
        x0, y0, z0, x1, y1, z1 = box.Get()
        return FeatureDimensions(x1 - x0, y1 - y0, z1 - z0)
    except Exception:
        return None


def inspect_feature(document: Document, feature: Feature) -> list[str]:
    settings = settings_for(document, feature)
    if not settings.enabled or not feature.enabled:
        return []
    warnings: list[str] = []
    prefix = f"{feature.name}: "
    # A colour stamp is read by its colour, not by its depth, so the limit a
    # machined mark has to clear does not apply to it.  What does apply - whether
    # the printer has a whole layer to change filament on - is checked at export.
    if (
        feature.operation.kind is not OperationKind.COLOR
        and feature.operation.depth < settings.min_depth_mm
    ):
        warnings.append(prefix + f"depth {feature.operation.depth:g} mm is below the {settings.min_depth_mm:g} mm manufacturing limit.")
    if abs(feature.operation.draft_angle) > 45:
        warnings.append(prefix + f"draft {feature.operation.draft_angle:g}° is unusually steep; verify the manufacturing process.")
    if feature.profile.code is not None:
        code = feature.profile.code
        from stamp.io.code_profile import verify_code

        verification = verify_code(code)
        if not verification.readable:
            warnings.append(prefix + f"code verifier failed: {verification.detail}.")
        if code.module_mm < settings.min_detail_mm:
            warnings.append(prefix + f"code module {code.module_mm:g} mm is below the {settings.min_detail_mm:g} mm detail limit.")
        if code.quiet_zone < 1:
            warnings.append(prefix + "code has no quiet zone; scanners may not read the finished mark.")
        if code.quiet_zone < 2:
            warnings.append(prefix + "code quiet zone is marginal; keep at least two blank modules around the mark.")
    for modifier in feature.modifiers:
        if modifier.enabled and modifier.value < settings.min_detail_mm:
            warnings.append(prefix + f"{modifier.label} is below the {settings.min_detail_mm:g} mm detail limit.")
        if modifier.enabled and modifier.value < settings.min_detail_mm * 1.5:
            warnings.append(prefix + f"{modifier.label} may be below a practical cutter radius; verify the toolpath.")
    clearance = anchor_clearance(document, feature)
    if clearance is not None and clearance < settings.min_clearance_mm:
        warnings.append(
            prefix + f"placement origin is {clearance:g} mm from a face edge or hole, below the {settings.min_clearance_mm:g} mm clearance limit."
        )
    return warnings


def inspect_document(document: Document) -> list[str]:
    return [warning for feature in document.features for warning in inspect_feature(document, feature)]


__all__ = [
    "ClearanceMeasurement", "FeatureDimensions", "anchor_clearance",
    "anchor_clearance_measurement", "apply_ruleset", "feature_dimensions",
    "inspect_document", "inspect_feature", "settings_for",
]
