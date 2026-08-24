"""Conservative, format-neutral manufacturing guidance."""

from __future__ import annotations

from stamp.core.document import Document, Feature, InspectionSettings


def settings_for(document: Document, feature: Feature) -> InspectionSettings:
    return feature.inspection or document.inspection


def _anchor_clearance(document: Document, feature: Feature) -> float | None:
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
        nearest: float | None = None
        explorer = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
        while explorer.More():
            distance = BRepExtrema_DistShapeShape(vertex, TopoDS.Edge_s(explorer.Current()))
            if distance.IsDone() and distance.NbSolution() > 0:
                value = float(distance.Value())
                nearest = value if nearest is None else min(nearest, value)
            explorer.Next()
        return nearest
    except Exception:
        return None


def inspect_feature(document: Document, feature: Feature) -> list[str]:
    settings = settings_for(document, feature)
    if not settings.enabled or not feature.enabled:
        return []
    warnings: list[str] = []
    prefix = f"{feature.name}: "
    if feature.operation.depth < settings.min_depth_mm:
        warnings.append(prefix + f"depth {feature.operation.depth:g} mm is below the {settings.min_depth_mm:g} mm manufacturing limit.")
    if abs(feature.operation.draft_angle) > 45:
        warnings.append(prefix + f"draft {feature.operation.draft_angle:g}° is unusually steep; verify the manufacturing process.")
    if feature.profile.code is not None:
        code = feature.profile.code
        if code.module_mm < settings.min_detail_mm:
            warnings.append(prefix + f"code module {code.module_mm:g} mm is below the {settings.min_detail_mm:g} mm detail limit.")
        if code.quiet_zone < 1:
            warnings.append(prefix + "code has no quiet zone; scanners may not read the finished mark.")
    for modifier in feature.modifiers:
        if modifier.enabled and modifier.value < settings.min_detail_mm:
            warnings.append(prefix + f"{modifier.label} is below the {settings.min_detail_mm:g} mm detail limit.")
    clearance = _anchor_clearance(document, feature)
    if clearance is not None and clearance < settings.min_clearance_mm:
        warnings.append(
            prefix + f"placement origin is {clearance:g} mm from a face edge or hole, below the {settings.min_clearance_mm:g} mm clearance limit."
        )
    return warnings


def inspect_document(document: Document) -> list[str]:
    return [warning for feature in document.features for warning in inspect_feature(document, feature)]


__all__ = ["inspect_document", "inspect_feature", "settings_for"]
