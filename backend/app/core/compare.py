"""Structure comparison and Materials Project matching (IMPROVEMENT_SUGGESTIONS.md §7.2).

``compare(atoms_a, atoms_b, payload_a, payload_b) -> CompareResponse``
    * ``structures_match`` / ``rms_displacement`` / ``max_displacement``: pymatgen
      ``StructureMatcher`` with its default tolerances (ltol 0.2, stol 0.3, angle_tol 5,
      ``primitive_cell=True``, ``scale=True``); the displacements are
      ``get_rms_dist`` (normalised by (V / N)^(1/3), pymatgen's convention), only
      when the structures match.
    * ``spacegroup_a`` / ``spacegroup_b``: as carried by the payloads (spglib at the
      payload's symprec).
    * ``group_subgroup`` (only when the reduced compositions are equal): from the
      space-group *types* with pymatgen ``SpaceGroup.is_subgroup`` (chains of maximal
      subgroups): "b is a subgroup of a", "a is a subgroup of b", "same space group"
      or "unrelated". It is a statement about the groups only, not a proof that one
      structure is a distortion of the other (that is what ``structures_match`` with
      a loose tolerance, or a mode decomposition, would add).
    * Energies are the payloads' (same model assumed): b - a per atom, and per
      formula unit (x atoms per reduced formula) when the compositions are equal.

``match_mp(atoms, client) -> MPMatchResponse``
    ``client.search(reduced formula)`` then ``client.get_structure`` for every hit
    with at most ``MAX_MATCH_SITES`` (64) sites (at most ``MAX_MATCH_ENTRIES`` (20)
    of them, MP-E_hull order), each compared with ``StructureMatcher`` (defaults);
    the matches are sorted by rms.
"""

from __future__ import annotations

from typing import List, Optional

from ase import Atoms

from app.core import mp_client
from app.schemas.api import CompareResponse, MPMatch, MPMatchResponse
from app.schemas.crystal import CrystalStatePayload

__all__ = ["compare", "match_mp", "group_subgroup_relation", "MAX_MATCH_SITES", "MAX_MATCH_ENTRIES"]

MAX_MATCH_SITES = 64
MAX_MATCH_ENTRIES = 20


def _structure(atoms: Atoms):
    from pymatgen.io.ase import AseAtomsAdaptor

    plain = Atoms(numbers=atoms.get_atomic_numbers(), positions=atoms.positions, cell=atoms.cell.array, pbc=True)
    return AseAtomsAdaptor.get_structure(plain)


def group_subgroup_relation(number_a: int, number_b: int) -> Optional[str]:
    """Relation between two space-group types (None if either number is invalid)."""
    from pymatgen.symmetry.groups import SpaceGroup

    if not (1 <= int(number_a) <= 230 and 1 <= int(number_b) <= 230):
        return None
    if int(number_a) == int(number_b):
        return "same space group"
    try:
        ga = SpaceGroup.from_int_number(int(number_a))
        gb = SpaceGroup.from_int_number(int(number_b))
        if gb.is_subgroup(ga):
            return "b is a subgroup of a"
        if ga.is_subgroup(gb):
            return "a is a subgroup of b"
    except Exception:  # noqa: BLE001 - pymatgen symmetry data unavailable
        return None
    return "unrelated"


def compare(atoms_a: Atoms, atoms_b: Atoms, payload_a: CrystalStatePayload,
            payload_b: CrystalStatePayload) -> CompareResponse:
    from pymatgen.analysis.structure_matcher import StructureMatcher

    sa, sb = _structure(atoms_a), _structure(atoms_b)
    same_comp = sa.composition.reduced_composition.almost_equals(sb.composition.reduced_composition)
    matcher = StructureMatcher()
    rms = max_d = None
    match = False
    if same_comp:
        dist = matcher.get_rms_dist(sa, sb)
        if dist is not None:
            match = True
            rms, max_d = float(dist[0]), float(dist[1])
    va = float(sa.volume) / len(sa)
    vb = float(sb.volume) / len(sb)
    ea, eb = float(payload_a.energy_per_atom), float(payload_b.energy_per_atom)
    d_mev = (eb - ea) * 1000.0
    per_fu = None
    relation = None
    if same_comp:
        atoms_per_fu = float(sa.composition.reduced_composition.num_atoms)
        per_fu = d_mev * atoms_per_fu
        relation = group_subgroup_relation(payload_a.spacegroup_number, payload_b.spacegroup_number)
    return CompareResponse(
        same_composition=bool(same_comp),
        structures_match=match,
        rms_displacement=rms,
        max_displacement=max_d,
        spacegroup_a=payload_a.spacegroup_symbol,
        spacegroup_b=payload_b.spacegroup_symbol,
        group_subgroup=relation,
        volume_per_atom_a=va,
        volume_per_atom_b=vb,
        volume_change_percent=(vb - va) / va * 100.0,
        energy_per_atom_a=ea,
        energy_per_atom_b=eb,
        energy_difference_mev_per_atom=d_mev,
        energy_difference_mev_per_formula_unit=per_fu,
        method=(
            "pymatgen StructureMatcher (ltol 0.2, stol 0.3, angle_tol 5, primitive cells, volume scaled; "
            "rms / max displacement normalised by (V/N)^(1/3)); group-subgroup relation of the space-group "
            "types (pymatgen SpaceGroup.is_subgroup); energies as carried by the payloads (same model assumed)"
        ),
    )


def match_mp(atoms: Atoms, client: mp_client.MPClient) -> MPMatchResponse:
    """MP entries of the same reduced formula that ``StructureMatcher`` matches (see module docstring)."""
    from pymatgen.analysis.structure_matcher import StructureMatcher

    s = _structure(atoms)
    formula = s.composition.reduced_formula
    entries = list(client.search(formula))
    matcher = StructureMatcher()
    matches: List[MPMatch] = []
    candidates = [e for e in mp_client.sort_entries(entries) if 0 < e.nsites <= MAX_MATCH_SITES][:MAX_MATCH_ENTRIES]
    for entry in candidates:
        try:
            full, other = client.get_structure(entry.material_id)
        except mp_client.MPNotFoundError:
            continue
        if not getattr(other, "is_ordered", True):
            continue
        dist = matcher.get_rms_dist(s, other)
        if dist is not None:
            matches.append(MPMatch(entry=full, rms_displacement=float(dist[0])))
    matches.sort(key=lambda m: (m.rms_displacement, m.entry.material_id))
    return MPMatchResponse(formula=formula, n_searched=len(entries), matches=matches)
