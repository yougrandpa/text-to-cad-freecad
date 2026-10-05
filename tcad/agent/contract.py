"""Assemble the CAD operation protocol shared by agent hosts."""

from pathlib import Path


def cad_contract() -> str:
    root = Path(__file__).parent / "contracts"
    return "\n\n".join((root / name).read_text(encoding="utf-8")
                       for name in ("cad.md", "assembly.md"))
