"""The rebuild engine - spec §6.6.

Linear and total: start from the base part, apply every enabled feature in order.
Nothing is destructive, so changing the depth of the first feature after adding four
more rebuilds all five.

Caching is per feature index (optimization 1 in §6.6): editing feature N only
recomputes N onward.  The worker thread and the debounce live in the UI layer, which
calls :meth:`RebuildEngine.rebuild` from off the GUI thread.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from stamp import diagnostics
from stamp.core.document import (
    Document,
    EdgeRole,
    Feature,
    OperationKind,
    PlacementMode,
)
from stamp.core.refs import ReferenceError, resolve_anchor, resolve_face_ref
from stamp.geom import mesh_ops, solid_ops
from stamp.geom.tool_solid import ToolSolid, ToolSolidError, build_tool_solid
from stamp.io.normalize import Profile


class Cancelled(RuntimeError):
    """Raised inside a rebuild when the caller asked it to stop."""


@dataclass
class FeatureResult:
    """What one feature contributed, and everything that went wrong doing it."""

    feature_id: str
    ok: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: The tool solid, kept for the live preview and for edge highlighting.
    tool: ToolSolid | None = None
    #: Edges that refused a fillet, so the viewport can show which ones.
    failed_edges: list = field(default_factory=list)
    suggested_values: dict[str, float] = field(default_factory=dict)

    @property
    def broken(self) -> bool:
        return bool(self.errors)


@dataclass
class RebuildResult:
    #: ``TopoDS_Shape`` in solid mode, ``manifold3d.Manifold`` in mesh mode.
    geometry: object | None = None
    mode: str = "solid"
    features: list[FeatureResult] = field(default_factory=list)
    duration_ms: float = 0.0
    volume: float = 0.0
    #: True when every feature applied cleanly.
    ok: bool = True

    def result_for(self, feature_id: str) -> FeatureResult | None:
        for f in self.features:
            if f.feature_id == feature_id:
                return f
        return None

    @property
    def warnings(self) -> list[str]:
        out = []
        for f in self.features:
            out.extend(f.warnings)
        return out

    @property
    def errors(self) -> list[str]:
        out = []
        for f in self.features:
            out.extend(f.errors)
        return out


class RebuildEngine:
    """Rebuilds a document, with a per-feature cache and a cancel hook.

    *profile_loader* takes a :class:`~stamp.core.document.ProfileRef` and returns a
    normalized :class:`~stamp.io.normalize.Profile`.  It is injected so the engine
    stays independent of the file layer and is easy to test.
    """

    def __init__(
        self,
        profile_loader: Callable[[object], Profile],
        *,
        tessellation_deflection: float = 0.02,
    ) -> None:
        self._load_profile = profile_loader
        self.deflection = tessellation_deflection
        #: geometry after feature i, keyed by the chain signature up to i
        self._cache: dict[str, object] = {}
        self._cache_order: list[str] = []
        self._cache_limit = 32

    # ------------------------------------------------------------------- cache

    def invalidate(self) -> None:
        self._cache.clear()
        self._cache_order.clear()

    def _remember(self, key: str, geometry: object) -> None:
        if key not in self._cache:
            self._cache_order.append(key)
            if len(self._cache_order) > self._cache_limit:
                self._cache.pop(self._cache_order.pop(0), None)
        self._cache[key] = geometry

    @staticmethod
    def _chain_key(document: Document, upto: int) -> str:
        import hashlib
        import json

        payload = {
            "base": document.base.source_hash if document.base else "",
            "features": [f.to_dict() for f in document.features[:upto] if f.enabled],
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:24]

    # ----------------------------------------------------------------- rebuild

    def rebuild(
        self,
        document: Document,
        *,
        should_cancel: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> RebuildResult:
        start = time.perf_counter()
        if document.base is None or document.base.runtime is None:
            return RebuildResult(ok=False, mode="solid")

        mode = document.base.mode
        result = RebuildResult(mode=mode)

        enabled = [f for f in document.features if f.enabled]
        geometry, resume_at = self._resume_point(document, enabled)

        for index, feature in enumerate(enabled):
            if should_cancel and should_cancel():
                raise Cancelled()
            if index < resume_at:
                continue
            def report(step: str, _i=index, _f=feature) -> None:
                # One feature can hold the rebuild for a long time.  A fillet on a
                # thousand edges is one call that cannot be divided, thus the name
                # of the step is the only progress there is to give.
                if progress:
                    progress(_i + 1, len(enabled), f"{_f.name} - {step}")

            report("start")
            instances = feature.pattern_instances()
            feature_result = FeatureResult(feature_id=feature.id)
            for instance in instances:
                geometry, instance_result = self._apply_feature(
                    document, instance, geometry, mode, report=report
                )
                feature_result.warnings.extend(instance_result.warnings)
                feature_result.errors.extend(instance_result.errors)
                feature_result.failed_edges.extend(instance_result.failed_edges)
                feature_result.suggested_values.update(instance_result.suggested_values)
                feature_result.tool = instance_result.tool
            feature_result.ok = not feature_result.errors
            result.features.append(feature_result)
            if feature_result.broken:
                result.ok = False
            self._remember(self._chain_key(document, self._absolute_index(document, feature) + 1), geometry)

        # Cached-through features still need a result row so the tree can show them.
        if resume_at:
            done = {r.feature_id for r in result.features}
            cached_rows = [
                FeatureResult(feature_id=f.id) for f in enabled[:resume_at] if f.id not in done
            ]
            result.features = cached_rows + result.features

        result.geometry = geometry
        result.duration_ms = (time.perf_counter() - start) * 1000.0
        result.volume = self._volume(geometry, mode)
        return result

    @staticmethod
    def _anchor_shape(document: Document, feature: Feature, geometry: object, mode: str):
        """Which shape a feature's anchor resolves against.

        Base faces do not move, so an anchor on one resolves against the *original*
        part, never against the partly-built result.  This is the mitigation §8.2
        calls for, and it is not optional: once a raised logo exists, its own top
        face is a plane with the same normal a few tenths of a millimetre away from
        the face below it, and it can outscore the real base face.  A later feature
        would then silently anchor to the logo instead of the part.

        Only a reference that explicitly names the feature that created its face
        resolves against the current geometry.
        """
        if mode != "solid":
            return document.base.runtime
        ref = feature.placement.anchor.face_ref
        if ref is not None and ref.origin_feature_id is None:
            return document.base.runtime
        return geometry

    def _absolute_index(self, document: Document, feature: Feature) -> int:
        return document.index_of(feature.id)

    def _resume_point(self, document: Document, enabled: list[Feature]) -> tuple[object, int]:
        """Find the longest cached prefix, so an edit to feature N only redoes N onward."""
        for count in range(len(enabled), 0, -1):
            last = enabled[count - 1]
            key = self._chain_key(document, document.index_of(last.id) + 1)
            if key in self._cache:
                return self._cache[key], count
        return document.base.runtime, 0

    # ---------------------------------------------------------- single feature

    def _apply_feature(
        self,
        document: Document,
        feature: Feature,
        geometry: object,
        mode: str,
        report: Callable[[str], None] | None = None,
    ) -> tuple[object, FeatureResult]:
        out = FeatureResult(feature_id=feature.id)

        def step(text: str) -> None:
            if report:
                report(text)

        try:
            profile = self._load_profile(feature.profile)
        except Exception as exc:
            out.errors.append(f"{feature.name}: {exc}")
            return geometry, out
        if profile.blocked:
            blocking = [i.message for i in profile.issues if i.blocking]
            out.errors.append(f"{feature.name}: {blocking[0] if blocking else 'the profile is not usable.'}")
            return geometry, out

        anchor_shape = self._anchor_shape(document, feature, geometry, mode)
        target_face = None
        try:
            plane, plane_warnings = resolve_anchor(feature.placement.anchor, anchor_shape, document.datums)
            if feature.placement.mode is PlacementMode.WRAP:
                if mode != "solid" or feature.placement.anchor.face_ref is None:
                    raise ToolSolidError("Wrap is available only on cylindrical and conical faces of solid parts.")
                target_face = resolve_face_ref(feature.placement.anchor.face_ref, anchor_shape).face
        except ReferenceError as exc:
            out.errors.append(f"{feature.name}: {exc}")
            return geometry, out
        except Exception as exc:  # a mesh part has no faces to resolve against
            if mode == "mesh" and feature.placement.anchor.plane is not None:
                plane = feature.placement.anchor.plane
                plane_warnings = []
            else:
                out.errors.append(f"{feature.name}: {exc}")
                return geometry, out
        out.warnings.extend(f"{feature.name}: {w}" for w in plane_warnings)
        feature.placement.anchor.plane = plane

        step("the tool solid")
        diagnostics.breadcrumb(
            "tool solid: feature=%s op=%s depth_mode=%s depth=%s",
            feature.name, feature.operation.kind, feature.operation.depth_mode,
            feature.operation.depth,
        )
        try:
            tool = build_tool_solid(
                profile,
                feature.placement,
                feature.operation,
                plane,
                part_diagonal=document.base.diagonal,
                to_face_distance=self._to_face_distance(feature, plane, anchor_shape),
                target_face=target_face,
            )
        except ToolSolidError as exc:
            out.errors.append(f"{feature.name}: {exc}")
            return geometry, out
        out.tool = tool

        # Target A: modifiers on the feature's own edges, applied to the tool solid.
        tool_shape = tool.shape
        for modifier in feature.modifiers:
            if modifier.target.role in (EdgeRole.BLEND, EdgeRole.BOTTOM):
                # Both act where the feature meets the base surface, so they are
                # applied to the result after the boolean (target B).  Applied to
                # the tool instead, they point the wrong way: a chamfer at the base
                # of a boss cuts an undercut notch, and a chamfer on a pocket rim
                # leaves an overhanging lip.
                continue
            edges = solid_ops.select_edges(tool_shape, modifier, tool.direction)
            step(f"{modifier.kind} on {len(edges)} edges")
            diagnostics.breadcrumb(
                "modifier: feature=%s kind=%s role=%s value=%s edges=%d",
                feature.name, modifier.kind, modifier.target.role,
                modifier.value, len(edges),
            )
            label = f"{feature.name} - {modifier.label}"
            if (
                edges
                and feature.operation.kind is OperationKind.CUT
                and not solid_ops.edges_reach_shape(
                    edges, geometry, max(document.base.diagonal * 1e-3, 1e-3)
                )
            ):
                # A through cut runs the tool past the part, thus the end caps of
                # the tool are outside the material and rounding them shows nothing.
                out.warnings.append(
                    f"{label}: these edges are not in the part, thus you cannot "
                    f"see a change. Use the side edges, or give the cut a blind depth."
                )
            applied = solid_ops.apply_modifier(
                tool_shape, modifier, edges, label=label
            )
            tool_shape = applied.shape
            out.warnings.extend(applied.warnings)
            out.failed_edges.extend(applied.failed_edges)
            if applied.suggested_value is not None:
                out.suggested_values[modifier.id] = applied.suggested_value

        kind = "add" if feature.operation.kind is OperationKind.ADD else "cut"
        step(f"the {kind}")
        diagnostics.breadcrumb("boolean: feature=%s kind=%s mode=%s", feature.name, kind, mode)
        if mode == "solid":
            geometry, out = self._boolean_solid(feature, geometry, tool_shape, tool, kind, out, document)
        else:
            geometry, out = self._boolean_mesh(feature, geometry, tool_shape, kind, out)
        return geometry, out

    def _boolean_solid(self, feature, geometry, tool_shape, tool, kind, out, document):
        try:
            boolean = solid_ops.boolean(
                geometry, tool_shape, kind, bbox_diagonal=document.base.diagonal
            )
        except solid_ops.GeometryError as exc:
            out.errors.append(f"{feature.name}: {exc}")
            return geometry, out
        out.warnings.extend(f"{feature.name}: {w}" for w in boolean.warnings)
        shape = boolean.shape

        # Target B: blend into the base surface, from the boolean's own history.
        blends = [
            m for m in feature.modifiers
            if m.target.role in (EdgeRole.BLEND, EdgeRole.BOTTOM)
        ]
        if blends:
            edges = solid_ops.find_blend_edges(
                shape, boolean.section_edges, tool_shape, tool.direction,
                min_length=max(tool.contact_overlap * 3.0, 1e-5),
            )
            for modifier in blends:
                applied = solid_ops.apply_modifier(
                    shape, modifier, edges, label=f"{feature.name} - blend {modifier.label}"
                )
                shape = applied.shape
                out.warnings.extend(applied.warnings)
                out.failed_edges.extend(applied.failed_edges)
                if applied.suggested_value is not None:
                    out.suggested_values[modifier.id] = applied.suggested_value
        return shape, out

    def _boolean_mesh(self, feature, geometry, tool_shape, kind, out):
        blends = [
            m for m in feature.modifiers
            if m.target.role in (EdgeRole.BLEND, EdgeRole.BOTTOM)
        ]
        if blends:
            out.warnings.append(f"{feature.name}: {mesh_ops.BLEND_NOT_AVAILABLE}")
        try:
            tool_manifold = mesh_ops.shape_to_manifold(tool_shape, self.deflection)
            boolean = mesh_ops.boolean(geometry, tool_manifold, kind)
        except Exception as exc:
            out.errors.append(f"{feature.name}: {exc}")
            return geometry, out
        out.warnings.extend(f"{feature.name}: {w}" for w in boolean.warnings)
        return boolean.manifold, out

    # ----------------------------------------------------------------- helpers

    def _to_face_distance(self, feature: Feature, plane, shape) -> float | None:
        from stamp.core.document import DepthMode
        from stamp.core.refs import resolve_face_ref
        from stamp.geom.tool_solid import distance_to_face

        if feature.operation.depth_mode is not DepthMode.TO_FACE:
            return None
        ref = feature.operation.to_face_ref
        if ref is None:
            return None
        try:
            resolved = resolve_face_ref(ref, shape)
        except ReferenceError:
            return None
        from stamp.core.refs import face_center

        return distance_to_face(plane, face_center(resolved.face))

    @staticmethod
    def _volume(geometry: object, mode: str) -> float:
        if geometry is None:
            return 0.0
        try:
            if mode == "solid":
                return solid_ops.volume(geometry)  # type: ignore[arg-type]
            return float(geometry.volume())  # type: ignore[attr-defined]
        except Exception:
            return 0.0


__all__ = ["Cancelled", "FeatureResult", "RebuildEngine", "RebuildResult"]
