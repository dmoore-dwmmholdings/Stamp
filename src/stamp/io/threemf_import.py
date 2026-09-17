"""Reading a 3MF into a scene of placed bodies - spec §5.2.

trimesh reads a 3MF, but not one written by a slicer.  Bambu Studio and Orca put
every object in its own file under ``3D/Objects`` and point at it from a
component, and trimesh re-reads that file each time a component points at it.
A cylinder placed six times arrived as six stacked copies in every one of the
six places; merging their vertices made every edge belong to twelve faces, and
a part Bambu Studio opens as a closed solid came in as an open mesh.  It also
merges every object in a file into one, ignores which object a component names,
and cannot tell a lid from the modifier volume sitting inside it.

So Stamp reads the package itself, following the core specification and its
production extension:

- an object is named by the file it is in *and* its id, since each model file
  numbers its objects from its own 1;
- a component resolves to exactly the object it names, in the file it names,
  or the file it is written in when it names none;
- transforms compose down the tree, build item outermost;
- every build item is one body, however many components it is made of.

Slicer project files also carry parts that are not geometry - negative volumes,
modifiers, support blockers.  They sit in the model like any other mesh and are
told apart only in the slicer's own config, which is read here for that and for
the names people gave their objects.
"""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

#: The relationship type that names a package's root model.
_START_PART = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"

#: Object types that are not part of the thing being printed (3MF core §4.1).
_NOT_A_BODY = {"support", "other"}

#: Components nest.  Deeper than this is a loop the id check did not catch.
_MAX_DEPTH = 64


class ThreeMFError(ValueError):
    """The package does not describe geometry Stamp can place."""


@dataclass
class _Object:
    kind: str
    name: str
    vertices: np.ndarray | None = None
    triangles: np.ndarray | None = None
    #: (file, object id, 4x4 transform) per component.
    components: list[tuple[str, str, np.ndarray]] = field(default_factory=list)


@dataclass
class _Model:
    unit: str
    objects: dict[str, _Object]
    #: (object id, 4x4 transform) per build item.
    build: list[tuple[str, np.ndarray]]


def load_3mf(path: str | Path):
    """Read *path* into a ``trimesh.Scene`` with one node per build item.

    Each node's mesh is already in build coordinates and carries the object's
    name; the scene's metadata carries the unit the file declares.
    """
    import trimesh

    path = Path(path)
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ThreeMFError(f"It is not a 3MF package ({exc}).") from exc

    with archive:
        reader = _Package(archive)
        root = reader.model(reader.root_path())
        settings = _slicer_settings(reader)

        scene = trimesh.Scene()
        scene.metadata["units"] = root.unit
        used: dict[str, int] = {}
        for object_id, transform in root.build:
            vertices, triangles = reader.flatten(
                reader.root_path(), object_id, transform, settings
            )
            if not len(triangles):
                continue
            base = settings.names.get(object_id) or root.objects[object_id].name
            used[base] = used.get(base, 0) + 1
            name = base if used[base] == 1 else f"{base} ({used[base]})"
            mesh = trimesh.Trimesh(vertices=vertices, faces=triangles, process=True)
            mesh.metadata["name"] = name
            scene.add_geometry(mesh, node_name=name, geom_name=name)
        return scene


# ------------------------------------------------------------------ the package


class _Package:
    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        # Part names are case-insensitive in OPC, and exporters disagree on case.
        self.names = {info.filename.lower(): info.filename for info in archive.infolist()}
        self.models: dict[str, _Model] = {}

    def read(self, part: str) -> bytes | None:
        real = self.names.get(part.lstrip("/").lower())
        return None if real is None else self.archive.read(real)

    def root_path(self) -> str:
        rels = self.read("_rels/.rels")
        if rels is not None:
            for rel in _parse(rels, "_rels/.rels").iter("{*}Relationship"):
                if rel.get("Type") == _START_PART and rel.get("Target"):
                    return _normal(rel.get("Target"))
        return "/3D/3dmodel.model"

    def model(self, part: str) -> _Model:
        part = _normal(part)
        if part not in self.models:
            data = self.read(part)
            if data is None:
                raise ThreeMFError(f"The package has no model part {part}.")
            self.models[part] = _read_model(_parse(data, part), part)
        return self.models[part]

    def flatten(
        self,
        part: str,
        object_id: str,
        transform: np.ndarray,
        settings: _SlicerSettings,
        *,
        root_id: str | None = None,
        depth: int = 0,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Every triangle of one object and its components, in build coordinates."""
        if depth > _MAX_DEPTH:
            raise ThreeMFError(f"The components of object {object_id} refer to each other in a loop.")
        model = self.model(part)
        obj = model.objects.get(object_id)
        if obj is None:
            raise ThreeMFError(f"A component names object {object_id} in {part}, which does not exist.")
        root_id = object_id if root_id is None else root_id
        if obj.kind in _NOT_A_BODY:
            return _empty()

        vertices: list[np.ndarray] = []
        triangles: list[np.ndarray] = []
        count = 0
        if obj.vertices is not None and len(obj.triangles):
            tris = settings.solid_triangles(root_id, object_id, depth, obj.triangles)
            vertices.append(_apply(transform, obj.vertices))
            # A mirroring transform turns the surface inside out unless the
            # winding is turned with it.
            triangles.append(tris[:, ::-1] if np.linalg.det(transform[:3, :3]) < 0 else tris)
            count = len(obj.vertices)
        for child_part, child_id, child_transform in obj.components:
            if depth == 0 and not settings.is_solid_part(root_id, child_id):
                continue
            v, t = self.flatten(
                child_part, child_id, transform @ child_transform, settings,
                root_id=root_id, depth=depth + 1,
            )
            vertices.append(v)
            triangles.append(t + count)
            count += len(v)
        if not vertices:
            return _empty()
        return np.concatenate(vertices), np.concatenate(triangles)


def _read_model(root, part: str) -> _Model:
    objects: dict[str, _Object] = {}
    resources = root.find("{*}resources")
    for element in [] if resources is None else resources.iterfind("{*}object"):
        object_id = element.get("id")
        if object_id is None:
            continue
        obj = _Object(kind=(element.get("type") or "model").lower(), name=element.get("name") or object_id)
        mesh = element.find("{*}mesh")
        if mesh is not None:
            obj.vertices, obj.triangles = _read_mesh(mesh, part, object_id)
        components = element.find("{*}components")
        for component in [] if components is None else components.iterfind("{*}component"):
            target = _attribute(component, "path")
            obj.components.append((
                _normal(target) if target else part,
                component.get("objectid"),
                _transform(component.get("transform")),
            ))
        objects[object_id] = obj

    build = []
    for element in root.iterfind("{*}build/{*}item"):
        object_id = element.get("objectid")
        if object_id not in objects:
            raise ThreeMFError(f"The build places object {object_id}, which {part} does not define.")
        build.append((object_id, _transform(element.get("transform"))))
    return _Model(unit=root.get("unit") or "millimeter", objects=objects, build=build)


def _read_mesh(mesh, part: str, object_id: str) -> tuple[np.ndarray, np.ndarray]:
    # One join and one parse: a slicer mesh is hundreds of thousands of elements,
    # and a float() per coordinate is most of the time a load takes.
    vertices = np.array(
        " ".join([f"{v.get('x')} {v.get('y')} {v.get('z')}" for v in mesh.iterfind("{*}vertices/{*}vertex")]).split(),
        dtype=np.float64,
    ).reshape(-1, 3)
    triangles = np.array(
        " ".join([f"{t.get('v1')} {t.get('v2')} {t.get('v3')}" for t in mesh.iterfind("{*}triangles/{*}triangle")]).split(),
        dtype=np.int64,
    ).reshape(-1, 3)
    if len(triangles) and (triangles.min() < 0 or triangles.max() >= len(vertices)):
        raise ThreeMFError(f"Object {object_id} in {part} has a triangle that names a vertex it does not have.")
    return vertices, triangles


# ----------------------------------------------------------- slicer settings


@dataclass
class _SlicerSettings:
    #: Build object id -> the name the user gave it.
    names: dict[str, str] = field(default_factory=dict)
    #: (build object id, component object id) for Bambu/Orca parts that are not geometry.
    skipped_parts: set[tuple[str, str]] = field(default_factory=set)
    #: Build object id -> inclusive triangle ranges that are not geometry (PrusaSlicer).
    skipped_ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    def is_solid_part(self, root_id: str, component_id: str) -> bool:
        return (root_id, component_id) not in self.skipped_parts

    def solid_triangles(self, root_id: str, object_id: str, depth: int, triangles: np.ndarray) -> np.ndarray:
        ranges = self.skipped_ranges.get(root_id) if depth == 0 else None
        if not ranges:
            return triangles
        keep = np.ones(len(triangles), dtype=bool)
        for first, last in ranges:
            keep[max(first, 0):last + 1] = False
        return triangles[keep]


def _slicer_settings(package: _Package) -> _SlicerSettings:
    """What Bambu Studio, Orca or PrusaSlicer recorded about the objects.

    Only ever a refinement: a config Stamp cannot read leaves every mesh in and
    every object named as the model names it.
    """
    settings = _SlicerSettings()
    bambu = package.read("Metadata/model_settings.config")
    if bambu is not None:
        try:
            config = _parse(bambu, "model_settings.config")
        except ThreeMFError:
            config = None
        for obj in [] if config is None else config.iterfind("object"):
            object_id = obj.get("id")
            name = _config_value(obj, "name")
            if name:
                settings.names[object_id] = name
            for part in obj.iterfind("part"):
                subtype = (part.get("subtype") or "normal_part").lower()
                if subtype != "normal_part":
                    settings.skipped_parts.add((object_id, part.get("id")))

    prusa = package.read("Metadata/Slic3r_PE_model.config")
    if prusa is not None:
        try:
            config = _parse(prusa, "Slic3r_PE_model.config")
        except ThreeMFError:
            config = None
        for obj in [] if config is None else config.iterfind("object"):
            object_id = obj.get("id")
            name = _config_value(obj, "name")
            if name:
                settings.names.setdefault(object_id, name)
            for volume in obj.iterfind("volume"):
                kind = _config_value(volume, "volume_type") or "ModelPart"
                # Older PrusaSlicer wrote modifier="1" rather than a volume_type.
                if kind != "ModelPart" or _config_value(volume, "modifier") == "1":
                    try:
                        span = (int(volume.get("firstid")), int(volume.get("lastid")))
                    except (TypeError, ValueError):
                        continue
                    settings.skipped_ranges.setdefault(object_id, []).append(span)
    return settings


def _config_value(element, key: str) -> str | None:
    for meta in element.iterfind("metadata"):
        if meta.get("key") == key:
            return meta.get("value")
    return None


# --------------------------------------------------------------------- helpers


def _parse(data: bytes, part: str):
    from lxml import etree

    # No entities and no network: a 3MF is a file off the internet.
    parser = etree.XMLParser(huge_tree=True, resolve_entities=False, no_network=True)
    try:
        return etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise ThreeMFError(f"{part.lstrip('/')} is not valid XML ({exc}).") from exc


def _normal(part: str) -> str:
    return posixpath.normpath("/" + part.replace("\\", "/").lstrip("/"))


def _attribute(element, local: str) -> str | None:
    """An attribute by local name, whatever namespace prefix the writer chose."""
    for key, value in element.attrib.items():
        if key == local or key.endswith("}" + local):
            return value
    return None


def _transform(text: str | None) -> np.ndarray:
    """A 3MF transform - twelve numbers, row-vector convention - as a 4x4 matrix."""
    matrix = np.eye(4)
    if not text:
        return matrix
    values = np.array(text.split(), dtype=np.float64)
    if values.shape != (12,):
        raise ThreeMFError(f"The transform {text!r} does not have twelve numbers.")
    rows = values.reshape(4, 3)
    matrix[:3, :3] = rows[:3].T
    matrix[:3, 3] = rows[3]
    return matrix


def _apply(transform: np.ndarray, vertices: np.ndarray) -> np.ndarray:
    return vertices @ transform[:3, :3].T + transform[:3, 3]


def _empty() -> tuple[np.ndarray, np.ndarray]:
    return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)


__all__ = ["ThreeMFError", "load_3mf"]
