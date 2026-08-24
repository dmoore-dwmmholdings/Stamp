# Stamp 1.0 goal

Stamp 1.0 makes repeatable, production-ready marking practical without turning
Stamp into a general CAD system.

## Deliverables

1. **Wrap:** exact add and cut artwork on cylindrical and conical faces of
   solid (STEP/BREP-class) parts.  Mesh parts, unsupported curved faces, and
   artwork crossing the unwrap seam are refused with useful guidance.
2. **Patterns:** editable linear, circular, and mirror patterns.  A pattern is
   one feature-tree item backed by a seed feature, not destructive copies.
3. **Batch stamping:** a saved `.stamp` project can be applied to a CSV of
   input parts through both a desktop wizard and `stamp batch`.  CSV rows have
   `input` and `output` columns plus values substituted into text as
   `{{column}}`.  Jobs stop at the first failed row and write a report.
4. **Export preflight:** shared validation blocks invalid geometry and
   incompatible formats; manufacturing cautions remain warnings that users can
   acknowledge in the desktop app and see in CLI output.

## Release bar

Version all package metadata as `1.0.0`; preserve backward compatibility for
existing projects; document every workflow and its limits; cover the feature,
batch, and preflight paths with automated tests; and smoke-test the Windows
release package while retaining documented macOS/Linux build instructions.

## Non-goals

Automatic seam splitting, arbitrary-surface projection, mesh wrapping,
dedicated template files, and all-or-nothing batch rollback remain out of scope.
