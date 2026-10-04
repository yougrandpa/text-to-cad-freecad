#!/usr/bin/python3
"""Explicit Linux adapter for FreeCAD's native Python modules.

Some distro FreeCADCmd builds cannot initialize their embedded interpreter,
although the matching system Python can import the same native CAD modules.
Set TCAD_FREECAD_CMD to this executable to run the unchanged worker/probes in
that interpreter. This is real FreeCAD, not a geometry mock or an automatic
fallback. The shebang must name the Python ABI matching your FreeCAD package.

TCAD_FREECAD_LIB_DIR optionally selects the directory containing FreeCAD.so;
otherwise common distro paths are searched. Only the small FreeCADCmd argument
subset used by this repository is accepted. No shell commands are evaluated.
"""

from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys


def parse_args(args: list[str]) -> tuple[list[str], str, list[str]]:
    paths: list[str] = []
    script = None
    passthrough: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--console", "-c"):
            i += 1
        elif arg == "-P" and i + 1 < len(args):
            paths.append(args[i + 1])
            i += 2
        elif arg == "--pass":
            passthrough = args[i + 1:]
            break
        elif arg.startswith("-") or script is not None:
            raise ValueError(f"unsupported FreeCADCmd argument: {arg}")
        else:
            script = arg
            i += 1
    if script is None:
        raise ValueError("a Python script is required")
    return paths, script, passthrough


def main(args: list[str] | None = None) -> int:
    argv = sys.argv[1:] if args is None else args
    version_only = argv in (["--version"], ["-v"])
    try:
        paths, script, passthrough = ([], "", []) if version_only else parse_args(argv)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    explicit = os.environ.get("TCAD_FREECAD_LIB_DIR")
    candidates = ([Path(explicit)] if explicit else [
        Path("/usr/lib/freecad-python3/lib"),
        Path("/usr/lib/freecad/lib"),
        Path("/usr/local/lib/freecad/lib"),
    ])
    lib = next((p for p in candidates if (p / "FreeCAD.so").is_file()), None)
    if lib is None:
        print("FreeCAD.so not found; set TCAD_FREECAD_LIB_DIR to its directory. "
              "The adapter must use the matching system Python.", file=sys.stderr)
        return 2
    sys.path[:0] = paths + [str(lib), str(Path(script).resolve().parent)]
    # PartDesign / Sketcher use native modules from lib. Mod also supports
    # standard distro extension layouts without importing GUI modules.
    for mod in (lib.parent / "Mod", Path("/usr/share/freecad/Mod"), Path("/usr/lib/freecad/Mod")):
        if mod.is_dir():
            sys.path.append(str(mod))
    try:
        import FreeCAD  # noqa: F401
        import Part  # noqa: F401
        import Sketcher  # noqa: F401
    except ImportError as exc:
        print(f"Cannot import native FreeCAD with {sys.executable}: {exc}", file=sys.stderr)
        return 2
    if version_only:
        print("FreeCAD " + ".".join(FreeCAD.Version()[:3]) + " (native Python adapter)")
        return 0
    sys.argv = [script, *passthrough]
    runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
