"""Conversions between ``CrystalStatePayload`` dicts, ASE / pymatgen objects and text formats.

ASE and pymatgen are optional and imported lazily. Structures are sent to the
session as extxyz text (keeps the atom order; no dependency needed to write it),
and to the stateless ``POST /api/import/cif`` (used to save an object to the
project library without touching the session) as a P1 CIF.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Sequence

Crystal = Dict[str, Any]


def _extxyz(symbols: Sequence[str], cell: Sequence[Sequence[float]], positions: Sequence[Sequence[float]]) -> str:
    lattice = " ".join(f"{float(x):.10f}" for row in cell for x in row)
    lines = [str(len(symbols)), f'Lattice="{lattice}" Properties=species:S:1:pos:R:3 pbc="T T T"']
    lines += [f"{s} {float(p[0]):.10f} {float(p[1]):.10f} {float(p[2]):.10f}" for s, p in zip(symbols, positions)]
    return "\n".join(lines) + "\n"


def ase_to_extxyz(atoms: Any) -> str:
    """Periodic ``ase.Atoms`` -> extxyz text (species + Cartesian positions + lattice)."""
    if atoms.cell.rank != 3:
        raise ValueError("the Atoms object needs a 3D cell")
    return _extxyz(atoms.get_chemical_symbols(), atoms.cell.array.tolist(), atoms.positions.tolist())


def pymatgen_to_extxyz(structure: Any) -> str:
    """Ordered ``pymatgen.core.Structure`` -> extxyz text."""
    if not structure.is_ordered:
        raise ValueError("disordered pymatgen Structure; order it first (or send its CIF to Studio.ordering)")
    return _extxyz([site.specie.symbol for site in structure], structure.lattice.matrix.tolist(),
                   [site.coords.tolist() for site in structure])


def _cif(symbols: Sequence[str], cell: Sequence[Sequence[float]], frac: Sequence[Sequence[float]]) -> str:
    """P1 CIF (cell lengths / angles + fractional coordinates, atom order kept)."""
    a, b, c = ([float(x) for x in row] for row in cell)

    def norm(v: List[float]) -> float:
        return math.sqrt(sum(x * x for x in v))

    def angle(u: List[float], v: List[float]) -> float:
        return math.degrees(math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(u, v)) / (norm(u) * norm(v))))))

    lines = ["data_latticeflow", "_symmetry_space_group_name_H-M 'P 1'", "_symmetry_Int_Tables_number 1",
             f"_cell_length_a {norm(a):.10f}", f"_cell_length_b {norm(b):.10f}", f"_cell_length_c {norm(c):.10f}",
             f"_cell_angle_alpha {angle(b, c):.10f}", f"_cell_angle_beta {angle(a, c):.10f}",
             f"_cell_angle_gamma {angle(a, b):.10f}",
             "loop_", "_symmetry_equiv_pos_as_xyz", "'x, y, z'",
             "loop_", "_atom_site_label", "_atom_site_type_symbol", "_atom_site_fract_x", "_atom_site_fract_y",
             "_atom_site_fract_z", "_atom_site_occupancy"]
    lines += [f"{s}{i + 1} {s} {float(f[0]):.10f} {float(f[1]):.10f} {float(f[2]):.10f} 1"
              for i, (s, f) in enumerate(zip(symbols, frac))]
    return "\n".join(lines) + "\n"


def ase_to_cif(atoms: Any) -> str:
    """Periodic ``ase.Atoms`` -> P1 CIF text."""
    if atoms.cell.rank != 3:
        raise ValueError("the Atoms object needs a 3D cell")
    return _cif(atoms.get_chemical_symbols(), atoms.cell.array.tolist(),
                atoms.get_scaled_positions(wrap=False).tolist())


def pymatgen_to_cif(structure: Any) -> str:
    """Ordered ``pymatgen.core.Structure`` -> P1 CIF text."""
    if not structure.is_ordered:
        raise ValueError("disordered pymatgen Structure; order it first (or send its CIF to Studio.ordering)")
    return _cif([site.specie.symbol for site in structure], structure.lattice.matrix.tolist(),
                [site.frac_coords.tolist() for site in structure])


def crystal_to_ase(crystal: Crystal) -> Any:
    """``CrystalStatePayload`` dict -> ``ase.Atoms`` with forces / energy attached as a calculator result."""
    from ase import Atoms

    sites = sorted(crystal["atoms"], key=lambda s: s["index"])
    atoms = Atoms(
        symbols=[s["element"] for s in sites],
        cell=crystal["lattice"]["matrix"],
        scaled_positions=[s["frac_coords"] for s in sites],
        pbc=True,
    )
    try:
        import numpy as np
        from ase.calculators.singlepoint import SinglePointCalculator

        stress = crystal.get("stress_gpa") or None
        atoms.calc = SinglePointCalculator(
            atoms,
            energy=float(crystal["energy_per_atom"]) * len(atoms),
            forces=np.array([s["force"] for s in sites], dtype=float),
            # payload stress is GPa with ASE's sign; ASE stores eV/A^3
            stress=np.array(stress, dtype=float) / 160.21766208 if stress and len(stress) == 6 else None,
        )
    except Exception:  # noqa: BLE001 - results are a convenience
        pass
    return atoms


def crystal_to_pymatgen(crystal: Crystal) -> Any:
    """``CrystalStatePayload`` dict -> ``pymatgen.core.Structure``."""
    from pymatgen.core import Lattice, Structure

    sites = sorted(crystal["atoms"], key=lambda s: s["index"])
    return Structure(Lattice(crystal["lattice"]["matrix"]), [s["element"] for s in sites],
                     [s["frac_coords"] for s in sites])


# --------------------------------------------------------------------------- formulas
_TOKEN = re.compile(r"([A-Z][a-z]?)(\d*\.?\d*)|(\()|(\))(\d*)")


def parse_formula(formula: str) -> Dict[str, int]:
    """``"SrTiO3"`` / ``"Sr Ti O3"`` / ``"Ba(TiO3)"`` / ``"Sr1Ti1O3"`` -> ``{"Sr": 1, "Ti": 1, "O": 3}`` (integers only)."""
    text = re.sub(r"\s+", "", formula or "")
    if not text:
        raise ValueError("empty formula")
    stack: List[Dict[str, float]] = [{}]
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None or m.end() == pos:
            raise ValueError(f"cannot parse formula {formula!r} at {text[pos:]!r}")
        pos = m.end()
        if m.group(1):
            n = float(m.group(2)) if m.group(2) else 1.0
            stack[-1][m.group(1)] = stack[-1].get(m.group(1), 0.0) + n
        elif m.group(3):
            stack.append({})
        else:
            if len(stack) == 1:
                raise ValueError(f"unbalanced ')' in {formula!r}")
            group, mult = stack.pop(), float(m.group(5)) if m.group(5) else 1.0
            for el, n in group.items():
                stack[-1][el] = stack[-1].get(el, 0.0) + n * mult
    if len(stack) != 1:
        raise ValueError(f"unbalanced '(' in {formula!r}")
    out: Dict[str, int] = {}
    for el, n in stack[0].items():
        if abs(n - round(n)) > 1e-9 or round(n) < 1:
            raise ValueError(f"non-integer or zero count {n:g} for {el} in {formula!r}")
        out[el] = int(round(n))
    return out
