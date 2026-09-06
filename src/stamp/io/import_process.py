"""Reading a part in a second process - spec §5.1, §5.2.

OpenCascade holds the GIL for the whole of every call it is given.  Measured on
a 3MF someone converted to STEP: ``TransferRoots`` alone runs 35 s and the main
thread gets one scheduling slot in all that time.  So a worker *thread* cannot
keep the window alive during an import, and never could - only a worker
*process* can.

This module is both halves of that.  The child reads the file, meshes the result
for display, and writes it out as binary BREP beside a small JSON description.
The parent reads the BREP back, which costs a second and a half rather than a
minute, and gets the triangulation with it so nothing has to mesh again.

Everything crossing between them is a file: a ``TopoDS_Shape`` has no pickle and
a mesh of that size has no business going through a pipe.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from stamp.core.document import BasePart
from stamp.io.part_import import (
    PartImportError,
    PartImportResult,
    import_part,
    pretessellate,
    solids_intersect,
)

#: The child names the phase it is in in a file beside the request, and the
#: parent reads it while it waits.  Not the child's stdout, which sounds simpler
#: and is not: the shipped Stamp is a windowed build, where ``sys.stdout`` is
#: None and the first ``print`` would take the import down with it.
PHASE_SUFFIX = ".phase"

#: The argv word that turns an ordinary Stamp start into an import worker.  It
#: has to be argv rather than a module path: in a PyInstaller build there is no
#: interpreter to hand and ``sys.executable`` is Stamp.exe itself.
WORKER_COMMAND = "import-worker"


def phase_file(request_path: str | Path) -> Path:
    """Where the child writes the phase it is in, for the request at *request_path*."""
    request_path = Path(request_path)
    return request_path.with_name(request_path.name + PHASE_SUFFIX)


@dataclass
class ImportJob:
    """What the parent asks for, as it is written to the request file."""

    path: str
    out_dir: str
    unit_scale: float | None = None
    solid_index: int | None = None
    repair: bool = True
    draft: bool = False

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "out_dir": self.out_dir,
            "unit_scale": self.unit_scale,
            "solid_index": self.solid_index,
            "repair": self.repair,
            "draft": self.draft,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ImportJob:
        return cls(
            path=d["path"],
            out_dir=d["out_dir"],
            unit_scale=d.get("unit_scale"),
            solid_index=d.get("solid_index"),
            repair=bool(d.get("repair", True)),
            draft=bool(d.get("draft", False)),
        )


# ------------------------------------------------------------------ the child


def run_worker(request_path: str | Path) -> int:
    """Read the part named in the request file and write the answer beside it.

    Always writes an answer, failure included: a parent watching for the file is
    otherwise left waiting on a process that has already gone.
    """
    request_path = Path(request_path)
    answer_path = request_path.with_suffix(".answer.json")
    try:
        job = ImportJob.from_dict(json.loads(request_path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001 - a malformed request is still an answer
        answer_path.write_text(
            json.dumps({"ok": False, "error": f"The import request is unreadable: {exc}"}),
            encoding="utf-8",
        )
        return 2

    phase_path = phase_file(request_path)

    def say(phase: str) -> None:
        try:
            phase_path.write_text(phase, encoding="utf-8")
        except OSError:
            pass  # the phase is a courtesy; losing it must not lose the import

    try:
        result = import_part(
            job.path,
            unit_scale=job.unit_scale,
            solid_index=job.solid_index,
            repair=job.repair,
            progress=say,
        )
        answer = _write_result(result, Path(job.out_dir), draft=job.draft, say=say)
    except PartImportError as exc:
        answer = {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the parent shows this, never a traceback
        answer = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    answer_path.write_text(json.dumps(answer), encoding="utf-8")
    return 0 if answer.get("ok") else 1


def _write_result(result: PartImportResult, out_dir: Path, *, draft: bool, say) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    part = result.part
    answer: dict = {
        "ok": True,
        "part": part.to_dict(),
        "units_ambiguous": result.units_ambiguous,
        "solid_count": result.solid_count or len(result.solids),
        "solids_disjoint": None,
    }

    if len(result.solids) > 1:
        # Worked out here because it is a boolean per pair of solids, and the
        # dialog that asks the question cannot afford to run them.
        say("Comparing the bodies")
        answer["solids_disjoint"] = not solids_intersect(result.solids)

    if part.mode == "solid":
        say("Preparing it for display")
        pretessellate(part.runtime, draft)
        say("Handing it over")
        answer["runtime"] = {"kind": "brep", "file": str(_write_brep(part.runtime, out_dir))}
    else:
        say("Handing it over")
        answer["runtime"] = {"kind": "mesh", "file": str(_write_mesh(part.runtime, out_dir))}
    return answer


def _write_brep(shape, out_dir: Path) -> Path:
    """Binary BREP, triangulation included - that is what makes it worth doing."""
    from OCP.BinTools import BinTools

    target = out_dir / "part.bin"
    BinTools.Write_s(shape, str(target))
    return target


def _write_mesh(manifold, out_dir: Path) -> Path:
    import numpy as np

    mesh = manifold.to_mesh()
    target = out_dir / "part.npz"
    np.savez(
        target,
        vertices=np.asarray(mesh.vert_properties)[:, :3].astype(np.float32),
        triangles=np.asarray(mesh.tri_verts).astype(np.uint32),
    )
    return target.with_suffix(".npz")


# ----------------------------------------------------------------- the parent


def read_answer(answer_path: str | Path) -> PartImportResult:
    """Turn what the child wrote back into a :class:`PartImportResult`.

    Raises :class:`PartImportError` with the child's own message, so a failure in
    the subprocess reaches the user reading exactly as it would have in process.
    """
    answer_path = Path(answer_path)
    if not answer_path.exists():
        raise PartImportError(
            "The import stopped before it produced anything. If the file is very "
            "large, Stamp may have run out of memory reading it."
        )
    answer = json.loads(answer_path.read_text(encoding="utf-8"))
    if not answer.get("ok"):
        raise PartImportError(answer.get("error", "The import failed."))

    part = BasePart.from_dict(answer["part"])
    runtime = answer["runtime"]
    part.runtime = (
        _read_brep(runtime["file"])
        if runtime["kind"] == "brep"
        else _read_mesh(runtime["file"])
    )
    return PartImportResult(
        part=part,
        units_ambiguous=bool(answer.get("units_ambiguous")),
        solid_count=int(answer.get("solid_count", 0)),
        solids_disjoint=answer.get("solids_disjoint"),
    )


def _read_brep(path: str):
    from OCP.BinTools import BinTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    if not BinTools.Read_s(shape, str(path)) or shape.IsNull():
        raise PartImportError("Stamp could not read the part back from the importer.")
    return shape


def _read_mesh(path: str):
    import numpy as np
    from manifold3d import Manifold, Mesh

    data = np.load(path)
    return Manifold(
        Mesh(vert_properties=data["vertices"], tri_verts=data["triangles"])
    )


__all__ = [
    "PHASE_SUFFIX",
    "WORKER_COMMAND",
    "ImportJob",
    "phase_file",
    "read_answer",
    "run_worker",
]


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(run_worker(sys.argv[1]))
