"""Supercells, interstitials and neutral point-defect formation energies (IMPROVEMENT_SUGGESTIONS.md §5.9).

Supercells
----------
:func:`make_supercell` builds L' = M L for an integer matrix M (rows = new lattice vectors in units of
the old ones, det M > 0). Atom order is *atom-major*: every original atom in its original order,
each followed by its det(M) translated copies, the translations sorted lexicographically. Positions
are wrapped into the new cell (fractional round-off snapped to 0).

Formation energies
------------------
E_f = E_def - E_bulk - sum_i dn_i mu_i (neutral defects, no corrections).

* The defect cell and the bulk may differ in size by an integer factor k = V_def / V_bulk
  (e.g. a 5-atom bulk entry and a 2x2x2 defect cell); E_bulk and the bulk counts are then scaled by k
  (``e_bulk_ev`` is the scaled value; ``method`` states k).
* Defects are identified from the composition difference dn = n_def - k n_bulk: an added element and a
  removed element pair up as a substitution "A_B" (greedy, alphabetical), the remaining removals are
  vacancies "V_B" and the remaining additions interstitials "A_i"; multiplicities prefix the label
  ("2V_O"). Parts are joined by " + " (substitutions, then vacancies, then interstitials, each sorted).
* E_def and E_bulk are single points of the structures as given with the engine passed in. Warnings
  when max|F| > 0.05 eV/A or |P| > 0.5 GPa in either.
* Chemical potentials come from the uMLIP-relaxed MP reference set of the chemical system of the bulk
  plus any added elements (:func:`hull_analyzer.get_reference_set`, same cache, keyed by
  ``engine.backend_name``). The bulk itself (its re-evaluated energy) is added to the phase diagram.
  Limits (keys of ``chemical_potentials`` / ``formation_energies_ev``):

  - one per vertex of the bulk's stability region, named by the coexisting phases as pymatgen does,
    e.g. "SrTiO3-TiO2-O2" (``PhaseDiagram.get_all_chempots``);
  - "<X>-rich" for every element X: the vertex with the largest mu_X. mu_X equals the elemental
    reference whenever phase X coexists with the bulk; otherwise it is the largest mu_X compatible
    with the bulk's stability and a warning gives the gap to the elemental reference.

  If the bulk lies above the reference hull, the vertices are those of the facets of its
  decomposition products (the chemical potentials at which those products coexist), with a warning.
* ``min_image_distance``: the defect-to-own-periodic-image distance, i.e. the shortest non-zero
  lattice vector of the defect cell (Minkowski-reduced). A warning is added below 10 A.
"""

from __future__ import annotations

import itertools
import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from ase import Atoms
from ase.data import atomic_numbers

from app.core import hull_analyzer
from app.schemas.messages import FormationEnergyResult

EV_A3_TO_GPA = 160.21766208
WARN_FORCE = 0.05             # eV/A
WARN_PRESSURE_GPA = 0.5
WARN_IMAGE_DISTANCE = 10.0    # A
MIN_ADD_DISTANCE = 0.5        # A
MAX_SUPERCELL_ATOMS = 4096    # hard memory guard; the WS layer enforces MAX_ATOMS
VOLUME_RATIO_TOL = 0.08


class DefectCancelled(RuntimeError):
    """``should_cancel()`` returned True during :func:`formation_energy`."""


# --------------------------------------------------------------------------- #
# supercells and atoms
# --------------------------------------------------------------------------- #
def _int_matrix(matrix) -> np.ndarray:
    m = np.asarray(matrix, dtype=float)
    if m.shape != (3, 3) or not np.all(np.isfinite(m)) or not np.allclose(m, np.round(m)):
        raise ValueError("supercell matrix must be a 3x3 integer matrix")
    return np.round(m).astype(int)


def _wrap(atoms: Atoms) -> Atoms:
    scaled = np.mod(atoms.get_scaled_positions(wrap=False), 1.0)
    scaled[np.isclose(scaled, 1.0, rtol=0.0, atol=1e-10) | (np.abs(scaled) < 1e-10)] = 0.0
    atoms.set_scaled_positions(scaled)
    return atoms


def make_supercell(atoms: Atoms, matrix) -> Atoms:
    """L' = M L (see module docstring). ValueError for a non-integer or det <= 0 matrix."""
    m = _int_matrix(matrix)
    det = int(round(np.linalg.det(m)))
    if det <= 0:
        raise ValueError(f"supercell matrix must have a positive determinant (got {det})")
    if det * len(atoms) > MAX_SUPERCELL_ATOMS:
        raise ValueError(f"supercell would contain {det * len(atoms)} atoms (> {MAX_SUPERCELL_ATOMS})")
    cell = np.asarray(atoms.cell.array, dtype=float)
    new_cell = m @ cell
    # Lattice translations t (in old fractional units) inside the new cell: s = t M^-1 in [0, 1)^3.
    corners = np.array(list(itertools.product((0, 1), repeat=3))) @ m
    lo, hi = corners.min(axis=0), corners.max(axis=0)
    grid = np.array(list(itertools.product(*(range(lo[k], hi[k] + 1) for k in range(3)))), dtype=float)
    s = grid @ np.linalg.inv(m)
    inside = np.all((s > -1e-9) & (s < 1.0 - 1e-9), axis=1)
    trans = grid[inside]
    if len(trans) != det:  # pragma: no cover - numerical safety net
        raise ValueError("could not enumerate the supercell translations")
    trans = trans[np.lexsort(trans.T[::-1])]
    frac = atoms.get_scaled_positions(wrap=False)
    new_frac = (frac[:, None, :] + trans[None, :, :]).reshape(-1, 3) @ np.linalg.inv(m)
    out = Atoms(
        numbers=np.repeat(atoms.get_atomic_numbers(), det),
        cell=new_cell,
        scaled_positions=new_frac,
        pbc=True,
    )
    return _wrap(out)


def supercell_label(matrix) -> str:
    """"2x2x2" for a diagonal matrix, else "[[1,1,0],[-1,1,0],[0,0,2]]"."""
    m = _int_matrix(matrix)
    if np.count_nonzero(m - np.diag(np.diag(m))) == 0:
        return "x".join(str(int(v)) for v in np.diag(m))
    return "[" + ",".join("[" + ",".join(str(int(v)) for v in row) + "]" for row in m) + "]"


def _min_periodic_distance(atoms: Atoms, point: np.ndarray) -> float:
    from ase.geometry import find_mic

    if len(atoms) == 0:
        return math.inf
    d = np.asarray(atoms.positions, dtype=float) - point[None, :]
    _, lengths = find_mic(d, atoms.cell, pbc=True)
    return float(np.min(lengths))


def add_atom(atoms: Atoms, element: str, frac_coords) -> Atoms:
    """Copy of ``atoms`` with ``element`` appended at ``frac_coords`` (wrapped).

    ValueError for an unknown element, non-finite coordinates, or a position within 0.5 A of an
    existing atom (any periodic image).
    """
    if element not in atomic_numbers or atomic_numbers[element] == 0:
        raise ValueError(f"unknown element {element!r}")
    f = np.asarray(frac_coords, dtype=float)
    if f.shape != (3,) or not np.all(np.isfinite(f)):
        raise ValueError("frac_coords must be 3 finite numbers")
    cart = f @ np.asarray(atoms.cell.array, dtype=float)
    dmin = _min_periodic_distance(atoms, cart)
    if dmin < MIN_ADD_DISTANCE:
        raise ValueError(f"{element} would be {dmin:.2f} A from an existing atom (minimum {MIN_ADD_DISTANCE} A)")
    out = Atoms(
        numbers=list(atoms.get_atomic_numbers()) + [atomic_numbers[element]],
        cell=np.asarray(atoms.cell.array, dtype=float),
        positions=np.vstack([np.asarray(atoms.positions, dtype=float), cart[None, :]]),
        pbc=True,
    )
    return _wrap(out)


def shortest_lattice_vector(atoms: Atoms) -> float:
    from ase.geometry import minkowski_reduce

    cell = np.asarray(atoms.cell.array, dtype=float)
    red, _ = minkowski_reduce(cell)
    best = math.inf
    for n in itertools.product((-1, 0, 1), repeat=3):
        if n == (0, 0, 0):
            continue
        best = min(best, float(np.linalg.norm(np.asarray(n, dtype=float) @ red)))
    return best


# --------------------------------------------------------------------------- #
# defect identification
# --------------------------------------------------------------------------- #
def _counts(atoms: Atoms) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for s in atoms.get_chemical_symbols():
        out[s] = out.get(s, 0) + 1
    return out


def defect_label(delta_n: Dict[str, int]) -> str:
    """Kroeger-Vink-like label of a composition difference (see module docstring)."""
    added = {e: n for e, n in sorted(delta_n.items()) if n > 0}
    removed = {e: -n for e, n in sorted(delta_n.items()) if n < 0}
    subs: List[Tuple[str, str, int]] = []
    for a in sorted(added):
        for r in sorted(removed):
            k = min(added[a], removed[r])
            if k > 0:
                subs.append((a, r, k))
                added[a] -= k
                removed[r] -= k
    def mult(k: int) -> str:
        return "" if k == 1 else str(k)

    parts = [f"{mult(k)}{a}_{r}" for a, r, k in subs]
    parts += [f"{mult(k)}V_{r}" for r, k in sorted(removed.items()) if k > 0]
    parts += [f"{mult(k)}{a}_i" for a, k in sorted(added.items()) if k > 0]
    return " + ".join(parts)


# --------------------------------------------------------------------------- #
# chemical potentials
# --------------------------------------------------------------------------- #
def _chemical_potential_limits(
    refset: "hull_analyzer.ReferenceSet", bulk_counts: Dict[str, int], e_bulk_per_atom: float,
    elements: Sequence[str], warnings: List[str],
) -> Dict[str, Dict[str, float]]:
    from pymatgen.analysis.phase_diagram import PDEntry, PhaseDiagram
    from pymatgen.core import Composition

    comp = Composition({e: float(n) for e, n in bulk_counts.items()})
    base = refset.phase_diagram()
    bulk_entry = PDEntry(comp, e_bulk_per_atom * comp.num_atoms, name=comp.reduced_formula, attribute="bulk")
    pd = PhaseDiagram(list(base.all_entries) + [bulk_entry])
    e_above = float(pd.get_e_above_hull(bulk_entry))
    if e_above > 1e-4:
        warnings.append(
            f"the bulk is {1000 * e_above:.1f} meV/atom above the uMLIP reference hull; chemical potentials "
            "are those at which its decomposition products coexist (no stability region of its own)"
        )
    vertices = pd.get_all_chempots(comp)
    limits: Dict[str, Dict[str, float]] = {}
    for name, mus in vertices.items():
        limits[name] = {str(el): float(mu) for el, mu in mus.items()}
    el_ref = {str(el): float(entry.energy_per_atom) for el, entry in pd.el_refs.items()}
    rich: Dict[str, Dict[str, float]] = {}
    for x in sorted(elements):
        if not limits:
            break
        best = max(sorted(limits), key=lambda k: limits[k][x])
        mus = dict(limits[best])
        gap = el_ref[x] - mus[x]
        if gap > 1e-3:
            warnings.append(
                f"{x}-rich: elemental {x} cannot coexist with the bulk; using the most {x}-rich stability "
                f"vertex {best} (mu_{x} {gap:.3f} eV below the elemental reference)"
            )
        rich[f"{x}-rich"] = mus
    return {**rich, **limits}


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def _single_point(atoms: Atoms, engine) -> Tuple[float, float, float]:
    e, f, s = engine.energy_forces_stress(atoms)
    fmax = float(np.max(np.linalg.norm(f, axis=1))) if len(f) else 0.0
    p = float(-np.mean(np.asarray(s)[:3]) * EV_A3_TO_GPA)
    return float(e), fmax, p


def formation_energy(
    defect_atoms: Atoms,
    bulk_atoms: Atoms,
    engine,
    *,
    api_key: Optional[str],
    fetcher=None,
    cache: Optional["hull_analyzer.HullCache"] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> FormationEnergyResult:
    """Neutral defect formation energies over the chemical-potential limits (module docstring).

    Raises ValueError (no composition difference, incompatible cells), hull_analyzer errors
    (``HullNoApiKeyError`` when references are not cached and no key is given, ``HullFetchError``,
    ``HullMissingElementalReferenceError``) and :class:`DefectCancelled`.
    """
    def check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise DefectCancelled("formation-energy calculation cancelled")

    def report(done: int, total: int, msg: str) -> None:
        if progress is not None:
            try:
                progress(done, total, msg)
            except Exception:  # noqa: BLE001
                pass

    if len(defect_atoms) == 0 or len(bulk_atoms) == 0:
        raise ValueError("defect and bulk structures must contain atoms")
    v_def = abs(float(np.linalg.det(defect_atoms.cell.array)))
    v_bulk = abs(float(np.linalg.det(bulk_atoms.cell.array)))
    ratio = v_def / v_bulk
    k = max(1, int(round(ratio)))
    if abs(ratio - k) > VOLUME_RATIO_TOL * k:
        raise ValueError(
            f"defect cell volume is {ratio:.3f} x the bulk volume; it must be an integer multiple "
            "(same lattice up to relaxation)"
        )
    warnings: List[str] = []
    bulk_counts = _counts(bulk_atoms)
    def_counts = _counts(defect_atoms)
    elements = sorted(set(bulk_counts) | set(def_counts))
    delta_n = {e: def_counts.get(e, 0) - k * bulk_counts.get(e, 0) for e in elements}
    delta_n = {e: n for e, n in delta_n.items() if n != 0}
    if not delta_n:
        raise ValueError("the defect cell has the same composition as the bulk; nothing to compare")
    n_changed = sum(abs(n) for n in delta_n.values())
    if n_changed > 0.25 * len(defect_atoms):
        warnings.append(
            f"{n_changed} atoms differ out of {len(defect_atoms)}: not a dilute point defect"
        )
    label = defect_label(delta_n)

    check_cancel()
    report(0, 3, "Defect cell single point")
    e_def, f_def, p_def = _single_point(defect_atoms, engine)
    check_cancel()
    report(1, 3, "Bulk single point")
    e_bulk1, f_bulk, p_bulk = _single_point(bulk_atoms, engine)
    e_bulk = k * e_bulk1
    for what, fm, p in (("defect cell", f_def, p_def), ("bulk", f_bulk, p_bulk)):
        if fm > WARN_FORCE:
            warnings.append(f"{what} is not relaxed (max|F| = {fm:.3f} eV/A > {WARN_FORCE})")
        if abs(p) > WARN_PRESSURE_GPA:
            warnings.append(f"{what} is under pressure (P = {p:+.2f} GPa)")

    dmin = shortest_lattice_vector(defect_atoms)
    if dmin < WARN_IMAGE_DISTANCE:
        warnings.append(
            f"defect-image distance {dmin:.2f} A < {WARN_IMAGE_DISTANCE:.0f} A: defect-defect interaction "
            "is not negligible; use a larger supercell"
        )

    check_cancel()
    report(2, 3, "Chemical potentials (uMLIP reference phases)")

    def ref_progress(done: int, total: int, msg: str) -> None:
        report(2, 3, msg)

    try:
        refset = hull_analyzer.get_reference_set(
            elements, engine, api_key=api_key, fetcher=fetcher, cache=cache,
            should_cancel=should_cancel, progress=ref_progress,
        )
    except hull_analyzer.HullCancelled as exc:
        raise DefectCancelled(str(exc)) from exc
    check_cancel()
    n_bulk = len(bulk_atoms)
    limits = _chemical_potential_limits(refset, bulk_counts, e_bulk1 / n_bulk, elements, warnings)
    formation = {
        name: float(e_def - e_bulk - sum(dn * mus[e] for e, dn in delta_n.items()))
        for name, mus in limits.items()
    }
    negative = sorted(name for name, ef in formation.items() if ef < 0)
    if negative:
        warnings.append(
            f"negative formation energy at {', '.join(negative)}: the defect would form spontaneously there. "
            "For a stable host this usually points to force-field error or a too small cell, not to real physics"
        )
    report(3, 3, "Done")
    scale = f"; bulk scaled x{k} to the defect cell" if k != 1 else ""
    method = (
        f"neutral defect, E_f = E_def - E_bulk - sum dn_i mu_i; single points with {engine.backend_name} "
        f"of the structures as given{scale}; mu from {len(refset.references)} uMLIP-relaxed MP reference "
        f"phases of {refset.chemsys} plus the bulk (stability-region vertices named by coexisting phases; "
        "<X>-rich = vertex of maximal mu_X); no finite-size or charge corrections"
    )
    return FormationEnergyResult(
        defect_label=label,
        delta_n=delta_n,
        e_defect_ev=e_def,
        e_bulk_ev=float(e_bulk),
        chemical_potentials=limits,
        formation_energies_ev=formation,
        min_image_distance=float(dmin),
        warnings=warnings,
        method=method,
    )
